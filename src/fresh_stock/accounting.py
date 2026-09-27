"""가게가 실제로 남기는 기록과, 그 기록으로 판매량을 역산하는 계산.

포스기가 없으므로 품목별 판매량은 기록되지 않는다.
어제 재고 + 오늘 입고 - 오늘 재고 = 오늘 소진량(정가 판매 + 떨이)이다.
버리는 물건은 없고, 정가 기한이 지난 물건은 떨이로 전부 판다.
떨이 양은 따로 적지 않아도 입고일과 품목별 정가 판매 일수로 추정할 수 있다.
"""

from dataclasses import dataclass
from datetime import date

from .catalog import Catalog
from .domain import Item


@dataclass(frozen=True)
class DayRecord:
    """하루치 기록: 입고, 마감 재고표, 돈통 현금, 카드리더기 합계."""

    day: date
    received: dict[str, float]
    closing: dict[str, float]
    cash: int
    card: int

    @property
    def revenue(self) -> int:
        return self.cash + self.card


def outflow(prev_closing: dict[str, float], received: dict[str, float], closing: dict[str, float]) -> dict[str, float]:
    """품목별 소진량. 정가 판매와 떨이가 섞여 있고, 재고를 적을 때의 반올림 오차도 들어간다."""
    ids = prev_closing.keys() | received.keys() | closing.keys()
    return {
        i: max(0.0, round(prev_closing.get(i, 0) + received.get(i, 0) - closing.get(i, 0), 2))
        for i in ids
    }


def clearance_estimate(ledger: list[DayRecord], idx: int, item: Item) -> float:
    """ledger[idx]날 아침 정가 기한이 지나 떨이로 뺀 양.

    오래된 것부터 팔린다고 보면, 전날 마감 재고 중 최근 (정가 판매 일수 - 1)일 동안
    들어온 양을 넘는 부분이 기한이 지난 물건이다.
    """
    if idx == 0:
        return 0.0
    newer = sum(ledger[k].received.get(item.id, 0) for k in range(max(0, idx - item.shelf_life + 1), idx))
    return max(0.0, ledger[idx - 1].closing.get(item.id, 0) - newer)


def regular_sales_series(ledger: list[DayRecord], catalog: Catalog) -> list[dict[str, float]]:
    """ledger의 날짜마다 품목별 정가 판매량 추정치 (소진량 - 떨이). 첫날은 전날 재고를 0으로 본다."""
    series = []
    prev: dict[str, float] = {}
    for idx, rec in enumerate(ledger):
        out = outflow(prev, rec.received, rec.closing)
        series.append(
            {i: max(0.0, round(q - clearance_estimate(ledger, idx, catalog.item(i)), 2)) for i, q in out.items()}
        )
        prev = rec.closing
    return series


def estimated_revenue(day_sales: dict[str, float], catalog: Catalog) -> int:
    """추정 판매량을 모두 정가에 팔았다고 가정한 매출. 실제 매출과의 차이는 떨이 할인과 기록 실수다."""
    return round(sum(q * catalog.item(i).price for i, q in day_sales.items()))
