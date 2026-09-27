"""가게가 미리 아는 일: 날씨 예보, 명절, TV 방송.

발주 규칙은 메모(note) 글만 볼 수 있고, 그 일이 수요를 얼마나 바꾸는지는 시뮬레이터만 안다.
숫자 규칙은 이런 글을 읽지 못한다. 글을 읽고 발주에 반영하는 것이 에이전트가 할 일이다.

배율은 사장님 경험(data/store_knowledge.md)에서 가져왔고, 경험에 없던 값은 가정값으로 표시했다.
"""

import random
from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import Catalog
from .domain import Category, Item

# 명절 날짜 (음력). 시뮬레이션 기간에 걸리는 해를 추가해 쓴다
HOLIDAYS = {
    "추석": [date(2025, 10, 6), date(2026, 9, 25)],
    "설날": [date(2026, 2, 17)],
}
# 명절 3일 전부터 당일까지 대목. 매출 2~3배, 과일이 특히 많고 야채도 뒤지지 않는다
HOLIDAY_FACTOR = {Category.FRUIT: 2.7, Category.VEGETABLE: 2.3, Category.OTHER: 2.0}
HOLIDAY_AFTER_FACTOR = 0.5  # 명절 다음날 (가정값)

RAIN_PROBABILITY = 0.08
RAIN_FACTOR = 0.6  # 비 오면 손님이 줄어든다. 크기는 가정값

TV_EVERY = (15, 30)  # TV 효능 소개 간격 (일, 가정값)
TV_SPIKE = (3.0, 1.8)  # 방송 다음날, 그다음날 해당 품목 수요 배율 (가정값)


@dataclass(frozen=True)
class StoreEvent:
    day: date
    note: str
    factor: float = 1.0  # 수요 배율
    category: Category | None = None  # 배율을 적용할 분류. None이면 전 품목
    item_id: str | None = None  # 특정 품목에만 적용
    extra: float = 0.0  # 특정 품목에 더해지는 수요

    def adjust(self, item: Item, demand: float) -> float:
        if self.item_id is not None:
            return demand * self.factor + self.extra if item.id == self.item_id else demand
        if self.category is None or self.category == item.category:
            return demand * self.factor
        return demand


def generate_events(catalog: Catalog, days: list[date], seed: int) -> list[StoreEvent]:
    rng = random.Random(seed + 100)
    events = []
    for d in days:
        if rng.random() < RAIN_PROBABILITY:
            events.append(StoreEvent(d, "비 예보 (강수확률 80%)", factor=RAIN_FACTOR))

    for name, days_of_holiday in HOLIDAYS.items():
        for holiday in days_of_holiday:
            for before in (3, 2, 1, 0):
                d = holiday - timedelta(days=before)
                label = f"{name} 당일" if before == 0 else f"{name} {before}일 전"
                for category, factor in HOLIDAY_FACTOR.items():
                    events.append(StoreEvent(d, f"{label} (명절 대목)", factor=factor, category=category))
            events.append(
                StoreEvent(holiday + timedelta(days=1), f"{name} 다음날 (연휴, 손님 적음)", factor=HOLIDAY_AFTER_FACTOR)
            )

    produce = [i for i in catalog.items if i.category in (Category.FRUIT, Category.VEGETABLE)]
    d = days[0] + timedelta(days=rng.randint(*TV_EVERY))
    while d <= days[-1]:
        item = rng.choice(produce)
        events.append(
            StoreEvent(
                d + timedelta(days=1),
                f"어제 저녁 TV 건강 프로그램에서 {item.name} 효능 소개 (손님 문의 많을 듯)",
                factor=TV_SPIKE[0],
                item_id=item.id,
            )
        )
        events.append(
            StoreEvent(d + timedelta(days=2), f"그저께 TV에서 {item.name} 효능 소개", factor=TV_SPIKE[1], item_id=item.id)
        )
        d += timedelta(days=rng.randint(*TV_EVERY))

    return [e for e in events if days[0] <= e.day <= days[-1]]
