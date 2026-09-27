"""발주 에이전트 테스트. 실제 LLM 대신 정해진 답을 돌려주는 가짜 클라이언트를 쓴다."""

from datetime import date

from fresh_stock.agent import AgentPolicy
from fresh_stock.catalog import load_catalog
from fresh_stock.domain import Category
from fresh_stock.llm import LLMError
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.ordering import SeasonalPolicy
from fresh_stock.simulator import run_simulation

catalog = load_catalog()
FRUIT = {i.id for i in catalog.items if i.category == Category.FRUIT}


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


def _adj(target, factor, day="전체", reason="이유"):
    return {"target": target, "day": day, "factor": factor, "reason": reason}


def _submit(*adjustments, summary="특이사항 없음"):
    return {"action": "submit", "item_ids": [], "adjustments": list(adjustments), "summary": summary}


def _history(*ids):
    return {"action": "history", "item_ids": list(ids), "adjustments": [], "summary": ""}


def _run(llm, days=12):
    # 메모 없는 날에도 LLM을 부르게 해서 에이전트 동작만 따로 확인한다
    policy = AgentPolicy(llm, skip_quiet_days=False)
    return run_simulation(catalog, MarketCalendar(), policy, start=date(2026, 7, 1), days=days, typo_rate=0)


def _first_order(r, item_id, day=date(2026, 7, 1)):
    return next(o.final for o in r.orders if o.day == day and o.item_id == item_id)


def test_no_adjustment_matches_rule_policy():
    agent = _run(ScriptedLLM([]))
    rule = run_simulation(catalog, MarketCalendar(), SeasonalPolicy(), start=date(2026, 7, 1), days=12, typo_rate=0)
    assert [o.final for o in agent.orders] == [o.final for o in rule.orders]


def test_history_lookup_then_raise_factor():
    llm = ScriptedLLM([_history("tomato"), _submit(_adj("tomato", 3.0, reason="TV 효능 소개"))])
    r = _run(llm, days=3)
    dec = r.policy.decisions[0]
    assert dec.item_id == "tomato" and dec.qty > dec.base
    assert _first_order(r, "tomato") == dec.qty
    assert "[판매 기록 조회 1]" in llm.prompts[1]
    assert "토마토(박스)" in llm.prompts[1]
    assert dec.reason == "TV 효능 소개"


def test_category_factor_applies_to_all_fruit():
    r = _run(ScriptedLLM([_submit(_adj("과일", 2.5))]), days=2)
    assert {d.item_id for d in r.policy.decisions} == FRUIT


def test_item_overrides_category():
    r = _run(ScriptedLLM([_submit(_adj("과일", 2.5), _adj("tomato", 1.0))]), days=2)
    tomato = next(d for d in r.policy.decisions if d.item_id == "tomato")
    assert tomato.qty == tomato.base


def test_factor_is_clamped():
    huge = _run(ScriptedLLM([_submit(_adj("tomato", 100))]), days=2)
    four = _run(ScriptedLLM([_submit(_adj("tomato", 4.0))]), days=2)
    assert _first_order(huge, "tomato") == _first_order(four, "tomato")


def test_day_specific_factor_only_hits_that_day():
    # 7/3(금) 발주는 7/4(토)·7/5(일) 이틀 치다. 일요일에만 배율을 주면 이틀 모두 준 것보다 적게 나온다
    friday = date(2026, 7, 3)
    only_sunday = _run(ScriptedLLM([_submit(), _submit(), _submit(_adj("tomato", 3.0, day="07-05"))]), days=5)
    both_days = _run(ScriptedLLM([_submit(), _submit(), _submit(_adj("tomato", 3.0))]), days=5)
    base = _run(ScriptedLLM([]), days=5)
    assert (
        _first_order(base, "tomato", friday)
        < _first_order(only_sunday, "tomato", friday)
        < _first_order(both_days, "tomato", friday)
    )


def test_unknown_target_is_ignored():
    r = _run(ScriptedLLM([_submit(_adj("durian", 3.0))]), days=2)
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
