"""⑥ 투자 판단 보조: LLM 출력 스키마, 코드 산출 항목(E2·E3), 근거 ID 검증, 정규화."""

import statistics
from typing import Literal

from pydantic import BaseModel, Field

from config import ITEMS, UNKNOWN_SCORE
from agents._scoring_grounding import cap_score, delivery_quote, has_company_metric

Q_IDS = [f"Q{i}" for i in range(1, 12)]
ITEM_IDS = [i for items in ITEMS.values() for i in items]

# E2 매출 (천원 단위): 10억 / 1억
SALES_HIGH = 1_000_000
SALES_MID = 100_000
# E3 누적 투자 (천원 단위): 100억 / 10억
INVEST_HIGH = 10_000_000
INVEST_MID = 1_000_000


class ChecklistOut(BaseModel):
    id: Literal["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7", "Q8", "Q9", "Q10", "Q11"]
    판정: Literal["YES", "PARTIAL", "NO", "확인불가"]
    근거: str
    근거ID: list[str] = Field(default_factory=list)


class ScoreOut(BaseModel):
    id: Literal["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3", "E1", "E2", "E3"]
    점수: int = Field(ge=1, le=5)
    확인불가: bool = Field(description="자료에 없거나 비공개라 판단할 수 없으면 true")
    채점이유: str
    근거ID: list[str] = Field(default_factory=list)


class DetailsOut(BaseModel):
    판단사유: str
    주요위험: list[str]
    추가확인사항: list[str]
    재검토조건: list[str]


class InvestmentOut(BaseModel):
    checklist: list[ChecklistOut]
    scorecard: list[ScoreOut]
    decision_details: DetailsOut


# ── 코드 산출 항목 ─────────────────────────────────────────────────────────

def score_e2(company: dict) -> tuple[int, bool, str]:
    """E2 매출 — 최근 연도 매출(국내+해외, 천원). N/A는 1점, 비공개는 확인 불가 2점."""
    s = company.get("매출액") or {}
    status = s.get("상태")
    if status in ("비공개", "확인불가") or (status is None and not s):
        return UNKNOWN_SCORE, True, "매출 비공개 또는 기재 없음 → 확인 불가"
    if status == "N/A":
        return 1, False, "매출 N/A → 매출 없음"
    if s.get("해외") and s.get("해외단위") not in (None, "천원", "KRW_THOUSAND"):
        return UNKNOWN_SCORE, True, f"해외 매출 단위 {s['해외단위']}: 검증된 환산 근거가 없어 합산 불가"
    if s.get("국내") is None and s.get("해외") is None:
        return UNKNOWN_SCORE, True, "매출 금액 기재 없음 → 확인 불가"
    total = (s.get("국내") or 0) + (s.get("해외") or 0)
    year = s.get("연도")
    if total >= SALES_HIGH:
        return 5, False, f"{year}년 매출 {total:,}천원 (10억 원 이상)"
    if total >= SALES_MID:
        return 3, False, f"{year}년 매출 {total:,}천원 (1억~10억 원)"
    if total > 0:
        return 2, False, f"{year}년 매출 {total:,}천원 (1억 원 미만)"
    return 1, False, f"{year}년 매출 없음"


def _eok_text(thousand: float) -> str:
    v = thousand / 100_000
    return f"{v:,.0f}억 원" if v >= 10 else f"{v:,.1f}억 원"


def judge_q7(company: dict) -> tuple[str, str]:
    """체크리스트 Q7 '매출이 발생하는가?' — 숫자만으로 정해지므로 코드로 판정한다(LLM이 천원 단위를 잘못 환산한 적이 있다).
    YES: 최근 연도 매출 1억 원 이상 / PARTIAL: 1억 원 미만이나 발생 / NO: 매출 없음 / 확인불가: 비공개·기재 없음.
    달러 해외 매출은 E2와 같은 정책으로 원화와 합산하지 않는다: 국내 매출만으로 1억 원 이상이면 YES, 아니면 확인불가."""
    s = company.get("매출액") or {}
    status, year = s.get("상태"), s.get("연도")
    if status in ("비공개", "확인불가") or (status is None and not s):
        return "확인불가", "매출 비공개 또는 기재 없음"
    if status == "N/A":
        return "NO", f"{year}년 매출 없음(N/A)"
    domestic, overseas = s.get("국내"), s.get("해외")
    if domestic is None and overseas is None:
        return "확인불가", "매출 금액 기재 없음"
    if overseas and s.get("해외단위") not in (None, "천원", "KRW_THOUSAND"):
        if (domestic or 0) >= SALES_MID:
            return "YES", f"{year}년 국내 매출 약 {_eok_text(domestic)}({domestic:,}천원)으로 1억 원 이상 (해외 매출은 환산하지 않음)"
        return "확인불가", f"{year}년 해외 매출({s['해외단위']})이 있으나 환산 근거가 없어 1억 원 이상 여부를 확인할 수 없음"
    total = (domestic or 0) + (overseas or 0)
    if total <= 0:
        return "NO", f"{year}년 매출 없음"
    note = f"{year}년 매출 약 {_eok_text(total)}({int(total):,}천원)"
    return ("YES", f"{note}, 1억 원 이상") if total >= SALES_MID else ("PARTIAL", f"{note}, 1억 원 미만")


