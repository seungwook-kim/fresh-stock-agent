"""가상 매장 시뮬레이터.

진짜 수요(DayTruth)는 시뮬레이터만 알고, 발주 규칙은 가게가 남긴 기록(DayRecord)만 본다.
같은 seed면 발주 규칙이 달라도 수요와 입력 실수 발생 여부가 같아서 규칙끼리 공정하게 비교할 수 있다.
"""

import math
import random
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .accounting import DayRecord
from .catalog import Catalog
from .market_calendar import MarketCalendar
from .ordering import OrderContext, OrderPolicy, check_order

# 아래 값들은 가정값이다. 실제 매장 경험에 맞춰 조정한다.
WEEKDAY_FACTOR = (0.9, 0.9, 0.95, 1.0, 1.1, 1.3, 1.2)  # 월~일
MARKET_DAY_FACTOR = 1.6
DEMAND_CV = 0.3  # 수요 변동계수
MARKDOWN_RATE = 0.5  # 떨이 판매가 비율
CARD_SHARE = (0.5, 0.7)  # 매출 중 카드 비중 범위


@dataclass
class _Batch:
    qty: float
    age: int = 0


@dataclass(frozen=True)
class DayTruth:
    day: date
    demand: dict[str, float]
    sold: dict[str, float]  # 정가 판매
    sold_markdown: dict[str, float]  # 떨이 판매
    wasted: dict[str, float]
    lost: dict[str, float]  # 재고가 없어 못 판 수요


@dataclass(frozen=True)
class OrderLine:
    day: date
    arrival: date
    item_id: str
    proposed: int  # 발주 규칙이 낸 수량
    entered: int  # 사람이 입력한 수량 (입력 실수 포함)
    final: int  # 실제로 들어온 수량
    typo: bool
    warning: str | None


@dataclass(frozen=True)
class Summary:
    policy: str
    days: int
    revenue: int
    purchase_cost: int
    ending_stock_cost: int
    waste_cost: int
    lost_sales: int  # 품절로 놓친 매출 (정가 기준)
    markdown_loss: int  # 떨이로 깎아준 금액
    typos: int
    typos_caught: int
    false_alarms: int

    @property
    def profit(self) -> int:
        return self.revenue - self.purchase_cost + self.ending_stock_cost

    @property
    def waste_rate(self) -> float:
        return self.waste_cost / self.purchase_cost if self.purchase_cost else 0.0


@dataclass
class SimulationResult:
    policy: OrderPolicy
    catalog: Catalog
    ledger: list[DayRecord] = field(default_factory=list)
    truths: list[DayTruth] = field(default_factory=list)
    orders: list[OrderLine] = field(default_factory=list)
    ending_stock: dict[str, float] = field(default_factory=dict)

    def summary(self) -> Summary:
        item = self.catalog.item
        return Summary(
            policy=self.policy.label,
            days=len(self.ledger),
            revenue=sum(r.revenue for r in self.ledger),
            purchase_cost=round(sum(q * item(i).cost for r in self.ledger for i, q in r.received.items())),
            ending_stock_cost=round(sum(q * item(i).cost for i, q in self.ending_stock.items())),
            waste_cost=round(sum(q * item(i).cost for t in self.truths for i, q in t.wasted.items())),
            lost_sales=round(sum(q * item(i).price for t in self.truths for i, q in t.lost.items())),
            markdown_loss=round(
                sum(q * item(i).price * (1 - MARKDOWN_RATE) for t in self.truths for i, q in t.sold_markdown.items())
            ),
            typos=sum(o.typo for o in self.orders),
            typos_caught=sum(o.typo and o.warning is not None for o in self.orders),
            false_alarms=sum(not o.typo and o.warning is not None for o in self.orders),
        )


def generate_demand(
    catalog: Catalog, calendar: MarketCalendar, days: list[date], seed: int
) -> dict[date, dict[str, float]]:
    rng = random.Random(seed)
    demand = {}
    for d in days:
        factor = WEEKDAY_FACTOR[d.weekday()] * (MARKET_DAY_FACTOR if calendar.is_market_day(d) else 1.0)
        demand[d] = {}
        for item in catalog.items:
            mu = item.base_demand * factor
            demand[d][item.id] = round(max(0.0, rng.gauss(mu, mu * DEMAND_CV)), 1)
    return demand


