from datetime import date

from fresh_stock.accounting import DayRecord, outflow, sales_series


def _rec(day: int, received: dict, closing: dict, wasted: dict | None = None) -> DayRecord:
    return DayRecord(date(2026, 7, day), received, closing, wasted or {}, cash=0, card=0)


def test_outflow_is_prev_plus_received_minus_closing():
    assert outflow({"apple": 2}, {"apple": 5}, {"apple": 1.5}) == {"apple": 5.5}


def test_outflow_never_negative():
    # 재고를 잘못 세서 늘어난 경우에도 음수 판매량은 만들지 않는다
    assert outflow({"apple": 1}, {}, {"apple": 2}) == {"apple": 0.0}


def test_sales_series_subtracts_waste():
    ledger = [
        _rec(1, {"tomato": 8}, {"tomato": 3}),
        _rec(2, {"tomato": 6}, {"tomato": 2}, wasted={"tomato": 1}),
    ]
    assert sales_series(ledger) == [{"tomato": 5}, {"tomato": 6}]
