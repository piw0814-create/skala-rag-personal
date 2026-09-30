"""보고서 markdown → 증권사 리서치 스타일 PDF (HTML+CSS → Chrome headless). 추가 패키지 없이 동작한다."""

import html
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf

from agents import _report_charts as charts
from agents._report_render import ID_RE, REQUIREMENTS, latest_round
from config import WEIGHTS

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome", "chromium", "chrome",
]
DISCLAIMER = "본 보고서는 공개 자료와 AI 분석에 기반해 자동 생성되었으며 투자 권유가 아닙니다. 최종 투자 판단과 책임은 투자자에게 있습니다."

# ── markdown → HTML (이 보고서가 쓰는 문법만: 제목·표·불릿·번호·굵게·코드·주석) ──────────

_REFS: dict[str, int] = {}  # 근거 ID → REFERENCE 번호 (render_html이 설정). 비어 있으면 ID 태그로 표시


def _parse_refs(reference_md: str) -> dict[str, int]:
    """'1. 출처 — [ID, ID]' 줄에서 ID → 번호 표를 만든다."""
    refs: dict[str, int] = {}
    for m in re.finditer(r"(?m)^(\d+)\. .*— \[([^\]]*)\]\s*$", reference_md):
        for i in m.group(2).split(","):
            refs[i.strip()] = int(m.group(1))
    return refs


def _inline(text: str) -> str:
    t = html.escape(text, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    # [TEC-C07-01, MKT-C07-01] → 작은 인용 태그
    def cites(m: re.Match) -> str:
        ids = [i.strip() for i in m.group(1).split(",")]
        if _REFS:  # 제3자용: 내부 ID 대신 REFERENCE 번호(각주)로 표시
            nums = sorted({_REFS[i] for i in ids if i in _REFS})
            return f'<sup class="ref">{",".join(map(str, nums))}</sup>' if nums else ""
        return "".join(f'<span class="cite">{i}</span>' for i in ids)
    return re.sub(r"\[((?:(?:DIR|ELG|TEC|MKT|CMP)-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+(?:,\s*)?)+)\]", cites, t)


JUDGE = {"YES": "yes", "PARTIAL": "partial", "NO": "no", "확인불가": "unk", "충족": "yes"}
CHART_RE = re.compile(r"^<!--chart:([a-z_]+)(?::([^\s>]*))?-->\s*$", re.M)


def _cell(text: str) -> tuple[str, str]:
    """표 셀 → (HTML, 판정 클래스). YES/NO 등 판정 값은 색 배지로 바꾼다."""
    token = text.replace("⚠주의", "").strip()
    if token in JUDGE:
        cls = JUDGE[token]
        warn = ' <span class="warnmark">⚠ 주의</span>' if "⚠" in text else ""
        return f'<span class="badge {cls}">{html.escape(token)}</span>{warn}', cls
    if token.startswith("미충족"):
        return f'<span class="badge no">{html.escape(token)}</span>', ""
    return _inline(text), ""


def md_to_html(md: str, chart_fn=None) -> str:
    """chart_fn(name, arg) → SVG/HTML. 없으면 그래프 지시문은 무시한다."""
    md = CHART_RE.sub(lambda m: f"@@CHART:{m.group(1)}:{m.group(2) or ''}@@", md)
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    skip_table = False  # <!--chart:skip_table--> : 바로 다음 표는 그래프가 대신하므로 PDF에서만 생략
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
        elif ln.startswith("@@CHART:"):
            _, name, arg = ln.strip("@").split(":", 2)
            if name == "skip_table":
                skip_table = chart_fn is not None
            else:
                out.append((chart_fn(name, arg) if chart_fn else "") or "")
            i += 1
        elif ln.startswith("#### "):
            out.append(f"<h4>{_inline(ln[5:])}</h4>"); i += 1
        elif ln.startswith("### "):
            out.append(f"<h3>{_inline(ln[4:])}</h3>"); i += 1
        elif ln.startswith("## "):
            out.append(f"<h2>{_inline(ln[3:])}</h2>"); i += 1
        elif ln.startswith("# "):
            i += 1  # 제목은 상단 띠에서 처리
        elif ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")]); i += 1
            if skip_table:  # 그래프가 같은 정보를 보여 주므로 표는 건너뛴다
                skip_table = False
                continue
            head, body = rows[0], [r for r in rows[2:]]
            th = "".join(f"<th>{_inline(c)}</th>" for c in head)
            trs = []
            for r in body:
                cells = [_cell(c) for c in r]
                tint = next((JUDGE[c.replace("⚠주의", "").strip()] for c in r if c.replace("⚠주의", "").strip() in ("YES", "PARTIAL", "NO", "확인불가")), "")
                trs.append(f'<tr class="r-{tint}">' if tint else "<tr>")
                trs.append("".join(f"<td>{h}</td>" for h, _ in cells) + "</tr>")
            tb = "".join(trs)
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{tb}</tbody></table>")
        elif re.match(r"^\s*[-*] ", ln):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*] ", lines[i]):
                items.append(re.sub(r"^\s*[-*] ", "", lines[i])); i += 1
            out.append("<ul>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ul>")
        elif re.match(r"^\d+\. ", ln):
            items = []
            while i < len(lines) and re.match(r"^\d+\. ", lines[i]):
                items.append(re.sub(r"^\d+\. ", "", lines[i])); i += 1
            out.append('<ol class="ref">' + "".join(f"<li>{_inline(x)}</li>" for x in items) + "</ol>")
        else:
            para = [ln]; i += 1
            while i < len(lines) and lines[i].strip() and not re.match(r"^(#|\||\s*[-*] |\d+\. )", lines[i]):
                para.append(lines[i].rstrip()); i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def split_sections(md: str) -> tuple[str, dict[str, str]]:
    """(제목, {'## 제목 줄': 본문 md}) — 순서 유지."""
    title = next((l[2:].strip() for l in md.splitlines() if l.startswith("# ")), "투자 평가 보고서")
    parts = re.split(r"(?m)^## ", md)
    sections = {}
    for p in parts[1:]:
        head, _, body = p.partition("\n")
        sections[head.strip()] = f"## {head.strip()}\n{body}"
    return title, sections


