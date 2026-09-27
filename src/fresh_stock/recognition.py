"""마감 재고표 사진 인식.

1. Claude가 사진을 보고 줄마다 적힌 이름·수량을 그대로 옮기고, 품목 목록에서 맞는 품목을 고른다.
2. 수량 글자("반", "1반", "3박스", "x")를 숫자로 바꾸는 건 코드가 한다.
3. 코드가 이상한 줄에 표시를 붙인다. 확인 화면은 표시된 줄만 노란색으로 보여준다.
"""

import re
from dataclasses import dataclass, field

from .catalog import Catalog
from .llm import ImageInput, LLMClient

UNIT_WORDS = ("박스", "box", "개", "판", "봉", "팩", "단", "모", "b")
# 손글씨 0은 영문 o나 한글 ㅇ으로 읽히기 쉽다. 수량 칸에서는 0으로 본다
ZERO_WORDS = {"x", "X", "-", "0", "o", "O", "ㅇ", "없음", "품절"}
KOREAN_NUMBERS = {"한": 1, "두": 2, "세": 3, "네": 4}
# 예상 재고보다 이만큼 적으면 "11봉"을 "1봉"으로 읽은 것처럼 적게 읽었을 수 있다
UNDER_READ_RATIO = 0.25
UNDER_READ_GAP = 4

READ_SYSTEM_PROMPT = """너는 과일·야채 매장의 손글씨 마감 재고표 사진을 읽는다.

- 품목 줄마다 적힌 이름을 raw_name에, 적힌 수량을 raw_qty에 **적힌 그대로** 옮긴다. 계산하거나 바꾸지 않는다 ("반", "1반", "3박스", "x"도 그대로).
- 수량을 줄 긋고 고쳐 쓴 줄은 corrected를 true로 하고, 줄 그어 지운 글자는 빼고 옆에 새로 쓴 최종 값만 raw_qty에 쓴다. 지운 자국을 +, & 같은 기호로 옮기지 않는다.
- item_id는 아래 품목 목록에서 가장 알맞은 id다. 줄임말도 있다 (예: 방토 = 방울토마토). 목록에 없으면 빈 문자열로 둔다.
- 글씨가 흐리거나 이름·수량이 애매하면 confident를 false로 한다.
- 제목·날짜 줄은 넣지 않는다. 사진에 있는 품목 줄을 빠짐없이 넣는다."""

