"""저장소. SQLite(기본)와 PostgreSQL(Supabase 등)을 같은 코드로 쓴다.

하루 기록(입고·마감 재고·현금·카드), 가게 메모, 발주, 재고표 인식 결과를 저장한다.
재고표 인식 결과는 모델이 읽은 값과 사람이 확정한 값을 함께 남긴다. 실제 매장에서 쓰기 시작하면
이 기록이 그대로 실제 손글씨 인식률 평가 데이터가 된다.

    Store("out/store.db")                          # SQLite 파일
    Store("postgresql://user:pw@host:5432/db")     # PostgreSQL (Supabase 연결 문자열)

SQL은 두 DB에서 모두 도는 문법만 쓴다 (ON CONFLICT ... DO UPDATE, IS DISTINCT FROM, CASE WHEN).
"""

import json
import sqlite3
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .accounting import DayRecord

SCHEMA = """
CREATE TABLE IF NOT EXISTS day_record (
    day TEXT PRIMARY KEY,
    cash INTEGER NOT NULL DEFAULT 0,
    card INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS closing (
    day TEXT NOT NULL, item_id TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (day, item_id)
);
CREATE TABLE IF NOT EXISTS received (
    day TEXT NOT NULL, item_id TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (day, item_id)
);
CREATE TABLE IF NOT EXISTS note (
    id {auto_id},
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
    base INTEGER NOT NULL,
    recommended INTEGER NOT NULL,
    final INTEGER NOT NULL,
    factors TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (order_day, item_id)
);
CREATE TABLE IF NOT EXISTS sheet_image (
    day TEXT PRIMARY KEY, data {blob} NOT NULL
);
CREATE TABLE IF NOT EXISTS sheet_row (
    day TEXT NOT NULL, idx INTEGER NOT NULL,
    raw_name TEXT NOT NULL, raw_qty TEXT NOT NULL,
    item_id_read TEXT, qty_read DOUBLE PRECISION, flags TEXT NOT NULL,
    item_id_final TEXT, qty_final DOUBLE PRECISION,
    PRIMARY KEY (day, idx)
)
"""
DIALECT_TYPES = {
    "sqlite": {"auto_id": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB"},
    "postgres": {"auto_id": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA"},
}
# 데이터를 옮길 때의 테이블 순서와 열
TABLES = {
    "day_record": ("day", "cash", "card"),
    "closing": ("day", "item_id", "qty"),
    "received": ("day", "item_id", "qty"),
    "note": ("id", "day", "text"),
    "order_plan": ("order_day", "arrival", "summary", "approved"),
    "order_line": ("order_day", "item_id", "base", "recommended", "final", "factors", "reason"),
    "sheet_image": ("day", "data"),
    "sheet_row": (
        "day", "idx", "raw_name", "raw_qty", "item_id_read", "qty_read", "flags", "item_id_final", "qty_final",
    ),
}  # fmt: skip


@dataclass(frozen=True)
class OrderLineRecord:
    item_id: str
    base: int  # 규칙 추천
    recommended: int  # 에이전트 반영 추천
    final: int  # 사람이 승인한 수량
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


def is_postgres_url(url: str) -> bool:
    return url.startswith(("postgres://", "postgresql://"))


def mask_url(url: str) -> str:
    """화면에 찍을 때 비밀번호를 가린다."""
    if not is_postgres_url(url) or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    auth, host = rest.rsplit("@", 1)
    user = auth.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


class _Tx:
    """트랜잭션 안에서 쓰는 실행기. SQL의 ? 자리표시자를 DB에 맞게 바꾼다."""

    def __init__(self, conn: Any, postgres: bool):
        self.conn = conn
        self.postgres = postgres

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self.postgres else sql

    def execute(self, sql: str, params: tuple = ()) -> Any:
        return self.conn.execute(self._sql(sql), params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        if not rows:
            return
        if self.postgres:
            with self.conn.cursor() as cur:
                cur.executemany(self._sql(sql), rows)
        else:
            self.conn.executemany(sql, rows)

    def all(self, sql: str, params: tuple = ()) -> list[tuple]:
        return [tuple(r) for r in self.execute(sql, params).fetchall()]

    def one(self, sql: str, params: tuple = ()) -> tuple | None:
        row = self.execute(sql, params).fetchone()
        return tuple(row) if row is not None else None


class Store:
    def __init__(self, url: Path | str):
        self.url = str(url)
        self.postgres = is_postgres_url(self.url)
        if self.postgres:
            from psycopg_pool import ConnectionPool

            # 원격 DB(Supabase)는 연결이 끊길 수 있어서 풀이 연결 상태를 확인하고 다시 잇는다
            self._pool = ConnectionPool(
                self.url, min_size=1, max_size=4, check=ConnectionPool.check_connection, open=True
            )
        else:
            if self.url != ":memory:":
                Path(self.url).parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(self.url, check_same_thread=False)
        types = DIALECT_TYPES["postgres" if self.postgres else "sqlite"]
        with self._tx() as tx:
            for statement in SCHEMA.format(**types).split(";"):
                if statement.strip():
                    tx.execute(statement)

    @contextmanager
    def _tx(self) -> Iterator[_Tx]:
        if self.postgres:
            with self._pool.connection() as conn, conn.transaction():
                yield _Tx(conn, postgres=True)
        else:
            with self._sqlite:
                yield _Tx(self._sqlite, postgres=False)

    def close(self) -> None:
        if self.postgres:
            self._pool.close()
        else:
            self._sqlite.close()

    # 하루 기록 ----------------------------------------------------------------

    def save_day(self, day: date, closing: dict[str, float], cash: int, card: int) -> None:
        d = day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO day_record (day, cash, card) VALUES (?, ?, ?)"
                " ON CONFLICT (day) DO UPDATE SET cash = excluded.cash, card = excluded.card",
                (d, cash, card),
            )
            tx.execute("DELETE FROM closing WHERE day = ?", (d,))
            tx.executemany("INSERT INTO closing (day, item_id, qty) VALUES (?, ?, ?)", [(d, i, q) for i, q in closing.items()])

    def save_received(self, day: date, received: dict[str, float]) -> None:
        d = day.isoformat()
        with self._tx() as tx:
            tx.execute("DELETE FROM received WHERE day = ?", (d,))
            tx.executemany(
                "INSERT INTO received (day, item_id, qty) VALUES (?, ?, ?)",
                [(d, i, q) for i, q in received.items() if q > 0],
            )

    def received(self, day: date) -> dict[str, float]:
        with self._tx() as tx:
            return dict(tx.all("SELECT item_id, qty FROM received WHERE day = ?", (day.isoformat(),)))

    def closing(self, day: date) -> dict[str, float]:
        with self._tx() as tx:
            return dict(tx.all("SELECT item_id, qty FROM closing WHERE day = ?", (day.isoformat(),)))

    def last_closed_day(self) -> date | None:
        with self._tx() as tx:
            row = tx.one("SELECT MAX(day) FROM day_record")
        return date.fromisoformat(row[0]) if row and row[0] else None

    def ledger(self) -> list[DayRecord]:
        """마감한 날들의 기록 (날짜순). 원격 DB에서 느리지 않게 테이블마다 한 번씩만 읽는다."""
        with self._tx() as tx:
            days = tx.all("SELECT day, cash, card FROM day_record ORDER BY day")
            closing_rows = tx.all("SELECT day, item_id, qty FROM closing")
            received_rows = tx.all("SELECT day, item_id, qty FROM received")
        closing: dict[str, dict[str, float]] = defaultdict(dict)
        received: dict[str, dict[str, float]] = defaultdict(dict)
        for d, i, q in closing_rows:
            closing[d][i] = q
        for d, i, q in received_rows:
            received[d][i] = q
        return [
            DayRecord(date.fromisoformat(d), dict(received[d]), dict(closing[d]), cash=cash, card=card)
            for d, cash, card in days
        ]

    # 가게 메모 ----------------------------------------------------------------

    def add_note(self, day: date, text: str) -> None:
        with self._tx() as tx:
            tx.execute("INSERT INTO note (day, text) VALUES (?, ?)", (day.isoformat(), text.strip()))

    def delete_note(self, note_id: int) -> None:
        with self._tx() as tx:
            tx.execute("DELETE FROM note WHERE id = ?", (note_id,))

    def notes(self, start: date | None = None, end: date | None = None) -> list[tuple[int, date, str]]:
        with self._tx() as tx:
            rows = tx.all("SELECT id, day, text FROM note ORDER BY day, id")
        result = [(i, date.fromisoformat(d), t) for i, d, t in rows]
        return [(i, d, t) for i, d, t in result if (start is None or d >= start) and (end is None or d <= end)]

    # 발주 ----------------------------------------------------------------------

    def save_plan(self, plan: OrderPlan) -> None:
        d = plan.order_day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO order_plan (order_day, arrival, summary, approved) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (order_day) DO UPDATE SET"
                " arrival = excluded.arrival, summary = excluded.summary, approved = excluded.approved",
                (d, plan.arrival.isoformat(), plan.summary, int(plan.approved)),
            )
            tx.execute("DELETE FROM order_line WHERE order_day = ?", (d,))
            tx.executemany(
                "INSERT INTO order_line (order_day, item_id, base, recommended, final, factors, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(d, ln.item_id, ln.base, ln.recommended, ln.final, ln.factors, ln.reason) for ln in plan.lines],
            )

    def plan(self, order_day: date) -> OrderPlan | None:
        d = order_day.isoformat()
        with self._tx() as tx:
            row = tx.one("SELECT arrival, summary, approved FROM order_plan WHERE order_day = ?", (d,))
            if row is None:
                return None
            lines = tx.all(
                "SELECT item_id, base, recommended, final, factors, reason FROM order_line"
                " WHERE order_day = ? ORDER BY item_id",
                (d,),
            )
        return OrderPlan(
            order_day, date.fromisoformat(row[0]), row[1], bool(row[2]), tuple(OrderLineRecord(*ln) for ln in lines)
        )

    def approved_finals(self, item_id: str, limit: int = 10) -> list[tuple[date, int]]:
        """승인된 지난 발주 (입고일, 수량). 추천 없이 넣는 발주의 가드레일에 쓴다."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT p.arrival, l.final FROM order_line l JOIN order_plan p ON p.order_day = l.order_day"
                " WHERE p.approved = 1 AND l.item_id = ? ORDER BY p.arrival DESC LIMIT ?",
                (item_id, limit),
            )
        return [(date.fromisoformat(a), f) for a, f in reversed(rows)]

    # 재고표 --------------------------------------------------------------------

    def save_sheet(self, day: date, image: bytes, rows: list[SheetRowRecord]) -> None:
        d = day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO sheet_image (day, data) VALUES (?, ?) ON CONFLICT (day) DO UPDATE SET data = excluded.data",
                (d, image),
            )
            tx.execute("DELETE FROM sheet_row WHERE day = ?", (d,))
            tx.executemany(
                "INSERT INTO sheet_row (day, idx, raw_name, raw_qty, item_id_read, qty_read, flags, item_id_final,"
                " qty_final) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        d,
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
        with self._tx() as tx:
            row = tx.one("SELECT data FROM sheet_image WHERE day = ?", (day.isoformat(),))
        return bytes(row[0]) if row else None

    def sheet_rows(self, day: date) -> list[SheetRowRecord]:
        with self._tx() as tx:
            rows = tx.all(
                "SELECT idx, raw_name, raw_qty, item_id_read, qty_read, flags, item_id_final, qty_final"
                " FROM sheet_row WHERE day = ? ORDER BY idx",
                (day.isoformat(),),
            )
        return [
            SheetRowRecord(idx, name, qty_text, iid, q, tuple(json.loads(flags)), fid, fq)
            for idx, name, qty_text, iid, q, flags, fid, fq in rows
        ]

    def finalize_sheet_rows(self, day: date, finals: dict[int, tuple[str | None, float | None]]) -> None:
        with self._tx() as tx:
            tx.executemany(
                "UPDATE sheet_row SET item_id_final = ?, qty_final = ? WHERE day = ? AND idx = ?",
                [(iid, q, day.isoformat(), idx) for idx, (iid, q) in finals.items()],
            )

    def confirmed_names(self) -> dict[str, str]:
        """재고표에 적힌 이름 → 사람이 확정한 품목 id. 같은 이름이 여러 품목으로 확정됐으면 가장 많은 쪽."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT raw_name, item_id_final, COUNT(*) AS n FROM sheet_row"
                " WHERE item_id_final IS NOT NULL GROUP BY raw_name, item_id_final ORDER BY n DESC, raw_name"
            )
        names: dict[str, str] = {}
        for raw, item_id, _ in rows:
            names.setdefault(raw.strip(), item_id)
        return names

    def correction_stats(self) -> dict[str, int]:
        """사람이 확정한 재고표 줄 중 모델이 읽은 값과 달랐던 줄 수. 실제 인식률 추적용."""
        changed = "(item_id_read IS DISTINCT FROM item_id_final OR qty_read IS DISTINCT FROM qty_final)"
        with self._tx() as tx:
            total, n_changed, n_flagged = tx.one(
                f"SELECT COUNT(*), SUM(CASE WHEN {changed} THEN 1 ELSE 0 END),"
                f" SUM(CASE WHEN {changed} AND flags <> '[]' THEN 1 ELSE 0 END)"
                " FROM sheet_row WHERE item_id_final IS NOT NULL"
            )
        return {"확정한 줄": total or 0, "사람이 고친 줄": n_changed or 0, "그중 확인 표시가 있던 줄": n_flagged or 0}

    # 다른 DB로 옮기기 -------------------------------------------------------------

    def is_empty(self) -> bool:
        with self._tx() as tx:
            return all(tx.one(f"SELECT COUNT(*) FROM {table}")[0] == 0 for table in TABLES)

    def dump(self) -> dict[str, list[tuple]]:
        with self._tx() as tx:
            return {table: tx.all(f"SELECT {', '.join(cols)} FROM {table}") for table, cols in TABLES.items()}

    def load(self, data: dict[str, list[tuple]]) -> dict[str, int]:
        """dump()로 뽑은 데이터를 빈 저장소에 넣는다. 테이블별로 넣은 행 수를 돌려준다."""
        if not self.is_empty():
            raise ValueError("데이터를 넣을 저장소가 비어 있지 않습니다.")
        with self._tx() as tx:
            for table, cols in TABLES.items():
                marks = ", ".join("?" for _ in cols)
                tx.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", data.get(table, []))
            if self.postgres:
                # id를 직접 넣었으므로 다음 메모 id가 겹치지 않게 순번을 맞춘다
                tx.execute("SELECT setval(pg_get_serial_sequence('note', 'id'), COALESCE(MAX(id), 0) + 1, false) FROM note")
        return {table: len(data.get(table, [])) for table in TABLES}
