"""⑦ 보고서 보조: 코드로 만드는 결정적 섹션(표·점수·후보 현황·한계점), 인용 검증, REFERENCE 생성."""

import re
from collections import Counter
from urllib.parse import urlparse

from agents.investment import rank_key
from config import INVEST_THRESHOLD, TECH_MIN

QUESTIONS = {
    "Q1": "목표 시장이 구체적인가?", "Q2": "구체적인 문제를 해결하는가?", "Q3": "고객이 구매할 이유가 있는가?",
    "Q4": "차별성이 있는가?", "Q5": "주요 구성원을 신뢰할 수 있는가?", "Q6": "초기 고객이 있는가?",
    "Q7": "매출이 발생하는가?", "Q8": "글로벌 기회가 있는가?", "Q9": "창업자가 분야에 꾸준히 몸담아 왔는가?",
    "Q10": "개발이 어디까지 진행됐는가?", "Q11": "외부에서 검증받았는가?",
}
ITEM_NAMES = {
    "A1": "기술 전문성", "A2": "팀 완성도", "A3": "분야 몰입도", "B1": "시장 규모·성장성", "B2": "고객 가치",
    "B3": "글로벌 확장성", "C1": "문제 해결력", "C2": "개발 성숙도", "C3": "분야별 핵심 기술 지표",
    "D1": "지식재산", "D2": "경쟁사 대비 차별성", "D3": "외부 검증", "E1": "고객 확보", "E2": "매출", "E3": "투자 유치",
}

MAX_REPORT_CHARS = 12_000  # 5장(SUMMARY·REFERENCE 포함) 추정 상한. 최종 PDF에서 페이지 수로 재확인한다.

ID_RE = re.compile(r"\b(?:DIR|ELG|TEC|MKT|CMP)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+\b")


# ── 인용 검증 ──────────────────────────────────────────────────────────────

def strip_invalid_ids(text: str, valid: set[str]) -> tuple[str, list[str]]:
    """본문에서 유효하지 않은 근거 ID를 제거한다. 반환: (정리된 본문, 제거된 ID 목록)."""
    removed: list[str] = []

    def repl(m: re.Match) -> str:
        if m.group(0) in valid:
            return m.group(0)
        removed.append(m.group(0))
        return ""

    out = ID_RE.sub(repl, text)
    out = re.sub(r"\[[\s,]*\]", "", out)  # 빈 대괄호
    out = re.sub(r"\[\s*,\s*", "[", out)
    out = re.sub(r"\s*,\s*\]", "]", out)
    return out, removed


def cited_ids(body: str) -> list[str]:
    seen: list[str] = []
    for i in ID_RE.findall(body):
        if i not in seen:
            seen.append(i)
    return seen


def cite(ids: list[str]) -> str:
    return "[" + ", ".join(ids) + "]" if ids else ""


# ── REFERENCE ──────────────────────────────────────────────────────────────

def format_reference(e: dict) -> str:
    """가이드 표기 형식 3종. source_type별로 사용 필드가 다르다."""
    pub, year, title = e.get("publisher") or "", e.get("pub_year") or "n.d.", e.get("출처명") or e.get("title") or ""
    url = e.get("url")
    kind = e.get("source_type")
    if kind == "학술 논문":
        page = f", {e['source_page']}." if e.get("source_page") else "."
        return f"{pub}({year}). {title}{page}"
    if kind == "웹페이지":
        site = urlparse(url).netloc if url else pub
        return f"{pub}({year}). {title}. {site}" + (f", {url}" if url else "")
    return f"{pub}({year}). {title}." + (f" {url}" if url else "")


def build_reference(body: str, evidence: list[dict]) -> str:
    """본문에서 실제 인용된 근거 ID의 출처만. 같은 출처(표기 동일)는 한 줄로 합치고 ID를 병기한다."""
    by_id = {e["근거ID"]: e for e in evidence if e.get("근거ID")}
    grouped: dict[str, list[str]] = {}
    for i in cited_ids(body):
        if i in by_id:
            grouped.setdefault(format_reference(by_id[i]), []).append(i)
    lines = [f"{n}. {ref} — {cite(ids)}" for n, (ref, ids) in enumerate(grouped.items(), 1)]
    return "\n".join(lines) if lines else "인용된 근거가 없습니다."


