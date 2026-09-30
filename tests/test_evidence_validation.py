import json
from types import SimpleNamespace

import pytest

from agents import competitor, loader, report
from agents._report_scoring import allowed_evidence_ids, normalize
from schemas import CompanyRecord
from tests.test_investment import _llm_out, _state


def test_real_company_records_match_shared_schema():
    for company in json.loads(loader.CACHE.read_text()):
        CompanyRecord.model_validate(company)


def test_unbacked_scores_and_checks_become_unknown():
    output = _llm_out(5)
    for item in output.scorecard + output.checklist:
        item.근거ID = ["TEC-INVENTED-9999"]
    checks, scores, unknown = normalize(output, _state())
    assert checks["Q1"]["판정"] == "확인불가"
    assert scores["A1"]["점수"] == 2
    assert scores["A1"]["근거ID"] == []
    assert "A1" in unknown
    assert scores["E2"]["점수"] == 5  # 원문 기업 데이터로 코드 산출하는 항목은 보존


def test_analysis_id_without_evidence_record_is_not_allowed():
    assert allowed_evidence_ids({"technology_analysis": {"근거ID": ["TEC-INVENTED-9999"]}}) == set()


def test_missing_market_values_are_unknown_even_if_llm_claims_otherwise():
    state = _state()
    state["market_analysis"] = {"시장규모": {"값": "확인 불가"}, "성장률": {"값": "확인 불가"}}
    _, scores, unknown = normalize(_llm_out(5), state)
    assert scores["B1"]["점수"] == 2
    assert "B1" in unknown


@pytest.mark.parametrize("bad", ["url", "excerpt", "value"])
def test_invalid_competitor_facts_cannot_survive_in_comparison(monkeypatch, bad):
    product = {"기업명": "Rival", "제품": "Chip", "핵심지표값": [{"이름": "TOPS", "값": "12 TOPS"}],
               "source_url": "https://example.com", "source_excerpt": "Chip has 12 TOPS"}
    if bad == "url":
        product["source_url"] = "https://missing.example"
    elif bad == "excerpt":
        product["source_excerpt"] = "invented quote"
    else:
        product["핵심지표값"][0]["값"] = "999 TOPS"
    comparison = competitor.CompetitorComparison.model_validate({
        "경쟁제품": [product], "열위": ["Rival보다 낮음"],
        "비교표": [{"지표": "TOPS", "대상기업": "1 TOPS", "경쟁사": [{"이름": "Rival", "값": "999 TOPS"}]}],
    })
    model = SimpleNamespace(with_structured_output=lambda *a, **k: SimpleNamespace(invoke=lambda _: comparison))
    monkeypatch.setenv("TAVILY_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setattr(competitor, "get_llm", lambda: model)
    monkeypatch.setattr(competitor, "_retrieve_context", lambda *a: ([], ""))
    monkeypatch.setattr(competitor, "_search_sources", lambda *a: ([{"url": "https://example.com", "content": "Chip has 12 TOPS"}], ""))
    result = competitor.run({"current_company": {"company_id": "C03"},
                             "technology_analysis": {"核心": "NPU"}, "market_analysis": {"目標": "AI"}})
    assert result["competitor_analysis"]["비교표"] == []
    assert result["competitor_analysis"]["열위"] == []


def test_verified_comparison_values_are_preserved():
    row = competitor.CompetitorComparison.model_validate({
        "비교표": [{"지표": "TOPS", "대상기업": "10 TOPS", "경쟁사": [{"이름": "Rival", "값": "12 TOPS"}]}],
    }).비교표
    products = [{"기업명": "Rival", "핵심지표값": [{"이름": "TOPS", "값": "12 TOPS"}]}]
    tech = {"성능지표": [{"지표명": "TOPS", "값": "10 TOPS"}]}
    assert competitor._validated_rows(row, products, tech) == [r.model_dump() for r in row]


def test_unscored_report_does_not_ask_llm_to_invent_summary(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("점수 미산출 요약에는 LLM을 호출하면 안 됨")
    monkeypatch.setattr(report, "structured_call", fail)
    body, _ = report._no_selection_report({}, [], {})
    assert "투자 점수가 산출된 기업이 없어" in body
    assert "최고 점수" not in body


def test_report_team_does_not_invent_years_and_risks_use_actual_unknowns():
    from agents._report_grounding import team_text, risk_text, disclosure_text
    company = {"주요구성원": [{"직책": "CEO", "이름": "대표", "학력": "관련 박사", "경력": ""}]}
    card = {"unknown_items": ["B1"], "items": {"A2": {"점수": 3}, "B1": {"근거ID": ["MKT-13-0001"]}}}
    assert "10년" not in team_text(company, card, ["DIR-C03-01"])
    risks = risk_text({"scorecard": card}, ["DIR-C03-01"])
    assert "MKT-13-0001" in risks and "시장" in risks
    assert "미공개" not in disclosure_text("시장 수치가 미공개이며 성능이 공개되지 않아 비교가 어렵다")
