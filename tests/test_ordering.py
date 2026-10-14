from fresh_stock.catalog import load_catalog
from fresh_stock.ordering import check_order
from fresh_stock.simulator import _typo

tomato = load_catalog().item("tomato")


def test_typo_repeats_last_digit():
    assert _typo(8) == 88
    assert _typo(12) == 122


def test_guardrail_catches_tomato_88_boxes():
    warning = check_order(tomato, 88, cover_days=1, past_daily=[8, 7, 9, 8])
    assert warning is not None
    assert "88박스" in warning


def test_guardrail_allows_saturday_double_order():
    # 토요일은 이틀 치라 16박스여도 하루 8박스꼴이다
    assert check_order(tomato, 16, cover_days=2, past_daily=[8, 7, 9, 8]) is None


def test_guardrail_allows_market_day_spike():
    assert check_order(tomato, 14, cover_days=1, past_daily=[8, 7, 9, 8]) is None


def test_guardrail_compares_with_recommendation():
    assert "추천 8박스" in check_order(tomato, 88, cover_days=1, past_daily=[], recommended=8)


def test_guardrail_allows_big_recommended_order():
    # 명절 대목처럼 추천 자체가 평소보다 훨씬 커도, 추천대로 넣으면 경고하지 않는다
    assert check_order(tomato, 30, cover_days=1, past_daily=[8, 7, 9, 8], recommended=30) is None


def test_guardrail_needs_history():
    assert check_order(tomato, 88, cover_days=1, past_daily=[8]) is None


def test_new_item_is_not_forecast_as_zero():
    """새로 들인 품목은 들어오기 전 날들을 판매 0으로 보지 않는다. 그러면 추천이 계속 0이라 한 번도 안 들어온다."""
    from datetime import date, timedelta

    from fresh_stock.accounting import DayRecord
    from fresh_stock.domain import Category, Item
    from fresh_stock.market_calendar import MarketCalendar
    from fresh_stock.ordering import GAP_DAYS, OrderContext, SeasonalPolicy

    melon = Item("melon", "참외", Category.FRUIT, "박스", "garak", 2.0, 30000, 22000, 3)
    catalog = load_catalog().with_item(melon)
    start = date(2026, 7, 6)
    ledger = [DayRecord(start + timedelta(days=k), {"tomato": 5}, {"tomato": 1}, 0, 0) for k in range(14)]

    def ctx(records):
        today = records[-1].day
        return OrderContext(today, today + timedelta(days=1), [today + timedelta(days=1)], records, catalog,
                            MarketCalendar(market_day_digits=()))  # fmt: skip

    assert ctx(ledger).demand_history("melon") == []
    assert SeasonalPolicy().decide(ctx(ledger))["melon"] == 3  # 기본 수요 2 × 1.2
    first = start + timedelta(days=14)
    ledger.append(DayRecord(first, {"tomato": 5, "melon": 3}, {"tomato": 1, "melon": 1}, 0, 0))
    assert ctx(ledger).demand_history("melon") == [(first, 2.0)]

    # 한동안 취급하지 않은 기간(계속 0개)도 수요 0으로 보지 않는다. 하루이틀 없던 날은 그대로 둔다
    day = first
    for _ in range(GAP_DAYS + 3):
        day += timedelta(days=1)
        ledger.append(DayRecord(day, {"tomato": 5}, {"tomato": 1}, 0, 0))
    history = ctx(ledger).demand_history("melon")
    # 들어온 날, 남은 1박스를 판 다음 날, 그 뒤 0개였던 GAP_DAYS일까지만 남는다
    assert [d for d, _ in history] == [first + timedelta(days=k) for k in range(GAP_DAYS + 2)]