# ── 공통 표 헬퍼 ───────────────────────────────────────────────────────────

def md_table(header: list[str], rows: list[list]) -> str:
    esc = lambda v: str(v).replace("|", "/").replace("\n", " ")  # noqa: E731
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def _clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ── 1.1 기업 정보 ──────────────────────────────────────────────────────────

def latest_round(company: dict) -> str:
    confirmed = [h for h in company.get("투자유치이력") or [] if h.get("확정", True)]
    if not confirmed:
        return "확인 불가"
    last = max(confirmed, key=lambda h: h.get("일자") or "")
    return f"{last.get('단계') or '단계 미상'} ({last.get('일자') or '일자 미상'})"


def company_info_table(company: dict, dir_ids: list[str]) -> str:
    rows = [
        ["설립일", company.get("설립일") or "확인 불가"],
        ["직원 수", f"{company['직원수']}명" if company.get("직원수") is not None else "확인 불가"],
        ["대표자", company.get("대표자명") or "확인 불가"],
        ["업종 / 기술분야", f"{company.get('업종') or '-'} / {company.get('기술분야') or '-'}"],
        ["투자 단계 (기업 자료)", latest_round(company)],
        ["메인 아이템", company.get("메인아이템") or "확인 불가"],
    ]
    return md_table(["항목", f"내용 {cite(dir_ids)}".strip()], rows)


# ── 3.1 평가 범위 및 후보 현황 ─────────────────────────────────────────────

def not_selected_reason(top: dict, other: dict) -> str:
    """1위와 순위 규칙(총점 → 기술력 → 확인 불가 수 → 실적)을 차례로 비교해 미선정 사유를 만든다."""
    a, b = rank_key(top), rank_key(other)
    ta, tb = top["scorecard"], other["scorecard"]
    if a[0] != b[0]:
        return f"종합 점수 {tb['total']}점으로 1위({ta['total']}점)보다 {ta['total'] - tb['total']:.1f}점 낮음"
    if a[1] != b[1]:
        return f"종합 점수 동점, 기술력 평균 {tb['averages']['제품/기술력']:.2f}이 1위({ta['averages']['제품/기술력']:.2f})보다 낮음"
    if a[2] != b[2]:
        return f"종합 점수·기술력 동점, 자료 없는 항목이 {len(tb.get('unknown_items', []))}개로 1위({len(ta.get('unknown_items', []))}개)보다 많음"
    return f"앞선 기준 동점, 실적 평균 {tb['averages']['실적']:.2f}이 1위({ta['averages']['실적']:.2f})보다 낮음"


def _exclusion_reason(r: dict) -> str:
    """제외·보류 사유: 분기 사유 → 자격 요건 미충족 사유(적격이 아닐 때만) → 판단 사유 순."""
    elig = r.get("eligibility") or {}
    failed = [f"{REQUIREMENTS[g]}: {friendly_reason(elig[g].get('사유', ''))}"
              for g in ("G1", "G2", "G3", "G4") if (elig.get(g) or {}).get("결과") == "미충족"]
    if failed:
        return "투자 요건 미충족: " + "; ".join(failed)
    eligibility_reason = elig.get("사유") if elig.get("판정") != "적격" else None
    return (r.get("route_reason") or eligibility_reason
            or (r.get("decision_details") or {}).get("판단사유") or "사유 미기재")


SOURCE_LABEL = "창업진흥원 「2025 초격차 스타트업 1000+ 프로젝트 디렉토리북」 시스템반도체 분야 수록 기업"


def source_label(state: dict) -> str:
    """파일 경로처럼 보이는 값은 독자가 알아볼 수 있는 자료 이름으로 바꾼다."""
    src = str(state.get("source_document") or "")
    return SOURCE_LABEL if (not src or "/" in src or src.lower().endswith(".pdf")) else src


