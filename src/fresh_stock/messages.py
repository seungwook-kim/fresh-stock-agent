"""거래처별 발주 문자 작성.

주문 목록(품목·수량)은 코드가 확정된 발주에서 그대로 찍는다. LLM이 수량을 옮겨 적다 틀릴 일을 없앤다.
LLM은 거래처별 인사말과 끝맺음만 쓴다. 숫자가 들어가거나 너무 길면 기본 문구로 바꾼다.
"""

import re
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from .catalog import Catalog
from .domain import Item, Supplier
from .llm import LLMClient, LLMError

WEEKDAYS = "월화수목금토일"
MAX_PHRASE_LEN = 80


@dataclass(frozen=True)
class Greeting:
    opening: str
    closing: str


@dataclass(frozen=True)
class OrderMessage:
    supplier: Supplier
    arrival: date
    lines: tuple[tuple[Item, int], ...]
    greeting: Greeting
    greeting_source: str  # "llm" 또는 "기본" (LLM 문구가 규칙에 걸려 기본 문구를 쓴 경우 포함)

    @property
    def text(self) -> str:
        items = "\n".join(f"- {item.name} {qty}{item.unit}" for item, qty in self.lines)
        when = f"{self.arrival.month}월 {self.arrival.day}일({WEEKDAYS[self.arrival.weekday()]}) 입고분"
        if self.supplier.channel == "전화":
            return f"[통화 메모] {self.supplier.name} · {when}\n{self.greeting.opening}\n{items}\n{self.greeting.closing}"
        return f"{self.greeting.opening}\n{when} 주문드립니다.\n\n{items}\n\n{self.greeting.closing}"


class GreetingWriter(Protocol):
    def write(
        self, suppliers: list[tuple[Supplier, list[Item]]], arrival: date, notes: list[str]
    ) -> dict[str, Greeting]:
        """거래처 id별 인사말. 빠진 거래처는 기본 문구를 쓴다."""
        ...


def default_greeting(store_name: str) -> Greeting:
    return Greeting(opening=f"사장님 안녕하세요, {store_name}입니다.", closing="감사합니다.")


def is_safe_phrase(text: str) -> bool:
    """LLM이 쓴 인사말에 숫자가 섞이면 주문 수량과 헷갈릴 수 있어 쓰지 않는다."""
    return bool(text.strip()) and len(text) <= MAX_PHRASE_LEN and not re.search(r"[0-9０-９]", text)


def build_messages(
    catalog: Catalog,
    arrival: date,
    orders: dict[str, int],
    store_name: str,
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
    if writer is not None and suppliers:
        written = writer.write([(s, [i for i, _ in by_supplier[s.id]]) for s in suppliers], arrival, list(notes))

    fallback = default_greeting(store_name)
    messages = []
    for s in suppliers:
        g = written.get(s.id)
        if g is not None and is_safe_phrase(g.opening) and is_safe_phrase(g.closing):
            greeting, source = g, "llm"
        else:
            greeting, source = fallback, "기본"
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
