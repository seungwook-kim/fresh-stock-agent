"""발주 규칙과 발주 입력 가드레일.

발주 규칙은 가게가 실제로 가진 기록(DayRecord)만 본다. 진짜 수요는 모른다.
"""

import math
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from statistics import mean, median
from typing import Protocol

from .accounting import DayRecord, sales_series
from .catalog import Catalog
from .domain import Item
from .market_calendar import MarketCalendar

SOLD_OUT_BUMP = 1.3  # 마감 때 다 팔렸으면 수요를 이만큼 더 있었다고 본다


@dataclass(frozen=True)
class OrderContext:
    today: date  # 마감 후 발주를 넣는 날
    arrival: date  # 이번 발주가 들어오는 날
    cover_days: list[date]  # 이번 입고분으로 버텨야 하는 날들
    ledger: list[DayRecord]  # 오늘까지의 기록
    catalog: Catalog
    calendar: MarketCalendar

    def stock(self, item_id: str) -> float:
        return self.ledger[-1].closing.get(item_id, 0.0) if self.ledger else 0.0

    @cached_property
    def _sales(self) -> list[dict[str, float]]:
        return sales_series(self.ledger)

    def demand_history(self, item_id: str) -> list[tuple[date, float]]:
        """날짜별 수요 추정치. 다 팔린 날은 실제 수요가 더 컸을 것이므로 올려 잡는다."""
        return [
            (rec.day, sold.get(item_id, 0.0) * (SOLD_OUT_BUMP if rec.closing.get(item_id, 0.0) == 0 else 1.0))
            for rec, sold in zip(self.ledger, self._sales)
        ]


class OrderPolicy(Protocol):
    name: str
    label: str

    def decide(self, ctx: OrderContext) -> dict[str, int]: ...


def _order_qty(need: float) -> int:
    """발주는 정수 단위로만 넣는다."""
    return max(0, math.ceil(need - 1e-9))


class YesterdayPolicy:
    """기존 방식: 오늘 판매량만큼 버틸 날 수대로 가져온다.

    실제 매장 방식 그대로 남은 재고는 빼지 않는다. 남은 건 떨이로 판다.
    """

    name = "yesterday"
    label = "기존 방식 (전일 판매량 기준)"

    def decide(self, ctx: OrderContext) -> dict[str, int]:
        orders = {}
        for item in ctx.catalog.items:
            history = ctx.demand_history(item.id)
            last = history[-1][1] if history else item.base_demand
            orders[item.id] = _order_qty(last * len(ctx.cover_days))
        return orders


class SeasonalPolicy:
    """요일·장날 보정: 최근 같은 요일 수요의 중앙값에 장날 배율을 곱하고, 안전재고를 더한 뒤 남은 재고를 뺀다."""

    name = "seasonal"
    label = "요일·장날 보정"
    default_market_ratio = 1.5  # 장날 기록이 쌓이기 전에 쓰는 사장님 경험값
    safety_factor = 1.2  # 수요가 튀는 날을 버틸 여유분

    def forecast(self, ctx: OrderContext, item: Item, day: date) -> float:
        cal = ctx.calendar
        history = ctx.demand_history(item.id)[-35:]
        normal = [(d, q) for d, q in history if not cal.is_market_day(d)]
        same_weekday = [q for d, q in normal if d.weekday() == day.weekday()][-4:]
        if len(same_weekday) >= 2:
            base = median(same_weekday)
        elif normal:
            base = median(q for _, q in normal[-7:])
        else:
            base = history[-1][1] if history else item.base_demand
        if cal.is_market_day(day):
            market = [q for d, q in history if cal.is_market_day(d)]
            normal_median = median(q for _, q in normal) if normal else 0
            ratio = median(market) / normal_median if market and normal_median > 0 else self.default_market_ratio
            base *= ratio
        return base

    def decide(self, ctx: OrderContext) -> dict[str, int]:
        orders = {}
        for item in ctx.catalog.items:
            need = sum(self.forecast(ctx, item, d) for d in ctx.cover_days)
            orders[item.id] = _order_qty(need * self.safety_factor - ctx.stock(item.id))
        return orders


def check_order(item: Item, qty: int, cover_days: int, past_daily: list[float]) -> str | None:
    """평소보다 지나치게 많은 발주 수량이면 확인 메시지를 돌려준다 (토마토 8박스 → 88박스 같은 실수).

    토요일처럼 이틀 치를 받는 날이 있으므로 하루당 수량으로 환산해 비교한다.
    past_daily는 이 품목의 지난 발주를 하루당 수량으로 적은 목록이다.
    """
    past = [q for q in past_daily if q > 0][-10:]
    if len(past) < 3:
        return None
    avg = mean(past)
    daily = qty / cover_days
    if daily >= 3 * avg and daily - avg >= 3:
        return (
            f"{item.name} {qty}{item.unit}: 하루 {daily:.1f}{item.unit}꼴로, "
            f"최근 평균 {avg:.1f}{item.unit}의 {daily / avg:.1f}배입니다. 맞나요?"
        )
    return None


POLICIES: dict[str, type[OrderPolicy]] = {p.name: p for p in (YesterdayPolicy, SeasonalPolicy)}
