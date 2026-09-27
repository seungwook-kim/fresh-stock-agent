import re
from datetime import date

from fresh_stock.catalog import load_catalog
from fresh_stock.llm import LLMError
from fresh_stock.messages import Greeting, LLMGreetingWriter, build_messages, is_safe_phrase

catalog = load_catalog()
ARRIVAL = date(2026, 9, 22)
ORDERS = {"tomato": 8, "apple": 3, "tofu": 70, "bean_sprout": 0, "king_oyster": 18}


class FixedWriter:
    def __init__(self, greetings):
        self.greetings = greetings

    def write(self, suppliers, arrival, notes):
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
    msgs = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게"))
    assert set(msgs) == {"garak", "tofu", "mushroom"}
    assert [(i.id, q) for i, q in msgs["tofu"].lines] == [("tofu", 70)]  # 콩나물 0은 빠진다


def test_quantities_in_text_match_orders_exactly():
    for m in build_messages(catalog, ARRIVAL, ORDERS, "우리가게"):
        found = dict(re.findall(r"^- (\S+) (\d+)", m.text, flags=re.M))
        assert found == {i.name: str(q) for i, q in m.lines}


def test_text_message_format():
    garak = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게"))["garak"]
    assert garak.text.startswith("사장님 안녕하세요, 우리가게입니다.")
    assert "9월 22일(화) 입고분 주문드립니다." in garak.text
    assert "- 토마토 8박스" in garak.text


def test_phone_supplier_gets_call_memo():
    mushroom = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게"))["mushroom"]
    assert mushroom.text.startswith("[통화 메모] 버섯 거래처")
    assert "- 새송이 18봉" in mushroom.text


def test_llm_greeting_is_used_when_safe():
    writer = FixedWriter({"garak": Greeting("형님, 추석 대목이라 좋은 물건 부탁드려요.", "늘 고맙습니다.")})
    garak = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게", writer=writer))["garak"]
    assert garak.greeting_source == "llm"
    assert garak.text.startswith("형님, 추석 대목이라")


def test_llm_greeting_with_digits_falls_back():
    writer = FixedWriter({"garak": Greeting("토마토 80박스 부탁드려요.", "감사합니다.")})
    garak = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게", writer=writer))["garak"]
    assert garak.greeting_source == "기본"
    assert "80" not in garak.text


def test_phrase_rules():
    assert is_safe_phrase("대목이라 좋은 걸로 부탁드립니다.")
    assert not is_safe_phrase("내일 3시까지 부탁드립니다.")
    assert not is_safe_phrase("")
    assert not is_safe_phrase("가" * 100)


def test_llm_writer_parses_response_and_passes_notes():
    llm = ScriptedLLM({"greetings": [{"supplier_id": "tofu", "opening": " 안녕하세요 ", "closing": "감사합니다."}]})
    writer = LLMGreetingWriter(llm, "우리가게")
    msgs = _by_supplier(build_messages(catalog, ARRIVAL, ORDERS, "우리가게", ["추석 3일 전 (명절 대목)"], writer))
    assert msgs["tofu"].greeting == Greeting("안녕하세요", "감사합니다.")
    assert msgs["garak"].greeting_source == "기본"  # 응답에 빠진 거래처는 기본 문구
    assert "추석 3일 전" in llm.calls[0]


def test_llm_writer_failure_uses_default():
    writer = LLMGreetingWriter(ScriptedLLM(LLMError("timeout")), "우리가게")
    msgs = build_messages(catalog, ARRIVAL, ORDERS, "우리가게", writer=writer)
    assert all(m.greeting_source == "기본" for m in msgs)
