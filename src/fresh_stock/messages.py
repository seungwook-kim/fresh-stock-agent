"""거래처별 발주 문자·구매 목록 작성.

거래처마다 주문하는 방식이 다르다.
- 문자: "사각두부 2판, 손두부 2판, 순두부 1판"처럼 한 줄로 보낸다
- 전화: 통화하면서 읽을 메모
- 구매목록: 가락시장은 지인이 경매로 사 오므로, 분류별로 정리한 종이 목록을 보여준다

품목·수량은 코드가 확정된 발주에서 그대로 찍는다. LLM이 수량을 옮겨 적다 틀릴 일을 없앤다.
인사말은 선택 기능이다. 켜면 LLM이 인사말과 끝맺음만 쓰고, 숫자가 들어가거나 너무 길면 빼고 보낸다.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .catalog import Catalog
from .domain import Category, Item, Supplier
from .llm import LLMClient, LLMError

WEEKDAYS = "월화수목금토일"
MAX_PHRASE_LEN = 80
PURCHASE_LIST = "구매목록"


@dataclass(frozen=True)
class Greeting:
    opening: str
    closing: str


@dataclass(frozen=True)
class OrderMessage:
    supplier: Supplier
    arrival: date
    lines: tuple[tuple[Item, int], ...]
    greeting: Greeting | None
    greeting_source: str  # "없음", "llm", "제외" (LLM 문구가 규칙에 걸림)

    @property
    def text(self) -> str:
        when = f"{self.arrival.month}월 {self.arrival.day}일({WEEKDAYS[self.arrival.weekday()]}) 입고분"
        if self.supplier.channel == PURCHASE_LIST:
            sections = []
            for category in Category:
                rows = [f"- {i.name} {q}{i.unit}" for i, q in self.lines if i.category == category]
                if rows:
                    sections.append(f"{category}\n" + "\n".join(rows))
            return f"[{self.supplier.name} 구매 목록] {when}\n\n" + "\n\n".join(sections)

        body = [", ".join(f"{i.name} {q}{i.unit}" for i, q in self.lines)]
        if self.greeting:
            body = [self.greeting.opening, *body, self.greeting.closing]
        if self.supplier.channel == "전화":
            body = [f"[통화 메모] {self.supplier.name} · {when}", *body]
        return "\n".join(body)


class GreetingWriter(Protocol):
    def write(
        self, suppliers: list[tuple[Supplier, list[Item]]], arrival: date, notes: list[str]
    ) -> dict[str, Greeting]:
        """거래처 id별 인사말. 빠진 거래처는 인사말 없이 보낸다."""
        ...


def is_safe_phrase(text: str) -> bool:
    """LLM이 쓴 인사말에 숫자가 섞이면 주문 수량과 헷갈릴 수 있어 쓰지 않는다."""
    return bool(text.strip()) and len(text) <= MAX_PHRASE_LEN and not re.search(r"[0-9０-９]", text)


def build_messages(
    catalog: Catalog,
    arrival: date,
    orders: dict[str, int],
    notes: list[str] = (),
    writer: GreetingWriter | None = None,
) -> list[OrderMessage]:
    by_supplier: dict[str, list[tuple[Item, int]]] = {}
    for item in catalog.items:
        qty = orders.get(item.id, 0)
        if qty > 0:
            by_supplier.setdefault(item.supplier_id, []).append((item, qty))

    suppliers = [catalog.supplier(sid) for sid in by_supplier]
    written: dict[str, Greeting] = {}
    texted = [s for s in suppliers if s.channel != PURCHASE_LIST]
    if writer is not None and texted:
        written = writer.write([(s, [i for i, _ in by_supplier[s.id]]) for s in texted], arrival, list(notes))

    messages = []
    for s in suppliers:
        g = written.get(s.id)
        if g is None:
            greeting, source = None, "없음"
        elif is_safe_phrase(g.opening) and is_safe_phrase(g.closing):
            greeting, source = g, "llm"
        else:
            greeting, source = None, "제외"
        messages.append(OrderMessage(s, arrival, tuple(by_supplier[s.id]), greeting, source))
    return messages


GREETING_SYSTEM_PROMPT = """너는 과일·야채 매장 사장님을 대신해 거래처에 보낼 발주 문자의 인사말과 끝맺음을 쓴다.

- 주문 품목과 수량은 시스템이 따로 넣는다. 너는 인사말(opening)과 끝맺음(closing)만 쓴다.
- 숫자를 쓰지 않는다. 날짜, 수량, 가격 모두 쓰지 않는다.
- 각각 한 문장, 짧게 쓴다. 거래처 사장님께 쓰는 존댓말로 쓴다.
- 가게 메모에 명절 대목, 날씨처럼 거래처가 알면 좋은 일이 있으면 자연스럽게 한마디 덧붙인다 (예: 대목이라 좋은 물건으로 부탁드린다).
- 특별한 일이 없으면 평범하게 쓴다. 꾸미거나 과장하지 않는다.
- 전화 거래처는 통화할 때 사장님이 읽을 말투로 쓴다."""

GREETING_SCHEMA = {
    "type": "object",
    "properties": {
        "greetings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "supplier_id": {"type": "string"},
                    "opening": {"type": "string"},
                    "closing": {"type": "string"},
                },
                "required": ["supplier_id", "opening", "closing"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["greetings"],
    "additionalProperties": False,
}


class LLMGreetingWriter:
    """하루치 거래처 인사말을 LLM 한 번 호출로 쓴다."""

    def __init__(self, llm: LLMClient, store_name: str):
        self.llm = llm
        self.store_name = store_name

    def write(
        self, suppliers: list[tuple[Supplier, list[Item]]], arrival: date, notes: list[str]
    ) -> dict[str, Greeting]:
        lines = [
            f"가게 이름: {self.store_name}",
            f"입고일: {arrival:%m-%d}({WEEKDAYS[arrival.weekday()]})",
            "가게 메모: " + (" / ".join(notes) if notes else "없음"),
            "",
            "거래처 (id | 이름 | 연락 방식 | 이번 주문 품목):",
        ]
        for s, items in suppliers:
            lines.append(f"{s.id} | {s.name} | {s.channel} | {', '.join(i.name for i in items)}")
        try:
            resp = self.llm.generate_json(
                GREETING_SYSTEM_PROMPT, "\n".join(lines), GREETING_SCHEMA, purpose=f"greeting {arrival}"
            )
        except LLMError:
            return {}
        return {g["supplier_id"]: Greeting(g["opening"].strip(), g["closing"].strip()) for g in resp["greetings"]}
