import pytest

from agents import _report_llm, investment
from agents._report_scoring import (
    ITEM_IDS,
    Q_IDS,
    ChecklistOut,
    DetailsOut,
    InvestmentOut,
    ScoreOut,
    score_e2,
    score_e3,
)
from tests.fixtures.fake_results import company, fake_results


def test_e2_sales_rules():
    assert score_e2(company("C1", "a", {"연도": 2024, "국내": 1_000_000, "해외": None, "상태": "공개"}))[0] == 5
    assert score_e2(company("C1", "a", {"연도": 2024, "국내": 100_000, "해외": None, "상태": "공개"}))[0] == 3
    assert score_e2(company("C1", "a", {"연도": 2024, "국내": 99_999, "해외": None, "상태": "공개"}))[0] == 2
    s, unk, _ = score_e2(company("C1", "a", {"연도": 2024, "국내": None, "해외": None, "상태": "N/A"}))
    assert (s, unk) == (1, False)  # N/A = 매출 없음
    s, unk, _ = score_e2(company("C1", "a", {"연도": 2024, "국내": None, "해외": None, "상태": "비공개"}))
    assert (s, unk) == (2, True)  # 비공개 = 확인 불가


def test_e2_requires_verified_currency_conversion():
    """다른 통화를 환산 근거 없이 합산하지 않는다."""
    usd = {"연도": 2024, "국내": None, "해외": 100_000, "해외단위": "USD", "상태": "공개"}
    score, unk, reason = score_e2(company("C1", "a", usd))
    assert (score, unk) == (2, True) and "합산 불가" in reason
    mixed = {"연도": 2024, "국내": 30_000, "해외": 50_000, "해외단위": "USD", "상태": "공개"}
    assert score_e2(company("C1", "a", mixed))[:2] == (2, True)


def test_e3_investment_rules():
    big = [{"금액": 10_000_000, "투자자": ["A", "B"], "확정": True}]
    assert score_e3(company("C1", "a", history=big))[0] == 5
    assert score_e3(company("C1", "a", history=[{"금액": 10_000_000, "투자자": ["A"], "확정": True}]))[0] == 4
    assert score_e3(company("C1", "a", history=[{"금액": 1_000_000, "투자자": ["A"], "확정": True}]))[0] == 3
    # 협의 중(미확정)은 금액에서 제외
    pending = [{"금액": 9_000_000, "투자자": ["A"], "확정": False}]
    assert score_e3(company("C1", "a", history=pending))[0] == 1
    assert score_e3(company("C1", "a", history=None)) [:2] == (2, True)


def _llm_out(score=4, unknown=(), bad_ids=False) -> InvestmentOut:
    ids = ["NOPE-C03-99", "DIR-C03-01"] if bad_ids else ["DIR-C03-01"]
    return InvestmentOut(
        checklist=[ChecklistOut(id=q, 판정="YES", 근거="x", 근거ID=ids) for q in Q_IDS],
        scorecard=[ScoreOut(id=i, 점수=score, 확인불가=i in unknown, 채점이유="r", 근거ID=ids) for i in ITEM_IDS],
        decision_details=DetailsOut(판단사유="s", 주요위험=["r"], 추가확인사항=[], 재검토조건=[]),
    )


def _state():
    rec = fake_results()[0]
    return {k: rec[k] for k in ("current_company", "eligibility", "technology_analysis", "market_analysis",
                                "competitor_analysis", "current_evidence")}


def test_run_computes_in_code(monkeypatch):
    monkeypatch.setattr(investment, "structured_call", lambda *a, **k: _llm_out(4, unknown={"B1"}, bad_ids=True))
    monkeypatch.setattr(investment, "load_prompt", lambda n: "p")
    out = investment.run(_state())
    sc = out["scorecard"]
    assert set(sc["items"]) == set(ITEM_IDS)
    assert sc["items"]["B1"]["점수"] == 2 and sc["unknown_items"] == ["B1"]  # 확인 불가는 2점 고정
    assert sc["items"]["E2"]["점수"] == 5 and "코드 산출" in sc["items"]["E2"]["채점이유"]  # LLM 값 덮어씀
    assert "NOPE-C03-99" not in sc["items"]["A1"]["근거ID"]  # 없는 근거 ID 제거
    assert sc["items"]["A1"]["근거ID"] == ["DIR-C03-01"]
    assert sc["total"] == investment.total_score({i: v["점수"] for i, v in sc["items"].items()})[1]
    assert out["decision"] == "투자 적격"
    assert set(out["checklist"]) == set(Q_IDS)