def friendly_reason(text: str) -> str:
    """내부 처리 사유(G1~G4, 단계 이름 등)를 제3자가 읽을 수 있는 말로 바꾼다."""
    t = text
    for a, b in (("자격 요건 미충족", "투자 요건 미충족"), ("자격 요건 확인 필요", "투자 요건 확인 필요"),
                 ("G1", "비상장 요건"), ("G2", "투자 단계 요건"), ("G3", "M&A(Exit) 요건"), ("G4", "AI 관련 사업 요건"),
                 ("Tavily 검색", "최신 웹 검색"), ("Tavily", "웹 검색"), ("사전 스크리닝", "사전 검토"),
                 ("확인 불가", "확인되지 않음")):
        t = t.replace(a, b)
    for stage, label in (("technology", "기술 분석"), ("market", "시장 분석"), ("competitor", "경쟁사 분석"),
                         ("eligibility", "투자 요건 확인"), ("investment", "종합 평가")):
        t = t.replace(f"오류: {stage}", f"분석 오류({label} 단계)").replace(f"{stage} 단계 오류", f"분석 오류({label} 단계)")
    return t


def scope_sentence(state: dict, records: list[dict]) -> str:
    c = Counter(r.get("decision") for r in records)
    return (
        f"평가 범위: {source_label(state)} {len(records)}개사를 조사 기준일 {state.get('as_of_date', '확인 불가')}에 평가한 결과, "
        f"투자 적격 {c['투자 적격']}곳, 보류 {c['보류']}곳(기준 미달 또는 추가 확인 필요), 제외 {c['제외']}곳(투자 요건 미충족)으로 분류했다."
    )


def candidate_status(state: dict, records: list[dict], by_id: dict[str, dict]) -> str:
    ranking = [i for i in state.get("ranking") or [] if i in by_id]
    parts = [scope_sentence(state, records)]
    if ranking:
        top = by_id[ranking[0]]
        rows = []
        for n, cid in enumerate(ranking, 1):
            r = by_id[cid]
            note = "투자 추천" if n == 1 else not_selected_reason(top, r)
            rows.append([n, r["current_company"]["기업명"], r["scorecard"]["total"],
                         f"{r['scorecard']['averages']['제품/기술력']:.2f}", len(r["scorecard"].get("unknown_items", [])), note])
        rows = rows[:3]  # 2·3위까지 (보고서 분량)
        parts.append(md_table(["순위", "기업", "종합 점수", "기술력 평균", "자료 없는 항목", "비고 / 미선정 사유"], rows))
        if len(ranking) > 3:
            parts.append(f"그 외 투자 적격 {len(ranking) - 3}곳은 분량상 생략했다.")
        if len(ranking) >= 2:
            first = by_id[ranking[0]]["scorecard"].get("repeat") or {}
            second = by_id[ranking[1]]["scorecard"].get("repeat") or {}
            if first and second and max(first["total_min"], second["total_min"]) <= min(first["total_max"], second["total_max"]):
                parts.append("상위 후보의 반복 채점 점수 범위가 겹친다. 최종 순위는 항목별 중앙값으로 정했으며, "
                             "작은 점수 차이를 확정적인 경쟁력 격차로 해석하기 어렵다. 투자 전 추가 근거 확인이 필요하다.")
    others = [r for r in records if r.get("decision") in ("제외", "보류")]
    reasons = Counter(_clip(friendly_reason(_exclusion_reason(r)), 40) for r in others)
    if reasons:
        parts.append("제외·보류 주요 사유: " + "; ".join(f"{k} ({v}곳)" for k, v in reasons.most_common(5)))
    return "\n\n".join(parts)


# ── 3.2 평가 결과 ──────────────────────────────────────────────────────────

REQUIREMENTS = {"G1": "비상장 기업", "G2": "투자 단계 Series C 이하", "G3": "M&A 등 Exit 미완료", "G4": "AI 관련 사업"}


def eligibility_table(elig: dict) -> str:
    rows = [[name, elig.get(g, {}).get("결과", "확인불가"), _clip(friendly_reason(elig.get(g, {}).get("사유", "")), 56), cite(elig.get(g, {}).get("근거ID", []))]
            for g, name in REQUIREMENTS.items()]
    return md_table(["투자 요건", "결과", "확인 내용", "출처"], rows)


