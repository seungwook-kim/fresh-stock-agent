"""합성 마감 재고표 이미지 생성기.

실제 재고표 사진이 없을 때 인식 파이프라인을 만들고 평가하기 위한 데이터다.
시뮬레이터의 마감 재고를 손글씨 폰트로 그리고, 현장에서 생길 법한 잡음을 넣는다.
정답(SheetTruth)이 함께 나오므로 인식 정확도를 자동으로 잴 수 있다.

폰트로 쓴 글씨는 실제 손글씨보다 깨끗하다. 여기서 잰 정확도는 파이프라인 검증용이지 실제 인식률이 아니다.
폰트는 Google Fonts의 OFL 한글 손글씨 폰트를 .cache/fonts에 받아 쓴다 (git에는 넣지 않는다).
"""

import io
import random
import urllib.request
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .catalog import Catalog
from .domain import Item

FONT_DIR = Path(".cache/fonts")
FONTS = {
    "NanumPenScript-Regular.ttf": "nanumpenscript",
    "Gaegu-Regular.ttf": "gaegu",
    "HiMelody-Regular.ttf": "himelody",
}
FONT_URL = "https://github.com/google/fonts/raw/main/ofl/{folder}/{name}"
PRINT_FONT = ("NanumGothic-Regular.ttf", "nanumgothic")  # 엑셀로 인쇄한 글자
WEEKDAYS = "월화수목금토일"

# 재고표에 자주 쓸 법한 줄임말 (합성 잡음용)
ALIASES = {
    "방울토마토": ["방토", "방울"],
    "토마토": ["토마"],
    "샤인머스캣": ["샤인", "샤머"],
    "블루베리": ["블베"],
    "파인애플": ["파인"],
    "청양고추": ["청양"],
    "풋고추": ["풋고"],
    "파프리카": ["파프"],
    "브로콜리": ["브로"],
    "양배추": ["양배"],
    "애호박": ["호박"],
    "깐마늘": ["마늘"],
    "고구마": ["고구"],
    "사각두부": ["사각"],
    "손두부": ["손두"],
    "순두부": ["순두"],
    "콩나물": ["콩나"],
    "메추리알": ["메추리"],
    "느타리": ["느타"],
    "칼국수면": ["칼국수"],
    "떡국떡": ["떡국"],
    # 예시 가게 목록 (sample_store)
    "깐메추리알": ["깐메추리"],
    "국물멸치": ["멸치"],
    "단호박": ["단호"],
}


# 그 가게만 아는 한 글자 약어. 다른 품목과 헷갈려서 모델이 추측할 수 없다 (양: 양파/양배추, 사: 사과/사각두부)
STORE_ABBREVIATIONS = {
    "양배추": "양",
    "방울토마토": "방",
    "사각두부": "사",
    "손두부": "손",
    "청양고추": "청",
    "샤인머스캣": "샤",
    "블루베리": "블",
    "느타리": "느",
    "대파": "파",
    "새송이": "새",
    "메추리알": "메",
    "칼국수면": "칼",
}


def synthetic_aliases(catalog: Catalog, store_abbr: bool = False) -> dict[str, str]:
    """합성 재고표에 쓰이는 줄임말 전체. 줄임말을 이미 익힌 상태를 흉내 낼 때 쓴다."""
    by_name = {i.name: i.id for i in catalog.items}
    aliases = {alias: by_name[name] for name, names in ALIASES.items() for alias in names if name in by_name}
    if store_abbr:
        aliases.update({abbr: by_name[name] for name, abbr in STORE_ABBREVIATIONS.items() if name in by_name})
    return aliases


