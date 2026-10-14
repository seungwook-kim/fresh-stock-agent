"""매장 도메인 모델: 품목, 거래처."""

from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    FRUIT = "과일"
    VEGETABLE = "야채"
    OTHER = "기타"


# 품목 단위. 재고표 수량 칸의 단위 글자("3봉")를 떼고 읽을 수 있는 것만 쓴다 (recognition.UNIT_WORDS)
UNITS = ("박스", "봉", "망", "개", "판", "팩", "단", "모", "kg")
# 거래처에 주문하는 방식 (messages.py)
CHANNELS = ("구매목록", "문자", "전화")


@dataclass(frozen=True)
class Supplier:
    id: str
    name: str
    channel: str  # 가락시장 / 문자 / 전화


@dataclass(frozen=True)
class Item:
    id: str
    name: str
    category: Category
    unit: str  # 재고표에 숫자만 적히면 이 단위로 읽는다 (야채는 박스)
    supplier_id: str
    base_demand: float  # 평일 하루 평균 판매량 (unit 기준)
    price: int  # unit당 판매가 (원)
    cost: int  # unit당 매입가 (원)
    shelf_life: int  # 정가로 팔 수 있는 일수. 그다음 하루는 떨이, 이후 폐기

    @property
    def count_step(self) -> float:
        """마감 재고를 적는 최소 단위. 박스는 반 박스까지 적는다."""
        return 0.5 if self.unit == "박스" else 1.0
