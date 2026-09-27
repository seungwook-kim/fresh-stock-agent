"""발주 에이전트 테스트. 실제 LLM 대신 정해진 답을 돌려주는 가짜 클라이언트를 쓴다."""

from datetime import date

from fresh_stock.agent import AgentPolicy
from fresh_stock.catalog import load_catalog
from fresh_stock.llm import LLMError
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.ordering import SeasonalPolicy
from fresh_stock.simulator import run_simulation

catalog = load_catalog()


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.prompts = []

    def generate_json(self, system, prompt, schema, purpose=""):
        self.prompts.append(prompt)
        self.calls.append(purpose)
        resp = self.responses.pop(0) if self.responses else _submit()
        if isinstance(resp, Exception):
            raise resp
        return resp


def _submit(adjustments=(), summary="특이사항 없음"):
    return {"action": "submit", "item_ids": [], "adjustments": list(adjustments), "summary": summary}


def _history(*ids):
    return {"action": "history", "item_ids": list(ids), "adjustments": [], "summary": ""}


def _run(llm, days=12):
    # 메모 없는 날에도 LLM을 부르게 해서 에이전트 동작만 따로 확인한다
    policy = AgentPolicy(llm, skip_quiet_days=False)
    return run_simulation(catalog, MarketCalendar(), policy, start=date(2026, 7, 1), days=days, typo_rate=0)


def test_no_adjustment_matches_rule_policy():
    agent = _run(ScriptedLLM([]))
    rule = run_simulation(catalog, MarketCalendar(), SeasonalPolicy(), start=date(2026, 7, 1), days=12, typo_rate=0)
    assert [o.final for o in agent.orders] == [o.final for o in rule.orders]


def test_history_lookup_then_adjust():
    llm = ScriptedLLM([_history("tomato"), _submit([{"item_id": "tomato", "qty": 9, "reason": "단체 주문"}])])
    r = _run(llm, days=3)
    first_day = [o for o in r.orders if o.day == date(2026, 7, 1)]
    assert next(o.final for o in first_day if o.item_id == "tomato") == 9
    assert "[판매 기록 조회 1]" in llm.prompts[1]
    assert "토마토(박스)" in llm.prompts[1]
    assert r.policy.decisions[0].reason == "단체 주문"


def test_adjustment_is_capped():
    llm = ScriptedLLM([_submit([{"item_id": "tomato", "qty": 500, "reason": "?"}])])
    r = _run(llm, days=2)
    dec = r.policy.decisions[0]
    assert dec.qty == max(dec.base * 3, dec.base + 10)


def test_unknown_item_is_ignored():
    llm = ScriptedLLM([_submit([{"item_id": "durian", "qty": 3, "reason": "?"}])])
    r = _run(llm, days=2)
    assert r.policy.decisions == []


def test_llm_failure_falls_back_to_rule():
    llm = ScriptedLLM([LLMError("timeout")])
    r = _run(llm, days=2)
    assert r.policy.failures == 1
    assert r.orders  # 발주는 규칙 추천으로 나갔다


def test_lookups_are_limited():
    llm = ScriptedLLM([_history("tomato"), _history("apple"), _history("pear"), _submit()])
    _run(llm, days=2)
    assert "조회는 더 할 수 없다" in llm.prompts[2]
    assert len(llm.prompts) == 3  # 세 번째 조회 요청은 제출로 처리된다


def test_quiet_days_skip_llm():
    llm = ScriptedLLM([])
    r = run_simulation(
        catalog, MarketCalendar(), AgentPolicy(llm), start=date(2026, 7, 1), days=12, typo_rate=0, with_events=False
    )
    assert llm.calls == []
    assert r.orders


def test_prompt_includes_store_notes():
    llm = ScriptedLLM([])
    run_simulation(catalog, MarketCalendar(), AgentPolicy(llm), start=date(2026, 9, 20), days=5, typo_rate=0)
    assert any("추석" in p for p in llm.prompts)
