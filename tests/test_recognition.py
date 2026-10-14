from datetime import date

import pytest

from fresh_stock.catalog import load_catalog, sample_store_catalog
from fresh_stock.domain import UNITS
from fresh_stock.llm import ImageInput
from fresh_stock.recognition import (
    RecognizedRow,
    parse_price,
    parse_qty,
    recognize_sheet,
    validate,
)
from fresh_stock.sheet_eval import score_sheet, summarize
from fresh_stock.sheets import (
    FONT_DIR,
    FONTS,
    PRINT_FONT,
    WRITERS,
    SheetEntry,
    SheetTruth,
    render_excel_sheet,
    render_sheet,
    render_store_sheet,
    store_entries,
)

catalog = load_catalog()


@pytest.mark.parametrize(
    "raw, qty",
    [
        ("3", 3.0),
        ("1.5", 1.5),
        ("반", 0.5),
        ("4반", 4.5),
        ("1반박스", 1.5),
        ("한박스반", 1.5),
        ("3박스", 3.0),
        ("10봉", 10.0),
        ("2판", 2.0),
        ("x", 0.0),
        ("-", 0.0),
        ("0", 0.0),
        ("o", 0.0),
        (" 2 ", 2.0),
        ("세", 3.0),
        ("??", None),
        ("3.5.1", None),
    ],
)
def test_parse_qty(raw, qty):
    assert parse_qty(raw) == qty


def _row(item_id, qty, raw_qty="1"):
    return RecognizedRow(raw_name="?", raw_qty=raw_qty, item_id=item_id, qty=qty, corrected=False)


def test_validate_flags_problems():
    rows = [
        _row("tomato", 2.5),
        _row("durian", 1),
        _row("apple", None, raw_qty="??"),
        _row("tofu_typo", 1),
        _row("king_oyster", 1.5),  # 봉 단위인데 0.5가 붙음
        _row("pear", 30),  # 어제 재고 + 오늘 입고보다 많음
        _row("kiwi", 1),
        _row("kiwi", 2),
    ]
    validate(rows, catalog, max_possible={"tomato": 5, "pear": 3, "king_oyster": 20, "kiwi": 5, "apple": 2})
    flags = [r.flags for r in rows]
    assert flags[0] == []
    assert flags[1] == ["품목을 찾지 못함"] and rows[1].item_id is None
    assert "수량을 해석하지 못함" in flags[2]
    assert "봉 단위로 맞지 않는 수량" in flags[4]
    assert any("보다 많음" in f for f in flags[5])
    assert "같은 품목이 두 번 나옴" in flags[6] and "같은 품목이 두 번 나옴" in flags[7]


def test_validate_flags_under_read():
    rows = [_row("shiitake", 1), _row("tomato", 1), _row("kiwi", 5)]
    validate(rows, catalog, expected={"shiitake": 9.5, "tomato": 2.0, "kiwi": 6.0})
    assert rows[0].flags == ["예상 재고 10보다 크게 적음"]  # 11봉을 1봉으로 읽은 경우
    assert rows[1].flags == []  # 차이가 작으면 표시하지 않는다
    assert rows[2].flags == []


class FakeVisionLLM:
    def __init__(self, rows):
        self.rows = rows
        self.images = None
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.images = images
        self.prompt = prompt
        return {"rows": self.rows}


def _read(raw_name, raw_qty, item_id, corrected=False, confident=True):
    return {"raw_name": raw_name, "raw_qty": raw_qty, "item_id": item_id, "corrected": corrected, "confident": confident}


def test_recognize_sheet_parses_and_flags():
    llm = FakeVisionLLM(
        [
            _read("토마", "4반", "tomato"),
            _read("방토", "1", "cherry_tomato", confident=False),
            _read("귤", "3박스", "tangerine", corrected=True),
        ]
    )
    rows = recognize_sheet(llm, ImageInput(b"jpg"), catalog)
    assert [(r.item_id, r.qty) for r in rows] == [("tomato", 4.5), ("cherry_tomato", 1.0), ("tangerine", 3.0)]
    assert rows[0].flags == []
    assert rows[1].flags == ["글씨가 애매함"]
    assert rows[2].flags == ["고쳐 쓴 칸"]
    assert llm.images[0].data == b"jpg"