READ_SCHEMA = {
    "type": "object",
    "properties": {
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "raw_name": {"type": "string"},
                    "raw_qty": {"type": "string"},
                    "item_id": {"type": "string"},
                    "corrected": {"type": "boolean"},
                    "confident": {"type": "boolean"},
                },
                "required": ["raw_name", "raw_qty", "item_id", "corrected", "confident"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["rows"],
    "additionalProperties": False,
}


@dataclass
class RecognizedRow:
    raw_name: str
    raw_qty: str
    item_id: str | None
    qty: float | None
    corrected: bool
    flags: list[str] = field(default_factory=list)  # 사람이 확인해야 하는 이유

    @property
    def needs_review(self) -> bool:
        return bool(self.flags)


def parse_qty(raw: str) -> float | None:
    """재고표 수량 글자를 숫자로 바꾼다. 해석할 수 없으면 None."""
    text = raw.strip().replace(" ", "")
    if text in ZERO_WORDS:
        return 0.0
    for unit in UNIT_WORDS:
        if text.endswith(unit) and len(text) > len(unit):
            text = text[: -len(unit)]
            break
    if text in ZERO_WORDS:
        return 0.0
    if text == "반":
        return 0.5
    m = re.fullmatch(r"(\d+|[한두세네])(?:박스)?반", text)
    if m:
        n = m.group(1)
        return (KOREAN_NUMBERS[n] if n in KOREAN_NUMBERS else int(n)) + 0.5
    if text in KOREAN_NUMBERS:
        return float(KOREAN_NUMBERS[text])
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    return None


def validate(
    rows: list[RecognizedRow],
    catalog: Catalog,
    max_possible: dict[str, float] | None = None,
    expected: dict[str, float] | None = None,
) -> None:
    """이상한 줄에 확인 표시를 붙인다.

    max_possible: 품목별 어제 재고 + 오늘 입고. 이보다 많으면 불가능한 값이다.
    expected: 품목별 예상 재고 (max_possible - 최근 평균 판매). 이보다 크게 적으면 적게 읽었을 수 있다.
    """
    known = {i.id for i in catalog.items}
    seen: dict[str, int] = {}
    for row in rows:
        if row.item_id not in known:
            row.item_id = None
            row.flags.append("품목을 찾지 못함")
        if row.qty is None:
            row.flags.append("수량을 해석하지 못함")
        if row.item_id and row.qty is not None:
            item = catalog.item(row.item_id)
            if (row.qty / item.count_step) % 1:
                row.flags.append(f"{item.unit} 단위로 맞지 않는 수량")
            if max_possible is not None and row.qty > max_possible.get(row.item_id, 0) + item.count_step:
                row.flags.append(f"어제 재고와 오늘 입고를 합친 {max_possible.get(row.item_id, 0):g}보다 많음")
            if expected is not None:
                e = expected.get(row.item_id, 0)
                if e - row.qty >= UNDER_READ_GAP and row.qty <= e * UNDER_READ_RATIO:
                    row.flags.append(f"예상 재고 {e:.0f}보다 크게 적음")
        if row.item_id:
            seen[row.item_id] = seen.get(row.item_id, 0) + 1
    for row in rows:
        if row.item_id and seen[row.item_id] > 1:
            row.flags.append("같은 품목이 두 번 나옴")


def recognize_sheet(
    llm: LLMClient,
    image: ImageInput,
    catalog: Catalog,
    max_possible: dict[str, float] | None = None,
    expected: dict[str, float] | None = None,
    purpose: str = "sheet",
    aliases: dict[str, str] | None = None,
) -> list[RecognizedRow]:
    """재고표 사진 한 장을 읽는다.

    max_possible·expected는 validate 참고. aliases는 이 매장에서 사람이 확인한 "적힌 이름 → 품목 id"다.
    Claude에게 알려주고, 결과도 코드로 대조한다.
    """
    aliases = aliases or {}
    known = {i.id for i in catalog.items}
    items = "\n".join(f"{i.id} | {i.name} | {i.unit}" for i in catalog.items)
    prompt = f"품목 목록 (id | 이름 | 단위):\n{items}\n\n"
    if aliases:
        pairs = ", ".join(f"{raw}={catalog.item(i).name}({i})" for raw, i in sorted(aliases.items()) if i in known)
        prompt += f"이 매장에서 쓰는 줄임말 (사람이 확인함): {pairs}\n\n"
    prompt += "사진 속 재고표를 읽어라."
    resp = llm.generate_json(READ_SYSTEM_PROMPT, prompt, READ_SCHEMA, purpose=purpose, images=(image,))
    rows = []
    for r in resp["rows"]:
        row = RecognizedRow(
            raw_name=r["raw_name"],
            raw_qty=r["raw_qty"],
            item_id=r["item_id"] or None,
            qty=parse_qty(r["raw_qty"]),
            corrected=r["corrected"],
        )
        if not r["confident"]:
            row.flags.append("글씨가 애매함")
        if r["corrected"]:
            row.flags.append("고쳐 쓴 칸")
        learned = aliases.get(row.raw_name.strip())
        if learned in known:
            if row.item_id not in known:
                row.item_id = learned  # 모델이 못 찾은 품목을 확인된 줄임말로 채운다
            elif row.item_id != learned:
                row.flags.append(f"확인된 줄임말({catalog.item(learned).name})과 다름")
        rows.append(row)
    validate(rows, catalog, max_possible, expected)
    return rows