# ── 1쪽 사이드바 메타 ──────────────────────────────────────────────────────

def report_meta(state: dict) -> dict:
    """State에서 사이드바에 쓸 값만 추린다 (보고서 본문과 같은 출처: evaluation_results)."""
    records = state.get("evaluation_results") or []
    by_id = {r["company_id"]: r for r in records}
    sel = state.get("selected_company_id")
    meta = {"as_of": state.get("as_of_date", ""), "n_total": len(records),
            "n_ok": len(state.get("ranking") or []), "selected": None,
            "_records": by_id, "selected_id": sel if sel in by_id else None}
    if sel and sel in by_id:
        rec = by_id[sel]
        co, sc = rec["current_company"], rec["scorecard"]
        meta["selected"] = {
            "name": co["기업명"], "rating": rec["decision"], "total": sc["total"], "averages": sc["averages"],
            "rank": f"1 / {meta['n_ok']}", "keydata": [
                ("설립일", co.get("설립일") or "확인 불가"),
                ("직원 수", f"{co['직원수']}명" if co.get("직원수") is not None else "확인 불가"),
                ("기업자료 투자", latest_round(co)),
                ("메인 아이템", co.get("메인아이템") or "확인 불가"),
                ("기술분야", co.get("기술분야") or "확인 불가"),
            ], "unknown": len(sc.get("unknown_items", [])),
            "reqs": [(name, (rec["eligibility"].get(g) or {}).get("결과", "확인불가")) for g, name in REQUIREMENTS.items()],
            "checks": {k: sum(1 for q in (rec.get("checklist") or {}).values() if q.get("판정") == k) for k in ("YES", "PARTIAL", "NO", "확인불가")}}
    else:
        from agents._report_nomatch import funnel_counts, near_miss_candidates, shortfall
        from agents._report_nomatch import scored_holds
        c = funnel_counts(records)
        meta["funnel"] = [("투자 요건 미충족(제외)", c["excluded"]), ("자료 확인 불가·분석 오류", c["pending"]),
                          ("종합 점수 기준 미달(보류)", len(scored_holds(records))), ("투자 적격", c["ok"])]
        top = near_miss_candidates(records, 1)
        if top:
            g = shortfall(top[0])
            meta["best"] = {"name": top[0]["current_company"]["기업명"], **{k: g[k] for k in ("total", "tech", "total_gap", "tech_gap", "cause")}}
    return meta