def test_run_missing_items_become_unknown(monkeypatch):
    partial = _llm_out(5)
    partial.scorecard = [s for s in partial.scorecard if s.id != "C3"]
    partial.checklist = partial.checklist[:5]
    monkeypatch.setattr(investment, "structured_call", lambda *a, **k: partial)
    monkeypatch.setattr(investment, "load_prompt", lambda n: "p")
    out = investment.run(_state())
    assert "C3" in out["scorecard"]["unknown_items"] and out["scorecard"]["items"]["C3"]["점수"] == 2
    assert out["checklist"]["Q11"]["판정"] == "확인불가"


def test_ineligible_or_pending_never_invests(monkeypatch):
    monkeypatch.setattr(investment, "structured_call", lambda *a, **k: _llm_out(5))
    monkeypatch.setattr(investment, "load_prompt", lambda n: "p")
    st = _state()
    st["eligibility"] = {**st["eligibility"], "판정": "확인필요"}
    assert investment.run(st)["decision"] == "보류"


def test_prompt_file_exists():
    assert "Q11" in _report_llm.load_prompt("investment")


# ── 반복 채점(중앙값) ────────────────────────────────────────────────────────

def _sequence(monkeypatch, outs):
    """structured_call이 호출될 때마다 outs를 차례로 돌려주게 한다(스레드 안전)."""
    import threading
    lock, calls = threading.Lock(), []

    def fake(*a, **k):
        with lock:
            calls.append(1)
            o = outs[(len(calls) - 1) % len(outs)]
        return o

    monkeypatch.setattr(investment, "structured_call", fake)
    monkeypatch.setattr(investment, "load_prompt", lambda n: "p")
    return calls


def test_runs_scoring_multiple_times_and_takes_item_median(monkeypatch):
    from agents._report_scoring import CODE_SCORED, SCORING_RUNS
    calls = _sequence(monkeypatch, [_llm_out(3), _llm_out(4), _llm_out(5)])
    out = investment.run(_state())
    assert len(calls) == SCORING_RUNS == 3
    llm_items = [i for i in ITEM_IDS if i not in CODE_SCORED]
    assert all(out["scorecard"]["items"][i]["점수"] == 4 for i in llm_items)  # 3·4·5의 중앙값
    assert out["scorecard"]["repeat"] == {"runs": 3, "total_min": out["scorecard"]["repeat"]["total_min"],
                                          "total_max": out["scorecard"]["repeat"]["total_max"]}
    assert out["scorecard"]["repeat"]["total_min"] < out["scorecard"]["repeat"]["total_max"]


def test_median_stabilises_decision_across_noisy_runs(monkeypatch):
    """한 회차가 튀어도(예: 매우 낮게 채점) 판정이 뒤집히지 않는다."""
    _sequence(monkeypatch, [_llm_out(4), _llm_out(4), _llm_out(1)])
    assert investment.run(_state())["decision"] == "투자 적격"
    _sequence(monkeypatch, [_llm_out(2), _llm_out(2), _llm_out(5)])
    assert investment.run(_state())["decision"] == "보류"


def test_unknown_needs_majority(monkeypatch):
    _sequence(monkeypatch, [_llm_out(4, unknown={"B1"}), _llm_out(4, unknown={"B1"}), _llm_out(5)])
    sc = investment.run(_state())["scorecard"]
    assert "B1" in sc["unknown_items"] and sc["items"]["B1"]["점수"] == 2  # 2/3가 자료 없음 → 자료 없음
    _sequence(monkeypatch, [_llm_out(3, unknown={"B1"}), _llm_out(4), _llm_out(4)])
    sc = investment.run(_state())["scorecard"]
    assert "B1" not in sc["unknown_items"] and sc["items"]["B1"]["점수"] == 4  # 1/3만 자료 없음 → 무시


def test_checklist_median_by_judgement_order(monkeypatch):
    def with_q1(judge):
        o = _llm_out(4)
        o.checklist[0] = ChecklistOut(id="Q1", 판정=judge, 근거="x", 근거ID=["DIR-C03-01"])
        return o
    _sequence(monkeypatch, [with_q1("YES"), with_q1("NO"), with_q1("NO")])
    assert investment.run(_state())["checklist"]["Q1"]["판정"] == "NO"
    _sequence(monkeypatch, [with_q1("YES"), with_q1("YES"), with_q1("NO")])
    assert investment.run(_state())["checklist"]["Q1"]["판정"] == "YES"


