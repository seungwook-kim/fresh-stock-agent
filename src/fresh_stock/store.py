"""SQLite 저장소.

하루 기록(입고·마감 재고·현금·카드), 가게 메모, 발주, 재고표 인식 결과를 저장한다.
재고표 인식 결과는 모델이 읽은 값과 사람이 확정한 값을 함께 남긴다. 실제 매장에서 쓰기 시작하면
이 기록이 그대로 실제 손글씨 인식률 평가 데이터가 된다.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .accounting import DayRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_record (
    day TEXT PRIMARY KEY,
    cash INTEGER NOT NULL DEFAULT 0,
    card INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS closing (
    day TEXT NOT NULL, item_id TEXT NOT NULL, qty REAL NOT NULL,
    PRIMARY KEY (day, item_id)
);
CREATE TABLE IF NOT EXISTS received (
    day TEXT NOT NULL, item_id TEXT NOT NULL, qty REAL NOT NULL,
    PRIMARY KEY (day, item_id)
);
CREATE TABLE IF NOT EXISTS note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL, text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_plan (
    order_day TEXT PRIMARY KEY,
    arrival TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    approved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS order_line (
    order_day TEXT NOT NULL, item_id TEXT NOT NULL,
    base INTEGER NOT NULL,         -- 규칙 추천
    recommended INTEGER NOT NULL,  -- 에이전트 반영 추천
    final INTEGER NOT NULL,        -- 사람이 승인한 수량
    factors TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (order_day, item_id)
);
CREATE TABLE IF NOT EXISTS sheet_image (
    day TEXT PRIMARY KEY, data BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS sheet_row (
    day TEXT NOT NULL, idx INTEGER NOT NULL,
    raw_name TEXT NOT NULL, raw_qty TEXT NOT NULL,
    item_id_read TEXT, qty_read REAL, flags TEXT NOT NULL,
    item_id_final TEXT, qty_final REAL,
    PRIMARY KEY (day, idx)
);
"""


@dataclass(frozen=True)
class OrderLineRecord:
    item_id: str
    base: int
    recommended: int
    final: int
    factors: str
    reason: str


@dataclass(frozen=True)
class OrderPlan:
    order_day: date
    arrival: date
    summary: str
    approved: bool
    lines: tuple[OrderLineRecord, ...]


