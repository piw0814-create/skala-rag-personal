"""저장된 전체 평가의 점수·판정·순위·근거와 PDF를 다시 확인한다."""

import argparse
import json
from collections import Counter
from pathlib import Path

from agents._report_render import ID_RE
from agents.investment import decide, rank_key, total_score
from agents._scoring_grounding import cap_score
from agents._report_scoring import judge_q7, score_e2, score_e3
from config import ITEMS, OUTPUT_DIR, UNKNOWN_SCORE
from runtime import write_json


def audit(payload: dict) -> dict:
    records = payload.get("evaluation_results") or []
    issues = []
    ids = [r.get("company_id") for r in records]
    if len(ids) != len(set(ids)):
        issues.append("기업 결과에 중복 ID가 있음")
    for rec in records:
        cid = rec["company_id"]
        valid = {e["근거ID"] for e in rec.get("current_evidence") or [] if e.get("근거ID")}
        for key in ("eligibility", "technology_analysis", "market_analysis", "competitor_analysis"):
            if set((rec.get(key) or {}).get("근거ID") or []) - valid:
                issues.append(f"{cid}: {key}에 원문 레코드가 없는 근거 ID")
        sc = rec.get("scorecard")
        if not sc:
            if rec.get("decision") == "투자 적격":
                issues.append(f"{cid}: 점수 없이 투자 적격으로 표시됨")
            continue
        expected = {i for items in ITEMS.values() for i in items}
        items = sc.get("items") or {}
        if set(items) != expected:
            issues.append(f"{cid}: 15개 평가 항목 불일치")
            continue
        unknown = set(sc.get("unknown_items") or [])
        for key, item in items.items():
            score = item.get("점수")
            if type(score) is not int or not 1 <= score <= 5:
                issues.append(f"{cid}: {key}의 점수 범위 오류")
            if key in unknown and score != UNKNOWN_SCORE:
                issues.append(f"{cid}: {key} 확인불가 점수 오류")
            if set(item.get("근거ID") or []) - valid:
                issues.append(f"{cid}: {key}에 유효하지 않은 근거 ID")
            if key not in ("E2", "E3") and key not in unknown and not item.get("근거ID"):
                issues.append(f"{cid}: {key}의 점수에 유효한 근거가 없음")
            if type(score) is int and cap_score(key, score, "", rec)[0] < score:
                issues.append(f"{cid}: {key}의 높은 점수에 경력 기간·정량 효과 근거가 부족함")
        averages, total = total_score({k: v["점수"] for k, v in items.items()})
        if total != sc.get("total") or averages != sc.get("averages"):
            issues.append(f"{cid}: 항목 점수와 총점·평균 불일치")
        verdict = (rec.get("eligibility") or {}).get("판정", "확인필요")
        if decide(verdict, total, averages["제품/기술력"]) != rec.get("decision"):
            issues.append(f"{cid}: 평가 기준과 최종 판정 불일치")
        for question in (rec.get("checklist") or {}).values():
            if set(question.get("근거ID") or []) - valid:
                issues.append(f"{cid}: 핵심 점검에 유효하지 않은 근거 ID")
        if (rec.get("checklist") or {}).get("Q7", {}).get("판정") != judge_q7(rec["current_company"])[0]:
            issues.append(f"{cid}: Q7 판정과 최근 연도 매출 불일치")
        for key, fn in (("E2", score_e2), ("E3", score_e3)):
            score, is_unknown, _ = fn(rec["current_company"])
            if items[key]["점수"] != score or (key in unknown) != is_unknown:
                issues.append(f"{cid}: {key} 점수·확인불가 표시와 원천 금액 불일치")
    ranked = sorted((r for r in records if r.get("decision") == "투자 적격" and r.get("scorecard")), key=rank_key)
    ranking = [r["company_id"] for r in ranked]
    if ranking != (payload.get("ranking") or []):
        issues.append("저장된 순위와 재계산한 순위 불일치")
    if payload.get("selected_company_id") != (ranking[0] if ranking else None):
        issues.append("최종 선정 기업과 1위 불일치")
    return {"passed": not issues, "companies": len(records),
            "scored": sum(bool(r.get("scorecard")) for r in records),
            "decisions": dict(Counter(r.get("decision") for r in records)),
            "analysis_errors": payload.get("errors") or [], "ranking": ranking, "issues": issues}


def summary_markdown(payload: dict, validation: dict) -> str:
    lines = ["# 전체 기업 평가 결과", "", f"조사 기준일: {payload.get('as_of_date')}", "",
             f"평가 {validation['companies']}개사 / 점수 산출 {validation['scored']}개사 / "
             f"분석 오류 {len(validation['analysis_errors'])}건", "",
             "| ID | 기업 | 적격성 | 최종 판정 | 총점 | 기술력 평균 | 미확인 항목 |",
             "|---|---|---|---|---:|---:|---|" ]
    for rec in payload.get("evaluation_results") or []:
        sc = rec.get("scorecard") or {}
        lines.append(f"| {rec['company_id']} | {rec['current_company']['기업명']} | "
                     f"{(rec.get('eligibility') or {}).get('판정', '-')} | {rec.get('decision')} | "
                     f"{sc.get('total', '-')} | {sc.get('averages', {}).get('제품/기술력', '-')} | "
                     f"{', '.join(sc.get('unknown_items') or []) or '-'} |")
    lines += ["", "상세 사유와 원문 근거는 evaluation_results.json에 저장된다. "
              "미확인 정보가 있는 기업의 보류는 경쟁력 부족이 확인되었다는 뜻이 아니다."]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=OUTPUT_DIR / "evaluation_results.json")
    args = parser.parse_args()
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    validation = audit(payload)
    if not payload.get("run", {}).get("complete"):
        validation["issues"].append("전체 실행이 완료되지 않음")
    if validation["analysis_errors"]:
        validation["issues"].append("분석 오류가 발생한 기업이 있음")
    folder = args.results.parent
    md_path, pdf_path = folder / "report.md", folder / "report.pdf"
    if md_path.exists():
        body = md_path.read_text(encoding="utf-8").split("## REFERENCE")[0]
        valid = {e["근거ID"] for r in payload.get("evaluation_results") or []
                 for e in r.get("current_evidence") or [] if e.get("근거ID")}
        if set(ID_RE.findall(body)) - valid:
            validation["issues"].append("보고서 본문에 유효하지 않은 근거 ID")
    else:
        validation["issues"].append("Markdown 보고서 없음")
    if pdf_path.exists() and (payload.get("report_outputs") or {}).get("pdf"):
        import pymupdf
        with pymupdf.open(pdf_path) as document:
            validation["pdf_pages"] = len(document)
            if not 1 <= len(document) <= 5:
                validation["issues"].append("PDF가 5쪽 제한을 초과함")
    else:
        validation["issues"].append("이번 실행의 PDF 보고서 없음")
    validation["passed"] = not validation["issues"]
    write_json(folder / "validation.json", validation)
    (folder / "evaluation_summary.md").write_text(summary_markdown(payload, validation), encoding="utf-8")
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    if not validation["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
