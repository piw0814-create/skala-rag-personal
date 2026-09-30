"""보고서 그래프 — 외부 패키지 없이 인라인 SVG 문자열로 그린다 (PDF 변환 시 HTML에 삽입)."""

import html
import math
import re

from agents._report_highlights import first_number, plain
from config import INVEST_THRESHOLD

# 색: 의미별 고정 (좋음=초록, 보통=주황, 나쁨=빨강, 확인불가=회색, 기준선=남색)
GOOD, MID, BAD, UNK, NAVY, ACCENT = "#0f7b4b", "#d98a1c", "#c0392b", "#8a94a6", "#12284c", "#1f5fbf"
GRID = "#d5dae3"

ITEM_NAMES = {
    "A1": "기술 전문성", "A2": "팀 완성도", "A3": "분야 몰입도", "B1": "시장 규모·성장성", "B2": "고객 가치",
    "B3": "글로벌 확장성", "C1": "문제 해결력", "C2": "개발 성숙도", "C3": "핵심 기술 지표",
    "D1": "지식재산", "D2": "경쟁사 대비 차별성", "D3": "외부 검증", "E1": "고객 확보", "E2": "매출", "E3": "투자 유치",
}
JUDGE_COLOR = {"YES": GOOD, "PARTIAL": MID, "NO": BAD, "확인불가": UNK}


def _svg(w: int, h: int, body: str, defs: str = "") -> str:
    return (f'<svg class="chart" viewBox="0 0 {w} {h}" width="100%" xmlns="http://www.w3.org/2000/svg" role="img">'
            f"<defs>{defs}</defs>{body}</svg>")


def _t(x, y, text, size=9, anchor="start", fill="#1b2230", weight="400", extra="") -> str:
    return (f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" text-anchor="{anchor}" fill="{fill}" '
            f'font-weight="{weight}" {extra}>{html.escape(str(text))}</text>')


def score_color(score: float) -> str:
    return GOOD if score >= 4 else MID if score >= 3 else BAD


def _hatch() -> str:
    return (f'<pattern id="hatch" width="4" height="4" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
            f'<rect width="4" height="4" fill="#eef0f5"/><line x1="0" y1="0" x2="0" y2="4" stroke="{UNK}" stroke-width="1.6"/></pattern>')


# ── 레이더 (대분류 평균 vs 기준 3.5) ────────────────────────────────────────

def radar(averages: dict[str, float], ref: float = INVEST_THRESHOLD / 100 * 5) -> str:
    cats = list(averages)
    n, cx, cy, R = len(cats), 150, 122, 78
    ang = lambda i: -math.pi / 2 + 2 * math.pi * i / n  # noqa: E731
    pt = lambda i, v: (cx + R * v / 5 * math.cos(ang(i)), cy + R * v / 5 * math.sin(ang(i)))  # noqa: E731
    poly = lambda vals: " ".join(f"{pt(i, v)[0]:.1f},{pt(i, v)[1]:.1f}" for i, v in enumerate(vals))  # noqa: E731
    body = ""
    for r in (1, 2, 3, 4, 5):
        body += f'<polygon points="{poly([r] * n)}" fill="none" stroke="{GRID}" stroke-width="{1 if r < 5 else 1.4}"/>'
    for i in range(n):
        x, y = pt(i, 5)
        body += f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="{GRID}"/>'
    body += f'<polygon points="{poly([ref] * n)}" fill="none" stroke="{UNK}" stroke-width="1.4" stroke-dasharray="4 3"/>'
    vals = [averages[c] for c in cats]
    body += f'<polygon points="{poly(vals)}" fill="{ACCENT}" fill-opacity=".22" stroke="{ACCENT}" stroke-width="2"/>'
    for i, v in enumerate(vals):
        x, y = pt(i, v)
        body += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{score_color(v)}" stroke="#fff" stroke-width="1"/>'
        lx, ly = pt(i, 5.95)
        anchor = "middle" if abs(lx - cx) < 8 else ("start" if lx > cx else "end")
        body += _t(lx, ly - 2, cats[i], 9, anchor, NAVY, "700") + _t(lx, ly + 9, f"{v:.2f}", 9, anchor, score_color(v), "800")
    body += _t(cx + R + 4, cy - 3, "5", 7, "start", UNK) + _t(300, 16, "분야별 평균 (5점 만점)", 8.5, "end", NAVY, "700")
    body += f'<line x1="0" y1="12" x2="16" y2="12" stroke="{UNK}" stroke-width="1.4" stroke-dasharray="4 3"/>' \
            + _t(20, 15, f"투자 적격 기준선 (평균 {ref:.1f})", 7.5, "start", UNK)
    return _svg(300, 218, body)


