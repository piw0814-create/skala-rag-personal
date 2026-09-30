import pytest

from agents import report
from agents._report_render import (
    MAX_REPORT_CHARS,
    build_reference,
    format_reference,
    not_selected_reason,
    strip_invalid_ids,
)
from agents.report import ReportProse
from tests.fixtures.fake_results import evidence, fake_results


@pytest.fixture(autouse=True)
def _no_files(monkeypatch, tmp_path):
    """run()이 outputs/에 쓰지 않도록 격리하고, Chrome 변환은 별도 테스트에서만 한다."""
    monkeypatch.setattr(report, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(report, "export_pdf", lambda *a, **k: None)


def _fake_llm(monkeypatch, **over):
    from agents.report import NoMatchProse
    prose = dict(
        summary="총점이 높고 기술력이 우수하다 [TEC-C07-01]. 시장 성장이 크다 [MKT-C07-01, TEC-C99-01].",
        idea="엣지 NPU로 전력 문제를 해결한다 [DIR-C07-01].",
        team="CEO는 박사다 [DIR-C07-01].",
        tech="TOPS/W 12 (자체 발표) [DIR-C07-01]. 업계 로드맵도 검토했다 [TEC-C07-01].",
        market="CAGR 25% [MKT-C07-01].",
        competition="Hailo 대비 우위 [CMP-C07-01].",
        risks="- 양산 이력 없음 [DIR-C07-01]",
    ) | over
    nm = NoMatchProse(summary=prose.get("nm_summary", "투자 대상 없음. 근접 후보도 기준에 못 미쳤다."),
                      commentary=[report.Commentary(company_id=c, text=f"{c} 해설 [DIR-{c}-01]") for c in ("C01", "C02", "C03", "C04")],
                      patterns=prose.get("nm_patterns", "- 공통 약점 [DIR-C01-01]"))
    prose = {k: v for k, v in prose.items() if not k.startswith("nm_")}
    monkeypatch.setattr(report, "structured_call", lambda schema, *a, **k: nm if schema is NoMatchProse else ReportProse(**prose))
    monkeypatch.setattr(report, "load_prompt", lambda n: "p")


def _state(**over):
    results = fake_results()
    s = {
        "evaluation_results": results, "selected_company_id": "C07", "ranking": ["C07", "C03"],
        "source_document": "2025 초격차 스타트업 1000+ 디렉토리북", "as_of_date": "2026-09-30", "errors": [],
        # 마지막 후보 값이 남아 있는 상황을 흉내: 보고서가 이걸 쓰면 안 된다
        "current_company": results[-1]["current_company"], "current_evidence": results[-1]["current_evidence"],
    }
    return s | over


def test_selected_report_structure(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    for h in ["## SUMMARY", "## 1. 기업 개요", "### 1.1", "### 1.2", "### 1.3", "## 2.", "### 2.1", "### 2.2", "### 2.3",
              "## 3. 투자 판단", "### 3.1", "### 3.2", "### 3.3", "### 3.4", "## REFERENCE"]:
        assert h in text, h
    assert text.index("## SUMMARY") < text.index("## 1.") < text.index("## REFERENCE")
    assert "베타실리콘" in text and "감마반도체" not in text.split("## REFERENCE")[0].split("### 3.1")[0]  # 선정 기업(C07)
    assert len(text) <= MAX_REPORT_CHARS


def test_uses_selected_record_not_current_fields(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    assert "감마반도체" not in text.split("### 3.1")[0]  # current_company(마지막 후보 C09)가 섞이지 않음
    assert "DIR-C09" not in text


def test_reference_only_cited_and_invalid_ids_stripped(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    body, ref = text.split("## REFERENCE")
    assert "TEC-C99-01" not in body  # 존재하지 않는 근거 ID는 본문에서 제거
    assert "TEC-C07-01" in ref and "WSTS" in ref and "Hailo" in ref
    # 인용되지 않은 근거는 REFERENCE에 없다: 이 픽스처의 ELG는 3.2 표에서 인용되므로 포함, 다른 기업 것은 제외
    assert "C03-" not in ref and "C09-" not in ref


def test_unknown_evidence_not_in_reference():
    ev = [evidence("TEC", "C01"), evidence("MKT", "C01", 출처명="사용되지 않음", publisher="X")]
    ref = build_reference("본문 [TEC-C01-01] 만 인용", ev)
    assert "IEEE(2024)" in ref and "사용되지 않음" not in ref


def test_reference_formats_by_source_type():
    org = format_reference(evidence("TEC", "C1"))
    assert org == "IEEE(2024). IRDS 2024 More Moore. https://irds.ieee.org/"
    web = format_reference(evidence("CMP", "C1", source_type="웹페이지", publisher="Hailo", pub_year="2026-08-01",
                                    출처명="Hailo-8", url="https://hailo.ai/p"))
    assert web == "Hailo(2026-08-01). Hailo-8. hailo.ai, https://hailo.ai/p"
    paper = format_reference(evidence("TEC", "C1", source_type="학술 논문", publisher="Kim", pub_year=2023, 출처명="논문", source_page="12-20"))
    assert paper == "Kim(2023). 논문, 12-20."


def test_strip_invalid_ids_cleans_brackets():
    valid = {"TEC-C01-01"}
    assert strip_invalid_ids("값 [TEC-C01-01, TEC-C09-01].", valid)[0] == "값 [TEC-C01-01]."
    assert strip_invalid_ids("값 [TEC-C09-01].", valid) == ("값 .", ["TEC-C09-01"])


def test_not_selected_reason_follows_rank_order():
    a, b = fake_results()[1], fake_results()[0]  # C07(총점 높음) vs C03
    assert "종합 점수" in not_selected_reason(a, b) and "낮음" in not_selected_reason(a, b)
    import copy
    c = copy.deepcopy(a)
    c["scorecard"]["averages"]["제품/기술력"] = 3.0
    assert "기술력" in not_selected_reason(a, c)  # 종합 점수 동점이면 기술력 비교로 넘어감


def test_status_lists_second_place_with_reason(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    sec = text.split("### 3.1")[1].split("### 3.2")[0]
    assert "알파칩스" in sec and "낮음" in sec  # 2위 + 미선정 사유


def test_no_selection_uses_its_own_layout(monkeypatch):
    """투자 대상이 없을 때는 일반 보고서와 목차가 달라야 한다(간소화된 미충족 사유 중심 양식)."""
    _fake_llm(monkeypatch)
    text = report.run(_state(evaluation_results=_all_held(), selected_company_id=None, ranking=[]))["final_report"]
    for h in ["## SUMMARY", "## 1. 심사 결과 개요", "### 1.1 단계별 탈락 현황", "### 1.2 점수 산출 후보",
              "## 2. 후보별 미충족 사유", "## 3. 한계점", "## REFERENCE"]:
        assert h in text, h
    for normal_only in ["### 1.1 기업 정보", "### 2.1 기술력", "### 3.2 종합 평가", "### 3.3 사업 리스크"]:
        assert normal_only not in text, normal_only  # 일반 보고서 목차가 섞이지 않는다
    for dropped in ["공통 미충족 패턴", "## 3. 재검토 조건", "개선 시나리오", "분야별 점수"]:
        assert dropped not in text, dropped  # 간소화로 뺀 부분


def test_missing_selected_record_raises(monkeypatch):
    _fake_llm(monkeypatch)
    with pytest.raises(KeyError):
        report.run(_state(selected_company_id="C99"))


def test_empty_prose_becomes_unknown_not_invented(monkeypatch):
    _fake_llm(monkeypatch, tech="")
    text = report.run(_state())["final_report"]
    assert "### 2.1 기술력\n\n확인 불가" in text


def test_hold_reason_is_not_eligibility_text(monkeypatch):
    """점수 미달 보류 기업의 사유로 'G1~G4 모두 충족'이 나오면 오해를 부른다."""
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    line = [ln for ln in text.splitlines() if ln.startswith("제외·보류 주요 사유")][0]
    assert "모두 충족" not in line and "기술력 우수" in line


def test_run_saves_markdown_and_calls_pdf_export(monkeypatch, tmp_path):
    _fake_llm(monkeypatch)
    calls = []
    monkeypatch.setattr(report, "export_pdf", lambda md, meta, path: calls.append((md, meta, path)))
    text = report.run(_state())["final_report"]
    assert (tmp_path / "report.md").read_text(encoding="utf-8") == text
    assert calls and calls[0][2] == tmp_path / "report.pdf" and calls[0][1]["selected"]["name"] == "베타실리콘"


def test_pdf_failure_keeps_markdown_and_warns(monkeypatch, tmp_path):
    _fake_llm(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("no chrome")

    monkeypatch.setattr(report, "export_pdf", boom)
    with pytest.warns(UserWarning, match="PDF 생성 실패"):
        out = report.run(_state())
    assert "## REFERENCE" in out["final_report"] and (tmp_path / "report.md").exists()


def test_real_pdf_export(monkeypatch, tmp_path):
    """Chrome이 있을 때만: 실제 PDF가 만들어지고 5장 이내이며 핵심 텍스트가 들어 있다."""
    from agents import _report_pdf
    try:
        _report_pdf._find_chrome()
    except RuntimeError:
        pytest.skip("Chrome 없음")
    import pymupdf
    _fake_llm(monkeypatch)
    st = _state()
    md = report.run(st)["final_report"]
    out = _report_pdf.export_pdf(md, _report_pdf.report_meta(st), tmp_path / "r.pdf")
    doc = pymupdf.open(out)
    text = "".join(p.get_text() for p in doc)
    assert doc.page_count <= 5
    assert "베타실리콘" in text and "SUMMARY" in text and "REFERENCE" in text and "투자 적격" in text


def _held(cid, name, scores, unknown=()):
    from tests.fixtures.fake_results import record
    r = record(cid, name, "보류", scores, unknown=list(unknown))
    return r


def _all_held():
    base = {i: 3 for i in "A1 A2 A3 B1 B2 B3 C1 C2 C3 D1 D2 D3 E1 E2 E3".split()}
    return [
        _held("C01", "가나칩", base | {"C1": 2, "C2": 2, "C3": 2, "B1": 2}, unknown=["B1"]),  # 기술력 2.0 → 기술력·총점 미달
        _held("C02", "다라칩", {i: 3 for i in base}),                                          # 총점 60, 기술력 3.0
        _held("C03", "마바칩", {i: 2 for i in base}),                                          # 총점 40
        _held("C04", "사아칩", {i: 2 for i in base}),                                          # 4번째: 표시 제외
    ]


def test_near_miss_picks_top3_by_rank_rules():
    from agents._report_nomatch import near_miss_candidates
    recs = _all_held()
    top = near_miss_candidates(recs)
    assert [r["company_id"] for r in top][:2] == ["C02", "C01"] and len(top) == 3  # 총점 순, 3곳만
    assert top[-1]["company_id"] == "C03"  # 4번째(사아칩, 동점 C04)는 3곳 제한으로 제외


def test_shortfall_is_computed_by_code():
    from agents._report_nomatch import shortfall
    g = shortfall(_all_held()[1])  # 전 항목 3점 → 총점 60
    assert g["total_gap"] == 10.0 and g["tech_gap"] == 0.0
    g1 = shortfall(_all_held()[0])
    assert g1["tech_gap"] > 0 and g1["unknown"] == ["B1"] and g1["scenarios"][0]["total"] > g1["total"]
    assert g1["categories"][0][0] == "제품/기술력"  # 기준 대비 감점이 가장 큰 대분류가 맨 위


def test_no_selection_report_explains_shortfall(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state(evaluation_results=_all_held(), selected_company_id=None, ranking=[]))["final_report"]
    sec = text.split("## 2. 후보별 미충족 사유")[1].split("## 3.")[0]
    assert "### 2.1 " in sec and "### 2.2 " in sec and "### 2.3 " in sec and "### 2.4 " not in sec  # 상위 3곳만
    assert "10.0점 부족" in sec  # 종합 점수 60 → 기준 70
    for label in ["미충족", "점수를 깎은 항목", "보완하면 통과하는가?", "해설"]:
        assert label in sec, label
    assert "C01 해설" in sec  # LLM 해설이 해당 기업 블록에 들어감
    assert len(text) <= MAX_REPORT_CHARS


def test_cause_labels_and_scenarios():
    from agents._report_nomatch import shortfall
    recs = _all_held()
    assert shortfall(recs[1])["cause"] == "종합 점수 미달형"          # 전 항목 3점: 총점 60, 기술력 3.0
    assert shortfall(recs[0])["cause"] == "점수·기술력 동반 미달형"  # 기술력 2.0, 총점 미달
    g = shortfall(recs[2])                                        # 전 항목 2점
    assert g["scenarios"] and g["scenarios"][0]["label"].startswith("낮은 항목") and not g["scenarios"][0]["pass"]


def test_info_shortage_cause_when_unknown_would_pass():
    from agents._report_nomatch import shortfall
    base = {i: 4 for i in "A1 A2 A3 B1 B2 B3 C1 C2 C3 D1 D2 D3 E1 E2 E3".split()}
    rec = _held("C09", "정보부족칩", base | {"C1": 2, "C3": 2, "D2": 2, "B1": 2}, unknown=["C1", "C3", "D2", "B1"])
    g = shortfall(rec)
    assert g["cause"] == "정보 부족형" and g["scenarios"][0]["pass"]


def test_funnel_counts_and_excluded_have_no_detail(monkeypatch):
    from agents._report_nomatch import funnel_counts, near_miss_candidates
    recs = _all_held()
    ex = fake_results()[0]; ex["decision"] = "제외"; ex.pop("scorecard")
    pend = fake_results()[1]; pend["decision"] = "보류"; pend.pop("scorecard")
    allr = recs + [ex, pend]
    c = funnel_counts(allr)
    assert (c["excluded"], c["pending"], c["scored"], c["ok"]) == (1, 1, 4, 0)
    assert all(r.get("scorecard") for r in near_miss_candidates(allr))  # 점수 없는 기업은 상세 대상이 아님


def test_pdf_converter_handles_h4():
    from agents._report_pdf import md_to_html
    assert "<h4>3.2.1 가나칩</h4>" in md_to_html("#### 3.2.1 가나칩")


def test_low_items_capped_for_page_budget():
    from agents._report_nomatch import LOW_ITEM_SHOW, candidate_block
    block = candidate_block(_all_held()[2], 3)  # 전 항목 2점 → 낮은 항목 15개
    assert f"그 외 낮은 항목 {15 - LOW_ITEM_SHOW}개" in block
    low = block.split("**점수를 깎은 항목**")[1].split("그 외")[0]
    assert low.count("| 2점 |") == LOW_ITEM_SHOW  # 표에는 핵심 이유 3개만



def test_inconsistent_hold_is_flagged_not_mislabelled():
    """점수는 기준을 넘는데 보류로 들어온 데이터: '미달형'이라고 잘못 부르지 않는다."""
    from agents._report_nomatch import shortfall
    rec = _held("C10", "불일치칩", {i: 4 for i in "A1 A2 A3 B1 B2 B3 C1 C2 C3 D1 D2 D3 E1 E2 E3".split()})
    g = shortfall(rec)
    assert g["total_gap"] == 0 and g["tech_gap"] == 0 and g["cause"].startswith("기준 충족")


def test_inconsistent_hold_excluded_from_near_miss_and_warned(monkeypatch):
    """점수가 이미 기준을 넘는 보류 기업은 '미충족 후보'가 될 수 없다. 분석에서 빼고 경고로 표시한다."""
    from agents._report_nomatch import inconsistent_holds, near_miss_candidates
    ok_scores = {i: 4 for i in "A1 A2 A3 B1 B2 B3 C1 C2 C3 D1 D2 D3 E1 E2 E3".split()}
    odd = _held("C09", "불일치칩", ok_scores)  # 총점 80, 기술력 4 → 이미 충족
    recs = _all_held() + [odd]
    assert [r["company_id"] for r in inconsistent_holds(recs)] == ["C09"]
    assert "C09" not in [r["company_id"] for r in near_miss_candidates(recs, 10)]
    _fake_llm(monkeypatch)
    text = report.run(_state(evaluation_results=recs, selected_company_id=None, ranking=[]))["final_report"]
    assert "분류 재확인 필요 1곳" in text and "불일치칩" in text
    assert "### 2.1 불일치칩" not in text and "불일치칩: " not in text.split("## 2.")[1]


def test_chunk_based_rag_ids_are_recognised():
    """CONTRACTS 3-4: RAG 근거 ID는 TEC-{chunk_id}(예: TEC-02-0007). 인용 검사·REFERENCE가 이를 인식해야 한다."""
    from agents._report_render import cited_ids
    ev = [evidence("TEC", "X", 1, 근거ID="TEC-02-0007", 출처명="IRDS More Moore"),
          evidence("MKT", "X", 1, 근거ID="MKT-13-0001", 출처명="WSTS Forecast", publisher="WSTS", pub_year=2026)]
    body = "전력 효율은 기준 이상이다 [TEC-02-0007]. 시장은 성장한다 [MKT-13-0001, TEC-99-9999]."
    assert cited_ids(body) == ["TEC-02-0007", "MKT-13-0001", "TEC-99-9999"]
    cleaned, removed = strip_invalid_ids(body, {"TEC-02-0007", "MKT-13-0001"})
    assert removed == ["TEC-99-9999"] and "TEC-99-9999" not in cleaned
    ref = build_reference(cleaned, ev)
    assert "IRDS More Moore" in ref and "WSTS Forecast" in ref


def test_pdf_cite_tags_for_chunk_ids():
    from agents._report_pdf import md_to_html
    html = md_to_html("근거 [TEC-02-0007, DIR-C03-01].")
    assert '<span class="cite">TEC-02-0007</span>' in html and '<span class="cite">DIR-C03-01</span>' in html


# ── 시각 요소: 그래프·배지 ──────────────────────────────────────────────────

def test_funding_chart_only_when_history_exists():
    from agents import _report_charts as ch
    assert ch.funding_chart([]) == "" and ch.funding_chart(None) == ""
    svg = ch.funding_chart([{"일자": "2024", "단계": "Seed", "금액": 100_000, "확정": True},
                            {"일자": "2025", "단계": "Series A", "금액": 500_000, "확정": False}])
    assert svg.startswith("<svg") and "Seed" in svg and "협의 중" in svg


def test_funding_rounds_are_grouped_and_cumulative_excludes_unconfirmed():
    from agents import _report_charts as ch
    rows = [{"일자": "2024", "단계": "Seed", "금액": 200_000, "확정": True},
            {"일자": "2024", "단계": "Seed", "금액": 100_000, "확정": True},
            {"일자": "2025", "단계": "Pre A", "금액": 900_000, "확정": False}]
    g = ch._group_rounds(rows)
    assert len(g) == 2 and g[0]["금액"] == 300_000 and g[0]["건수"] == 2
    svg = ch.funding_chart(rows)
    assert "누적 3.0억" in svg  # 미확정 9억은 누적에서 제외


def test_report_has_funding_directive_only_for_companies_with_history(monkeypatch):
    _fake_llm(monkeypatch)
    text = report.run(_state())["final_report"]
    assert "<!--chart:funding-->" in text  # fixture 기업은 투자 이력이 있다
    st = _state()
    for r in st["evaluation_results"]:
        r["current_company"]["투자유치이력"] = []
    assert "chart:funding" not in report.run(st)["final_report"]  # 이력이 없으면 그래프 자체가 없다


def test_checklist_badges_distinguish_yes_and_no():
    from agents._report_pdf import md_to_html
    html = md_to_html("| # | 판정 |\n|---|---|\n| Q1 | YES |\n| Q2 | NO ⚠주의 |\n| Q3 | PARTIAL |\n| Q4 | 확인불가 ⚠주의 |")
    for cls in ("yes", "no", "partial", "unk"):
        assert f'badge {cls}' in html
    assert 'class="r-no"' in html and 'class="r-yes"' in html and "⚠ 주의" in html


def test_chart_directive_is_replaced_in_pdf_html_and_ignored_without_data():
    from agents._report_pdf import md_to_html
    assert "<svg" in md_to_html("<!--chart:radar_items-->", lambda n, a: "<svg></svg>")
    assert md_to_html("<!--chart:radar_items-->") == ""  # 함수가 없으면 조용히 무시


def test_all_chart_types_render_valid_svg():
    from agents import _report_charts as ch
    avg = {"창업자": 3.7, "시장성": 3.3, "제품/기술력": 4.7, "경쟁 우위": 3.3, "실적": 3.7}
    items = {i: {"점수": 3} for i in ch.ITEM_NAMES}
    for svg in (ch.radar(avg), ch.item_bars(items, ["B1"]),
                ch.candidates_bar([{"name": "가나칩", "total": 66.3, "tech_gap": 0.3}])):
        assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")


def test_table_replaced_by_chart_only_in_pdf():
    from agents._report_pdf import md_to_html
    md = "<!--chart:radar_items-->\n\n<!--chart:skip_table-->\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n| x | y |\n|---|---|\n| 3 | 4 |"
    with_chart = md_to_html(md, lambda n, a: "<svg></svg>")
    assert "<td>1</td>" not in with_chart and "<td>3</td>" in with_chart  # 바로 다음 표만 생략
    assert "<td>1</td>" in md_to_html(md)  # 그래프가 없으면(마크다운 전용) 표를 그대로 둔다


def test_reference_numbers_replace_internal_ids_in_pdf_and_do_not_leak():
    from agents import _report_pdf as pdf
    md = ("# 제목\n\n## SUMMARY\n\n기술이 우수하다 [TEC-02-0001, DIR-C01-01]. 시장이 크다 [MKT-13-0001].\n\n"
          "## REFERENCE\n\n1. IEEE(2024). IRDS. — [TEC-02-0001]\n2. 창업진흥원(2025). 디렉토리북. — [DIR-C01-01]\n3. WSTS(2026). 전망. — [MKT-13-0001]")
    html = pdf.render_html(md, {"selected": None, "as_of": "2026-09-30", "n_total": 1, "n_ok": 0, "_records": {}})
    assert '<sup class="ref">1,2</sup>' in html and '<sup class="ref">3</sup>' in html
    assert "TEC-02-0001" not in html and "DIR-C01-01" not in html and "— [" not in html  # 내부 ID 노출 없음
    assert pdf._REFS == {}  # 다음 변환에 번호 표가 남지 않는다


def test_friendly_reason_hides_internal_terms():
    from agents._report_render import friendly_reason
    t = friendly_reason("자격 요건 확인 필요: G2: Tavily 검색을 하지 않아 확인 불가 / 오류: technology")
    for internal in ("G2", "Tavily", "자격 요건", "오류: technology"):
        assert internal not in t
    assert "투자 단계 요건" in t and "분석 오류(기술 분석 단계)" in t


def test_recheck_hint_matches_scenario_kind():
    from agents._report_nomatch import candidate_block, shortfall
    base = {i: 4 for i in "A1 A2 A3 B1 B2 B3 C1 C2 C3 D1 D2 D3 E1 E2 E3".split()}
    low = _held("C21", "낮은항목칩", base | {"C1": 2, "C3": 2, "D2": 2, "B1": 2, "A2": 2, "E1": 2})   # 자료는 있으나 점수가 낮음
    info = _held("C22", "정보부족칩", base | {"C1": 2, "C3": 2, "D2": 2, "B1": 2, "A2": 2, "E1": 2}, unknown=["C1", "C3", "D2", "B1", "A2", "E1"])
    assert shortfall(low)["scenarios"][0]["kind"] == "low" and shortfall(info)["scenarios"][0]["kind"] == "unknown"
    assert "실제 개선이 확인되면" in candidate_block(low, 1) or "재검토할 여지" in candidate_block(low, 1)
    assert "자료가 확인되면" in candidate_block(info, 1)
    assert "실제 개선" not in candidate_block(info, 1)  # 자료 없음 때문인데 '개선'이라고 말하지 않는다
