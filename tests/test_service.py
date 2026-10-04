import math
from datetime import date, timedelta

import pytest

from fresh_stock.catalog import load_catalog
from fresh_stock.demo import demo_closing, seed_demo
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.memory import DayMemoryBank, HashEmbedder
from fresh_stock.service import ORDER, SHEET, StoreService
from fresh_stock.store import Store

catalog = load_catalog()
calendar = MarketCalendar()
START = date(2026, 7, 1)


class FakeLLM:
    """재고표 읽기와 발주 에이전트 요청에 정해진 답을 준다."""

    def __init__(self, sheet_rows=None, adjustments=()):
        self.sheet_rows = sheet_rows or []
        self.adjustments = list(adjustments)
        self.calls = []
        self.prompts = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.calls.append(purpose)
        self.prompts.append(prompt)
        if purpose.startswith("sheet"):
            return {"rows": self.sheet_rows}
        if purpose.startswith("order"):
            return {"action": "submit", "item_ids": [], "adjustments": self.adjustments, "summary": "메모 반영"}
        return {"greetings": []}


def _row(name, qty, item_id, confident=True):
    return {"raw_name": name, "raw_qty": qty, "item_id": item_id, "corrected": False, "confident": confident}


@pytest.fixture
def demo():
    store = Store(":memory:")
    first = seed_demo(store, catalog, calendar, START, days=30, seed=1)
    return store, first


def _service(store, llm):
    return StoreService(store, catalog, calendar, llm_factory=lambda: llm)


def test_seed_demo_starts_at_sheet_step(demo):
    store, first = demo
    service = _service(store, FakeLLM())
    assert service.status() == (SHEET, first)
    assert first == START + timedelta(days=30)
    assert store.received(first)  # 전날 발주가 오늘 입고로 들어와 있다
    past = [t for _, d, t in store.notes() if d < first]
    upcoming = [t for _, d, t in store.notes() if d >= first]
    assert past  # 지난 60일의 가게 메모 (과거 비슷한 날 검색용)
    assert len(upcoming) == 2


def test_full_day_flow(demo):
    store, day = demo
    llm = FakeLLM(
        sheet_rows=[_row("토마", "4반", "tomato"), _row("사각", "2", "square_tofu", confident=False)],
        adjustments=[{"target": "과일", "day": "전체", "factor": 2.0, "reason": "TV 효능 소개"}],
    )
    service = _service(store, llm)

    rows = service.read_sheet(day, b"jpg")
    assert [(r.item_id_read, r.qty_read) for r in rows] == [("tomato", 4.5), ("square_tofu", 2.0)]
    assert rows[1].flags == ("글씨가 애매함",)

    closing = service.confirm_sheet(day, {0: ("tomato", 4.5), 1: ("square_tofu", 3.0)}, cash=500_000, card=900_000)
    assert closing["tomato"] == 4.5 and closing["square_tofu"] == 3.0 and closing["apple"] == 0
    assert store.correction_stats() == {"확정한 줄": 2, "사람이 고친 줄": 1, "그중 확인 표시가 있던 줄": 1}

    if not calendar.is_supply_day(day + timedelta(days=1)):
        assert service.status() == (SHEET, day + timedelta(days=1))
        return
    assert service.status() == (ORDER, day)
    plan = service.plan_for(day)
    changed = [ln for ln in plan.lines if ln.reason]
    fruit = {i.id for i in catalog.items if i.category == "과일"}
    if service.order_context(day).notes:
        assert {ln.item_id for ln in changed} == fruit
        assert plan.summary == "메모 반영"
    finals = {ln.item_id: ln.recommended for ln in plan.lines}
    assert service.check_finals(day, finals) == {}  # 추천대로 넣으면 경고 없음
    tomato = finals["tomato"]
    finals["tomato"] = max(tomato, 3) * 11  # 8 -> 88 같은 입력 실수
    assert "tomato" in service.check_finals(day, finals)

    finals["tomato"] = tomato
    approved = service.approve(day, finals)
    assert approved.approved
    assert service.status() == (SHEET, day + timedelta(days=1))
    assert store.received(plan.arrival)
    messages = service.messages(day)
    assert {m.supplier.id for m in messages} >= {"garak"}


def test_plan_without_notes_skips_llm():
    store = Store(":memory:")
    first = seed_demo(store, catalog, calendar, START, days=30, seed=1)
    for note_id, _, _ in store.notes():
        store.delete_note(note_id)
    llm = FakeLLM(sheet_rows=[_row("토마", "3", "tomato")])
    service = _service(store, llm)
    service.read_sheet(first, b"jpg")
    service.confirm_sheet(first, {0: ("tomato", 3.0)}, 0, 0)
    day = first
    while not calendar.is_supply_day(day + timedelta(days=1)):
        day += timedelta(days=1)
        service.confirm_sheet(day, {}, 0, 0)
    plan = service.plan_for(day)
    assert all(ln.base == ln.recommended for ln in plan.lines)
    assert not [c for c in llm.calls if c.startswith("order")]


