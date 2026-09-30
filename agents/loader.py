"""① 후보 적재 에이전트 — 01_기업정보.pdf를 1페이지=1기업 레코드로 구조화 추출 (청킹하지 않음)."""

import json
from pathlib import Path

from config import DATA_DIR
from state import State
from schemas import CompanyRecord

CACHE = DATA_DIR / "processed" / "companies.json"


def load_companies(source: Path, refresh: bool = False) -> list[dict]:
    """캐시(companies.json)가 있으면 그대로 쓰고, 없으면 추출 후 저장한다."""
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))
    from rag.parser import parse_company_pages  # LLM 호출이 필요할 때만 import

    companies = parse_company_pages(source)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(companies, ensure_ascii=False, indent=2), encoding="utf-8")
    return companies


def run(state: State) -> dict:
    """
    입력: source_document
    처리: rag.parser.parse_company_pages() (bbox 컬럼 복원 → LLM 구조화 추출), 결과는 data/processed/companies.json 캐시
    출력: {"candidate_companies": [ {company_id, 기업명, 홈페이지, 설립일, ..., source_page}, ... ], "current_index": 0}
    """
    source = Path(state.get("source_document") or DATA_DIR / "raw" / "01_기업정보.pdf")
    companies = load_companies(source)
    ids = []
    for company in companies:
        if company.get("추출오류"):
            raise ValueError(f"기업 추출 실패: {company.get('company_id')}; 기업 목록을 다시 추출하세요.")
        CompanyRecord.model_validate(company)
        ids.append(company["company_id"])
    if len(ids) != len(set(ids)):
        raise ValueError("기업 목록에 중복 company_id가 있습니다.")
    # 재개 입력에는 저장을 마친 기업만 들어 있다. 미완료 기업은 처음부터 다시 평가한다.
    completed = state.get("evaluation_results") or []
    if [r["company_id"] for r in completed] != ids[:len(completed)]:
        raise ValueError("저장된 결과가 현재 기업 목록의 순서와 일치하지 않습니다.")
    return {"candidate_companies": companies, "current_index": len(completed)}
