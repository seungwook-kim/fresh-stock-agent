"""마감 재고표 사진 인식.

1. Claude가 사진을 보고 줄마다 적힌 이름·수량을 그대로 옮기고, 품목 목록에서 맞는 품목을 고른다.
2. 수량 글자("반", "1반", "3박스", "x")를 숫자로 바꾸는 건 코드가 한다.
3. 코드가 이상한 줄에 표시를 붙인다. 확인 화면은 표시된 줄만 노란색으로 보여준다.

실제 가게 재고표는 줄마다 [품목 | 남은 수량 | 사입 수량 | 사입 가격] 네 칸이 왼쪽·오른쪽 두 묶음이다.
purchases=True면 이 형식으로 알려주고 사입 수량·가격도 함께 읽는다.
"""

import re
from dataclasses import dataclass, field

from .catalog import Catalog
from .llm import ImageInput, LLMClient

UNIT_WORDS = ("박스", "box", "개", "판", "봉", "망", "팩", "단", "모", "kg", "b")
# 손글씨 0은 영문 o나 한글 ㅇ으로 읽히기 쉽다. 수량 칸에서는 0으로 본다
# 모델은 손글씨 0을 동그라미(○), -를 긴 줄표(–)로 옮기기도 한다 (실제 가게 형식 평가에서 42줄 중 41줄)
ZERO_WORDS = {"x", "X", "×", "-", "–", "—", "−", "0", "o", "O", "○", "◯", "〇", "ㅇ", "없음", "품절"}
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

PURCHASE_LAYOUT_PROMPT = """
- 이 재고표는 줄마다 [품목 | 남은 수량 | 사입 수량 | 사입 가격] 네 칸이고, 이 네 칸 묶음이 왼쪽·오른쪽에 하나씩 있다. 왼쪽 묶음을 위에서 아래로 다 읽은 뒤 오른쪽 묶음을 읽는다. 오른쪽 묶음의 "수량"도 남은 수량이다.
- raw_qty에는 남은 수량 칸, raw_buy_qty에는 사입 수량 칸, raw_buy_price에는 사입 가격 칸을 적힌 그대로 옮긴다 (쉼표도 그대로). 빈 칸은 빈 문자열로 둔다. 다른 칸의 숫자를 끌어오지 않는다."""

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



def _with_purchase_fields(schema: dict) -> dict:
    row = schema["properties"]["rows"]["items"]
    props = {**row["properties"], "raw_buy_qty": {"type": "string"}, "raw_buy_price": {"type": "string"}}
    items = {**row, "properties": props, "required": [*row["required"], "raw_buy_qty", "raw_buy_price"]}
    return {**schema, "properties": {"rows": {"type": "array", "items": items}}}


READ_PURCHASE_SCHEMA = _with_purchase_fields(READ_SCHEMA)
# 사입 가격이 평소 매입가와 이만큼 넘게 차이 나면 자릿수를 잘못 읽었을 수 있다 (42000 → 4200)
PRICE_RATIO = 2.5


@dataclass
class RecognizedRow:
    raw_name: str
    raw_qty: str
    item_id: str | None
    qty: float | None
    corrected: bool
    flags: list[str] = field(default_factory=list)  # 사람이 확인해야 하는 이유
    # 사입 칸 (purchases=True로 읽었을 때만). 빈 칸이면 None
    raw_buy_qty: str = ""
    raw_buy_price: str = ""
    buy_qty: float | None = None
    buy_price: int | None = None

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


def parse_price(raw: str) -> int | None:
    """사입 가격 글자를 원 단위 정수로 바꾼다. "42000", "42,000", "4.2만", "4만2천", "4만2천5백", "42천", 만원.
    만 뒤에 숫자만 붙으면 만 아래 자리로 읽는다: "3만8" = 38,000, "3만85" = "3만8500" = 38,500. 해석할 수 없으면 None."""
    text = raw.strip().replace(",", "").replace(" ", "").removesuffix("원")
    if text.startswith("만"):
        text = "1" + text
    if re.fullmatch(r"\d+", text):
        return int(text)
    m = re.fullmatch(r"(\d+(?:\.\d+)?)만(?:(\d)천)?(?:(\d)백)?", text)
    if m:
        return round(float(m.group(1)) * 10000) + int(m.group(2) or 0) * 1000 + int(m.group(3) or 0) * 100
    m = re.fullmatch(r"(\d+)만(\d{1,4})", text)
    if m:
        return int(m.group(1)) * 10000 + int(m.group(2).ljust(4, "0"))
    m = re.fullmatch(r"(\d+)천", text)
    return int(m.group(1)) * 1000 if m else None