def test_partial_failure_is_tolerated_but_total_failure_raises(monkeypatch):
    import threading
    lock, n = threading.Lock(), []

    def flaky(*a, **k):
        with lock:
            n.append(1)
            k_ = len(n)
        if k_ == 1:
            raise RuntimeError("timeout")
        return _llm_out(4)

    monkeypatch.setattr(investment, "structured_call", flaky)
    monkeypatch.setattr(investment, "load_prompt", lambda x: "p")
    assert investment.run(_state())["scorecard"]["repeat"]["runs"] == 2  # 1회 실패해도 남은 2회로 진행

    def always(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(investment, "structured_call", always)
    with pytest.raises(RuntimeError):
        investment.run(_state())

# ── Q7(매출이 발생하는가?)은 코드 판정 ───────────────────────────────────────

def test_judge_q7_rules_and_unit_handling():
    from agents._report_scoring import judge_q7
    # 망고부스트: 2024 국내 21,818천원 = 약 0.2억 원 → 1억 미만이므로 PARTIAL (LLM은 '약 2억 원, YES'라고 잘못 답했다)
    v, why = judge_q7(company("C03", "망고", {"연도": 2024, "국내": 21818, "해외": None, "해외단위": "USD", "상태": "공개"}))
    assert v == "PARTIAL" and "0.2억 원" in why and "21,818천원" in why
    assert judge_q7(company("C1", "a", {"연도": 2024, "국내": 100_000, "해외": None, "상태": "공개"}))[0] == "YES"      # 정확히 1억
    assert judge_q7(company("C1", "a", {"연도": 2024, "국내": 99_999, "해외": None, "상태": "공개"}))[0] == "PARTIAL"
    # 달러 해외 매출은 환산하지 않는다(E2와 같은 정책): 국내가 1억 미만이면 1억 이상 여부를 알 수 없다
    v, why = judge_q7(company("C1", "a", {"연도": 2024, "국내": 30_000, "해외": 50_000, "해외단위": "USD", "상태": "공개"}))
    assert v == "확인불가" and "환산 근거가 없어" in why
    # 국내 매출만으로 1억 이상이면 해외 값과 무관하게 YES
    v, why = judge_q7(company("C1", "a", {"연도": 2024, "국내": 250_000, "해외": 50_000, "해외단위": "USD", "상태": "공개"}))
    assert v == "YES" and "환산하지 않음" in why
    assert judge_q7(company("C1", "a", {"연도": 2024, "국내": None, "해외": None, "상태": "N/A"}))[0] == "NO"
    assert judge_q7(company("C1", "a", {"연도": 2024, "국내": None, "해외": None, "상태": "비공개"}))[0] == "확인불가"


def test_q7_and_e2_follow_the_same_currency_policy():
    """알에프온: 국내 9.0억 + 해외 $137,920. 달러는 환산하지 않으므로 E2는 확인불가(2점), Q7은 국내만으로 1억 이상이라 YES."""
    from agents._report_scoring import judge_q7, score_e2
    sales = {"연도": 2024, "국내": 901_000, "해외": 137_920, "해외단위": "USD", "상태": "공개"}
    co = company("C08", "알에프온", sales)
    assert score_e2(co)[:2] == (2, True)
    v, why = judge_q7(co)
    assert v == "YES" and "9.0억 원" in why and "901,000천원" in why


def test_llm_wrong_q7_is_overridden_by_code(monkeypatch):
    out = _llm_out(4)
    out.checklist[6] = ChecklistOut(id="Q7", 판정="YES", 근거="매출 약 2억 원 발생", 근거ID=[])  # LLM의 단위 환산 오류
    monkeypatch.setattr(investment, "structured_call", lambda *a, **k: out)
    monkeypatch.setattr(investment, "load_prompt", lambda n: "p")
    st = _state()
    st["current_company"] = company("C03", "망고", {"연도": 2024, "국내": 21818, "해외": None, "해외단위": "USD", "상태": "공개"})
    q7 = investment.run(st)["checklist"]["Q7"]
    assert q7["판정"] == "PARTIAL" and "코드 산출" in q7["근거"] and "2억 원 발생" not in q7["근거"]