TRUTH = SheetTruth(
    day=date(2026, 7, 21),
    entries=(
        SheetEntry("tomato", 4.5, "토마토", "4반", False),
        SheetEntry("apple", 0.5, "사과", "반", False),
        SheetEntry("kiwi", 1.0, "키위", "1", False),
        SheetEntry("pear", 2.0, "배", "2", True),
    ),
    omitted_zero=(),
)


def test_score_sheet():
    rows = [
        _row("tomato", 4.5),  # 맞음
        _row("apple", 1.5),  # 수량 틀림, 표시 없음
        _row("pear", 3.0),  # 수량 틀림, 표시 있음
        _row("lemon", 1.0),  # 적히지 않은 품목
    ]
    rows[2].flags.append("고쳐 쓴 칸")
    s = score_sheet(rows, TRUTH)
    assert (s.written, s.correct, s.wrong_qty, s.extra, s.missed) == (4, 1, 2, 1, 1)
    assert s.errors_flagged == 1
    assert s.silent_errors == 3  # 사과, 레몬, 누락된 키위
    summary = summarize([s])
    assert summary["줄 정확도"] == 0.25


@pytest.mark.skipif(not all((FONT_DIR / f).exists() for f in FONTS), reason="손글씨 폰트가 없음")
def test_render_sheet_is_deterministic():
    closing = {"tomato": 4.5, "apple": 0.5, "kiwi": 0.0}
    a, truth = render_sheet(catalog, date(2026, 7, 21), closing, seed=1)
    b, _ = render_sheet(catalog, date(2026, 7, 21), closing, seed=1)
    assert a == b and a[:2] == b"\xff\xd8"  # JPEG
    assert truth.written["tomato"] == 4.5
    assert all(parse_qty(e.raw_qty) == e.qty for e in truth.entries)


def test_learned_aliases_fill_and_check_items():
    llm = FakeVisionLLM(
        [
            _read("샤머", "1", ""),  # 모델이 못 찾은 줄임말
            _read("방토", "2", "tomato"),  # 확인된 줄임말(방울토마토)과 다르게 매칭
            _read("사과", "3", "apple"),
        ]
    )
    rows = recognize_sheet(
        llm, ImageInput(b"jpg"), catalog, aliases={"샤머": "shine_muscat", "방토": "cherry_tomato"}
    )
    assert "방토=방울토마토(cherry_tomato)" in llm.prompt
    assert rows[0].item_id == "shine_muscat" and rows[0].flags == []
    assert rows[1].item_id == "tomato" and rows[1].flags == ["확인된 줄임말(방울토마토)과 다름"]
    assert rows[2].flags == []


# 실제 가게 재고표 형식 (품목 | 남은 수량 | 사입 수량 | 사입 가격) ------------------------------------------

store_catalog = sample_store_catalog()


@pytest.mark.parametrize(
    "raw, price",
    [("42000", 42000), ("42,000", 42000), ("4.2만", 42000), ("4만2천", 42000), ("42천", 42000),
     ("6,900원", 6900), ("4만", 40000), ("abc", None), ("", None)],
)  # fmt: skip
def test_parse_price(raw, price):
    assert parse_price(raw) == price


def test_sample_store_catalog_fills_the_sheet():
    assert len(store_catalog.items) == 60  # 30줄 × 두 묶음
    assert {i.name for i in store_catalog.items} >= {"왕란", "특란", "메추리알", "깐메추리알", "국물멸치", "사각두부"}
    assert all(i.unit in UNITS and i.cost > 0 for i in store_catalog.items)


def _read_buy(raw_name, raw_qty, item_id, buy_qty="", buy_price="", confident=True):
    return {**_read(raw_name, raw_qty, item_id, confident=confident), "raw_buy_qty": buy_qty, "raw_buy_price": buy_price}