def score_e3(company: dict) -> tuple[int, bool, str]:
    """E3 투자 유치 — 확정 투자만 합산(협의 중 제외). 100억↑이며 투자자 2곳↑이면 5점."""
    history = company.get("투자유치이력")
    if history is None:
        return UNKNOWN_SCORE, True, "투자 유치 이력 기재 없음 → 확인 불가"
    confirmed = [h for h in history if h.get("확정", True)]
    if any(h.get("금액") is None for h in confirmed):
        return UNKNOWN_SCORE, True, "확정 투자 중 비공개 또는 미확인 금액이 있어 누적 금액 확인 불가"
    total = sum(h.get("금액") or 0 for h in confirmed)
    investors = {i for h in confirmed for i in (h.get("투자자") or [])}
    text = f"확정 투자 누적 {total:,}천원, 투자자 {len(investors)}곳 (협의 중 제외)"
    if total >= INVEST_HIGH:
        return (5 if len(investors) >= 2 else 4), False, text
    if total >= INVEST_MID:
        return 3, False, text
    return 1, False, text


CODE_SCORED = {"E2": score_e2, "E3": score_e3}


# ── 근거 ID ────────────────────────────────────────────────────────────────

def allowed_evidence_ids(state: dict) -> set[str]:
    """이번 기업에서 실제 근거 레코드로 확보한 ID만 허용한다."""
    return {e["근거ID"] for e in state.get("current_evidence") or [] if e.get("근거ID")}


def _clean_ids(ids: list[str], allowed: set[str]) -> list[str]:
    seen: list[str] = []
    for i in ids:
        if i in allowed and i not in seen:
            seen.append(i)
    return seen


# ── 정규화 ─────────────────────────────────────────────────────────────────

