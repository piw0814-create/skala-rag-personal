"""⑦ 보고서 생성 에이전트 — State만 사용 (신규 검색 금지). SUMMARY + 1~3장 + REFERENCE, 5장 이내."""

import json
import warnings

from pydantic import BaseModel

from agents._report_llm import load_prompt, structured_call
from agents._report_render import (
    MAX_REPORT_CHARS,
    build_reference,
    candidate_status,
    checklist_table,
    company_info_table,
    decision_sentence,
    eligibility_table,
    limitations,
    scope_sentence,
    strip_invalid_ids,
)
from agents._report_highlights import patents, pick_strengths, revenue_eok
from agents._report_grounding import competition_text, disclosure_text, summary_text, team_text, risk_text
from agents._report_nomatch import (
    LOW_ITEM_SHOW,
    candidate_block,
    near_miss_candidates,
    overview_section,
    shortfall,
)
from agents._report_pdf import export_pdf, report_meta
from config import INVEST_THRESHOLD, OUTPUT_DIR, TECH_MIN
from state import State

PDF_NAME = "report.pdf"  # 제출용 파일명(RAG-Output_울산-4반_{이름}.pdf)으로 바꿔 복사한다


class ReportProse(BaseModel):
    summary: str
    idea: str = ""
    team: str = ""
    tech: str = ""
    market: str = ""
    competition: str = ""
    risks: str


class Commentary(BaseModel):
    company_id: str
    text: str


class NoMatchProse(BaseModel):
    summary: str
    commentary: list[Commentary] = []


def _evidence_of(rec: dict) -> list[dict]:
    return rec.get("current_evidence") or []


