"""부분적으로 확인된 사실을 최고점 조건 전체로 확대하지 않도록 제한한다."""

import re


def delivery_quote(company: dict) -> str:
    """기업 원문에 현재 납품·양산을 직접 명시한 문장. 계획·부정 표현은 인정하지 않는다."""
    raw = str(company.get("원문") or "")
    for line in raw.splitlines():
        text = " ".join(line.split())
        if re.search(r"예정|계획|목표|추진|검토|않|미완료", text):
            continue
        if re.search(r"(?:납품|양산)(?:을|를)?\s*(?:하고\s*(?:있음|있다|있습니다)|중(?=\s|$|[.,])|완료|개시)", text):
            return text
    return ""


def _ten_years(members: list[dict]) -> bool:
    for member in members:
        # 연도·특허번호를 경력 기간으로 읽지 않는다. 기간이 직접 기재된 경우만 인정한다.
        text = " ".join(str(member.get(k) or "") for k in ("학력", "경력"))
        if any(int(years) >= 10 for years in re.findall(r"(?<!\d)(\d{1,2})\s*년(?=\s|이상|간|$|[.,])", text)):
            return True
        career = str(member.get("경력") or "")
        for start, end in re.findall(r"(19\d{2}|20\d{2})\s*년?\s*[~–-]\s*(19\d{2}|20\d{2})", career):
            if int(end) - int(start) >= 10:
                return True
    return False


def has_company_metric(state: dict) -> bool:
    metrics = (state.get("technology_analysis") or {}).get("성능지표") or []
    return any(re.search(r"\d", str(m.get("값") or ""))
               and any(i.startswith("DIR-") for i in m.get("근거ID") or []) for m in metrics)


def cap_score(item_id: str, score: int, reason: str, state: dict) -> tuple[int, str]:
    """조건 미확인 시 확인된 사실의 기준(3점)으로 제한; 낮은 점수를 올리지 않는다."""
    if score <= 3:
        return score, reason
    company = state.get("current_company") or {}
    members = company.get("주요구성원") or []
    if item_id in ("A1", "A3"):
        relevant = members if item_id == "A1" else [m for m in members
            if m.get("이름") == company.get("대표자명") or "CEO" in str(m.get("직책", "")).upper()]
        if not _ten_years(relevant):
            return 3, "관련 학력·분야 경험은 평가하되, 원문에서 경력 10년 이상을 확인하지 못해 3점으로 제한"
    if item_id in ("B2", "C1"):
        if not has_company_metric(state):
            label = "고객 이점" if item_id == "B2" else "문제 해결 효과"
            return 3, f"{label}는 제시됐으나 원문 검증을 통과한 기업 성능 수치가 없어 정성적 근거 기준 3점으로 제한"
    return score, reason