def _typo(qty: int) -> int:
    """마지막 숫자를 한 번 더 누르는 실수: 8 → 88, 12 → 122."""
    return qty * 10 + qty % 10


def _round_to_step(qty: float, step: float) -> float:
    return math.floor(qty / step + 0.5) * step


def run_simulation(
    catalog: Catalog,
    calendar: MarketCalendar,
    policy: OrderPolicy,
    start: date,
    days: int,
    seed: int = 42,
    typo_rate: float = 0.005,
    guardrail: bool = True,
) -> SimulationResult:
    dates = [start + timedelta(days=i) for i in range(days)]
    demand = generate_demand(catalog, calendar, dates, seed)
    pay_rng = random.Random(seed + 1)
    result = SimulationResult(policy=policy, catalog=catalog)

    batches: dict[str, list[_Batch]] = {i.id: [] for i in catalog.items}
    arrivals: dict[date, dict[str, float]] = defaultdict(dict)
    past_daily: dict[str, list[float]] = defaultdict(list)

    # 개장 첫날 재고: 평소 판매량 기준으로 첫 입고일 분량을 채워 둔다
    first_cover = len(calendar.cover_days(start))
    arrivals[start] = {i.id: math.ceil(i.base_demand * first_cover) for i in catalog.items}

    for d in dates:
        received = arrivals.pop(d, {})
        for item_id, qty in received.items():
            if qty > 0:
                batches[item_id].append(_Batch(qty))

        sold, sold_md, wasted, lost, closing, wasted_rec = {}, {}, {}, {}, {}, {}
        revenue = 0.0
        for item in catalog.items:
            remaining = demand[d][item.id]
            full = md = 0.0
            # 오래된 것부터 판다. 정가 판매 기한이 지난 묶음은 떨이 가격이다
            for b in sorted(batches[item.id], key=lambda b: -b.age):
                take = min(b.qty, remaining)
                b.qty -= take
                remaining -= take
                if b.age >= item.shelf_life:
                    md += take
                else:
                    full += take
            waste = sum(b.qty for b in batches[item.id] if b.age >= item.shelf_life)
            batches[item.id] = [b for b in batches[item.id] if b.age < item.shelf_life and b.qty > 1e-9]
            for b in batches[item.id]:
                b.age += 1
            sold[item.id], sold_md[item.id] = round(full, 2), round(md, 2)
            wasted[item.id], lost[item.id] = round(waste, 2), round(remaining, 2)
            closing[item.id] = _round_to_step(sum(b.qty for b in batches[item.id]), item.count_step)
            wasted_rec[item.id] = _round_to_step(waste, item.count_step)
            revenue += full * item.price + md * item.price * MARKDOWN_RATE

        card = round(revenue * pay_rng.uniform(*CARD_SHARE))
        result.ledger.append(DayRecord(
                day=d, received=received, closing=closing, wasted=wasted_rec, cash=round(revenue) - card, card=card
            ))
        result.truths.append(DayTruth(d, demand[d], sold, sold_md, wasted, lost))

        arrival = d + timedelta(days=1)
        if arrival > dates[-1] or not calendar.is_supply_day(arrival):
            continue
        ctx = OrderContext(
            today=d,
            arrival=arrival,
            cover_days=calendar.cover_days(arrival),
            ledger=list(result.ledger),
            catalog=catalog,
            calendar=calendar,
        )
        proposed = policy.decide(ctx)
        for item in catalog.items:
            qty = proposed.get(item.id, 0)
            if qty <= 0:
                continue
            # 입력 실수 여부는 (seed, 날짜, 품목)으로 정해서 발주 규칙이 달라도 같은 날 같은 품목에서 난다
            typo = random.Random(f"{seed}:{d}:{item.id}").random() < typo_rate
            entered = _typo(qty) if typo else qty
            warning = check_order(item, entered, len(ctx.cover_days), past_daily[item.id]) if guardrail else None
            # 경고를 본 사람은 원래 의도한 수량으로 바로잡는다
            final = qty if warning else entered
            arrivals[arrival][item.id] = final
            past_daily[item.id].append(final / len(ctx.cover_days))
            result.orders.append(OrderLine(d, arrival, item.id, qty, entered, final, typo, warning))

    result.ending_stock = {i: sum(b.qty for b in bs) for i, bs in batches.items()}
    return result