def test_recognize_store_sheet_reads_purchases():
    llm = FakeVisionLLM(
        [
            _read_buy("사과", "3", "apple", "5", "42,700"),
            _read_buy("왕란", "2", "king_egg", "19", "680"),  # 6,800원을 680으로 읽음: 평소 매입가와 크게 다름
            _read_buy("배", "1", "pear", "2", ""),  # 가격 칸이 비어 있음
            _read_buy("양파", "4", "onion", "1망", "15000"),
            _read_buy("귤", "x", "tangerine"),  # 오늘 안 삼
        ]
    )
    rows = recognize_sheet(llm, ImageInput(b"jpg"), store_catalog, purchases=True)
    assert [(r.qty, r.buy_qty, r.buy_price) for r in rows] == [
        (3.0, 5.0, 42700), (2.0, 19.0, 680), (1.0, 2.0, None), (4.0, 1.0, 15000), (0.0, None, None)
    ]  # fmt: skip
    assert rows[0].flags == [] and rows[3].flags == [] and rows[4].flags == []
    assert rows[1].flags == ["평소 매입가 7,500원과 크게 다름"]
    assert rows[2].flags == ["사입 수량·가격 중 하나만 있음"]

    # 사입 칸까지 읽는 방식이 아니면 예전과 같은 요청을 보낸다
    plain = FakeVisionLLM([_read("사과", "3", "apple")])
    rows = recognize_sheet(plain, ImageInput(b"jpg"), store_catalog)
    assert rows[0].buy_qty is None and rows[0].raw_buy_price == ""


def test_score_store_sheet_counts_purchase_cells():
    truth = SheetTruth(
        date(2026, 9, 21),
        (
            SheetEntry("apple", 3.0, "사과", "3", False, 5.0, 42700, "5", "42700"),
            SheetEntry("pear", 1.0, "배", "1", False, 2.0, 38200, "2", "38,200"),
            SheetEntry("tangerine", 0.0, "귤", "x", False),
        ),
        (),
    )
    rows = [
        RecognizedRow("사과", "3", "apple", 3.0, False, raw_buy_qty="5", raw_buy_price="42700", buy_qty=5.0,
                      buy_price=42700),
        RecognizedRow("배", "1", "pear", 1.0, False, raw_buy_qty="2", raw_buy_price="3,820", buy_qty=2.0,
                      buy_price=3820),  # 가격 자릿수를 잘못 읽음, 표시 없음
        RecognizedRow("귤", "x", "tangerine", 0.0, False),
    ]  # fmt: skip
    s = score_sheet(rows, truth, purchases=True)
    assert (s.correct, s.buy_checked, s.buy_correct, s.buy_errors_flagged) == (3, 3, 2, 0)
    summary = summarize([s])
    assert summary["사입 칸 틀린 줄"] == 1 and summary["사입 칸 표시 없이 틀린 줄"] == 1


@pytest.mark.skipif(not all((FONT_DIR / f).exists() for f in FONTS), reason="손글씨 폰트가 없음")
def test_render_store_sheet_is_full_table():
    closing = {i.id: 2.0 for i in store_catalog.items}
    purchases = {"apple": (5, 42700), "king_egg": (19, 6800)}
    image, truth = render_store_sheet(store_catalog, date(2026, 9, 21), closing, purchases, seed=3)
    assert image[:2] == b"\xff\xd8" and len(truth.entries) == 60
    apple = next(e for e in truth.entries if e.item_id == "apple")
    assert (apple.buy_qty, apple.buy_price, parse_price(apple.raw_buy_price)) == (5, 42700, 42700)
    pear = next(e for e in truth.entries if e.item_id == "pear")
    assert (pear.buy_qty, pear.buy_price, pear.raw_buy_qty, pear.raw_buy_price) == (0.0, None, "", "")