def test_duplicate_rows_are_summed(demo):
    store, day = demo
    service = _service(store, FakeLLM())
    closing = service.confirm_sheet(day, {0: ("tomato", 2.0), 1: ("tomato", 1.5), 2: (None, 3.0)}, 0, 0)
    assert closing["tomato"] == 3.5  # 창고 + 진열대


def test_revenue_compares_actual_and_estimated(demo):
    store, _ = demo
    rows = _service(store, FakeLLM()).revenue(days=7)
    assert len(rows) == 7
    # 시뮬레이션 매출은 재고 역산과 같은 방식으로 만들어져서 차이가 작아야 한다
    assert all(abs(r.gap) <= 0.2 * r.actual for r in rows)


def test_demo_closing_is_possible(demo):
    store, day = demo
    service = _service(store, FakeLLM())
    max_possible, _ = service.sheet_bounds(day)
    closing = demo_closing(service, day, seed=3)
    assert all(0 <= q <= max_possible[i] for i, q in closing.items())


def test_confirmed_names_become_aliases(demo):
    store, day = demo
    llm = FakeLLM(sheet_rows=[_row("방토", "2", ""), _row("사과", "1", "apple"), _row("토마 용 4반", "4반", "tomato")])
    service = _service(store, llm)
    service.read_sheet(day, b"jpg")
    service.confirm_sheet(day, {0: ("cherry_tomato", 2.0), 1: ("apple", 1.0), 2: ("tomato", 4.5)}, 0, 0)
    # 정식 이름(사과)과 숫자가 섞인 글자(토마 용 4반)는 줄임말로 쓰지 않는다
    assert service.learned_aliases() == {"방토": "cherry_tomato"}


def test_agent_gets_similar_past_days(demo):
    store, day = demo
    llm = FakeLLM(sheet_rows=[_row("토마", "3", "tomato")])
    memory = DayMemoryBank(catalog, calendar, HashEmbedder())
    service = StoreService(store, catalog, calendar, llm_factory=lambda: llm, memory=memory)
    service.read_sheet(day, b"jpg")
    service.confirm_sheet(day, {0: ("tomato", 3.0)}, 0, 0)
    similar = service.similar_days(day)
    assert similar and all("실제 판매" in text for text in similar)
    service.plan_for(day)
    order_prompt = next(p for c, p in zip(llm.calls, llm.prompts) if c.startswith("order"))
    assert "[과거 비슷한 날]" in order_prompt


def test_demo_notes_have_sources(demo):
    store, _ = demo
    sources = {n.text: n.source for n in store.note_records()}
    assert sources["비 예보 (강수확률 80%)"] == "날씨"
    assert any(src == "사장님" and "효능" in text for text, src in sources.items())


def test_new_store_first_order_is_not_zero():
    """처음 쓰는 가게: 첫 재고표는 전날 재고를 몰라 판매량을 알 수 없다. 그날을 판매 0으로 보면 발주가 전부 0이 된다."""
    service = StoreService(Store(":memory:"), catalog, calendar)
    day = date(2026, 10, 5)  # 월요일, 다음 날 입고
    service.store.save_day(day, {i.id: 3.0 for i in catalog.items}, 500000, 900000)
    plan = service.make_plan(day)
    # 판매 기록이 없으니 품목 기본 수요(하루 평균)로 시작한다. 10/6(화)는 장날이 아니고 하루만 버티면 된다
    expected = {i.id: max(0, math.ceil(i.base_demand * 1.2 - 3 - 1e-9)) for i in catalog.items}
    assert {ln.item_id: ln.recommended for ln in plan.lines} == expected and sum(expected.values()) > 0


def test_skip_closed_day_keeps_dates_and_demand(demo, monkeypatch):
    store, first = demo
    service = StoreService(store, catalog, calendar)
    monkeypatch.setattr("fresh_stock.service.today_kst", lambda: first + timedelta(days=3))
    assert service.status() == (SHEET, first) and service.days_behind() == 3
    stock = store.closing(first - timedelta(days=1))
    arrived = store.received(first)

    assert service.skip() == (SHEET, first)  # 쉬는 날: 판매 없이 재고가 그대로 넘어간다
    closing = store.closing(first)
    assert all(closing[i.id] == stock.get(i.id, 0) + arrived.get(i.id, 0) for i in catalog.items)
    assert store.ledger()[-1].revenue == 0

    step, day = service.status()
    if step == ORDER:  # 발주도 건너뛰면 입고 0
        assert service.skip() == (ORDER, first)
        assert store.plan(first).approved and store.received(first + timedelta(days=1)) == {}

    # 쉰 날은 수요 기록에서 빠진다 (요일 중앙값을 0으로 끌어내리지 않게)
    ctx = service.order_context(store.last_closed_day())
    if ctx is not None:
        assert first not in {d for d, _ in ctx.demand_history("tomato")}

    monkeypatch.setattr("fresh_stock.service.today_kst", lambda: first - timedelta(days=1))
    with pytest.raises(ValueError, match="아직 오지 않은 날"):
        service.skip()
