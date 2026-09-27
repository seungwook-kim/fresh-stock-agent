"""가게가 실제로 남기는 기록과, 그 기록으로 판매량을 역산하는 계산.

포스기가 없으므로 품목별 판매량은 기록되지 않는다.
어제 재고 + 오늘 입고 - 오늘 재고 = 오늘 소진량(판매 + 폐기)이고,
재고표의 폐기 칸을 빼서 판매량을 추정한다.
"""

from dataclasses import dataclass
from datetime import date

from .catalog import Catalog


@dataclass(frozen=True)
class DayRecord:
    """하루치 기록: 입고, 마감 재고표(재고·폐기), 돈통 현금, 카드리더기 합계."""

    day: date
    received: dict[str, float]
    closing: dict[str, float]
    wasted: dict[str, float]
    cash: int
    card: int

    @property
    def revenue(self) -> int:
        return self.cash + self.card


def outflow(prev_closing: dict[str, float], received: dict[str, float], closing: dict[str, float]) -> dict[str, float]:
    """품목별 소진량. 판매와 폐기가 섞여 있고, 재고를 적을 때의 반올림 오차도 들어간다."""
    ids = prev_closing.keys() | received.keys() | closing.keys()
    return {
        i: max(0.0, round(prev_closing.get(i, 0) + received.get(i, 0) - closing.get(i, 0), 2))
        for i in ids
    }


def sales_series(ledger: list[DayRecord]) -> list[dict[str, float]]:
    """ledger의 날짜마다 품목별 판매량 추정치 (소진량 - 폐기). 첫날은 전날 재고를 0으로 본다."""
    series = []
    prev: dict[str, float] = {}
    for rec in ledger:
        out = outflow(prev, rec.received, rec.closing)
        series.append({i: max(0.0, round(q - rec.wasted.get(i, 0), 2)) for i, q in out.items()})
        prev = rec.closing
    return series


def estimated_revenue(day_sales: dict[str, float], catalog: Catalog) -> int:
    """추정 판매량을 모두 정가에 팔았다고 가정한 매출. 실제 매출과의 차이는 떨이 할인과 기록 실수다."""
    return round(sum(q * catalog.item(i).price for i, q in day_sales.items()))