def checklist_table(checklist: dict) -> tuple[str, list[str]]:
    rows, warn = [], []
    for q, text in QUESTIONS.items():
        c = checklist.get(q, {"판정": "확인불가", "근거": "", "근거ID": []})
        mark = " ⚠주의" if c["판정"] in ("NO", "확인불가") else ""
        if mark:
            warn.append(f"{text} ({c['판정']})")
        rows.append([q.replace("Q", ""), text, c["판정"] + mark, (_clip(c.get("근거", ""), 40) + " " + cite(c.get("근거ID", []))).strip()])
    return md_table(["#", "점검 질문", "판정", "확인 내용"], rows), warn


def decision_sentence(rec: dict) -> str:
    sc = rec["scorecard"]
    tech = sc["averages"]["제품/기술력"]
    met = all(rec["eligibility"].get(g, {}).get("결과") == "충족" for g in REQUIREMENTS)
    return (f"**종합 판단: {rec['decision']}.** 종합 점수 {sc['total']}점(투자 적격 기준 {INVEST_THRESHOLD}점 이상), "
            f"기술력 평균 {tech:.2f}(기준 {TECH_MIN} 이상), 4대 투자 요건 {'모두 충족' if met else '중 일부 미충족'}.")


# ── 3.4 한계점 ─────────────────────────────────────────────────────────────

def _repeat_note(rec: dict | None) -> str:
    rp = ((rec or {}).get("scorecard") or {}).get("repeat")
    if not rp or rp.get("runs", 1) < 2:
        return "채점에는 다소 편차가 있을 수 있다. "
    return (f"채점 편차를 줄이려고 같은 자료를 {rp['runs']}회 반복 채점해 항목별 중앙값을 썼다"
            f"(회차별 종합 점수 {rp['total_min']}~{rp['total_max']}점). ")


def limitations(state: dict, rec: dict | None, checklist_warn: list[str]) -> str:
    items = [
        f"자료 시점: 기업 정보는 2025년 디렉토리북(기업이 제출한 홍보성 자료)에 기반하며, 상장·투자 단계·M&A 여부는 조사 기준일 "
        f"{state.get('as_of_date', '확인 불가')}의 외부 자료로 따로 확인했다.",
        "시장 통계 자료의 그래프 속 수치와 이미지로만 된 일부 페이지는 분석에 반영되지 않았을 수 있다.",
        f"평가 방법: 항목별 점수는 AI가 공개 자료를 읽고 채점했고, 종합 점수와 최종 판단은 정해진 공식으로 계산했다. "
        f"{_repeat_note(rec)}투자 적격 기준 {INVEST_THRESHOLD}점은 전 항목 평균 3.5점(5점 만점)에 해당한다.",
    ]
    if rec:
        sc = rec["scorecard"]
        unk = sc.get("unknown_items", [])
        items.append("공개 자료에서 확인되지 않아 2점(낮은 점수)을 준 항목: " + (", ".join(ITEM_NAMES[i] for i in unk) if unk else "없음"))
        if checklist_warn:
            items.append("핵심 점검 문항 중 주의가 필요한 항목: " + "; ".join(checklist_warn))
        topics = {
            "technology_analysis": "기술: 제품 성숙도·실측 성능·측정조건·동일 조건의 업계 기준 대조를 추가 확인해야 한다.",
            "market_analysis": "시장: 직접 목표 시장의 규모·성장률·기준연도·산정 기간을 추가 확인해야 한다.",
        }
        for key, summary in topics.items():
            gaps = (rec.get(key) or {}).get("미확인정보") or []
            if gaps:
                items.append(f"{summary} 미확인·검증 미완료 항목 {len(gaps)}건이며, 세부 내역은 별첨 평가 기록에 보존했다.")
    errs = state.get("errors") or []
    if errs:
        names = {r["company_id"]: r["current_company"]["기업명"] for r in state.get("evaluation_results") or [] if r.get("current_company")}
        who = ", ".join(sorted({names.get(e.get("company_id"), e.get("company_id", "?")) for e in errs}))
        items.append(f"{who}은(는) 분석 도중 기술적 오류로 평가를 마치지 못해 보류로 분류했다.")
    return "\n".join(f"- {t}" for t in items)