def _sidebar(meta: dict) -> str:
    s = meta["selected"]
    if not s:
        rows = "".join(f"<tr><th>{k}</th><td><b>{v}</b>곳</td></tr>" for k, v in meta.get("funnel", []))
        b = meta.get("best")
        best = ""
        if b:
            gaps = " · ".join(x for x in (f"총점 {b['total_gap']}점 부족" if b["total_gap"] else "", f"기술력 {b['tech_gap']:.2f} 부족" if b["tech_gap"] else "") if x)
            best = (f'<div class="box"><div class="label">최고 근접 후보</div><div class="bestname">{html.escape(b["name"])}</div>'
                    f'<div class="score"><b>{b["total"]}</b><span> / 100점</span></div>'
                    f'<div class="sub">{html.escape(gaps or "기준 충족")}</div><div class="sub">유형: {html.escape(b["cause"])}</div></div>')
        return (f'<aside><div class="box"><div class="label">투자의견</div><div class="rating hold">투자 대상 없음</div>'
                f'<div class="sub">평가 {meta["n_total"]}개사 · 투자 적격 {meta["n_ok"]}곳</div></div>'
                f'<div class="box"><div class="label">탈락 단계</div><table class="kd fn">{rows}</table></div>{best}</aside>')
    cls = {"투자 적격": "buy", "보류": "hold", "제외": "out"}.get(s["rating"], "hold")
    bars = "".join(
        f'<div class="bar"><span class="bn">{c}</span><span class="bt"><i style="width:{v / 5 * 100:.0f}%"></i></span>'
        f'<span class="bv">{v:.1f}</span><span class="bw">{WEIGHTS[c]}점</span></div>' for c, v in s["averages"].items())
    kd = "".join(f"<tr><th>{k}</th><td>{html.escape(str(v))}</td></tr>" for k, v in s["keydata"])
    dot = {"충족": "yes", "미충족": "no"}
    reqs = "".join(f'<div class="req"><span class="badge {dot.get(r, "unk")}">{html.escape(r)}</span>{html.escape(n)}</div>' for n, r in s["reqs"])
    ck = s["checks"]
    tot = sum(ck.values()) or 1
    seg = "".join(f'<i class="seg {c}" style="width:{ck[k] / tot * 100:.1f}%"></i>' for k, c in (("YES", "yes"), ("PARTIAL", "partial"), ("NO", "no"), ("확인불가", "unk")) if ck[k])
    checks = (f'<div class="box"><div class="label">핵심 점검 {tot}문항</div><div class="stack">{seg}</div>'
              f'<div class="sub">YES {ck["YES"]} · PARTIAL {ck["PARTIAL"]} · NO {ck["NO"]} · 자료 없음 {ck["확인불가"]}</div></div>')
    return f"""<aside>
  <div class="box"><div class="label">투자의견</div><div class="rating {cls}">{s['rating']}</div>
    <div class="score"><b>{s['total']}</b><span> / 100점</span></div>
    <div class="sub">투자 적격 순위 {s['rank']} · 확인 불가 {s['unknown']}개 항목</div></div>
  <div class="box"><div class="label">대분류 점수 (1~5)</div>{bars}</div>
  <div class="box"><div class="label">투자 요건 (4개)</div>{reqs}</div>
  {checks}
  <div class="box"><div class="label">Key Data</div><table class="kd">{kd}</table></div>
</aside>"""