@dataclass(frozen=True)
class SheetRowRecord:
    idx: int
    raw_name: str
    raw_qty: str
    item_id_read: str | None
    qty_read: float | None
    flags: tuple[str, ...]
    item_id_final: str | None
    qty_final: float | None


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.executescript(SCHEMA)

    # 하루 기록 ----------------------------------------------------------------

    def save_day(self, day: date, closing: dict[str, float], cash: int, card: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO day_record (day, cash, card) VALUES (?, ?, ?)", (day.isoformat(), cash, card)
            )
            self.conn.execute("DELETE FROM closing WHERE day = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT INTO closing (day, item_id, qty) VALUES (?, ?, ?)",
                [(day.isoformat(), i, q) for i, q in closing.items()],
            )

    def save_received(self, day: date, received: dict[str, float]) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM received WHERE day = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT INTO received (day, item_id, qty) VALUES (?, ?, ?)",
                [(day.isoformat(), i, q) for i, q in received.items() if q > 0],
            )

    def received(self, day: date) -> dict[str, float]:
        rows = self.conn.execute("SELECT item_id, qty FROM received WHERE day = ?", (day.isoformat(),))
        return dict(rows.fetchall())

    def closing(self, day: date) -> dict[str, float]:
        rows = self.conn.execute("SELECT item_id, qty FROM closing WHERE day = ?", (day.isoformat(),))
        return dict(rows.fetchall())

    def last_closed_day(self) -> date | None:
        row = self.conn.execute("SELECT MAX(day) FROM day_record").fetchone()
        return date.fromisoformat(row[0]) if row[0] else None

    def ledger(self) -> list[DayRecord]:
        """마감한 날들의 기록 (날짜순)."""
        days = [r[0] for r in self.conn.execute("SELECT day, cash, card FROM day_record ORDER BY day")]
        records = []
        for d in days:
            day = date.fromisoformat(d)
            cash, card = self.conn.execute("SELECT cash, card FROM day_record WHERE day = ?", (d,)).fetchone()
            records.append(DayRecord(day, self.received(day), self.closing(day), cash=cash, card=card))
        return records

    # 가게 메모 ----------------------------------------------------------------

    def add_note(self, day: date, text: str) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO note (day, text) VALUES (?, ?)", (day.isoformat(), text.strip()))

    def delete_note(self, note_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM note WHERE id = ?", (note_id,))

    def notes(self, start: date | None = None, end: date | None = None) -> list[tuple[int, date, str]]:
        rows = self.conn.execute("SELECT id, day, text FROM note ORDER BY day, id").fetchall()
        result = [(i, date.fromisoformat(d), t) for i, d, t in rows]
        return [(i, d, t) for i, d, t in result if (start is None or d >= start) and (end is None or d <= end)]

    # 발주 ----------------------------------------------------------------------

    def save_plan(self, plan: OrderPlan) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO order_plan (order_day, arrival, summary, approved) VALUES (?, ?, ?, ?)",
                (plan.order_day.isoformat(), plan.arrival.isoformat(), plan.summary, int(plan.approved)),
            )
            self.conn.execute("DELETE FROM order_line WHERE order_day = ?", (plan.order_day.isoformat(),))
            self.conn.executemany(
                "INSERT INTO order_line (order_day, item_id, base, recommended, final, factors, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (plan.order_day.isoformat(), ln.item_id, ln.base, ln.recommended, ln.final, ln.factors, ln.reason)
                    for ln in plan.lines
                ],
            )

    def plan(self, order_day: date) -> OrderPlan | None:
        row = self.conn.execute(
            "SELECT arrival, summary, approved FROM order_plan WHERE order_day = ?", (order_day.isoformat(),)
        ).fetchone()
        if row is None:
            return None
        lines = self.conn.execute(
            "SELECT item_id, base, recommended, final, factors, reason FROM order_line WHERE order_day = ?",
            (order_day.isoformat(),),
        ).fetchall()
        return OrderPlan(
            order_day, date.fromisoformat(row[0]), row[1], bool(row[2]), tuple(OrderLineRecord(*ln) for ln in lines)
        )

    def approved_finals(self, item_id: str, limit: int = 10) -> list[tuple[date, int]]:
        """승인된 지난 발주 (입고일, 수량). 추천 없이 넣는 발주의 가드레일에 쓴다."""
        rows = self.conn.execute(
            "SELECT p.arrival, l.final FROM order_line l JOIN order_plan p ON p.order_day = l.order_day"
            " WHERE p.approved = 1 AND l.item_id = ? ORDER BY p.arrival DESC LIMIT ?",
            (item_id, limit),
        ).fetchall()
        return [(date.fromisoformat(a), f) for a, f in reversed(rows)]

    # 재고표 --------------------------------------------------------------------

    def save_sheet(self, day: date, image: bytes, rows: list[SheetRowRecord]) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO sheet_image (day, data) VALUES (?, ?)", (day.isoformat(), image))
            self.conn.execute("DELETE FROM sheet_row WHERE day = ?", (day.isoformat(),))
            self.conn.executemany(
                "INSERT INTO sheet_row VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        day.isoformat(),
                        r.idx,
                        r.raw_name,
                        r.raw_qty,
                        r.item_id_read,
                        r.qty_read,
                        json.dumps(list(r.flags), ensure_ascii=False),
                        r.item_id_final,
                        r.qty_final,
                    )
                    for r in rows
                ],
            )

    def sheet_image(self, day: date) -> bytes | None:
        row = self.conn.execute("SELECT data FROM sheet_image WHERE day = ?", (day.isoformat(),)).fetchone()
        return row[0] if row else None

    def sheet_rows(self, day: date) -> list[SheetRowRecord]:
        rows = self.conn.execute(
            "SELECT idx, raw_name, raw_qty, item_id_read, qty_read, flags, item_id_final, qty_final"
            " FROM sheet_row WHERE day = ? ORDER BY idx",
            (day.isoformat(),),
        ).fetchall()
        return [
            SheetRowRecord(idx, name, qty_text, iid, q, tuple(json.loads(flags)), fid, fq)
            for idx, name, qty_text, iid, q, flags, fid, fq in rows
        ]

    def finalize_sheet_rows(self, day: date, finals: dict[int, tuple[str | None, float | None]]) -> None:
        with self.conn:
            self.conn.executemany(
                "UPDATE sheet_row SET item_id_final = ?, qty_final = ? WHERE day = ? AND idx = ?",
                [(iid, q, day.isoformat(), idx) for idx, (iid, q) in finals.items()],
            )

    def confirmed_names(self) -> dict[str, str]:
        """재고표에 적힌 이름 → 사람이 확정한 품목 id. 같은 이름이 여러 품목으로 확정됐으면 가장 많은 쪽."""
        rows = self.conn.execute(
            "SELECT raw_name, item_id_final, COUNT(*) AS n FROM sheet_row"
            " WHERE item_id_final IS NOT NULL GROUP BY raw_name, item_id_final ORDER BY n DESC"
        ).fetchall()
        names: dict[str, str] = {}
        for raw, item_id, _ in rows:
            names.setdefault(raw.strip(), item_id)
        return names

    def correction_stats(self) -> dict[str, int]:
        """사람이 확정한 재고표 줄 중 모델이 읽은 값과 달랐던 줄 수. 실제 인식률 추적용."""
        total, changed, flagged_changed = self.conn.execute(
            "SELECT COUNT(*),"
            " SUM(item_id_read IS NOT item_id_final OR qty_read IS NOT qty_final),"
            " SUM((item_id_read IS NOT item_id_final OR qty_read IS NOT qty_final) AND flags != '[]')"
            " FROM sheet_row WHERE item_id_final IS NOT NULL"
        ).fetchone()
        return {"확정한 줄": total or 0, "사람이 고친 줄": changed or 0, "그중 확인 표시가 있던 줄": flagged_changed or 0}