def normalize(out: InvestmentOut, state: dict) -> tuple[dict, dict, list[str]]:
    """LLM 출력 → (checklist, score items, unknown_items).

    - 없는 ID는 버리고, 누락 문항은 확인 불가로 채운다(점수 2).
    - 확인 불가는 LLM 점수와 무관하게 2점으로 고정한다.
    - E2·E3와 매출 체크리스트 Q7은 숫자 규칙으로 코드가 다시 계산해 LLM 값을 덮어쓴다.
    - 허용되지 않은 근거 ID는 제거한다(보고서 REFERENCE가 만들 수 없는 ID 방지).
    """
    allowed = allowed_evidence_ids(state)
    company = state.get("current_company") or {}
    market = state.get("market_analysis") or {}
    missing_market_values = not any(
        (market.get(key) or {}).get("값") not in (None, "", "확인 불가", "확인불가")
        for key in ("시장규모", "성장률")
    )

    checklist = {}
    for q in out.checklist:
        refs = _clean_ids(q.근거ID, allowed)
        checklist[q.id] = {"판정": q.판정 if refs else "확인불가",
                           "근거": q.근거 if refs else "유효한 원문 근거 없음 → 확인 불가", "근거ID": refs}
    for q in Q_IDS:
        checklist.setdefault(q, {"판정": "확인불가", "근거": "LLM 출력 누락", "근거ID": []})
    if checklist["Q2"]["판정"] == "YES" and not has_company_metric(state):
        checklist["Q2"].update(판정="PARTIAL", 근거="해결할 문제는 제시됐으나 원문 검증을 통과한 정량 효과가 없어 부분 확인")

    items: dict[str, dict] = {}
    unknown: list[str] = []
    for s in out.scorecard:
        refs = _clean_ids(s.근거ID, allowed)
        is_unknown = s.확인불가 or not refs or (s.id == "B1" and missing_market_values)
        score = UNKNOWN_SCORE if is_unknown else s.점수
        reason = s.채점이유 if refs else "유효한 원문 근거 없음 → 확인 불가"
        if not is_unknown:
            score, reason = cap_score(s.id, score, reason, state)
        items[s.id] = {"점수": score, "채점이유": reason,
                       "근거ID": refs}
        if is_unknown:
            unknown.append(s.id)

    dir_ids = sorted(i for i in allowed if i.startswith("DIR-"))
    verdict7, reason7 = judge_q7(company)
    checklist["Q7"] = {"판정": verdict7, "근거": f"{reason7} (코드 산출)", "근거ID": dir_ids}
    delivered = delivery_quote(company)
    if delivered and dir_ids:
        items["C2"] = {"점수": 5, "채점이유": f"기업 원문에서 현재 납품·양산 단계 확인: {delivered} (TRL 숫자는 추정하지 않음)",
                       "근거ID": dir_ids}
        unknown = [i for i in unknown if i != "C2"]
        checklist["Q10"] = {"판정": "YES", "근거": f"기업 자료의 납품·양산 명시: {delivered}", "근거ID": dir_ids}
    for item_id, fn in CODE_SCORED.items():
        score, is_unknown, reason = fn(company)
        items[item_id] = {"점수": score, "채점이유": f"{reason} (코드 산출)", "근거ID": dir_ids}
        if is_unknown and item_id not in unknown:
            unknown.append(item_id)
        if not is_unknown and item_id in unknown:
            unknown.remove(item_id)

    for item_id in ITEM_IDS:
        if item_id not in items:
            items[item_id] = {"점수": UNKNOWN_SCORE, "채점이유": "LLM 출력 누락 → 확인 불가", "근거ID": []}
            unknown.append(item_id)

    return checklist, items, [i for i in ITEM_IDS if i in unknown]


# ── 반복 채점 → 중앙값 ─────────────────────────────────────────────────────

SCORING_RUNS = 3  # LLM 채점 반복 횟수. 같은 기업이 실행마다 다른 판정을 받지 않도록 중앙값을 쓴다.
JUDGE_ORDER = ["YES", "PARTIAL", "NO", "확인불가"]


def aggregate(runs: list[tuple[dict, dict, list[str]]], details: list[dict], total_fn) -> tuple[dict, dict, list[str], dict, dict]:
    """정규화된 채점 결과 여러 개를 하나로 합친다.

    runs = [(checklist, items, unknown_items), ...], details = 각 회차의 decision_details,
    total_fn(item_scores) → (대분류 평균, 총점).
    - 세부 항목: 자료 없음 표시가 과반이면 자료 없음(2점), 아니면 자료 없음이 아닌 회차 점수의 중앙값.
    - 체크리스트: 판정을 YES<PARTIAL<NO<확인불가 순서로 놓고 중앙값.
    - 채점 이유·근거 ID는 선택된 값과 같은 결과를 낸 첫 회차의 것을 쓰고, decision_details는 총점이 중앙값인 회차의 것을 쓴다.
    반환: (checklist, items, unknown_items, decision_details, 반복 채점 요약)
    """
    n = len(runs)
    items: dict[str, dict] = {}
    unknown: list[str] = []
    for i in ITEM_IDS:
        flagged = [i in r[2] for r in runs]
        if sum(flagged) * 2 > n:
            src = next(r for r, f in zip(runs, flagged) if f)
            items[i] = dict(src[1][i])
            unknown.append(i)
            continue
        cands = [r for r, f in zip(runs, flagged) if not f]
        med = statistics.median_low(r[1][i]["점수"] for r in cands)
        items[i] = dict(next(r[1][i] for r in cands if r[1][i]["점수"] == med))

    checklist: dict[str, dict] = {}
    for q in Q_IDS:
        order = sorted(JUDGE_ORDER.index(r[0][q]["판정"]) for r in runs)
        med = JUDGE_ORDER[statistics.median_low(order)]
        checklist[q] = dict(next(r[0][q] for r in runs if r[0][q]["판정"] == med))

    totals = [total_fn({i: r[1][i]["점수"] for i in ITEM_IDS})[1] for r in runs]
    med_total = statistics.median_low(totals)
    chosen = details[totals.index(med_total)]
    summary = {"runs": n, "total_min": min(totals), "total_max": max(totals)}
    return checklist, items, [i for i in ITEM_IDS if i in unknown], chosen, summary
