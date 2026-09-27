from datetime import date

from fresh_stock.market_calendar import MarketCalendar

cal = MarketCalendar()


def test_market_days_follow_date_digits():
    assert [d for d in range(1, 32) if cal.is_market_day(date(2026, 7, d))] == [2, 7, 12, 17, 22, 27]


def test_no_supply_on_sunday():
    assert not cal.is_supply_day(date(2026, 7, 5))  # 일요일
    assert cal.is_supply_day(date(2026, 7, 6))


def test_saturday_arrival_covers_sunday():
    sat = date(2026, 7, 4)
    assert cal.cover_days(sat) == [sat, date(2026, 7, 5)]
    assert cal.next_supply_day(sat) == date(2026, 7, 6)


def test_weekday_arrival_covers_one_day():
    wed = date(2026, 7, 1)
    assert cal.cover_days(wed) == [wed]
