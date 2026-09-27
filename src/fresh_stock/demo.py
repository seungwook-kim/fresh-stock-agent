"""데모 데이터.

실제 매장 기록이 없으므로, 가상 매장 시뮬레이션 기록을 DB에 넣어 화면을 바로 써볼 수 있게 한다.
재고표 사진도 없으므로, 오늘 예상 재고로 합성 재고표를 만들어 인식 흐름을 시연한다.
"""

import math
import random
from collections import defaultdict
from datetime import date, timedelta

from .catalog import Catalog
from .market_calendar import MarketCalendar
from .ordering import SeasonalPolicy
from .service import StoreService
from .sheets import render_sheet
from .simulator import run_simulation
from .store import OrderLineRecord, OrderPlan, Store


def seed_demo(store: Store, catalog: Catalog, calendar: MarketCalendar, start: date, days: int, seed: int) -> date:
    """days일치 기록과 그 발주를 넣는다. 돌려주는 날짜가 재고표를 입력할 첫날이다."""
    # 하루 더 돌려서 마지막 날 저녁 발주(다음 날 입고분)까지 만든다
    result = run_simulation(catalog, calendar, SeasonalPolicy(), start, days + 1, seed=seed, typo_rate=0)
    first_open = start + timedelta(days=days)
    for rec in result.ledger[:days]:
        store.save_day(rec.day, rec.closing, rec.cash, rec.card)
        store.save_received(rec.day, rec.received)

    by_day = defaultdict(list)
    for o in result.orders:
        if o.arrival <= first_open:
            by_day[(o.day, o.arrival)].append(o)
    for (order_day, arrival), lines in by_day.items():
        finals = {o.item_id: o.final for o in lines}
        store.save_plan(
            OrderPlan(
                order_day,
                arrival,
                "시뮬레이션 기록",
                True,
                tuple(
                    OrderLineRecord(i.id, finals.get(i.id, 0), finals.get(i.id, 0), finals.get(i.id, 0), "", "")
                    for i in catalog.items
                ),
            )
        )
    arrival_orders = by_day.get((first_open - timedelta(days=1), first_open), [])
    store.save_received(first_open, {o.item_id: o.final for o in arrival_orders})

    # 앞으로 며칠 동안의 가게 메모
    tv_item = random.Random(seed).choice([i for i in catalog.items if i.category == "야채"])
    store.add_note(first_open + timedelta(days=1), f"어제 저녁 TV 건강 프로그램에서 {tv_item.name} 효능 소개 (손님 문의 많을 듯)")
    store.add_note(first_open + timedelta(days=2), "비 예보 (강수확률 80%)")
    return first_open


def demo_closing(service: StoreService, day: date, seed: int) -> dict[str, float]:
    """합성 재고표용 그날 마감 재고. 예상 재고에 판매 변동을 준다."""
    rng = random.Random(seed)
    max_possible, expected = service.sheet_bounds(day)
    closing = {}
    for item in service.catalog.items:
        sold = (max_possible[item.id] - expected[item.id]) * rng.uniform(0.6, 1.4)
        qty = max(0.0, max_possible[item.id] - sold)
        step = item.count_step
        closing[item.id] = min(max_possible[item.id], math.floor(qty / step + 0.5) * step)
    return closing


def demo_sheet_image(service: StoreService, day: date, seed: int) -> bytes:
    image, _ = render_sheet(service.catalog, day, demo_closing(service, day, seed), seed=seed)
    return image