# ── 15개 항목 막대 ─────────────────────────────────────────────────────────

def item_bars(items: dict[str, dict], unknown: list[str]) -> str:
    row, lw, bw, top = 12.4, 96, 168, 16
    h = top + row * len(ITEM_NAMES) + 16
    body = _t(0, 10, "세부 항목 점수 (1~5)", 8.5, "start", NAVY, "700")
    for k in (1, 2, 3, 4, 5):
        x = lw + bw * k / 5
        body += f'<line x1="{x:.1f}" y1="{top - 2}" x2="{x:.1f}" y2="{top + row * len(ITEM_NAMES) - 2}" stroke="{GRID}" stroke-width=".7"/>'
    for n, (i, name) in enumerate(ITEM_NAMES.items()):
        y = top + n * row
        s = items.get(i, {}).get("점수", 2)
        unk = i in unknown
        w = bw * s / 5
        fill = "url(#hatch)" if unk else score_color(s)
        body += _t(0, y + 8.6, f"{i} {name}", 8, "start", "#1b2230")
        body += f'<rect x="{lw}" y="{y + 1}" width="{w:.1f}" height="8.6" rx="1.6" fill="{fill}" {"stroke=" + chr(34) + UNK + chr(34) if unk else ""}/>'
        body += _t(lw + w + 3, y + 8.6, f"{s}{'*' if unk else ''}", 8, "start", UNK if unk else score_color(s), "800")
    ly = h - 4
    for x, c, label in ((lw, GOOD, "4~5 우수"), (lw + 52, MID, "3 보통"), (lw + 96, BAD, "1~2 미흡")):
        body += f'<rect x="{x}" y="{ly - 7}" width="8" height="8" rx="1.5" fill="{c}"/>' + _t(x + 11, ly, label, 7.5, "start", "#5b6577")
    body += f'<rect x="{lw + 146}" y="{ly - 7}" width="8" height="8" rx="1.5" fill="url(#hatch)" stroke="{UNK}"/>' + _t(lw + 157, ly, "* 자료 없음(2점 부여)", 7.5, "start", "#5b6577")
    return _svg(300, int(h), body, _hatch())


# ── 투자 유치 이력 ─────────────────────────────────────────────────────────

def _eok(thousand_won: float) -> float:
    return thousand_won / 100_000  # 천원 → 억원


def _fmt_eok(v: float) -> str:
    return f"{v:,.0f}억" if v >= 10 else f"{v:,.1f}억"


def _group_rounds(rounds: list[dict]) -> list[dict]:
    """같은 (일자, 단계, 확정) 라운드는 투자자별로 쪼개져 기록되므로 금액을 합쳐 한 막대로 만든다."""
    groups: dict[tuple, dict] = {}
    for r in sorted(rounds, key=lambda h: (str(h.get("일자") or ""), str(h.get("단계") or ""))):
        key = (r.get("일자"), r.get("단계"), bool(r.get("확정", True)))
        g = groups.setdefault(key, {"일자": r.get("일자"), "단계": r.get("단계"), "확정": key[2], "금액": None, "건수": 0})
        g["건수"] += 1
        if r.get("금액"):
            g["금액"] = (g["금액"] or 0) + r["금액"]
    return list(groups.values())


