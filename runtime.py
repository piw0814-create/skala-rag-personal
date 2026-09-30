"""전체 실행의 중간 저장·재개와 실행 요약. 평가 규칙은 agents/graph에 둔다."""

import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from config import LLM_MODEL, ROOT

RESULT_KEYS = ("source_document", "as_of_date", "evaluation_criteria", "ranking",
               "selected_company_id", "evaluation_results", "errors", "report_outputs")


def input_signature() -> str:
    """기업·분석 코드·프롬프트가 바뀐 실행의 결과를 섞어 재개하지 않는다."""
    files = [ROOT / p for p in ("config.py", "schemas.py", "state.py", "graph.py", "llm.py",
                               "app.py", "runtime.py", "data/manifest.json", "data/processed/companies.json")]
    files += sorted((ROOT / "data/raw").glob("*.pdf"))
    files += sorted((ROOT / "agents").glob("*.py"))
    files += sorted((ROOT / "prompts").glob("*.md"))
    files += sorted((ROOT / "rag").glob("*.py"))
    files += sorted((ROOT / "tools").glob("*.py"))
    # 사전 조사 결과가 바뀌어도 이전 결과와 섞지 않는다.
    files += [ROOT / "data/processed" / p for p in
              ("g4_screening_results.json", "external_eligibility_results.json")]
    digest = hashlib.sha256(LLM_MODEL.encode())
    for path in files:
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes() if path.exists() else b"MISSING")
    return digest.hexdigest()


def write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(payload, file, ensure_ascii=False, indent=2)
        file.write("\n")
        temporary = Path(file.name)
    temporary.replace(path)


def save_snapshot(path: Path, state: dict, signature: str, history: list[dict], complete: bool) -> None:
    write_json(path, {**{k: state.get(k) for k in RESULT_KEYS},
                      "run": {"complete": complete, "input_signature": signature, "model": LLM_MODEL},
                      "execution_history": history})


def resume_input(path: Path, as_of: str, criteria: dict, signature: str) -> tuple[dict, list[dict]]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    if saved.get("run", {}).get("input_signature") != signature:
        raise ValueError("기업 데이터·코드·사전 조사 결과가 바뀌었습니다. --resume 없이 새 평가를 실행하세요.")
    if saved.get("as_of_date") != as_of or saved.get("evaluation_criteria") != criteria:
        raise ValueError("기준일·평가 기준이 다른 결과는 재개할 수 없습니다.")
    records = saved.get("evaluation_results") or []
    completed_ids = {r["company_id"] for r in records}
    state = {k: saved[k] for k in ("source_document", "as_of_date", "evaluation_criteria")}
    state.update(evaluation_results=records,
                 errors=[e for e in saved.get("errors") or [] if e.get("company_id") in completed_ids])
    return state, saved.get("execution_history") or []


def print_summary(state: dict) -> None:
    records = state.get("evaluation_results") or []
    counts = Counter(r.get("decision") for r in records)
    print(f"평가 완료 {len(records)}개사 | 투자 적격 {counts['투자 적격']} | 보류 {counts['보류']} | 제외 {counts['제외']}")
    print(f"점수 산출 {sum(bool(r.get('scorecard')) for r in records)}개사 | 분석 오류 {len(state.get('errors') or [])}건")
    for cid in state.get("ranking") or []:
        rec = next(r for r in records if r["company_id"] == cid)
        sc = rec["scorecard"]
        print(f"  {cid} {rec['current_company']['기업명']}: {sc['total']}점 / 기술력 {sc['averages']['제품/기술력']:.2f}")
    outputs = state.get("report_outputs") or {}
    if outputs.get("pdf_error"):
        print(f"PDF 생성 실패: {outputs['pdf_error']} (Markdown·평가 결과는 저장됨)")
    else:
        print(f"보고서: {outputs.get('pdf') or outputs.get('markdown')}")
