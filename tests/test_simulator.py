from datetime import date

import pytest

from fresh_stock.catalog import load_catalog
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.ordering import SeasonalPolicy, YesterdayPolicy
from fresh_stock.simulator import run_simulation

catalog = load_catalog()
calendar = MarketCalendar()


def _run(policy, **kw):
    return run_simulation(catalog, calendar, policy, start=date(2026, 7, 1), days=30, **kw)


def test_catalog_loads():
    assert len(catalog.items) >= 60
    assert all(i.unit == "박스" for i in catalog.items if i.category == "야채")


@pytest.mark.parametrize("policy", [YesterdayPolicy(), SeasonalPolicy()])
def test_stock_is_conserved(policy):
    """들어온 양 = 정가 판매 + 떨이 + 남은 재고. 버리는 물건은 없다."""
    r = _run(policy)
    for item in catalog.items:
        i = item.id
        received = sum(rec.received.get(i, 0) for rec in r.ledger)
        used = sum(t.sold[i] + t.cleared[i] for t in r.truths)
        assert received == pytest.approx(used + r.ending_stock[i], abs=0.1)


def test_same_seed_same_result():
    assert _run(SeasonalPolicy(), seed=7).summary() == _run(SeasonalPolicy(), seed=7).summary()


def test_policies_see_same_demand():
    a, b = _run(YesterdayPolicy(), seed=3), _run(SeasonalPolicy(), seed=3)
    assert [t.demand for t in a.truths] == [t.demand for t in b.truths]


def test_guardrail_reduces_typo_damage():
    on = _run(SeasonalPolicy(), typo_rate=0.05).summary()
    off = _run(SeasonalPolicy(), typo_rate=0.05, guardrail=False).summary()
    assert on.typos_caught > 0
    assert on.clearance_cost < off.clearance_cost


def test_no_order_arrives_on_sunday():
    r = _run(SeasonalPolicy())
    assert all(o.arrival.weekday() != 6 for o in r.orders)
