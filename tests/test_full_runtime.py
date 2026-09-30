import copy
import json

import pytest

from agents import competitor, loader
from config import EVALUATION_CRITERIA
from runtime import resume_input, save_snapshot
from tests.fixtures.fake_results import fake_results
from tools.audit_results import audit


def test_resume_continues_after_saved_companies(monkeypatch):
    companies = json.loads(loader.CACHE.read_text())
    saved = [{"company_id": c["company_id"], "current_company": c} for c in companies[:3]]
    monkeypatch.setattr(loader, "load_companies", lambda source: companies)
    loaded = loader.run({"evaluation_results": saved})
    assert len(loaded["candidate_companies"]) == 45
    assert loaded["current_index"] == 3
    with pytest.raises(ValueError, match="순서"):
        loader.run({"evaluation_results": list(reversed(saved))})


def test_resume_keeps_completed_results_but_discards_unfinished_errors(tmp_path):
    path = tmp_path / "results.json"
    state = {"source_document": "company.pdf", "as_of_date": "2026-09-30",
             "evaluation_criteria": EVALUATION_CRITERIA,
             "evaluation_results": [{"company_id": "C01"}],
             "errors": [{"company_id": "C01"}, {"company_id": "C02"}]}
    save_snapshot(path, state, "same-input", [], False)
    resumed, _ = resume_input(path, "2026-09-30", EVALUATION_CRITERIA, "same-input")
    assert resumed["evaluation_results"] == state["evaluation_results"]
    assert resumed["errors"] == [{"company_id": "C01"}]
    with pytest.raises(ValueError, match="바뀌었"):
        resume_input(path, "2026-09-30", EVALUATION_CRITERIA, "changed-input")
    with pytest.raises(ValueError, match="기준일"):
        resume_input(path, "2026-10-01", EVALUATION_CRITERIA, "same-input")


def test_same_numeric_value_does_not_make_different_metrics_comparable():
    row = competitor.CompetitorComparison.model_validate({
        "비교표": [{"지표": "TOPS", "대상기업": "12", "경쟁사": [{"이름": "Rival", "값": "12"}]}]
    }).비교표
    products = [{"기업명": "Rival", "핵심지표값": [{"이름": "CPU cores", "값": "12"}]}]
    assert competitor._validated_rows(row, products, {"성능지표": []}) == []
    products[0]["핵심지표값"][0]["이름"] = "TOPS"
    actual = {"성능지표": [{"지표명": "CPU cores", "값": "12"}]}
    assert competitor._validated_rows(row, products, actual)[0]["대상기업"] == "확인 불가"


def test_metric_value_does_not_match_a_different_number():
    assert competitor._quote_contains("12 TOPS", "Rated at 12 TOPS")
    assert not competitor._quote_contains("12 TOPS", "Rated at 112 TOPS")


def test_audit_catches_wrong_total_and_ranking():
    results = copy.deepcopy(fake_results())
    for record in results:
        if record.get("scorecard"):
            record["scorecard"]["total"] += 1
    validation = audit({"evaluation_results": results, "ranking": [], "selected_company_id": None})
    assert not validation["passed"]
    assert any("총점" in issue for issue in validation["issues"])
    assert any("순위" in issue for issue in validation["issues"])


def test_audit_catches_stale_revenue_verdict_and_unknown_flag():
    results = copy.deepcopy(fake_results())
    rec = next(r for r in results if r.get("scorecard"))
    rec["current_company"]["매출액"] = {"연도": 2024, "국내": 21818,
                                      "해외": None, "해외단위": "USD", "상태": "공개"}
    rec["checklist"]["Q7"]["판정"] = "YES"
    rec["scorecard"]["items"]["E2"]["점수"] = 2
    rec["scorecard"]["unknown_items"].append("E2")
    issues = audit({"evaluation_results": results})["issues"]
    assert any("Q7 판정과 최근 연도 매출" in issue for issue in issues)
    assert any("E2 점수·확인불가 표시" in issue for issue in issues)


def test_professor_and_patent_years_do_not_prove_ten_years_of_career():
    from agents._scoring_grounding import cap_score
    state = {"current_company": {"주요구성원": [{"직책": "CEO", "학력": "2024년 반도체 박사", "경력": ""}]}}
    assert cap_score("A1", 5, "교수", state)[0] == 3
    assert cap_score("A3", 5, "10년 추정", state)[0] == 3
    state["current_company"]["주요구성원"][0]["경력"] = "반도체 기업에서 12년 근무"
    assert cap_score("A1", 5, "확인", state)[0] == 5