def _nice_step(raw: float) -> float:
    """눈금 간격을 1·2·5 × 10^k로 맞춘다."""
    exp = math.floor(math.log10(raw)) if raw > 0 else 0
    for m in (1, 2, 5, 10):
        if raw <= m * 10 ** exp:
            return m * 10 ** exp
    return 10 ** (exp + 1)


def funding_chart(history: list[dict], compact: bool = False) -> str:
    """투자 유치 이력 그래프. 이력이 없으면 빈 문자열(섹션 자체를 그리지 않는다)."""
    rounds = [h for h in history or [] if h]
    if not rounds:
        return ""
    rounds = _group_rounds(rounds)
    w, left, right = 520, 34, 14
    h_, base, top = (128, 88, 26) if compact else (172, 128, 34)
    n = len(rounds)
    slot = (w - left - right) / n
    amounts = [_eok(r["금액"]) if r.get("금액") else None for r in rounds]
    cum, run = [], 0.0
    for a, r in zip(amounts, rounds):
        if r.get("확정", True) and a:
            run += a
        cum.append(run)
    step = _nice_step(max([a for a in amounts if a] + [run, 0.1]) * 1.1 / 3)
    vmax = step * 3
    y = lambda v: base - (base - top) * v / vmax  # noqa: E731
    body = _t(0, 11, "투자 유치 이력 (막대=라운드별 금액, 선=확정 투자 누적, 억 원 · 같은 라운드는 합산)", 8.5, "start", NAVY, "700")
    for k in range(0, 4):
        gy = base - (base - top) * k / 3
        body += f'<line x1="{left}" y1="{gy:.1f}" x2="{w - right}" y2="{gy:.1f}" stroke="{GRID}" stroke-width=".7"/>'
        body += _t(left - 4, gy + 3, "0" if k == 0 else _fmt_eok(step * k), 7, "end", UNK)
    bw = min(46, slot * 0.5)
    pts = []
    for i, (r, a) in enumerate(zip(rounds, amounts)):
        cx = left + slot * (i + 0.5)
        confirmed = r.get("확정", True)
        if a:
            yy = y(a)
            style = (f'fill="{ACCENT}"' if confirmed else f'fill="#fff" stroke="{ACCENT}" stroke-width="1.6" stroke-dasharray="4 2"')
            body += f'<rect x="{cx - bw / 2:.1f}" y="{yy:.1f}" width="{bw:.1f}" height="{base - yy:.1f}" rx="2" {style}/>'
            inside = confirmed and (base - yy) > 16
            body += _t(cx, yy + 11 if inside else yy - 4, _fmt_eok(a), 8.5, "middle", "#fff" if inside else (NAVY if confirmed else UNK), "800")
        else:
            body += _t(cx, base - 6, "금액 미기재", 7.5, "middle", UNK)
        stage = str(r.get("단계") or "단계 미상")
        body += _t(cx, base + 12, stage, 8.5, "middle", "#1b2230", "700")
        note = str(r.get("일자") or "일자 미상") + (f" · {r['건수']}건" if r.get("건수", 1) > 1 else "") + ("" if confirmed else " · 협의 중")
        body += _t(cx, base + 23, note, 7.5, "middle", UNK if confirmed else BAD)
        if confirmed:
            pts.append((cx, y(cum[i]), cum[i]))
    if len(pts) >= 1:
        body += f'<polyline points="{" ".join(f"{x:.1f},{yy:.1f}" for x, yy, _ in pts)}" fill="none" stroke="{MID}" stroke-width="2"/>'
        for x, yy, v in pts:
            body += f'<circle cx="{x:.1f}" cy="{yy:.1f}" r="3.2" fill="{MID}" stroke="#fff"/>'
        x, yy, v = pts[-1]
        body += _t(x - 8 if x > w / 2 else x + 8, yy - 7, f"누적 {_fmt_eok(v)}", 8, "end" if x > w / 2 else "start", MID, "800")
    body += f'<rect x="{left}" y="{h_ - 9}" width="8" height="8" fill="{ACCENT}"/>' + _t(left + 11, h_ - 2, "확정", 7.5, "start", "#5b6577")
    body += f'<rect x="{left + 42}" y="{h_ - 9}" width="8" height="8" fill="#fff" stroke="{ACCENT}" stroke-dasharray="2 1"/>' + _t(left + 53, h_ - 2, "협의 중(미확정, 점수 제외)", 7.5, "start", "#5b6577")
    return _svg(w, h_, body)


