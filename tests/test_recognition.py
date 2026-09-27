from datetime import date

import pytest

from fresh_stock.catalog import load_catalog
from fresh_stock.llm import ImageInput
from fresh_stock.recognition import RecognizedRow, parse_qty, recognize_sheet, validate
from fresh_stock.sheet_eval import score_sheet, summarize
from fresh_stock.sheets import FONT_DIR, FONTS, SheetEntry, SheetTruth, render_sheet

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