def test_benchmark_mention_without_verified_values_does_not_get_five():
    from agents._scoring_grounding import cap_score
    state = {"technology_analysis": {"성능지표": [{"값": "확인 불가", "근거ID": ["DIR-C01-01"]}]}}
    assert cap_score("C1", 5, "MLPerf 언급", state)[0] == 3
    assert cap_score("B2", 5, "고객 가치 주장", state)[0] == 3
    state["technology_analysis"]["성능지표"][0]["값"] = "12 TOPS/W"
    assert cap_score("C1", 5, "확인", state)[0] == 5


@pytest.mark.parametrize("raw,expected", [("고객에게 납품하고 있음", True), ("2027년 납품 예정", False),
                                         ("납품하고 있지 않음", False), ("양산 중단", False)])
def test_actual_delivery_is_distinguished_from_plans(raw, expected):
    from agents._scoring_grounding import delivery_quote
    assert bool(delivery_quote({"원문": raw})) == expected


def test_delivery_without_trl_is_not_mistaken_for_prototype():
    from agents._report_scoring import normalize
    from tests.test_investment import _llm_out, _state
    state = _state()
    state["current_company"]["원문"] = "고객에게 납품하고 있음"
    output = _llm_out(3, unknown={"C2"})
    checks, scores, unknown = normalize(output, state)
    assert scores["C2"]["점수"] == 5 and "C2" not in unknown
    assert checks["Q10"]["판정"] == "YES"


def test_rescore_keeps_all_companies_and_rebuilds_ranking(monkeypatch, tmp_path):
    import app
    from agents import investment, report
    records = copy.deepcopy(fake_results())
    cache = tmp_path / "companies.json"
    cache.write_text(json.dumps([r["current_company"] for r in records]))
    monkeypatch.setattr(loader, "CACHE", cache)
    monkeypatch.setattr(app, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(app, "input_signature", lambda: "updated-scoring")
    monkeypatch.setattr("sys.argv", ["app.py", "--rescore"])
    save_snapshot(tmp_path / "evaluation_results.json", {
        "as_of_date": "2026-09-30", "evaluation_criteria": EVALUATION_CRITERIA,
        "evaluation_results": records, "ranking": ["C07", "C03"], "selected_company_id": "C07"
    }, "original", [], True)
    called = []
    def rescore(state):
        called.append(state["company_id"])
        return {"decision": "보류"}
    monkeypatch.setattr(investment, "run", rescore)
    monkeypatch.setattr(report, "run", lambda state: {"final_report": "report"})
    app.main()
    saved = json.loads((tmp_path / "evaluation_results.json").read_text())
    assert called == [r["company_id"] for r in records]
    assert len(saved["evaluation_results"]) == len(records)
    assert saved["ranking"] == [] and saved["selected_company_id"] is None
    assert saved["run"]["complete"] is True


def test_english_alias_and_own_domain_are_not_competitors():
    company = {"기업명": "디노티시아", "홈페이지": "https://www.dnotitia.com"}
    own = competitor.CompetitorProduct(기업명="Dnotitia, Inc.", 제품="Seahorse",
                                       source_url="https://example.com", source_excerpt="facts")
    assert competitor._is_target(own, company)
    own.기업명 = "Unknown"
    own.source_url = "https://www.dnotitia.com/product"
    assert competitor._is_target(own, company)
    own.기업명 = "Rival"
    own.source_url = "https://rival.com/product"
    assert not competitor._is_target(own, company)


def test_close_rankings_disclose_overlapping_scoring_ranges():
    from agents._report_render import candidate_status
    records = copy.deepcopy(fake_results())
    records[0]["scorecard"]["repeat"] = {"runs": 3, "total_min": 75, "total_max": 90}
    records[1]["scorecard"]["repeat"] = {"runs": 3, "total_min": 78, "total_max": 92}
    text = candidate_status({"ranking": ["C07", "C03"]}, records, {r["company_id"]: r for r in records})
    assert "반복 채점 점수 범위가 겹친다" in text


def test_resume_signature_changes_when_raw_corpus_changes(monkeypatch, tmp_path):
    import runtime
    monkeypatch.setattr(runtime, "ROOT", tmp_path)
    raw = tmp_path / "data/raw"
    raw.mkdir(parents=True)
    (raw / "source.pdf").write_bytes(b"original")
    before = runtime.input_signature()
    (raw / "source.pdf").write_bytes(b"updated")
    assert runtime.input_signature() != before