# ── 투자 대상 없음: 후보 총점 비교 ─────────────────────────

def candidates_bar(rows: list[dict], threshold: float = INVEST_THRESHOLD) -> str:
    """rows: [{name, total, tech_gap}] — 총점 막대와 70점 기준선."""
    row, lw, bw, top = 17, 118, 300, 24
    h = top + row * len(rows) + 22
    tx = lw + bw * threshold / 100
    body = _t(0, 11, "후보별 총점과 투자 적격 기준", 8.5, "start", NAVY, "700")
    for k in (0, 25, 50, 75, 100):
        x = lw + bw * k / 100
        body += f'<line x1="{x:.1f}" y1="{top - 4}" x2="{x:.1f}" y2="{top + row * len(rows) - 2}" stroke="{GRID}" stroke-width=".7"/>' + _t(x, top + row * len(rows) + 8, k, 7, "middle", UNK)
    for n, r in enumerate(rows):
        y = top + n * row
        w = bw * r["total"] / 100
        gap = threshold - r["total"]
        body += _t(lw - 6, y + 10, str(r["name"])[:13], 8.5, "end", "#1b2230")
        body += f'<rect x="{lw}" y="{y}" width="{w:.1f}" height="11" rx="2" fill="{MID}"/>'
        note = f"{r['total']:.1f}" + (f" (−{gap:.1f})" if gap > 0 else "")
        body += _t(lw + w + 4, y + 9.5, note, 8.5, "start", NAVY, "800")
        if r.get("tech_gap"):
            body += f'<circle cx="{lw + bw + 24}" cy="{y + 5.5}" r="3" fill="{BAD}"/>'
    body += f'<line x1="{tx:.1f}" y1="{top - 8}" x2="{tx:.1f}" y2="{top + row * len(rows) - 2}" stroke="{NAVY}" stroke-width="2" stroke-dasharray="4 3"/>'
    body += _t(tx, top - 11, f"기준 {threshold:.0f}", 8, "middle", NAVY, "800")
    body += f'<circle cx="{lw + bw + 24}" cy="{h - 6}" r="3" fill="{BAD}"/>' + _t(lw + bw + 31, h - 3, "기술력 기준 미달", 7.5, "start", "#5b6577")
    return _svg(520, int(h), body)


# ═══ 선정 근거 시각화 (강점 영역별) ═══════════════════════════════════════════

TEAL, INK = "#0f8b8d", "#1b2230"
RESULT_COLOR = {"상회": GOOD, "동등": ACCENT, "하회": BAD, "비교불가": UNK}


