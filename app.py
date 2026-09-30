"""전체 후보 평가. --prepare: 사전 조사 포함, --resume: 저장 완료 기업 이후 재개."""

import argparse
import json
from datetime import date
from time import perf_counter

from dotenv import load_dotenv

load_dotenv()

from config import DATA_DIR, EVALUATION_CRITERIA, OUTPUT_DIR  # noqa: E402
from graph import build_graph  # noqa: E402
from agents import report  # noqa: E402
from runtime import input_signature, print_summary, resume_input, save_snapshot  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--as-of", default=date.today().isoformat())
    p.add_argument("--graph", action="store_true", help="그래프 mermaid만 출력")
    p.add_argument("--pdf", action="store_true", help="5쪽 이내 PDF 생성 실패를 실행 오류로 반환")
    p.add_argument("--prepare", action="store_true", help="G4 및 DART·Tavily 사전 조사를 포함해 전체 실행")
    p.add_argument("--refresh-external", action="store_true", help="--prepare에서 외부 판정 재생성 (검색 캐시는 재사용)")
    p.add_argument("--refresh-competition", action="store_true", help="--rescore에서 점수 산출 기업들의 경쟁 분석도 갱신")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="저장 완료 기업 이후부터 재개 (같은 입력·코드·기준일)")
    mode.add_argument("--report-only", action="store_true", help="저장된 전체 평가 결과로 보고서만 재생성")
    mode.add_argument("--rescore", action="store_true", help="완료한 전체 평가의 분석·원문을 유지하고 채점과 보고서를 다시 생성")
    args = p.parse_args()
    try:
        date.fromisoformat(args.as_of)
    except ValueError:
        p.error("--as-of는 YYYY-MM-DD 형식이어야 합니다.")
    if args.refresh_external and not args.prepare:
        p.error("--refresh-external은 --prepare와 함께 사용하세요.")
    if args.refresh_competition and not args.rescore:
        p.error("--refresh-competition은 --rescore와 함께 사용하세요.")
    if args.prepare and (args.resume or args.report_only or args.rescore):
        p.error("재개·보고서 재생성은 사전 조사를 바꾸지 않습니다. --prepare를 빼세요.")

    app = build_graph()
    if args.graph:
        print(app.get_graph().draw_mermaid())
        return

    results_path = OUTPUT_DIR / "evaluation_results.json"
    if args.report_only or args.rescore:
        result = json.loads(results_path.read_text(encoding="utf-8"))
        if not result.get("run", {}).get("complete"):
            raise ValueError("전체 평가가 완료되지 않았습니다. --resume으로 평가를 먼저 마치세요.")
        if result.get("evaluation_criteria") != EVALUATION_CRITERIA and not args.rescore:
            raise ValueError("평가 기준이 바뀌었습니다. 보고서만 재생성하지 말고 전체 평가를 다시 실행하세요.")
        signature = result["run"]["input_signature"]
        if args.rescore:
            from agents import investment
            from agents.loader import CACHE
            from graph import rank
            if [r["current_company"] for r in result["evaluation_results"]] != json.loads(CACHE.read_text(encoding="utf-8")):
                raise ValueError("기업 입력이 바뀌었습니다. 전체 분석을 다시 실행하세요.")
            result["evaluation_criteria"] = EVALUATION_CRITERIA
            for rec in result["evaluation_results"]:
                if not rec.get("scorecard"):
                    continue
                if args.refresh_competition:
                    from agents import competitor
                    print(f"경쟁 분석 갱신: {rec['company_id']}", flush=True)
                    previous = [e for e in rec.get("current_evidence") or []
                                if not e.get("근거ID", "").startswith("CMP-")]
                    rec["current_evidence"] = previous
                    refreshed = competitor.run({**rec, "as_of_date": result["as_of_date"]})
                    rec["competitor_analysis"] = refreshed["competitor_analysis"]
                    rec["current_evidence"] = previous + refreshed["current_evidence"]
                tick = perf_counter()
                print(f"재채점: {rec['company_id']} {rec['current_company']['기업명']}", flush=True)
                update = investment.run({**rec, "evaluation_criteria": EVALUATION_CRITERIA,
                                         "as_of_date": result["as_of_date"]})
                rec.update(update)
                result.setdefault("execution_history", []).append(
                    {"stage": "investment", "phase": "rescore", "company_id": rec["company_id"],
                     "seconds": round(perf_counter() - tick, 3)})
            result.update(rank(result))
            signature = input_signature()
            save_snapshot(results_path, result, signature, result.get("execution_history") or [], False)
        result.update(report.run(result))
        save_snapshot(results_path, result, signature,
                      result.get("execution_history") or [], True)
    else:
        if args.prepare:
            from agents.loader import CACHE, load_companies
            from tools.g4_screening import screen
            from tools.run_tavily_eligibility import main as external_review
            load_companies(DATA_DIR / "raw" / "01_기업정보.pdf")
            print("1/3 AI 관련성 사전 검토", flush=True)
            screen(CACHE)
            print("2/3 DART·Tavily 투자 요건 조사", flush=True)
            external_review(["--refresh"] if args.refresh_external else [])
        signature = input_signature()
        initial = {"source_document": str(DATA_DIR / "raw" / "01_기업정보.pdf"),
                   "as_of_date": args.as_of, "evaluation_criteria": EVALUATION_CRITERIA}
        history = []
        if args.resume:
            initial, history = resume_input(results_path, args.as_of, EVALUATION_CRITERIA, signature)
            print(f"저장 완료 {len(initial['evaluation_results'])}개사 이후부터 재개", flush=True)
        result = initial
        stage = None
        tick = perf_counter()
        print("전체 후보 평가 시작", flush=True)
        try:
            for stream_mode, update in app.stream(initial, {"recursion_limit": 1000},
                                                 stream_mode=["updates", "values"]):
                if stream_mode == "values":
                    result = update
                    if stage == "save":
                        save_snapshot(results_path, result, signature, history, False)
                        rec = result["evaluation_results"][-1]
                        print(f"[{len(result['evaluation_results'])}/{len(result['candidate_companies'])}] "
                              f"{rec['current_company']['기업명']}: {rec['decision']}", flush=True)
                else:
                    for stage, values in update.items():
                        now = perf_counter()
                        company = (values or {}).get("current_company") or result.get("current_company") or {}
                        history.append({"stage": stage, "company_id": company.get("company_id"),
                                        "seconds": round(now - tick, 3)})
                        tick = now
            save_snapshot(results_path, result, signature, history, True)
        except BaseException:
            # 보고서/API 오류나 사용자 중단에도 완료한 기업의 결과를 보존한다.
            save_snapshot(results_path, result, signature, history, False)
            raise
    print_summary(result)
    if args.pdf and (result.get("report_outputs") or {}).get("pdf_error"):
        raise RuntimeError(result["report_outputs"]["pdf_error"])


if __name__ == "__main__":
    main()
