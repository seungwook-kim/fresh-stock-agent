from datetime import date

from fresh_stock.accounting import DayRecord, clearance_estimate, outflow, regular_sales_series
from fresh_stock.catalog import load_catalog

catalog = load_catalog()


def _rec(day: int, received: dict, closing: dict) -> DayRecord:
    return DayRecord(date(2026, 7, day), received, closing, cash=0, card=0)


def test_outflow_is_prev_plus_received_minus_closing():
    assert outflow({"apple": 2}, {"apple": 5}, {"apple": 1.5}) == {"apple": 5.5}


def test_outflow_never_negative():
    # 재고를 잘못 세서 늘어난 경우에도 음수 판매량은 만들지 않는다
    assert outflow({"apple": 1}, {}, {"apple": 2}) == {"apple": 0.0}


# 토마토는 정가로 이틀 판다 (shelf_life=2). 1일에 들어온 건 3일 아침에 떨이로 빠진다
LEDGER = [
    _rec(1, {"tomato": 8}, {"tomato": 5}),
    _rec(2, {}, {"tomato": 4}),
    _rec(3, {"tomato": 6}, {"tomato": 3}),
]


def test_clearance_estimate_finds_aged_stock():
    tomato = catalog.item("tomato")
    assert clearance_estimate(LEDGER, 1, tomato) == 0
    assert clearance_estimate(LEDGER, 2, tomato) == 4


def test_regular_sales_excludes_clearance():
    # 3일 소진량 7 = 떨이 4 + 정가 판매 3
    assert regular_sales_series(LEDGER, catalog) == [{"tomato": 3}, {"tomato": 1}, {"tomato": 3}]