def _all_evidence(records: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for r in records:
        for e in _evidence_of(r):
            seen.setdefault(e["근거ID"], e)
    return list(seen.values())


def _prose(mode: str, payload: dict, evidence: list[dict]) -> tuple[ReportProse, list[str]]:
    """LLM 서술 생성 후 유효하지 않은 근거 ID를 제거한다. 반환: (서술, 제거된 ID)."""
    user = json.dumps({"mode": mode, **payload,
                       "사용 가능한 근거ID": [e["근거ID"] for e in evidence if e.get("근거ID")]},
                      ensure_ascii=False, default=str)
    prose = structured_call(ReportProse, load_prompt("report"), user)
    valid = {e["근거ID"] for e in evidence if e.get("근거ID")}
    removed: list[str] = []
    cleaned = {}
    for k, v in prose.model_dump().items():
        cleaned[k], r = strip_invalid_ids(v, valid)
        removed += r
    return ReportProse(**cleaned), removed


def _or_unknown(text: str) -> str:
    return text.strip() or "확인 불가"


def _selected_report(state: State, records: list[dict], by_id: dict[str, dict]) -> str:
    rec = by_id[state["selected_company_id"]]
    company, sc = rec["current_company"], rec["scorecard"]
    dir_ids = [e["근거ID"] for e in _evidence_of(rec) if e["근거ID"].startswith("DIR-")]
    strengths = pick_strengths(rec)
    kinds = {st.kind for st in strengths}
    payload = {
        "기업": company, "적격성": rec.get("eligibility"), "기술분석": rec.get("technology_analysis"),
        "시장분석": rec.get("market_analysis"), "경쟁분석": rec.get("competitor_analysis"),
        "체크리스트": rec.get("checklist"), "스코어카드": sc, "판정": rec["decision"],
        "판단상세": rec.get("decision_details"), "순위": state.get("ranking"),
        "투자적격 수": len(state.get("ranking") or []),
        "선정핵심근거(코드 선정, 요약의 투자 근거로 그대로 사용)": [
            {"영역": st.area, "평균점수": round(st.avg, 1), "사실": st.fact} for st in strengths],
    }
    prose, _ = _prose("selected", payload, _evidence_of(rec))
    prose = ReportProse(**{k: disclosure_text(v) for k, v in prose.model_dump().items()})
    prose.team = team_text(company, sc, dir_ids)
    prose.risks = risk_text(rec, dir_ids)
    prose.competition = competition_text(rec)
    prose.summary = summary_text(rec, strengths, dir_ids)
    if prose.tech.strip():
        prose.tech = "기업 제출 자료에 기재된 기술·개발 단계 설명이다. " + prose.tech
    cl_table, cl_warn = checklist_table(rec.get("checklist") or {})
    n_ok = len(state.get("ranking") or [])
    head = (f"**투자 추천: {company['기업명']}** (투자 적격 {n_ok}곳 중 1위, 종합 점수 {sc['total']}점). "
            f"{scope_sentence(state, records)}")
    mkt = rec.get("market_analysis") or {}
    blocks = {  # 강점 영역일 때만 해당 그래프를 넣는다 (5장 이내). 투자 유치 이력은 이력이 있으면 항상 넣는다.
        "funding": "\n\n<!--chart:funding-->" if company.get("투자유치이력") else "",
        "revenue": "\n\n<!--chart:revenue-->" if "revenue" in kinds and revenue_eok(company.get("매출액") or {}) else "",
        "tech": "\n\n<!--chart:tech-->" if "tech" in kinds and rec.get("technology_analysis") else "",
        "ip": "\n\n<!--chart:ip-->" if "ip" in kinds and sum(patents(company)) else "",
        "competitor": "\n\n<!--chart:competitor-->" if any((rec.get("competitor_analysis") or {}).get(k) for k in ("경쟁제품", "우위", "열위", "비교표")) else "",
        "market": "\n\n<!--chart:market-->" if (mkt.get("시장규모") or {}).get("값") or (mkt.get("성장률") or {}).get("값") else "",
    }
    sections = [
        f"# 투자 평가 보고서: {company['기업명']}",
        f"## SUMMARY\n\n{head}\n\n{_or_unknown(prose.summary)}\n\n<!--chart:strengths-->",
        "## 1. 기업 개요",
        f"### 1.1 기업 정보\n\n{company_info_table(company, dir_ids)}{blocks['funding']}{blocks['revenue']}",
        f"### 1.2 사업 아이디어\n\n{_or_unknown(prose.idea)}",
        f"### 1.3 팀 구성\n\n{_or_unknown(prose.team)}",
        "## 2. 기술·시장·경쟁 분석",
        f"### 2.1 기술력\n\n{_or_unknown(prose.tech)}{blocks['tech']}{blocks['ip']}",
        f"### 2.2 시장성\n\n{_or_unknown(prose.market)}{blocks['market']}",
        f"### 2.3 경쟁 구도\n\n{_or_unknown(prose.competition)}{blocks['competitor']}",
        "## 3. 투자 판단",
        f"### 3.1 평가 범위 및 후보 현황\n\n{candidate_status(state, records, by_id)}",
        f"### 3.2 종합 평가\n\n**투자 요건**\n\n{eligibility_table(rec['eligibility'])}\n\n"
        f"**종합 점수 {sc['total']}점** (5개 분야 평균과 15개 세부 항목, 5점 만점)\n\n<!--chart:radar_items-->\n\n<!--chart:callouts-->\n\n"
        f"**핵심 점검 11문항**\n\n{cl_table}\n\n{decision_sentence(rec)}",
        f"### 3.3 사업 리스크\n\n{_or_unknown(prose.risks)}",
        f"### 3.4 한계점\n\n{limitations(state, rec, cl_warn)}",
    ]
    return "\n\n".join(sections), _evidence_of(rec)


def _nomatch_payload(records: list[dict], top: list[dict]) -> dict:
    """LLM 입력: 코드가 계산한 미충족 수치를 그대로 넘긴다(LLM은 인용만)."""
    cands = []
    for r in top:
        g, sc = shortfall(r), r["scorecard"]
        cands.append({
            "company_id": r["company_id"], "기업": r["current_company"]["기업명"], "미충족유형": g["cause"],
            "총점": g["total"], "총점부족": g["total_gap"], "기술력": round(g["tech"], 2), "기술력부족": g["tech_gap"],
            "감점큰대분류": [{"대분류": c, "기준대비": round(d, 1)} for c, _, _, d in g["categories"][:2]],
            "낮은항목": [{"id": i, "점수": sc["items"][i]["점수"], "채점이유": sc["items"][i].get("채점이유"),
                       "근거ID": sc["items"][i].get("근거ID")} for i in g["low_items"][:LOW_ITEM_SHOW]],
            "확인불가": g["unknown"], "개선시나리오": g["scenarios"], "판단상세": r.get("decision_details"),
        })
    return {"근접후보(코드 계산, 그대로 인용)": cands,
            "전체현황": {"평가": len(records), "후보별 판정": [{"기업": r["current_company"]["기업명"], "판정": r["decision"]}
                                                   for r in records if r.get("current_company")]}}


def _no_selection_report(state: State, records: list[dict], by_id: dict[str, dict]) -> tuple[str, list[dict]]:
    """투자 적격이 없을 때의 별도 양식: 미충족 사유 중심 (일반 보고서와 목차가 다르다)."""
    top = near_miss_candidates(records)
    evidence = _all_evidence(records)
    payload = _nomatch_payload(records, top)
    user = json.dumps({"mode": "no_selection", **payload,
                       "사용 가능한 근거ID": [e["근거ID"] for e in evidence if e.get("근거ID")]}, ensure_ascii=False, default=str)
    if not any(r.get("scorecard") for r in records):
        prose = NoMatchProse(summary=(
            "투자 점수가 산출된 기업이 없어 투자 대상을 선정하지 못했다. "
            "자격 요건 확인 또는 분석 오류로 평가가 완료되지 않았으며, 기업의 경쟁력 부족이나 점수 미달을 뜻하지 않는다. "
            "미확인 자격과 분석 오류를 보완한 뒤 다시 평가해야 한다."
        ))
    else:
        prose = structured_call(NoMatchProse, load_prompt("report"), user)
    valid = {e["근거ID"] for e in evidence if e.get("근거ID")}
    clean = lambda t: strip_invalid_ids(t, valid)[0]  # noqa: E731
    comments = {c.company_id: clean(c.text) for c in prose.commentary}

    blocks = "\n\n".join(candidate_block(r, n, comments.get(r["company_id"], "")) for n, r in enumerate(top, 1))
    intro = (f"점수가 산출된 보류 기업 중 종합 점수가 높은 {len(top)}곳이 왜 투자 적격이 되지 못했는지 정리한다. "
             f"투자 적격 기준은 종합 점수 {INVEST_THRESHOLD}점 이상이면서 기술력 평균 {TECH_MIN} 이상이다.") if top else \
            "점수가 산출된 보류 기업이 없어 항목별 미충족 사유를 분석할 수 없다."
    head = f"**투자 대상 없음.** {scope_sentence(state, records)}"
    sections = [
        "# 투자 평가 보고서: 투자 대상 없음",
        f"## SUMMARY\n\n{head}\n\n{_or_unknown(clean(prose.summary))}",
        f"## 1. 심사 결과 개요\n\n{overview_section(state, records)}",
        f"## 2. 후보별 미충족 사유\n\n{intro}\n\n{blocks}".rstrip(),
        f"## 3. 한계점\n\n{limitations(state, None, [])}",
    ]
    return "\n\n".join(sections), evidence


def run(state: State) -> dict:
    """
    입력: evaluation_results, selected_company_id, ranking, source_document, as_of_date
    처리: selected_company_id가 있으면 evaluation_results에서 그 기업 레코드를 찾아 보고서 작성,
          없으면 '후보별 평가 요약' 구성 (설계: 투자 보고서 › 4)
          ※ State의 current_company·current_evidence 등은 마지막 후보 값이므로 사용하지 않는다
          3.1에 ranking 2·3위와 미선정 사유 요약
          REFERENCE는 본문에서 실제 인용한 근거 ID만, 레코드의 current_evidence에서 source_type별 가이드 표기 형식으로 생성
    출력: {"final_report": str (markdown)}
    """
    records = state.get("evaluation_results") or []
    by_id = {r["company_id"]: r for r in records}
    selected = state.get("selected_company_id")
    if selected and selected not in by_id:
        raise KeyError(f"selected_company_id {selected!r} 가 evaluation_results에 없음")

    body, evidence = (_selected_report if selected else _no_selection_report)(state, records, by_id)
    report = f"{body}\n\n## REFERENCE\n\n{build_reference(body, evidence)}"
    if len(report) > MAX_REPORT_CHARS:
        report += f"\n\n<!-- 경고: {len(report):,}자 (5장 추정 상한 {MAX_REPORT_CHARS:,}자 초과) -->"
    outputs = _save_outputs(state, report)
    return {"final_report": report, "report_outputs": outputs}


def _save_outputs(state: State, report: str) -> dict:
    """최종 산출물은 PDF다. markdown도 함께 저장하고, PDF 변환이 실패해도 markdown은 남긴다."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "report.md").write_text(report, encoding="utf-8")
    outputs = {"markdown": str(OUTPUT_DIR / "report.md"), "pdf": None, "pdf_error": None}
    try:
        export_pdf(report, report_meta(state), OUTPUT_DIR / PDF_NAME)
        outputs["pdf"] = str(OUTPUT_DIR / PDF_NAME)
    except Exception as e:  # 평가 결과를 잃지 않도록 예외를 삼키되 경고는 크게 남긴다
        outputs["pdf_error"] = str(e)
        warnings.warn(f"PDF 생성 실패: {e!r}. outputs/report.md만 저장됨.", stacklevel=2)
    return outputs
