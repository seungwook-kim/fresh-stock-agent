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
