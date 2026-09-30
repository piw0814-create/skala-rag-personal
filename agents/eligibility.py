"""② 적격성 검증 에이전트 — 오프라인 근거로 판정하고 외부 확인 불가를 보존한다."""

import json
from datetime import date
from pathlib import Path

from state import State
from tools.eligibility_cache import cache_is_current, company_hash

ROOT = Path(__file__).resolve().parents[1]
G4_RESULTS_PATH = ROOT / "data" / "processed" / "g4_screening_results.json"
EXTERNAL_RESULTS_PATH = ROOT / "data" / "processed" / "external_eligibility_results.json"
SOURCE_TITLE = "2025 초격차 스타트업 1000+ 프로젝트 디렉토리북 1권"


def _company_id(company: dict) -> str:
    return str(company.get("company_id") or "UNKNOWN")


def _short_excerpt(text: str, limit: int = 300) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _pdf_evidence(company: dict, as_of_date: str, excerpt: str, ordinal: int) -> dict:
    cid = _company_id(company)
    return {
        "근거ID": f"ELG-{cid}-{ordinal:02d}",
        "출처명": SOURCE_TITLE,
        "publisher": "창업진흥원",
        "pub_year": 2025,
        "source_type": "기관 보고서",
        "url": None,
        "source_page": company.get("source_page"),
        "확인일": as_of_date,
        "원문발췌": _short_excerpt(excerpt),
        "chunk_id": None,
    }


def _read_g4_result(company_id: str) -> dict | None:
    if not G4_RESULTS_PATH.exists():
        return None
    payload = json.loads(G4_RESULTS_PATH.read_text(encoding="utf-8"))
    return next((row for row in payload.get("results", []) if row.get("company_id") == company_id), None)


def evaluate_company(
    company: dict,
    as_of_date: str,
    g4_result: dict | None = None,
    external_result: dict | None = None,
) -> tuple[dict, list[dict]]:
    """Apply available directory-book evidence; do not guess current external facts."""
    cid = _company_id(company)
    evidence: list[dict] = []
    external_criteria = (external_result or {}).get("criteria", {})
    external_evidence = (external_result or {}).get("current_evidence", [])
    evidence.extend(external_evidence)
    g1 = external_criteria.get("G1") or {
        "결과": "확인불가",
        "사유": "기준일·기업 입력에 맞는 외부 검증 결과 없음: tools.run_tavily_eligibility 실행 필요",
        "근거ID": [],
    }

    rounds = company.get("투자유치이력") or []
    round_groups: dict[int | None, list[str]] = {}
    for row in rounds:
        if isinstance(row, dict):
            line = str(row.get("원문") or "").strip()
            page = row.get("source_page", company.get("source_page"))
        else:
            line, page = str(row).strip(), company.get("source_page")
        if line:
            round_groups.setdefault(page, []).append(line)
    round_ids = []
    if round_groups:
        for page, lines in round_groups.items():
            evidence_company = {**company, "source_page": page}
            ev = _pdf_evidence(evidence_company, as_of_date, "투자유치이력: " + " | ".join(lines), len(evidence) + 1)
            evidence.append(ev)
            round_ids.append(ev["근거ID"])
        summary = "디렉토리북에 투자 행이 있으나 2025년 자료라 현재 최신 라운드는 외부 확인 필요"
        g2 = external_criteria.get("G2") or {"결과": "확인불가", "사유": summary, "근거ID": round_ids}
    else:
        g2 = external_criteria.get("G2") or {
            "결과": "확인불가",
            "사유": "디렉토리북에 투자 이력이 없고 Tavily 최신 검색도 수행하지 않음",
            "근거ID": [],
        }

    g3 = external_criteria.get("G3") or {
        "결과": "확인불가",
        "사유": "Tavily 검색을 하지 않아 인수·합병 또는 Exit 여부를 확인할 수 없음",
        "근거ID": [],
    }

    if g4_result is None:
        g4 = {
            "결과": "확인불가",
            "사유": "이 기업의 G4 사전 스크리닝 결과를 찾지 못함",
            "근거ID": [],
        }
    else:
        result_map = {"통과": "충족", "불통과": "미충족", "확인필요": "확인불가"}
        g4_status = result_map.get(g4_result.get("판정"), "확인불가")
        excerpt = " ".join(
            str(company.get(key) or "") for key in ("메인아이템", "사업Point", "혁신성")
        )
        ev = _pdf_evidence(company, as_of_date, excerpt, len(evidence) + 1)
        evidence.append(ev)
        g4 = {
            "결과": g4_status,
            "사유": g4_result.get("사유", "G4 사전 스크리닝 판정"),
            "근거ID": [ev["근거ID"]],
        }

    criteria = (g1, g2, g3, g4)
    if any(item["결과"] == "미충족" for item in criteria):
        verdict = "부적격"
    elif all(item["결과"] == "충족" for item in criteria):
        verdict = "적격"
    else:
        verdict = "확인필요"

    reasons = [f"{key}: {value['사유']}" for key, value in zip(("G1", "G2", "G3", "G4"), criteria)]
    ids = [evidence_id for value in criteria for evidence_id in value["근거ID"]]
    return {
        "판정": verdict,
        "G1": g1,
        "G2": g2,
        "G3": g3,
        "G4": g4,
        "사유": " / ".join(reasons),
        "근거ID": ids,
    }, evidence


def run(state: State) -> dict:
    """Graph node. Missing DART/Tavily verification stays 확인불가 and routes to hold."""
    company = state.get("current_company") or {}
    as_of_date = state.get("as_of_date") or date.today().isoformat()
    g4_result = _read_g4_result(_company_id(company))
    external_result = _read_external_result(_company_id(company))
    fingerprint = company_hash(company)
    if g4_result and g4_result.get("company_hash") != fingerprint:
        g4_result = None
    if external_result and (
        external_result.get("company_hash") != fingerprint
        or not cache_is_current(external_result.get("completed_at", ""), as_of_date)
    ):
        external_result = None
    eligibility, evidence = evaluate_company(company, as_of_date, g4_result, external_result)
    return {"eligibility": eligibility, "current_evidence": evidence}


def load_g4_results() -> dict[str, dict]:
    """Convenience loader for the C-lane offline batch utility."""
    if not G4_RESULTS_PATH.exists():
        return {}
    payload = json.loads(G4_RESULTS_PATH.read_text(encoding="utf-8"))
    return {row["company_id"]: row for row in payload.get("results", [])}


def _read_external_result(company_id: str) -> dict | None:
    if not EXTERNAL_RESULTS_PATH.exists():
        return None
    payload = json.loads(EXTERNAL_RESULTS_PATH.read_text(encoding="utf-8"))
    return next((row for row in payload.get("results", []) if row.get("company_id") == company_id), None)


def load_external_eligibility_results() -> dict[str, dict]:
    if not EXTERNAL_RESULTS_PATH.exists():
        return {}
    payload = json.loads(EXTERNAL_RESULTS_PATH.read_text(encoding="utf-8"))
    return {row["company_id"]: row for row in payload.get("results", [])}
