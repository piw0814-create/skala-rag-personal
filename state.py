"""Graph State (설계: 그래프 설계 › 1. State 설계). 생명주기별 4개 그룹."""

import operator
from typing import Annotated, Literal, TypedDict


def merge_evidence(old: list[dict], new: list[dict] | None) -> list[dict]:
    """None을 받으면 리셋, 리스트를 받으면 누적."""
    if new is None:
        return []
    return old + new


Decision = Literal["투자 적격", "보류", "제외"]


class State(TypedDict, total=False):
    # ① 입력·설정 (실행 전 고정)
    source_document: str
    as_of_date: str
    evaluation_criteria: dict
    candidate_companies: list[dict]

    # ② 흐름 제어
    current_index: int
    route_reason: str
    retrieve_count: int

    # ③ 작업 산출물 (후보 전환 시 리셋)
    current_company: dict | None
    eligibility: dict | None
    checklist: dict | None
    technology_analysis: dict | None
    market_analysis: dict | None
    competitor_analysis: dict | None
    current_evidence: Annotated[list[dict], merge_evidence]
    scorecard: dict | None
    decision: Decision | None
    decision_details: dict | None

    # ④ 실행 전체 (후보 경계를 넘어 유지)
    evaluation_results: Annotated[list[dict], operator.add]
    errors: Annotated[list[dict], operator.add]
    selected_company_id: str | None  # 투자 적격 중 1위, 없으면 None
    ranking: list[str]  # 투자 적격 기업 ID 순위
    final_report: str
    report_outputs: dict  # Markdown·PDF 저장 경로와 PDF 생성 오류


def next_attempt(state: State, output_key: str) -> int:
    """RAG 에이전트의 retrieve_count 갱신값. 해당 에이전트 첫 진입(산출물 None)이면 1부터 센다."""
    return 1 if state.get(output_key) is None else state.get("retrieve_count", 0) + 1