@dataclass(frozen=True)
class SheetEntry:
    item_id: str
    qty: float  # 정답 수량
    raw_name: str  # 종이에 적힌 이름
    raw_qty: str  # 종이에 적힌 수량 (최종 값)
    corrected: bool  # 줄 긋고 고쳐 쓴 줄
    # 사입 칸이 있는 재고표(render_store_sheet)만: 그날 사 온 양과 단위당 가격. 안 샀으면 0, None, 빈 칸
    buy_qty: float = 0.0
    buy_price: int | None = None
    raw_buy_qty: str = ""
    raw_buy_price: str = ""


@dataclass(frozen=True)
class SheetTruth:
    day: date
    entries: tuple[SheetEntry, ...]
    omitted_zero: tuple[str, ...]  # 재고가 0이라 적지 않은 품목

    @property
    def written(self) -> dict[str, float]:
        return {e.item_id: e.qty for e in self.entries}


def ensure_fonts() -> list[Path]:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, folder in FONTS.items():
        path = FONT_DIR / name
        if not path.exists():
            urllib.request.urlretrieve(FONT_URL.format(folder=folder, name=name), path)
        paths.append(path)
    return paths


def ensure_print_font() -> Path:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    name, folder = PRINT_FONT
    path = FONT_DIR / name
    if not path.exists():
        urllib.request.urlretrieve(FONT_URL.format(folder=folder, name=name), path)
    return path


def _qty_text(item: Item, qty: float, rng: random.Random) -> str:
    whole, half = int(qty), qty - int(qty) >= 0.5
    if half:
        if whole == 0:
            text = rng.choice(["반", "0.5"])
        else:
            text = rng.choice([f"{whole}.5", f"{whole}반"])
    else:
        text = str(whole)
    if rng.random() < 0.15:
        text += item.unit
    return text


def _entries(
    catalog: Catalog, closing: dict[str, float], rng: random.Random, store_abbr: bool = False
) -> tuple[list[SheetEntry], list[str]]:
    entries, omitted = [], []
    for item in catalog.items:
        qty = closing.get(item.id, 0.0)
        if qty == 0 and rng.random() < 0.7:
            omitted.append(item.id)
            continue
        name = item.name
        if store_abbr and name in STORE_ABBREVIATIONS:
            name = STORE_ABBREVIATIONS[name]
        elif name in ALIASES and rng.random() < 0.5:
            name = rng.choice(ALIASES[name])
        raw_qty = rng.choice(["0", "x", "-"]) if qty == 0 else _qty_text(item, qty, rng)
        entries.append(SheetEntry(item.id, qty, name, raw_qty, corrected=rng.random() < 0.05))
    return entries, omitted