CSS = """
@page { size: A4; margin: 14mm 14mm 16mm; @bottom-left { content: "%DISCLAIMER%"; font: 6.5pt 'Apple SD Gothic Neo'; color:#8a94a6; width: 150mm; }
  @bottom-right { content: counter(page) " / " counter(pages); font: 7.5pt 'Apple SD Gothic Neo'; color:#5b6577; } }
:root { --navy:#12284c; --accent:#1f5fbf; --line:#d5dae3; --mute:#5b6577; --bg:#f3f5f9; }
* { box-sizing: border-box; }
body { font-family: 'Apple SD Gothic Neo','AppleGothic',sans-serif; font-size: 8.6pt; line-height: 1.55; color:#1b2230; margin:0; }
.band { background: var(--navy); color:#fff; padding: 6mm 7mm 5mm; margin: 0 0 6mm; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.band .kicker { font-size: 7.5pt; letter-spacing: .12em; opacity:.75; }
.band h1 { font-size: 19pt; margin: 2mm 0 1mm; font-weight: 800; }
.band .meta { font-size: 8pt; opacity:.85; }
.wrap { display:flex; gap: 6mm; align-items:flex-start; }
.wrap main { flex: 1 1 0; min-width:0; } aside { width: 56mm; flex: none; }
h2 { font-size: 11.5pt; color: var(--navy); border-bottom: 1.6px solid var(--navy); padding: 1mm 0; margin: 5mm 0 2.5mm; break-after: avoid; }
h2:first-child { margin-top: 0; }
h3 { font-size: 9.4pt; color: var(--accent); margin: 3.5mm 0 1.2mm; break-after: avoid; }
h4 { font-size: 8.8pt; color: var(--navy); background: var(--bg); border-left: 2.2px solid var(--accent); padding: .8mm 2mm; margin: 4mm 0 1.5mm; break-after: avoid; }
p:has(+ table), p:has(+ ul), p:has(+ .chartbox), p:has(+ .chartrow) { break-after: avoid; }
p { margin: 1.2mm 0 2mm; text-align: justify; } ul { margin: 1mm 0 2mm; padding-left: 4.5mm; } li { margin-bottom: .8mm; }
strong { color: var(--navy); }
code { background: var(--bg); padding: 0 1mm; border-radius: 1mm; }
table { width:100%; border-collapse: collapse; margin: 1.5mm 0 3mm; font-size: 7.6pt; break-inside: auto; }
tr { break-inside: avoid; }
th { background: var(--navy); color:#fff; font-weight:600; padding: 1.2mm 1.6mm; text-align:left; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
td { padding: 1.1mm 1.6mm; border-bottom: .6px solid var(--line); vertical-align: top; }
tbody tr:nth-child(even) td { background: #f8f9fc; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.cite { display:inline-block; font-size: 5.6pt; color: var(--mute); background: var(--bg); border: .5px solid var(--line); border-radius: 1mm; padding: 0 1mm; margin: 0 .4mm; vertical-align: 1pt; white-space: nowrap; }
.box { border: 1px solid var(--line); border-top: 2.2px solid var(--navy); padding: 2.5mm 3mm; margin-bottom: 3mm; background:#fff; }
.label { font-size: 7pt; color: var(--mute); letter-spacing:.05em; margin-bottom: 1.2mm; font-weight:700; }
.req { display:flex; align-items:center; gap: 1.6mm; font-size: 7.4pt; margin: 1.1mm 0; } .req .badge { min-width: 11mm; justify-content:center; }
.stack { display:flex; height: 3.2mm; border-radius: 1.6mm; overflow:hidden; margin: 1mm 0; }
.seg { display:block; height:100%; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.seg.yes { background:#0f7b4b; } .seg.partial { background:#d98a1c; } .seg.no { background:#c0392b; } .seg.unk { background:#8a94a6; }
.bestname { font-size: 10.5pt; font-weight: 800; color: var(--navy); margin-bottom: .5mm; }
.nomatch { --accent:#b26a00; } .nomatch .band { border-bottom: 1.6mm solid #d98a1c; }
.rating { font-size: 15pt; font-weight: 800; } .rating.buy { color:#0f7b4b; } .rating.hold { color:#b26a00; } .rating.out { color:#8a94a6; }
.score b { font-size: 21pt; color: var(--navy); } .score span { color: var(--mute); font-size: 8pt; }
.sub { font-size: 7pt; color: var(--mute); margin-top: 1mm; }
.bar { display:grid; grid-template-columns: 17mm 1fr 7mm 8mm; align-items:center; gap: 1.2mm; font-size: 7.2pt; margin: 1.2mm 0; }
.bt { height: 2.6mm; background: #e6eaf2; border-radius: 1.3mm; overflow:hidden; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.bt i { display:block; height:100%; background: var(--accent); -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.bv { font-weight:700; text-align:right; } .bw { color: var(--mute); font-size: 6.5pt; }
.kd { font-size: 7.4pt; margin:0; } .kd th { background: none; color: var(--mute); font-weight:600; padding: .8mm 1mm .8mm 0; width: 15mm; } .fn th { width: 33mm; }
.kd td { padding: .8mm 0; border: 0; background:none !important; }
svg.chart text { font-family: 'Apple SD Gothic Neo','AppleGothic',sans-serif; }
.chartbox { margin: 1.5mm 0 3mm; padding: 1.5mm 2mm; border: .6px solid var(--line); border-radius: 1.5mm; background:#fbfcfe; break-inside: avoid; }
.chartrow { display:flex; gap: 3mm; align-items:center; margin: 1.5mm 0 3mm; padding: 1.5mm 2mm; border: .6px solid var(--line); border-radius: 1.5mm; background:#fbfcfe; break-inside: avoid; }
.cr-a { width: 43%; flex:none; } .cr-b { flex:1; min-width:0; }
.badge { display:inline-flex; align-items:center; gap:1mm; padding: .1mm 2mm; border-radius: 2.4mm; font-weight: 800; font-size: 7pt; white-space: nowrap; border: .6px solid; }
.badge::before { content:""; width: 1.7mm; height: 1.7mm; border-radius: 50%; background: currentColor; }
.badge.yes { color:#0f7b4b; background:#e3f4ea; border-color:#9fd3b6; } .badge.partial { color:#9a5b00; background:#fdf1dc; border-color:#ecc98a; }
.badge.no { color:#b3261e; background:#fbe4e2; border-color:#eaa7a2; } .badge.unk { color:#5b6577; background:#eceef3; border-color:#c5cad6; }
.warnmark { color:#b3261e; font-size:6.6pt; font-weight:700; }
tr.r-yes td:first-child { box-shadow: inset 2.2px 0 0 #0f7b4b; } tr.r-partial td:first-child { box-shadow: inset 2.2px 0 0 #d98a1c; }
tr.r-no td { background:#fdf3f2 !important; } tr.r-no td:first-child { box-shadow: inset 2.2px 0 0 #c0392b; }
tr.r-unk td:first-child { box-shadow: inset 2.2px 0 0 #8a94a6; }
tr.r-yes td, tr.r-partial td, tr.r-no td, tr.r-unk td { -webkit-print-color-adjust: exact; print-color-adjust: exact; }
sup.ref { font-size: 6.4pt; color: var(--accent); font-weight: 800; margin-left: .3mm; vertical-align: super; }
.tiles { display:flex; gap: 3mm; margin: 1.5mm 0 3mm; } .tile { flex:1; border: .6px solid var(--line); border-top: 2px solid var(--accent); border-radius: 1.5mm; padding: 1.6mm 2.4mm; background:#fbfcfe; }
.tl { font-size: 6.8pt; color: var(--mute); font-weight:700; } .tv { font-size: 10.5pt; font-weight: 800; color: var(--navy); margin: .4mm 0; } .ts { font-size: 6.8pt; color: var(--mute); }
.slabel { font-size: 7.6pt; font-weight: 800; color: var(--navy); letter-spacing:.04em; margin: 2.5mm 0 1.2mm; }
.scards { display:flex; gap: 2.4mm; margin-bottom: 3mm; }
.scard { flex:1; border: .6px solid var(--line); border-left: 2.6px solid var(--accent); border-radius: 1.5mm; padding: 1.8mm 2.4mm; background:#fff; break-inside: avoid; }
.sh { font-size: 8.4pt; font-weight: 800; color: var(--navy); display:flex; align-items:center; gap: 1.4mm; }
.sn { display:inline-flex; width: 4.2mm; height: 4.2mm; border-radius: 50%; background: var(--navy); color:#fff; font-size: 7pt; align-items:center; justify-content:center; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.ss { display:flex; align-items:baseline; gap: .8mm; margin: .8mm 0; } .ss b { font-size: 14pt; font-weight: 800; } .ss span { font-size: 7pt; color: var(--mute); }
.sbar { display:inline-block; flex:1; height: 2.2mm; background:#e6eaf2; border-radius: 1.1mm; margin-left: 1.5mm; overflow:hidden; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.sbar u { display:block; height:100%; text-decoration:none; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
.sf { font-size: 7.4pt; color:#333; line-height:1.4; }
.callouts { display:flex; gap: 3mm; margin: 1.5mm 0 3mm; } .co { flex:1; border-radius: 1.5mm; padding: 2mm 2.6mm; border: .6px solid; break-inside: avoid; }
.co.good { background:#eef8f2; border-color:#a9d9bd; } .co.bad { background:#fdf3f2; border-color:#eaa7a2; }
.cot { font-size: 8pt; font-weight: 800; margin-bottom: 1mm; } .co.good .cot { color:#0f7b4b; } .co.bad .cot { color:#b3261e; }
.co ul { margin: 0; padding-left: 3.6mm; } .co li { font-size: 7.4pt; margin-bottom: .9mm; } .why { color:#5b6577; }
.pt { font-weight: 800; font-size: 7pt; padding: 0 1.2mm; border-radius: 1mm; background:#fff; } .pt.pu { color:#5b6577; }
.pcards { display:flex; gap: 2.4mm; margin-bottom: 2.4mm; }
.pcard { flex:1; border: .6px solid var(--line); border-top: 2px solid #8a94a6; border-radius: 1.5mm; padding: 1.6mm 2.2mm; background:#fff; break-inside: avoid; }
.pn { font-size: 8.2pt; font-weight: 800; color: var(--navy); } .pp { font-size: 7pt; color: var(--mute); margin: .3mm 0 1mm; }
.mtag { display:inline-block; font-size: 6.6pt; background: var(--bg); border-radius: 1mm; padding: .2mm 1.2mm; margin: 0 .8mm .6mm 0; color:#333; }
.cnote { font-size: 6.9pt; color: var(--mute); margin: .5mm 0 2.5mm; }
.compview { margin: 1mm 0 2mm; break-inside: avoid; }
.references { break-inside: avoid; }
ol.ref { padding-left: 5mm; font-size: 7.2pt; color:#333; } ol.ref li { margin-bottom: .6mm; }
"""


