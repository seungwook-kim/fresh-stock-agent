"""가게가 미리 아는 일: 날씨 예보, 명절, 단체 주문.

발주 규칙은 메모(note) 글만 볼 수 있고, 그 일이 수요를 얼마나 바꾸는지는 시뮬레이터만 안다.
숫자 규칙은 이런 글을 읽지 못한다. 글을 읽고 발주에 반영하는 것이 에이전트가 할 일이다.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import Catalog
from .domain import Category, Item

# 추석 (음력 8월 15일). 시뮬레이션 기간에 걸리는 해를 추가해 쓴다
CHUSEOK = {2026: date(2026, 9, 25)}

RAIN_PROBABILITY = 0.08
RAIN_FACTOR = 0.6
GROUP_ORDER_EVERY = (12, 20)  # 단체 주문 간격 (일)
GROUP_BUYERS = ("햇살어린이집", "동네 교회", "경로당", "초등학교 행사")


@dataclass(frozen=True)
class StoreEvent:
    day: date
    note: str
    factor: float = 1.0  # 수요 배율
    category: Category | None = None  # 배율을 적용할 분류. None이면 전 품목
    item_id: str | None = None  # 단체 주문 품목
    extra: float = 0.0  # 단체 주문 수량

    def adjust(self, item: Item, demand: float) -> float:
        if self.item_id is not None:
            return demand + self.extra if item.id == self.item_id else demand
        if self.category is None or self.category == item.category:
            return demand * self.factor
        return demand


def generate_events(catalog: Catalog, days: list[date], seed: int) -> list[StoreEvent]:
    rng = random.Random(seed + 100)
    events = []
    for d in days:
        if rng.random() < RAIN_PROBABILITY:
            events.append(StoreEvent(d, "비 예보 (강수확률 80%)", factor=RAIN_FACTOR))

    for chuseok in CHUSEOK.values():
        for before in (3, 2, 1):
            d = chuseok - timedelta(days=before)
            events.append(StoreEvent(d, f"추석 {before}일 전 (선물·차례상 과일)", factor=2.2, category=Category.FRUIT))
            events.append(StoreEvent(d, f"추석 {before}일 전 (차례상 나물·야채)", factor=1.4, category=Category.VEGETABLE))
        for after in (0, 1):
            events.append(StoreEvent(chuseok + timedelta(days=after), "추석 연휴 (손님 적음)", factor=0.3))

    boxed_fruit = [i for i in catalog.items if i.category == Category.FRUIT and i.unit == "박스"]
    d = days[0] + timedelta(days=rng.randint(*GROUP_ORDER_EVERY))
    while d <= days[-1]:
        item = rng.choice(boxed_fruit)
        qty = rng.randint(3, 6)
        buyer = rng.choice(GROUP_BUYERS)
        events.append(StoreEvent(d, f"{buyer} 단체 주문: {item.name} {qty}{item.unit} (당일 수령)", item_id=item.id, extra=qty))
        d += timedelta(days=rng.randint(*GROUP_ORDER_EVERY))

    return [e for e in events if days[0] <= e.day <= days[-1]]
