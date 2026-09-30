"""LangGraph 흐름 (설계: 그래프 설계 › 2. Graph 흐름 설계).

에이전트 노드(agents/*)는 guarded()로 감싸 예외를 errors에 기록하고 handle_error로 분기한다.
흐름 제어 노드(select, exclude, hold, handle_error, save, next_index, rank)는 이 파일에 둔다.
루프는 후보 소진 시에만 종료하고, rank가 투자 적격 기업 중 1위를 selected_company_id로 고른다.
"""

from langgraph.graph import END, START, StateGraph
from time import perf_counter

from agents import competitor, eligibility, investment, loader, market, report, technology
from agents.investment import rank_key
from config import N_RETRIEVE_RETRY
from state import State

# 후보 전환 시 리셋하는 ③ 작업 산출물
_RESET = {
    "eligibility": None,
    "checklist": None,
    "technology_analysis": None,
    "market_analysis": None,
    "competitor_analysis": None,
    "current_evidence": None,  # merge_evidence 리듀서가 None을 리셋으로 처리
    "scorecard": None,
    "decision": None,
    "decision_details": None,
    "route_reason": "",
    "retrieve_count": 0,
}


def _company_id(state: State) -> str | None:
    return (state.get("current_company") or {}).get("company_id")


def guarded(stage: str, fn):
    def node(state: State) -> dict:
        company = state.get("current_company") or {}
        print(f"  실행 중: {stage} / {company.get('company_id', '-')} {company.get('기업명', '')}", flush=True)
        started = perf_counter()
        try:
            return fn(state)
        except Exception as e:  # 한 기업의 실패가 전체 루프를 멈추지 않도록
            return {"errors": [{"company_id": _company_id(state), "stage": stage, "error": repr(e)}]}
        finally:
            print(f"  단계 종료: {stage} / {perf_counter() - started:.1f}초", flush=True)

    return node


def _failed(state: State) -> bool:
    errors = state.get("errors") or []
    return bool(errors) and errors[-1]["company_id"] == _company_id(state)


# ---------- 흐름 제어 노드 ----------

def select(state: State) -> dict:
    company = state["candidate_companies"][state.get("current_index", 0)]
    return {"current_company": company, **_RESET}


def exclude(state: State) -> dict:
    reason = state["eligibility"].get("사유", "")
    return {"decision": "제외", "decision_details": {"판단사유": reason}, "route_reason": f"자격 요건 미충족: {reason}"}


def hold(state: State) -> dict:
    reason = state["eligibility"].get("사유", "")
    return {"decision": "보류", "decision_details": {"추가확인사항": reason}, "route_reason": f"자격 요건 확인 필요: {reason}"}


def handle_error(state: State) -> dict:
    stage = state["errors"][-1]["stage"]
    return {"decision": "보류", "decision_details": {"판단사유": f"{stage} 단계 오류"}, "route_reason": f"오류: {stage}"}


def save(state: State) -> dict:
    """기업별 결과를 누적. 투자 적격이어도 루프는 계속된다 (선정은 rank에서)."""
    keys = ("current_company", "eligibility", "checklist", "technology_analysis", "market_analysis",
            "competitor_analysis", "scorecard", "decision", "decision_details", "route_reason", "current_evidence")
    return {"evaluation_results": [{"company_id": _company_id(state), **{k: state.get(k) for k in keys}}]}


def next_index(state: State) -> dict:
    return {"current_index": state.get("current_index", 0) + 1}


def rank(state: State) -> dict:
    """최종 선정: 전체 후보 평가 후 투자 적격 기업 순위 산정, 1위를 보고서 대상으로."""
    passed = [r for r in state.get("evaluation_results", []) if r["decision"] == "투자 적격"]
    ids = [r["company_id"] for r in sorted(passed, key=rank_key)]
    return {"ranking": ids, "selected_company_id": ids[0] if ids else None}


# ---------- 라우터 ----------

def route_exists(state: State) -> str:
    return "select" if state.get("current_index", 0) < len(state["candidate_companies"]) else "rank"


def route_eligibility(state: State) -> str:
    if _failed(state):
        return "handle_error"
    return {"적격": "technology", "부적격": "exclude"}.get(state["eligibility"]["판정"], "hold")


def route_retry(output_key: str, retry_node: str, next_node: str):
    def route(state: State) -> str:
        if _failed(state):
            return "handle_error"
        enough = (state.get(output_key) or {}).get("근거충분", False)
        if enough or state.get("retrieve_count", 0) > N_RETRIEVE_RETRY:
            return next_node
        return retry_node  # 질의 재작성 후 재검색

    return route


def route_ok(next_node: str):
    return lambda state: "handle_error" if _failed(state) else next_node


def build_graph():
    g = StateGraph(State)
    g.add_node("load", loader.run)
    g.add_node("select", select)
    g.add_node("eligibility", guarded("eligibility", eligibility.run))
    g.add_node("technology", guarded("technology", technology.run))
    g.add_node("market", guarded("market", market.run))
    g.add_node("competitor", guarded("competitor", competitor.run))
    g.add_node("investment", guarded("investment", investment.run))
    g.add_node("exclude", exclude)
    g.add_node("hold", hold)
    g.add_node("handle_error", handle_error)
    g.add_node("save", save)
    g.add_node("next_index", next_index)
    g.add_node("rank", rank)
    g.add_node("report", report.run)

    g.add_edge(START, "load")
    g.add_conditional_edges("load", route_exists, ["select", "rank"])
    g.add_edge("select", "eligibility")
    g.add_conditional_edges("eligibility", route_eligibility, ["technology", "exclude", "hold", "handle_error"])
    g.add_conditional_edges("technology", route_retry("technology_analysis", "technology", "market"),
                            ["technology", "market", "handle_error"])
    g.add_conditional_edges("market", route_retry("market_analysis", "market", "competitor"),
                            ["market", "competitor", "handle_error"])
    g.add_conditional_edges("competitor", route_ok("investment"), ["investment", "handle_error"])
    g.add_conditional_edges("investment", route_ok("save"), ["save", "handle_error"])
    for n in ("exclude", "hold", "handle_error"):
        g.add_edge(n, "save")
    g.add_edge("save", "next_index")
    g.add_conditional_edges("next_index", route_exists, ["select", "rank"])
    g.add_edge("rank", "report")
    g.add_edge("report", END)
    return g.compile()