def build_chart(name: str, arg: str, meta: dict) -> str:
    """마크다운의 <!--chart:이름[:인자]--> 지시문을 SVG로 바꾼다. 데이터가 없으면 빈 문자열."""
    recs = meta.get("_records") or {}
    rec = recs.get(arg) if arg in recs else recs.get(meta.get("selected_id"))
    box = lambda inner: f'<div class="chartbox">{inner}</div>' if inner else ""  # noqa: E731
    if name == "candidates":
        from agents._report_nomatch import TABLE_MAX, scored_holds, shortfall
        rows = [{"name": r["current_company"]["기업명"], "total": r["scorecard"]["total"], "tech_gap": shortfall(r)["tech_gap"]}
                for r in scored_holds(list(recs.values()))[:TABLE_MAX]]
        return box(charts.candidates_bar(rows)) if rows else ""
    if not rec:
        return ""
    sc = rec.get("scorecard")
    if name == "strengths" and sc:
        from agents._report_highlights import pick_strengths
        return charts.strength_cards(pick_strengths(rec))
    if name == "callouts" and sc:
        from agents._report_highlights import strong_items, weak_items
        return charts.strength_weak_callouts(strong_items(rec), weak_items(rec))
    if name == "ip":
        return box(charts.ip_chart(rec["current_company"]))
    if name == "tech":
        return box(charts.tech_chart(rec.get("technology_analysis")))
    if name == "revenue":
        return box(charts.revenue_chart(rec["current_company"]))
    if name == "competitor":
        return charts.competitor_view(rec.get("competitor_analysis"), rec["current_company"]["기업명"])
    if name == "market":
        return charts.market_tiles(rec.get("market_analysis"))
    if name == "funding":
        # 후보 블록(arg=기업 ID)은 분량 때문에 작게, 단독 보고서(arg 없음)는 크게
        return box(charts.funding_chart(rec["current_company"].get("투자유치이력") or [], compact=bool(arg)))
    if name == "radar_items" and sc:
        return ('<div class="chartrow"><div class="cr-a">' + charts.radar(sc["averages"]) + '</div><div class="cr-b">'
                + charts.item_bars(sc["items"], sc.get("unknown_items", [])) + "</div></div>")
    return ""