def validate_purchases(
    rows: list[RecognizedRow],
    catalog: Catalog,
    ordered: dict[str, float] | None = None,
    ref_prices: dict[str, int] | None = None,
) -> None:
    """사입 칸이 이상한 줄에 확인 표시를 붙인다.

    ordered: 그날 들어오기로 한 발주 수량. 주문했는데 사입 칸이 비어 있으면 표시한다 (못 읽었거나 안 들어온 것).
    ref_prices: 품목별 최근 사입 가격. 없으면 품목 목록의 매입가와 비교한다.
    """
    ordered, ref_prices = ordered or {}, ref_prices or {}
    for row in rows:
        if row.raw_buy_qty.strip() and row.buy_qty is None:
            row.flags.append("사입 수량을 해석하지 못함")
        if row.raw_buy_price.strip() and row.buy_price is None:
            row.flags.append("사입 가격을 해석하지 못함")
        if bool(row.raw_buy_qty.strip()) != bool(row.raw_buy_price.strip()):
            row.flags.append("사입 수량·가격 중 하나만 있음")
        if row.item_id and row.buy_price:
            ref = ref_prices.get(row.item_id)
            label, cost = ("최근 사입가", ref) if ref else ("평소 매입가", catalog.item(row.item_id).cost)
            if cost and not cost / PRICE_RATIO <= row.buy_price <= cost * PRICE_RATIO:
                row.flags.append(f"{label} {cost:,}원과 크게 다름")
        if row.item_id and ordered.get(row.item_id, 0) > 0 and row.buy_qty is None and not row.raw_buy_qty.strip():
            item = catalog.item(row.item_id)
            row.flags.append(f"주문 {ordered[row.item_id]:g}{item.unit}인데 사입 칸이 비어 있음")


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
    purchases: bool = False,
    prev_closing: dict[str, float] | None = None,
    ordered: dict[str, float] | None = None,
    ref_prices: dict[str, int] | None = None,
) -> list[RecognizedRow]:
    """재고표 사진 한 장을 읽는다.

    max_possible·expected는 validate 참고. aliases는 이 매장에서 사람이 확인한 "적힌 이름 → 품목 id"다.
    Claude에게 알려주고, 결과도 코드로 대조한다. purchases면 사입 수량·가격 칸이 있는 형식으로 읽는다.
    사입 칸은 그날 실제로 들어온 양이라, prev_closing(어제 재고)을 주면 "어제 재고 + 사입 수량"으로
    불가능한 값을 검사한다. ordered·ref_prices는 validate_purchases 참고.
    """
    aliases = aliases or {}
    known = {i.id for i in catalog.items}
    items = "\n".join(f"{i.id} | {i.name} | {i.unit}" for i in catalog.items)
    prompt = f"품목 목록 (id | 이름 | 단위):\n{items}\n\n"
    if aliases:
        pairs = ", ".join(f"{raw}={catalog.item(i).name}({i})" for raw, i in sorted(aliases.items()) if i in known)
        prompt += f"이 매장에서 쓰는 줄임말 (사람이 확인함): {pairs}\n\n"
    prompt += "사진 속 재고표를 읽어라."
    system, schema = (READ_SYSTEM_PROMPT + PURCHASE_LAYOUT_PROMPT, READ_PURCHASE_SCHEMA) if purchases else (
        READ_SYSTEM_PROMPT, READ_SCHEMA
    )
    resp = llm.generate_json(system, prompt, schema, purpose=purpose, images=(image,))
    rows = []
    for r in resp["rows"]:
        row = RecognizedRow(
            raw_name=r["raw_name"],
            raw_qty=r["raw_qty"],
            item_id=r["item_id"] or None,
            qty=parse_qty(r["raw_qty"]),
            corrected=r["corrected"],
        )
        if purchases:
            row.raw_buy_qty, row.raw_buy_price = r.get("raw_buy_qty", ""), r.get("raw_buy_price", "")
            row.buy_qty = parse_qty(row.raw_buy_qty) if row.raw_buy_qty.strip() else None
            row.buy_price = parse_price(row.raw_buy_price) if row.raw_buy_price.strip() else None
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
    if purchases and prev_closing is not None and max_possible is not None:
        # 발주 수량이 아니라 종이에 적힌 사입 수량이 그날 입고다
        max_possible, expected = dict(max_possible), dict(expected) if expected is not None else None
        for row in rows:
            if row.item_id in known:
                limit = prev_closing.get(row.item_id, 0) + (row.buy_qty or 0)
                if expected is not None:
                    expected[row.item_id] = expected.get(row.item_id, 0) + limit - max_possible.get(row.item_id, 0)
                max_possible[row.item_id] = limit
    validate(rows, catalog, max_possible, expected)
    if purchases:
        validate_purchases(rows, catalog, ordered, ref_prices)
    return rows
