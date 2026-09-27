import re
from datetime import date

from fresh_stock.catalog import load_catalog
from fresh_stock.llm import LLMError
from fresh_stock.messages import Greeting, LLMGreetingWriter, build_messages, is_safe_phrase

catalog = load_catalog()
ARRIVAL = date(2026, 9, 22)
ORDERS = {
    "tomato": 8,
    "apple": 3,
    "onion": 5,
    "square_tofu": 2,
    "hand_tofu": 2,
    "soft_tofu": 1,
    "bean_sprout": 0,
    "king_oyster": 18,
}


class FixedWriter:
    def __init__(self, greetings):
        self.greetings = greetings
        self.suppliers = None

    def write(self, suppliers, arrival, notes):
        self.suppliers = [s.id for s, _ in suppliers]
        return self.greetings


class ScriptedLLM:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose=""):
        self.calls.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _by_supplier(messages):
    return {m.supplier.id: m for m in messages}


def test_orders_are_grouped_by_supplier_and_zero_skipped():
    msgs = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS))
    assert set(msgs) == {"garak", "tofu", "mushroom"}
    assert [i.id for i, _ in msgs["tofu"].lines] == ["square_tofu", "hand_tofu", "soft_tofu"]  # 콩나물 0은 빠진다


def test_quantities_in_text_match_orders_exactly():
    for m in build_messages(catalog, ARRIVAL, ORDERS):
        for item, qty in m.lines:
            assert f"{item.name} {qty}{item.unit}" in m.text
        assert len(re.findall(r"\d+(?:박스|판|봉|개|팩)", m.text)) == len(m.lines)


def test_text_supplier_gets_one_line_like_real_messages():
    tofu = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS))["tofu"]
    assert tofu.text == "사각두부 2판, 손두부 2판, 순두부 1판"


def test_garak_gets_purchase_list_by_category():
    garak = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS))["garak"]
    assert garak.text.startswith("[가락시장 (지인 경매 구매) 구매 목록] 9월 22일(화) 입고분")
    assert garak.text.index("과일\n- 사과 3박스") < garak.text.index("야채\n- 양파 5박스")


def test_phone_supplier_gets_call_memo():
    mushroom = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS))["mushroom"]
    assert mushroom.text == "[통화 메모] 버섯 거래처 · 9월 22일(화) 입고분\n새송이 18봉"


def test_llm_greeting_wraps_text_message():
    writer = FixedWriter({"tofu": Greeting("사장님, 대목이라 넉넉히 부탁드려요.", "늘 고맙습니다.")})
    msgs = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, writer=writer))
    assert msgs["tofu"].greeting_source == "llm"
    assert msgs["tofu"].text.splitlines() == [
        "사장님, 대목이라 넉넉히 부탁드려요.",
        "사각두부 2판, 손두부 2판, 순두부 1판",
        "늘 고맙습니다.",
    ]
    assert "garak" not in writer.suppliers  # 가락시장 구매 목록에는 인사말을 쓰지 않는다


def test_llm_greeting_with_digits_is_dropped():
    writer = FixedWriter({"tofu": Greeting("두부 20판 부탁드려요.", "감사합니다.")})
    tofu = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, writer=writer))["tofu"]
    assert tofu.greeting_source == "제외"
    assert tofu.text == "사각두부 2판, 손두부 2판, 순두부 1판"


def test_phrase_rules():
    assert is_safe_phrase("대목이라 좋은 걸로 부탁드립니다.")
    assert not is_safe_phrase("내일 3시까지 부탁드립니다.")
    assert not is_safe_phrase("")
    assert not is_safe_phrase("가" * 100)


def test_llm_writer_parses_response_and_passes_notes():
    llm = ScriptedLLM({"greetings": [{"supplier_id": "tofu", "opening": " 안녕하세요 ", "closing": "감사합니다."}]})
    writer = LLMGreetingWriter(llm, "우리가게")
    msgs = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, ["추석 3일 전 (명절 대목)"], writer))
    assert msgs["tofu"].greeting == Greeting("안녕하세요", "감사합니다.")
    assert msgs["mushroom"].greeting_source == "없음"  # 응답에 빠진 거래처는 인사말 없이
    assert "추석 3일 전" in llm.calls[0]


def test_llm_writer_failure_sends_without_greeting():
    writer = LLMGreetingWriter(ScriptedLLM(LLMError("timeout")), "우리가게")
    msgs = build_messages(catalog, ARRIVAL, ORDERS, writer=writer)
    assert all(m.greeting is None for m in msgs)