def _years(items: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for it in items:
        m = re.search(r"(19|20)\d{2}", str(it.get("일자") or ""))
        key = m.group(0) if m else "연도 미상"
        out[key] = out.get(key, 0) + 1
    return out


def ip_chart(company: dict) -> str:
    """지식재산권 등록·출원 현황: 왼쪽 건수 타일, 오른쪽 연도별 누적 막대."""
    ip = company.get("지식재산권") or {}
    reg, app = ip.get("등록") or [], ip.get("출원") or []
    if not reg and not app:
        return ""
    yr_r, yr_a = _years(reg), _years(app)
    years = sorted(set(yr_r) | set(yr_a))
    w, h = 520, 118
    body = _t(0, 11, "지식재산권 현황 (특허 등록·출원)", 8.5, "start", NAVY, "700")
    for x, n, label, fill, tc in ((0, len(reg), "등록 특허", NAVY, "#fff"), (100, len(app), "출원 특허", "#cfe0f7", NAVY)):
        body += f'<rect x="{x}" y="22" width="92" height="70" rx="4" fill="{fill}"/>'
        body += _t(x + 46, 60, n, 26, "middle", tc, "800") + _t(x + 46, 79, f"{label} (건)", 8.5, "middle", tc, "700")
    left, base, top = 228, 92, 30
    vmax = max([yr_r.get(y, 0) + yr_a.get(y, 0) for y in years] + [1])
    slot = (w - left - 10) / max(len(years), 1)
    bw = min(40, slot * 0.55)
    body += f'<line x1="{left - 6}" y1="{base}" x2="{w - 6}" y2="{base}" stroke="{GRID}"/>'
    for n, y in enumerate(years):
        cx = left + slot * (n + 0.5)
        r, a = yr_r.get(y, 0), yr_a.get(y, 0)
        hr, ha = (base - top) * r / vmax, (base - top) * a / vmax
        body += f'<rect x="{cx - bw / 2:.1f}" y="{base - hr:.1f}" width="{bw:.1f}" height="{hr:.1f}" fill="{NAVY}"/>'
        body += f'<rect x="{cx - bw / 2:.1f}" y="{base - hr - ha:.1f}" width="{bw:.1f}" height="{ha:.1f}" fill="#cfe0f7" stroke="{ACCENT}" stroke-width=".6"/>'
        body += _t(cx, base - hr - ha - 4, r + a, 8.5, "middle", NAVY, "800") + _t(cx, base + 12, y, 8, "middle", "#5b6577")
    body += f'<rect x="{left}" y="{h - 9}" width="8" height="8" fill="{NAVY}"/>' + _t(left + 11, h - 2, "등록", 7.5, "start", "#5b6577")
    body += f'<rect x="{left + 40}" y="{h - 9}" width="8" height="8" fill="#cfe0f7" stroke="{ACCENT}" stroke-width=".6"/>' + _t(left + 51, h - 2, "출원(심사 중)", 7.5, "start", "#5b6577")
    return _svg(w, h, body)


def tech_chart(tech: dict) -> str:
    """기술 성숙도(TRL 1~9)와 핵심 성능 지표의 업계 기준 대비 결과."""
    if not tech:
        return ""
    mat = tech.get("제품성숙도") or {}
    trl, stage = mat.get("TRL"), mat.get("단계") or "단계 미기재"
    bench = (tech.get("기준대조") or [])[:3]
    metrics = {m.get("지표명"): m for m in tech.get("성능지표") or []}
    rows = max(len(bench), 1)
    w, h = 520, max(96, 40 + rows * 30)
    body = _t(0, 11, "기술 성숙도 (TRL 1~9단계)", 8.5, "start", NAVY, "700")
    for k in range(1, 10):
        x = 0 + (k - 1) * 24
        on = trl and k <= trl
        body += f'<rect x="{x}" y="22" width="21" height="16" rx="2.5" fill="{ACCENT if on else "#e6eaf2"}"/>' + _t(x + 10.5, 34, k, 8, "middle", "#fff" if on else UNK, "700")
    body += _t(0, 56, f"현재 {('TRL ' + str(trl)) if trl else 'TRL 미기재'} · {stage}", 8.5, "start", INK, "700")
    body += _t(0, 70, "1~3 연구 · 4~6 시제품·검증 · 7~9 양산·상용화", 7.2, "start", UNK)
    x0 = 246
    body += _t(x0, 11, "핵심 성능 지표 vs 업계 기준", 8.5, "start", NAVY, "700")
    if not bench:
        body += _t(x0, 40, "업계 기준과 대조할 수 있는 지표가 확인되지 않았다", 8, "start", UNK)
    for n, b in enumerate(bench):
        y = 22 + n * 30
        name = b.get("지표명") or "지표"
        mine = metrics.get(name, {}).get("값")
        ref = b.get("업계기준")
        res = b.get("비교결과", "비교불가")
        body += _t(x0, y + 9, plain(name)[:14], 8.5, "start", INK, "700")
        a, c = first_number(mine), first_number(ref)
        if a and c:
            m = max(a, c)
            body += f'<rect x="{x0 + 66}" y="{y}" width="{88 * a / m:.1f}" height="8" rx="1.5" fill="{ACCENT}"/>' + _t(x0 + 70 + 88 * a / m, y + 7.5, plain(mine)[:14], 7.5, "start", ACCENT, "800")
            body += f'<rect x="{x0 + 66}" y="{y + 11}" width="{88 * c / m:.1f}" height="8" rx="1.5" fill="#b8bfcc"/>' + _t(x0 + 70 + 88 * c / m, y + 18.5, plain(ref)[:14] + " (기준)", 7.5, "start", "#5b6577")
        else:
            body += _t(x0 + 66, y + 8, f"기업 {plain(mine or '미공개')[:16]}", 7.8, "start", INK) + _t(x0 + 66, y + 19, f"기준 {plain(ref or '-')[:18]}", 7.8, "start", "#5b6577")
        body += f'<rect x="{w - 46}" y="{y + 2}" width="44" height="16" rx="8" fill="{RESULT_COLOR.get(res, UNK)}"/>' + _t(w - 24, y + 13.5, res, 8, "middle", "#fff", "800")
    return _svg(w, h, body)


def revenue_chart(company: dict) -> str:
    """연도별 매출(국내·해외, 억 원). 공개되지 않았거나 0이면 빈 문자열."""
    s = company.get("매출액") or {}
    if s.get("상태") != "공개":
        return ""
    hist = [e for e in (s.get("이력") or []) if e.get("연도")] or [s]
    if any(e.get("해외") and e.get("해외단위", s.get("해외단위")) not in (None, "천원", "KRW_THOUSAND") for e in hist):
        return _svg(520, 34, _t(0, 20, "해외 매출 통화가 달라 검증된 환산 근거 확인 필요", 10, "start", UNK))
    rows = []
    for e in sorted(hist, key=lambda e: e.get("연도") or 0):
        dom, ovs = (e.get("국내") or 0) / 100_000, (e.get("해외") or 0) / 100_000
        rows.append((str(e.get("연도")), dom, ovs))
    if not any(d + o > 0 for _, d, o in rows):
        return ""
    w, h, left, base, top = 520, 128, 34, 92, 26
    n = len(rows)
    slot = (w - left - 14) / n
    step = _nice_step(max(d + o for _, d, o in rows) * 1.15 / 3)
    vmax = step * 3
    y = lambda v: base - (base - top) * v / vmax  # noqa: E731
    body = _t(0, 11, "매출 추이 (억 원, 국내·해외 합산)", 8.5, "start", NAVY, "700")
    for k in range(4):
        gy = base - (base - top) * k / 3
        body += f'<line x1="{left}" y1="{gy:.1f}" x2="{w - 10}" y2="{gy:.1f}" stroke="{GRID}" stroke-width=".7"/>' + _t(left - 4, gy + 3, "0" if k == 0 else _fmt_eok(step * k).replace("억", ""), 7, "end", UNK)
    bw = min(46, slot * 0.5)
    for i, (yr, dom, ovs) in enumerate(rows):
        cx = left + slot * (i + 0.5)
        yd, yo = y(dom), y(dom + ovs)
        body += f'<rect x="{cx - bw / 2:.1f}" y="{yd:.1f}" width="{bw:.1f}" height="{base - yd:.1f}" fill="{ACCENT}"/>'
        if ovs:
            body += f'<rect x="{cx - bw / 2:.1f}" y="{yo:.1f}" width="{bw:.1f}" height="{yd - yo:.1f}" fill="{TEAL}"/>'
        body += _t(cx, yo - 4, _fmt_eok(dom + ovs), 8.5, "middle", NAVY, "800") + _t(cx, base + 12, f"{yr}년", 8.5, "middle", INK, "700")
    body += f'<rect x="{left}" y="{h - 10}" width="8" height="8" fill="{ACCENT}"/>' + _t(left + 11, h - 3, "국내", 7.5, "start", "#5b6577")
    body += f'<rect x="{left + 40}" y="{h - 10}" width="8" height="8" fill="{TEAL}"/>' + _t(left + 51, h - 3, "해외", 7.5, "start", "#5b6577")
    return _svg(w, h, body)


# ── HTML 카드류 ────────────────────────────────────────────────────────────

def market_tiles(mkt: dict) -> str:
    size, growth = (mkt or {}).get("시장규모") or {}, (mkt or {}).get("성장률") or {}
    tiles = []
    if size.get("값"):
        tiles.append(("시장 규모", plain(size["값"]), f"기준 {size['기준연도']}년" if size.get("기준연도") else ""))
    if growth.get("값"):
        tiles.append(("성장률", plain(growth["값"]), f"{growth['기간']}" if growth.get("기간") else ""))
    for c in ((mkt or {}).get("목표고객") or [])[:1]:
        tiles.append(("목표 고객", plain(c)[:22], ""))
    if not tiles:
        return ""
    return '<div class="tiles">' + "".join(
        f'<div class="tile"><div class="tl">{html.escape(a)}</div><div class="tv">{html.escape(str(b))}</div><div class="ts">{html.escape(c)}</div></div>'
        for a, b, c in tiles) + "</div>"


def strength_cards(strengths: list) -> str:
    """SUMMARY 바로 아래: 선정 핵심 근거 카드 (영역, 평균 점수 막대, 한 줄 사실)."""
    if not strengths:
        return ""
    cards = ""
    for n, s in enumerate(strengths, 1):
        color = score_color(s.avg)
        cards += (f'<div class="scard"><div class="sh"><span class="sn">{n}</span>{html.escape(s.area)}</div>'
                  f'<div class="ss"><b style="color:{color}">{s.avg:.1f}</b><span>/5</span><i class="sbar"><u style="width:{s.avg / 5 * 100:.0f}%;background:{color}"></u></i></div>'
                  f'<div class="sf">{html.escape(s.fact)}</div></div>')
    return f'<div class="slabel">선정 핵심 근거</div><div class="scards">{cards}</div>'


def strength_weak_callouts(strong: list[tuple], weak: list[tuple]) -> str:
    """강점(4점 이상)과 보완할 점(2점 이하·자료 없음) — 항목명, 점수, 한 줄 이유."""
    def li(i, v, unk=False):
        reason = "공개 자료에서 확인되지 않음" if unk else plain(v.get("채점이유") or "")[:70]
        return (f'<li><b>{html.escape(ITEM_NAMES[i])}</b> <span class="pt {"pu" if unk else ""}">{"자료 없음" if unk else str(v["점수"]) + "점"}</span> '
                f'<span class="why">{html.escape(reason)}</span></li>')
    good = "".join(li(i, v) for i, v in strong) or "<li>4점 이상 항목 없음</li>"
    bad = "".join(li(i, v, u) for i, v, u in weak) or "<li>2점 이하 항목 없음</li>"
    return (f'<div class="callouts"><div class="co good"><div class="cot">핵심 강점</div><ul>{good}</ul></div>'
            f'<div class="co bad"><div class="cot">보완할 점</div><ul>{bad}</ul></div></div>')


# ── 경쟁 구도 ──────────────────────────────────────────────────────────────

def _numeric_rows(comp: dict) -> list[dict]:
    """비교표에서 기업과 경쟁사 값이 모두 숫자인 지표만 뽑는다. [{metric, target, others:[(이름, 값)]}]"""
    rows = []
    for r in comp.get("비교표") or []:
        target = first_number(r.get("대상기업"))
        others = [(k, v) for k, v in r.items() if k not in ("지표", "대상기업") and first_number(v) is not None]
        if r.get("지표") and target is not None and others:
            rows.append({"metric": plain(r["지표"]), "target": r["대상기업"], "others": others[:3]})
    return rows[:3]


def _compare_svg(rows: list[dict], company_name: str) -> str:
    row_h, lw, bw = 13, 96, 220
    h = 18 + sum(len(r["others"]) * row_h + 14 for r in rows)
    body = _t(0, 10, "핵심 지표 비교 (막대가 길수록 값이 큼, 측정 조건이 다를 수 있음)", 8.5, "start", NAVY, "700")
    y = 20
    for r in rows:
        vals = [first_number(r["target"])] + [first_number(v) for _, v in r["others"]]
        m = max(v for v in vals if v) or 1
        body += _t(0, y + 8, r["metric"][:14], 8.5, "start", INK, "700")
        for name, val, color in [(company_name, r["target"], ACCENT)] + [(k, v, "#b8bfcc") for k, v in r["others"]]:
            w = bw * (first_number(val) or 0) / m
            body += _t(lw - 4, y + 8, str(name)[:12], 7.8, "end", "#5b6577") + f'<rect x="{lw}" y="{y}" width="{max(w, 1):.1f}" height="9" rx="1.5" fill="{color}"/>'
            body += _t(lw + w + 4, y + 8, plain(val)[:16], 7.8, "start", ACCENT if color == ACCENT else "#5b6577", "800")
            y += row_h
        y += 14
    return _svg(420, int(h), body)


def competitor_view(comp: dict, company_name: str) -> str:
    """경쟁 구도 시각화: 경쟁 제품 카드 + (수치 비교가 가능하면) 지표 막대 + 우위/열위 + 비교 한계."""
    if not comp:
        return ""
    products = (comp.get("경쟁제품") or [])[:4]
    strengths, weaknesses = (comp.get("우위") or [])[:4], (comp.get("열위") or [])[:4]
    rows = _numeric_rows(comp)
    if not (products or strengths or weaknesses or rows):
        return ""
    out = ""
    if products:
        cards = ""
        for p in products:
            metrics = p.get("핵심지표값") or {}
            if isinstance(metrics, list):  # 스키마 변형(이름/값 목록) 대응
                metrics = {m.get("이름"): m.get("값") for m in metrics}
            tags = "".join(f'<span class="mtag">{html.escape(plain(k))} <b>{html.escape(plain(v)[:14])}</b></span>' for k, v in list(metrics.items())[:2])
            cards += (f'<div class="pcard"><div class="pn">{html.escape(plain(p.get("기업명")))}</div>'
                      f'<div class="pp">{html.escape(plain(p.get("제품")))}</div>{tags}</div>')
        out += f'<div class="slabel">주요 경쟁 제품</div><div class="pcards">{cards}</div>'
    if rows:
        out += f'<div class="chartbox">{_compare_svg(rows, company_name)}</div>'
    if strengths or weaknesses:
        li = lambda xs, empty: "".join(f"<li>{html.escape(plain(x))}</li>" for x in xs) or f"<li>{empty}</li>"  # noqa: E731
        out += (f'<div class="callouts"><div class="co good"><div class="cot">경쟁 대비 우위</div><ul>{li(strengths, "확인된 우위 없음")}</ul></div>'
                f'<div class="co bad"><div class="cot">경쟁 대비 열위</div><ul>{li(weaknesses, "확인된 열위 없음")}</ul></div></div>')
    note = ("공개 지표를 비교했으며 서로 다른 측정조건에서는 우열을 단정할 수 없음" if rows
            else "동일 지표·측정조건을 확인하지 못해 정량 비교 불가")
    out += f'<div class="cnote">비교 조건·한계: {note}</div>'
    return f'<div class="compview">{out}</div>'