def render_sheet(
    catalog: Catalog, day: date, closing: dict[str, float], seed: int, store_abbr: bool = False
) -> tuple[bytes, SheetTruth]:
    """마감 재고를 손글씨 재고표 JPEG로 그린다. store_abbr이면 매장 고유 약어를 쓴다."""
    rng = random.Random(seed)
    fonts = ensure_fonts()
    font_path = rng.choice(fonts)
    size = rng.randint(34, 40)
    font = ImageFont.truetype(str(font_path), size)
    title_font = ImageFont.truetype(str(font_path), size + 8)
    ink = rng.choice([(25, 25, 30), (20, 35, 95), (40, 40, 40)])

    entries, omitted = _entries(catalog, closing, rng, store_abbr)
    columns = 3
    per_col = -(-len(entries) // columns)
    row_h = int(size * 1.55)
    width, height = 1200, 140 + per_col * row_h + 60
    paper = (rng.randint(242, 252), rng.randint(240, 250), rng.randint(228, 242))
    img = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(img)
    for y in range(140, height, row_h):
        draw.line([(20, y + row_h - 8), (width - 20, y + row_h - 8)], fill=(205, 210, 220), width=1)

    title = f"{day.month}/{day.day} ({WEEKDAYS[day.weekday()]}) 마감 재고"
    draw.text((60, 40), title, font=title_font, fill=ink)

    col_w = (width - 80) // columns
    for n, e in enumerate(entries):
        x0 = 50 + (n // per_col) * col_w + rng.randint(-6, 6)
        y0 = 140 + (n % per_col) * row_h + rng.randint(-4, 4)
        layer = Image.new("RGBA", (col_w, row_h + 10), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        name_w = ld.textlength(e.raw_name + " ", font=font)
        ld.text((0, 0), e.raw_name, font=font, fill=ink)
        qx = name_w + rng.randint(0, 25)
        if e.corrected:
            wrong = str(rng.randint(1, 9))
            ld.text((qx, 0), wrong, font=font, fill=ink)
            ww = ld.textlength(wrong, font=font)
            ld.line([(qx - 4, size * 0.55), (qx + ww + 4, size * 0.45)], fill=ink, width=3)
            qx += ww + 14
        ld.text((qx, 0), e.raw_qty, font=font, fill=ink)
        layer = layer.rotate(rng.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=False)
        img.paste(layer, (x0, y0), layer)

    return _finish(img, rng), SheetTruth(day, tuple(entries), tuple(omitted))


def _finish(img: Image.Image, rng: random.Random) -> bytes:
    """사진처럼 살짝 기울이고 흐리게 해서 JPEG로 저장한다."""
    img = img.rotate(rng.uniform(-1.5, 1.5), resample=Image.BICUBIC, expand=True, fillcolor=(90, 90, 90))
    if rng.random() < 0.5:
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.9)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.randint(55, 80))
    return buf.getvalue()


@dataclass(frozen=True)
class Writer:
    """재고표를 적는 사람 한 명: 글씨체와 적는 버릇. 손글씨는 사람마다 달라서 여러 사람으로 평가한다."""

    name: str
    font: str  # Google Fonts(OFL) 손글씨 폰트 파일
    folder: str
    size: int  # 글자 크기 (px)
    slant: float  # 글씨 기울기 (오른쪽으로 누우면 +)
    wobble: int  # 글자마다 위아래로 흔들리는 정도 (px)
    bold: int  # 펜을 누르는 힘 (획 두께 더하기)
    zero: tuple[str, ...]  # 재고 0을 적는 방식
    half: str  # 반 박스를 "반"(4반)으로 적는지 "0.5"(4.5)로 적는지
    comma: float  # 가격에 쉼표를 찍는 비율
    unit: float  # 수량 뒤에 단위(박스, 봉)를 붙이는 비율


WRITERS = (
    Writer("1 사장님", "NanumPenScript-Regular.ttf", "nanumpenscript", 34, 0.10, 2, 0, ("x",), "반", 0.0, 0.10),
    Writer("2 직원 가", "Gaegu-Regular.ttf", "gaegu", 32, 0.00, 1, 0, ("0",), "0.5", 1.0, 0.0),
    Writer("3 직원 나", "HiMelody-Regular.ttf", "himelody", 36, -0.05, 3, 0, ("-",), "반", 0.5, 0.20),
    Writer("4 직원 다", "NanumBrushScript-Regular.ttf", "nanumbrushscript", 38, 0.15, 3, 1, ("x", "-"), "반", 0.0, 0.0),
    Writer("5 직원 라", "GamjaFlower-Regular.ttf", "gamjaflower", 33, 0.05, 2, 0, ("ㅇ", "0"), "0.5", 1.0, 0.10),
    Writer("6 직원 마", "EastSeaDokdo-Regular.ttf", "eastseadokdo", 38, 0.20, 4, 0, ("x",), "반", 0.3, 0.30),
    Writer("7 직원 바", "Dokdo-Regular.ttf", "dokdo", 34, -0.10, 2, 0, ("-",), "0.5", 0.0, 0.0),
    Writer("8 직원 사", "CuteFont-Regular.ttf", "cutefont", 36, 0.00, 3, 0, ("0", "x"), "반", 0.7, 0.15),
    Writer("9 직원 아", "SingleDay-Regular.ttf", "singleday", 33, 0.08, 1, 1, ("x",), "0.5", 0.0, 0.05),
    Writer("10 직원 자", "PoorStory-Regular.ttf", "poorstory", 34, -0.03, 2, 0, ("-", "0"), "반", 1.0, 0.0),
)  # fmt: skip


def ensure_writer_font(writer: Writer) -> Path:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    path = FONT_DIR / writer.font
    if not path.exists():
        urllib.request.urlretrieve(FONT_URL.format(folder=writer.folder, name=writer.font), path)
    return path


def _writer_qty(item: Item, qty: float, writer: Writer, rng: random.Random) -> str:
    """이 사람의 버릇대로 적은 수량 글자."""
    if qty == 0:
        return rng.choice(writer.zero)
    whole, half = int(qty), qty - int(qty) >= 0.5
    if half:
        text = ("반" if whole == 0 else f"{whole}반") if writer.half == "반" else f"{whole}.5"
    else:
        text = str(whole)
    return text + item.unit if rng.random() < writer.unit else text


# 실제 가게 재고표 형식: [품목 | 남은 수량 | 사입 수량 | 사입 가격] 네 칸 묶음이 왼쪽(A~D)·오른쪽(E~H)에 하나씩, 30줄
STORE_HEADERS = (("품목", "남은수량", "사입수량", "사입가격"), ("품목", "수량", "사입수량", "사입가격"))
STORE_ROWS = 30


def _price_text(price: int, rng: random.Random) -> str:
    return f"{price:,}" if rng.random() < 0.4 else str(price)


def store_entries(
    catalog: Catalog,
    closing: dict[str, float],
    purchases: dict[str, tuple[float, int]],
    rng: random.Random,
    printed_names: bool = False,
    writer: Writer | None = None,
) -> list[SheetEntry]:
    """목록 순서대로 모든 품목을 적는다 (재고가 없어도 줄은 있다). purchases: 품목 id → (그날 사 온 양, 단위당 가격).

    printed_names면 품목 이름이 엑셀로 인쇄돼 있어 줄임말 없이 정식 이름이다. writer가 있으면 그 사람의 버릇대로 적는다.
    """
    entries = []
    for item in catalog.items:
        qty = closing.get(item.id, 0.0)
        name = item.name
        if not printed_names and name in ALIASES and ALIASES[name] and rng.random() < 0.5:
            name = rng.choice(ALIASES[name])
        if writer is not None:
            raw_qty = _writer_qty(item, qty, writer, rng)
        else:
            raw_qty = rng.choice(["0", "x", "-"]) if qty == 0 else _qty_text(item, qty, rng)
        buy_qty, buy_price = purchases.get(item.id, (0.0, None))
        buy_price = int(buy_price) if buy_price else None
        if writer is not None:
            raw_buy_qty = _writer_qty(item, buy_qty, writer, rng) if buy_qty > 0 else ""
        else:
            raw_buy_qty = _qty_text(item, buy_qty, rng) if buy_qty > 0 else ""
        raw_buy_price = _price_text(buy_price, rng) if buy_qty > 0 and buy_price else ""
        entries.append(
            SheetEntry(
                item.id, qty, name, raw_qty, rng.random() < 0.05,
                buy_qty if buy_qty > 0 else 0.0, buy_price if buy_qty > 0 else None, raw_buy_qty, raw_buy_price,
            )
        )  # fmt: skip
    return entries


def render_store_sheet(
    catalog: Catalog, day: date, closing: dict[str, float], purchases: dict[str, tuple[float, int]], seed: int
) -> tuple[bytes, SheetTruth]:
    """실제 가게 재고표 형식(8칸 × 30줄, 꽉 찬 표)으로 마감 재고와 사입 수량·가격을 손글씨로 그린다."""
    rng = random.Random(seed)
    font_path = rng.choice(ensure_fonts())
    size = rng.randint(30, 34)
    font = ImageFont.truetype(str(font_path), size)
    head_font = ImageFont.truetype(str(font_path), size - 4)
    title_font = ImageFont.truetype(str(font_path), size + 8)
    ink = rng.choice([(25, 25, 30), (20, 35, 95), (40, 40, 40)])
    grid = (170, 180, 195)

    entries = store_entries(catalog, closing, purchases, rng)
    n_rows = max(STORE_ROWS, -(-len(entries) // 2))  # 품목이 60개를 넘으면 줄을 늘린다
    col_w = (300, 150, 150, 170)  # 품목, 남은 수량, 사입 수량, 사입 가격
    block_w = sum(col_w)
    row_h = int(size * 1.4)
    left, top = 40, 150
    width = left * 2 + block_w * 2 + 20
    height = top + (n_rows + 1) * row_h + 40
    paper = (rng.randint(242, 252), rng.randint(240, 250), rng.randint(228, 242))
    img = Image.new("RGB", (width, height), paper)
    draw = ImageDraw.Draw(img)
    draw.text((60, 50), f"{day.month}/{day.day} ({WEEKDAYS[day.weekday()]}) 재고", font=title_font, fill=ink)

    # 인쇄된 표 칸
    for block in range(2):
        x0 = left + block * (block_w + 20)
        xs = [x0]
        for w in col_w:
            xs.append(xs[-1] + w)
        for r in range(n_rows + 2):
            draw.line([(x0, top + r * row_h), (xs[-1], top + r * row_h)], fill=grid, width=2 if r < 2 else 1)
        for x in xs:
            draw.line([(x, top), (x, top + (n_rows + 1) * row_h)], fill=grid, width=2)
        for x, label in zip(xs, STORE_HEADERS[block]):
            draw.text((x + 8, top + 6), label, font=head_font, fill=(60, 60, 70))

    def cell(text: str, x: int, y: int, w: int, strike: bool = False) -> None:
        if not text and not strike:
            return
        layer = Image.new("RGBA", (w, row_h + 10), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        tx = rng.randint(4, 14)
        if strike:  # 줄 긋고 옆에 고쳐 쓴 칸
            wrong = str(rng.randint(1, 9))
            ld.text((tx, 0), wrong, font=font, fill=ink)
            ww = ld.textlength(wrong, font=font)
            ld.line([(tx - 4, size * 0.55), (tx + ww + 4, size * 0.45)], fill=ink, width=3)
            tx += int(ww) + 12
        ld.text((tx, 0), text, font=font, fill=ink)
        layer = layer.rotate(rng.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=False)
        img.paste(layer, (x, y + rng.randint(-3, 3)), layer)

    for n, e in enumerate(entries):
        block, r = divmod(n, n_rows)
        x = left + block * (block_w + 20)
        y = top + (r + 1) * row_h + 4
        cell(e.raw_name, x, y, col_w[0])
        cell(e.raw_qty, x + col_w[0], y, col_w[1], strike=e.corrected)
        cell(e.raw_buy_qty, x + col_w[0] + col_w[1], y, col_w[2])
        cell(e.raw_buy_price, x + col_w[0] + col_w[1] + col_w[2], y, col_w[3])
    return _finish(img, rng), SheetTruth(day, tuple(entries), ())


def _perspective_coeffs(src: list[tuple[float, float]], dst: list[tuple[float, float]]) -> list[float]:
    """결과 이미지의 네 점(dst)이 원본의 네 점(src)에서 오도록 하는 원근 변환 계수 (Image.PERSPECTIVE)."""
    import numpy as np

    rows = []
    for (x, y), (u, v) in zip(dst, src):
        rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
        rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
    return np.linalg.solve(np.array(rows, dtype=float), np.array([c for p in src for c in p], dtype=float)).tolist()


def _phone_photo(img: Image.Image, rng: random.Random) -> bytes:
    """종이를 휴대폰으로 찍은 것처럼: 원근으로 기울고, 한쪽에 그림자가 지고, 잡음·흐림·JPEG 압축이 들어간다."""
    import numpy as np

    w, h = img.size
    corners = [(0, 0), (w, 0), (w, h), (0, h)]
    src = [(x + rng.uniform(-0.03, 0.03) * w, y + rng.uniform(-0.025, 0.025) * h) for x, y in corners]
    img = img.transform((w, h), Image.PERSPECTIVE, _perspective_coeffs(src, corners), Image.BICUBIC,
                        fillcolor=(70, 66, 60))  # fmt: skip
    arr = np.asarray(img).astype(float)
    yy, xx = np.mgrid[0:h, 0:w]
    angle = rng.uniform(0, 2 * np.pi)
    t = (xx / w - 0.5) * np.cos(angle) + (yy / h - 0.5) * np.sin(angle)
    shade = 1 - rng.uniform(0.08, 0.22) * (t - t.min()) / (t.max() - t.min())
    noise = np.random.default_rng(rng.randint(0, 2**31)).normal(0, rng.uniform(2, 5), arr.shape)
    img = Image.fromarray(np.clip(arr * shade[..., None] + noise, 0, 255).astype(np.uint8))
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 1.1)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.randint(60, 85))
    return buf.getvalue()


EXCEL_HEADERS = ("품목", "남은수량", "사입수량", "사입가격")


def render_excel_sheet(
    catalog: Catalog,
    day: date,
    closing: dict[str, float],
    purchases: dict[str, tuple[float, int]],
    seed: int,
    writer: Writer | None = None,
) -> tuple[bytes, SheetTruth]:
    """엑셀로 인쇄한 재고표(칸·머리글·품목 이름은 인쇄)에 숫자를 손으로 적고 휴대폰으로 찍은 사진.

    A~D, E~H 두 묶음 × 30줄. 손글씨는 칸보다 크거나 비뚤어져 칸 선에 걸치기도 한다.
    writer가 있으면 그 사람의 글씨체·기울기·흔들림·획 두께·적는 버릇으로 쓴다.
    """
    rng = random.Random(seed)
    hand_path = ensure_writer_font(writer) if writer else rng.choice(ensure_fonts())
    printed = ImageFont.truetype(str(ensure_print_font()), 22)
    printed_head = ImageFont.truetype(str(ensure_print_font()), 21)
    title_font = ImageFont.truetype(str(ensure_print_font()), 34)
    size = writer.size + rng.randint(-2, 2) if writer else rng.randint(30, 37)
    hand = ImageFont.truetype(str(hand_path), size)
    ink = rng.choice([(20, 40, 140), (30, 30, 35), (25, 50, 120)])  # 볼펜 파랑·검정

    entries = store_entries(catalog, closing, purchases, rng, printed_names=True, writer=writer)
    if writer is not None:  # 가격 쉼표도 그 사람의 버릇대로
        entries = [
            replace(e, raw_buy_price=(f"{e.buy_price:,}" if rng.random() < writer.comma else str(e.buy_price)))
            if e.buy_price else e
            for e in entries
        ]
    n_rows = max(STORE_ROWS, -(-len(entries) // 2))
    col_w = (210, 120, 120, 150)
    block_w = sum(col_w)
    row_h, head_h, left, top = 56, 46, 100, 190
    width = left * 2 + block_w * 2
    height = top + head_h + n_rows * row_h + 90
    img = Image.new("RGB", (width, height), (rng.randint(246, 253), rng.randint(246, 253), rng.randint(244, 251)))
    draw = ImageDraw.Draw(img)
    black = (35, 35, 35)

    # 인쇄된 부분: 제목, 날짜 칸, 머리글, 칸 선, 품목 이름
    draw.text((left, 80), "마감 재고표", font=title_font, fill=black)
    date_x = left + block_w * 2 - 330
    draw.text((date_x, 92), "날짜:          월          일", font=printed, fill=black)
    xs = [left]
    for _ in range(2):
        for w in col_w:
            xs.append(xs[-1] + w)
    draw.rectangle([left, top, xs[-1], top + head_h], fill=(217, 217, 217))
    for n, label in enumerate(EXCEL_HEADERS * 2):
        tw = draw.textlength(label, font=printed_head)
        draw.text((xs[n] + (xs[n + 1] - xs[n] - tw) / 2, top + 11), label, font=printed_head, fill=black)
    bottom = top + head_h + n_rows * row_h
    for r in range(n_rows + 1):
        y = top + head_h + r * row_h
        draw.line([(left, y), (xs[-1], y)], fill=black, width=1)
    draw.line([(left, top), (xs[-1], top)], fill=black, width=2)
    for n, x in enumerate(xs):
        draw.line([(x, top), (x, bottom)], fill=black, width=2 if n in (0, 4, 8) else 1)
    draw.line([(left, bottom), (xs[-1], bottom)], fill=black, width=2)
    for n, e in enumerate(entries):
        block, r = divmod(n, n_rows)
        y = top + head_h + r * row_h
        draw.text((xs[block * 4] + 8, y + (row_h - 24) / 2), e.raw_name, font=printed, fill=black)

    def write(text: str, x: float, y: float, w: float, strike: bool = False) -> None:
        """칸 (x, y, 너비 w)에 손으로 쓴다. 칸보다 크면 옆 칸으로 넘어간다."""
        if not text and not strike:
            return
        layer = Image.new("RGBA", (int(w) + 60, row_h + 30), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        tx = rng.randint(4, 26)
        if strike:  # 줄 긋고 옆에 고쳐 쓴 칸
            wrong = str(rng.randint(1, 9))
            ld.text((tx, 8), wrong, font=hand, fill=ink)
            ww = ld.textlength(wrong, font=hand)
            ld.line([(tx - 4, 8 + size * 0.6), (tx + ww + 4, 8 + size * 0.4)], fill=ink, width=3)
            tx += int(ww) + 10
        if writer is None:
            ld.text((tx, 8), text, font=hand, fill=ink)
            layer = layer.rotate(rng.uniform(-4, 4), resample=Image.BICUBIC, expand=False)
        else:
            # 글자마다 위아래로 흔들리고, 사람마다 기울기·획 두께가 다르다
            for ch in text:
                ld.text((tx, 8 + rng.randint(-writer.wobble, writer.wobble)), ch, font=hand, fill=ink,
                        stroke_width=writer.bold, stroke_fill=ink)  # fmt: skip
                tx += ld.textlength(ch, font=hand) + rng.uniform(-1, 3)
            shear = writer.slant + rng.uniform(-0.04, 0.04)
            layer = layer.transform(layer.size, Image.AFFINE, (1, shear, -shear * layer.height / 2, 0, 1, 0),
                                    resample=Image.BICUBIC)  # fmt: skip
            layer = layer.rotate(rng.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=False)
        img.paste(layer, (int(x), int(y + (row_h - size) / 2 - 12 + rng.randint(-9, 7))), layer)

    # 인쇄된 "월"·"일" 앞 빈칸에 손으로 적는다
    month_x = date_x + draw.textlength("날짜:   ", font=printed)
    day_x = date_x + draw.textlength("날짜:          월   ", font=printed)
    write(str(day.month), month_x, 74, 80)
    write(str(day.day), day_x, 74, 80)
    for n, e in enumerate(entries):
        block, r = divmod(n, n_rows)
        y = top + head_h + r * row_h
        base = block * 4
        write(e.raw_qty, xs[base + 1], y, col_w[1], strike=e.corrected)
        write(e.raw_buy_qty, xs[base + 2], y, col_w[2])
        write(e.raw_buy_price, xs[base + 3], y, col_w[3])
    return _phone_photo(img, rng), SheetTruth(day, tuple(entries), ())
