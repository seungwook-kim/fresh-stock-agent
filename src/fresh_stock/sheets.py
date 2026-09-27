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
from dataclasses import dataclass
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
}


@dataclass(frozen=True)
class SheetEntry:
    item_id: str
    qty: float  # 정답 수량
    raw_name: str  # 종이에 적힌 이름
    raw_qty: str  # 종이에 적힌 수량 (최종 값)
    corrected: bool  # 줄 긋고 고쳐 쓴 줄


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
    catalog: Catalog, closing: dict[str, float], rng: random.Random
) -> tuple[list[SheetEntry], list[str]]:
    entries, omitted = [], []
    for item in catalog.items:
        qty = closing.get(item.id, 0.0)
        if qty == 0 and rng.random() < 0.7:
            omitted.append(item.id)
            continue
        name = item.name
        if name in ALIASES and rng.random() < 0.5:
            name = rng.choice(ALIASES[name])
        raw_qty = rng.choice(["0", "x", "-"]) if qty == 0 else _qty_text(item, qty, rng)
        entries.append(SheetEntry(item.id, qty, name, raw_qty, corrected=rng.random() < 0.05))
    return entries, omitted


def render_sheet(
    catalog: Catalog, day: date, closing: dict[str, float], seed: int
) -> tuple[bytes, SheetTruth]:
    """마감 재고를 손글씨 재고표 JPEG로 그린다."""
    rng = random.Random(seed)
    fonts = ensure_fonts()
    font_path = rng.choice(fonts)
    size = rng.randint(34, 40)
    font = ImageFont.truetype(str(font_path), size)
    title_font = ImageFont.truetype(str(font_path), size + 8)
    ink = rng.choice([(25, 25, 30), (20, 35, 95), (40, 40, 40)])

    entries, omitted = _entries(catalog, closing, rng)
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

    img = img.rotate(rng.uniform(-1.5, 1.5), resample=Image.BICUBIC, expand=True, fillcolor=(90, 90, 90))
    if rng.random() < 0.5:
        img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.3, 0.9)))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=rng.randint(55, 80))
    return buf.getvalue(), SheetTruth(day, tuple(entries), tuple(omitted))