@pytest.mark.skipif(
    not all((FONT_DIR / f).exists() for f in [*FONTS, PRINT_FONT[0]]), reason="손글씨·인쇄 폰트가 없음"
)
def test_render_excel_sheet_has_printed_names():
    closing = {i.id: 1.0 for i in store_catalog.items}
    image, truth = render_excel_sheet(store_catalog, date(2026, 9, 24), closing, {"apple": (4, 42700)}, seed=5)
    again, _ = render_excel_sheet(store_catalog, date(2026, 9, 24), closing, {"apple": (4, 42700)}, seed=5)
    assert image == again and image[:2] == b"\xff\xd8"
    assert len(truth.entries) == 60
    assert all(e.raw_name == store_catalog.item(e.item_id).name for e in truth.entries)  # 인쇄된 정식 이름


def test_writers_habits_are_readable():
    """쓰는 사람마다 0·반·쉼표를 다르게 적지만, 코드는 모두 같은 숫자로 읽어야 한다."""
    import random

    assert len(WRITERS) == 10 and len({w.font for w in WRITERS}) == 10
    closing = {i.id: q for i, q in zip(store_catalog.items, [0, 0.5, 4.5, 3, 12] * 12)}
    purchases = {i.id: (2.5, 42700) for i in store_catalog.items[:20]}
    for writer in WRITERS:
        entries = store_entries(store_catalog, closing, purchases, random.Random(1), printed_names=True, writer=writer)
        assert all(parse_qty(e.raw_qty) == e.qty for e in entries), writer.name
        assert all(parse_qty(e.raw_buy_qty) == e.buy_qty for e in entries if e.buy_qty), writer.name
        zeros = {e.raw_qty for e in entries if e.qty == 0}
        assert zeros <= set(writer.zero)
        halves = [e.raw_qty for e in entries if e.qty == 4.5]
        assert all(("반" in h) == (writer.half == "반") for h in halves), writer.name


@pytest.mark.skipif(
    not all((FONT_DIR / w.font).exists() for w in WRITERS),
    reason="손글씨 폰트가 없음",
)
def test_render_excel_sheet_per_writer():
    closing = {i.id: 1.0 for i in store_catalog.items}
    images = {render_excel_sheet(store_catalog, date(2026, 9, 24), closing, {"apple": (4, 42700)}, 5, w)[0] for w in WRITERS}
    assert len(images) == 10  # 사람마다 다른 글씨


def test_store_sheet_checks_against_orders_and_purchases():
    llm = FakeVisionLLM(
        [
            _read_buy("사과", "6", "apple", "5", "42,000"),  # 주문은 2박스였지만 5박스 사 옴: 어제 재고 1 + 5 = 6이라 가능
            _read_buy("배", "1", "pear"),  # 주문했는데 사입 칸이 비어 있음
            _read_buy("귤", "9", "tangerine"),  # 안 샀는데 어제 재고 2보다 많음
            _read_buy("왕란", "5", "king_egg", "20", "6900"),  # 최근 사입가 7,200원 근처라 괜찮음
        ]
    )
    rows = recognize_sheet(
        llm,
        ImageInput(b"jpg"),
        store_catalog,
        max_possible={"apple": 3, "pear": 3, "tangerine": 2, "king_egg": 25},
        expected={"apple": 2, "pear": 2, "tangerine": 1, "king_egg": 10},
        purchases=True,
        prev_closing={"apple": 1, "pear": 1, "tangerine": 2, "king_egg": 0},
        ordered={"apple": 2, "pear": 2, "king_egg": 20},
        ref_prices={"king_egg": 7200},
    )
    assert rows[0].flags == []
    assert rows[1].flags == ["주문 2박스인데 사입 칸이 비어 있음"]
    assert rows[2].flags == ["어제 재고와 오늘 입고를 합친 2보다 많음"]
    assert rows[3].flags == []

    far = recognize_sheet(
        FakeVisionLLM([_read_buy("왕란", "5", "king_egg", "20", "690")]), ImageInput(b"jpg"), store_catalog,
        purchases=True, ref_prices={"king_egg": 7200},
    )  # fmt: skip
    assert far[0].flags == ["최근 사입가 7,200원과 크게 다름"]
