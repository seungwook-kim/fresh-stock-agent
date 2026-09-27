"""과거 비슷한 날 검색(RAG) 테스트. 임베딩 모델 대신 결정적인 해시 임베딩을 쓴다."""

from datetime import date, timedelta

import pytest

from fresh_stock.agent import AgentPolicy
from fresh_stock.catalog import load_catalog
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.memory import DayMemoryBank, HashEmbedder, bm25_scores, evaluate_retrieval, hybrid_rank
from fresh_stock.ordering import OrderContext, OwnerPolicy, SeasonalPolicy, SwitchPolicy
from fresh_stock.simulator import run_simulation

catalog = load_catalog()
calendar = MarketCalendar()

NOTES = ["추석 3일 전 (명절 대목)", "비 예보 (강수확률 80%)", "어제 저녁 TV 건강 프로그램에서 양배추 효능 소개", "설날 2일 전 (명절 대목)"]


def test_bm25_prefers_shared_keywords():
    scores = bm25_scores("추석 2일 전", NOTES)
    assert scores.argmax() == 0


@pytest.mark.parametrize("embedder", [None, HashEmbedder()])
def test_hybrid_rank(embedder):
    assert hybrid_rank("비가 온대요 예보", NOTES, embedder)[0] == 1
    assert hybrid_rank("TV에 브로콜리 효능", NOTES, embedder)[0] == 2
    assert hybrid_rank("아무거나", [], embedder) == []


@pytest.fixture(scope="module")
def year():
    """작년 추석(2025-10-06)과 올해 설(2026-02-17)이 들어간 기록. 사장님이 감으로 발주했다."""
    return run_simulation(catalog, calendar, OwnerPolicy(), date(2025, 8, 1), 240, seed=5, typo_rate=0)


def _ctx(result, today: date, notes: dict | None = None) -> OrderContext:
    idx = next(i for i, rec in enumerate(result.ledger) if rec.day == today)
    arrival = today + timedelta(days=1)
    return OrderContext(
        today,
        arrival,
        calendar.cover_days(arrival),
        result.ledger[: idx + 1],
        catalog,
        calendar,
        notes=notes or {},
        note_log={d: n for d, n in result.notes.items() if d <= today},
    )


def test_memory_records_actual_outcomes(year):
    bank = DayMemoryBank(catalog, calendar, HashEmbedder())
    memories = {m.day: m for m in bank.memories(year.ledger, year.notes)}
    chuseok_eve = memories[date(2025, 10, 5)]
    assert chuseok_eve.text == "추석 1일 전 (명절 대목)"
    assert chuseok_eve.category_ratio["과일"] > 1.8  # 시뮬레이터에서 명절 과일은 2.7배
    rainy = [m for m in memories.values() if m.notes == ("비 예보 (강수확률 80%)",)]
    assert rainy and all(m.category_ratio["야채"] < 0.9 for m in rainy)


def test_search_only_sees_the_past(year):
    bank = DayMemoryBank(catalog, calendar, HashEmbedder())
    before = _ctx(year, date(2025, 10, 1))
    assert all(m.day <= date(2025, 10, 1) for m in bank.memories(before.ledger, before.note_log))
    after = _ctx(year, date(2026, 2, 12))
    found = bank.search(after, "설날 3일 전 (명절 대목)")
    assert found[0].text.startswith(("추석", "설날"))
    assert "과일" in bank.describe(found)


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts, self.systems, self.calls = [], [], []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.prompts.append(prompt)
        self.systems.append(system)
        self.calls.append(purpose)
        if self.responses:
            return self.responses.pop(0)
        return {"action": "submit", "item_ids": [], "query": "", "adjustments": [], "summary": ""}


def test_agent_recalls_and_searches(year):
    today = date(2026, 2, 13)
    ctx = _ctx(year, today, notes={today + timedelta(days=1): ["설날 3일 전 (명절 대목)"]})
    llm = ScriptedLLM([{"action": "search", "item_ids": [], "query": "비 예보", "adjustments": [], "summary": ""}])
    agent = AgentPolicy(llm, memory=DayMemoryBank(catalog, calendar, HashEmbedder()), use_knowledge=False)
    agent.decide(ctx)
    assert "[과거 비슷한 날]" in llm.prompts[0]
    assert "2025-10-0" in llm.prompts[0]  # 작년 추석 기록이 떠오른다
    assert "검색어: 비 예보" in llm.prompts[1]
    assert "매장 경험" not in llm.systems[0]
    assert agent.label == "발주 에이전트 + 과거 기록 검색 (경험 노트 없음)"


def test_switch_policy_uses_agent_only_after_switch(year):
    today = date(2026, 2, 13)
    ctx = _ctx(year, today, notes={today + timedelta(days=1): ["설날 3일 전 (명절 대목)"]})
    llm = ScriptedLLM([])
    policy = SwitchPolicy(OwnerPolicy(), AgentPolicy(llm), switch=date(2026, 3, 1))
    assert policy.decide(ctx) == OwnerPolicy().decide(ctx)
    assert llm.calls == [] and policy.label == "발주 에이전트"


def test_sold_out_days_are_marked():
    rule_year = run_simulation(catalog, calendar, SeasonalPolicy(), date(2025, 8, 1), 70, seed=5, typo_rate=0)
    bank = DayMemoryBank(catalog, calendar)
    eve = next(m for m in bank.memories(rule_year.ledger, rule_year.notes) if m.day == date(2025, 10, 5))
    # 명절을 모르는 규칙 발주로는 대목 첫날 다 팔려서, 판매 기록만 보면 수요가 작아 보인다
    assert eve.category_ratio["과일"] < 1
    assert "품절 많음" in eve.describe(catalog, calendar)


def test_retrieval_eval_runs():
    results = evaluate_retrieval(HashEmbedder())
    assert set(results) == {"BM25 (글자 2-gram)", "임베딩", "하이브리드 (BM25 + 임베딩, RRF)"}
    top1, hit3, n, _ = results["BM25 (글자 2-gram)"]
    assert n == 13 and top1 <= hit3 <= n