def render_html(md: str, meta: dict) -> str:
    try:
        return _render_html(md, meta)
    finally:
        _REFS.clear()  # 전역 번호 표가 다음 보고서에 섞이지 않게 한다


def _render_html(md: str, meta: dict) -> str:
    title, sections = split_sections(md)
    keys = list(sections)
    top = [k for k in keys if k == "SUMMARY" or k.startswith("1")]   # 1쪽 좌측: SUMMARY + 1장 (또는 1~2 요약)
    rest = [k for k in keys if k not in top]
    _REFS.clear()
    if "REFERENCE" in sections:
        _REFS.update(_parse_refs(sections["REFERENCE"]))
        sections["REFERENCE"] = re.sub(r"(?m)\s+— \[[^\]]*\]\s*$", "", sections["REFERENCE"])
    fn = lambda name, arg: build_chart(name, arg, meta)  # noqa: E731
    left = "\n".join(md_to_html(sections[k], fn) for k in top)
    right = "\n".join(f'<section class="references">{md_to_html(sections[k], fn)}</section>'
                      if k == "REFERENCE" else md_to_html(sections[k], fn) for k in rest)
    s = meta["selected"]
    sub = f'{s["rating"]} · 총점 {s["total"]}점' if s else "투자 대상 없음"
    return f"""<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{CSS.replace('%DISCLAIMER%', DISCLAIMER)}</style></head><body class="{'nomatch' if not s else ''}">
<div class="band"><div class="kicker">AI SEMICONDUCTOR STARTUP · INVESTMENT RESEARCH</div>
<h1>{html.escape(title.replace('투자 평가 보고서:', '').strip() or title)}</h1>
<div class="meta">투자 평가 보고서 · {sub} · 조사 기준일 {html.escape(meta['as_of'])}</div></div>
<div class="wrap"><main>{left}</main>{_sidebar(meta)}</div>
<div class="full">{right}</div></body></html>"""


def _find_chrome() -> str:
    for c in CHROME_CANDIDATES:
        if Path(c).exists() or shutil.which(c):
            return c
    raise RuntimeError("Chrome을 찾을 수 없다. CHROME_CANDIDATES에 경로를 추가하거나 Chrome을 설치할 것.")


def export_pdf(md: str, meta: dict, out_path: str | Path) -> Path:
    """markdown 보고서 → PDF. 반환: 저장 경로."""
    out = Path(out_path).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "report.html"
        draft = Path(tmp) / "report.pdf"
        src.write_text(render_html(md, meta), encoding="utf-8")
        subprocess.run(
            [_find_chrome(), "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
             f"--print-to-pdf={draft}", src.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
        with pymupdf.open(draft) as document:
            if not 1 <= len(document) <= 5:
                raise ValueError(f"PDF가 {len(document)}쪽입니다. 보고서를 5쪽 이내로 줄여야 합니다.")
        shutil.copyfile(draft, out)
    return out
