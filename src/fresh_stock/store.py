"""저장소. SQLite(기본)와 PostgreSQL(Supabase 등)을 같은 코드로 쓴다.

- Database: DB 연결, 스키마, 가게와 가게 멤버(구글 계정 이메일)
- Store: 가게 하나의 데이터. 모든 조회·저장이 그 가게(store_id)로 한정된다

하루 기록(입고·마감 재고·현금·카드), 가게 메모와 메모 조사 결과, 발주, 재고표 인식 결과,
가게 설정(품목·거래처, 장날·입고 요일, 경험 노트)을 저장한다.
재고표 인식 결과는 모델이 읽은 값과 사람이 확정한 값을 함께 남긴다. 실제 매장에서 쓰기 시작하면
이 기록이 그대로 실제 손글씨 인식률 평가 데이터가 된다.

    db = Database("out/store.db")                        # SQLite 파일
    db = Database("postgresql://user:pw@host:5432/db")   # PostgreSQL (Supabase 연결 문자열)
    store = Store(db, store_id)

SQL은 두 DB에서 모두 도는 문법만 쓴다 (ON CONFLICT ... DO UPDATE, IS DISTINCT FROM, CASE WHEN).
"""

import json
import sqlite3
import threading
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .accounting import DayRecord
from .catalog import Catalog
from .domain import Category, Item, Supplier
from .market_calendar import now_kst

SCHEMA_VERSION = "2"
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stores (
    id {auto_id},
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
    store_id INTEGER NOT NULL, email TEXT NOT NULL,
    role TEXT NOT NULL,  -- owner: 가게 관리까지, staff: 매일 업무
    PRIMARY KEY (store_id, email)
);
CREATE TABLE IF NOT EXISTS day_record (
    store_id INTEGER NOT NULL, day TEXT NOT NULL,
    cash INTEGER NOT NULL DEFAULT 0,
    card INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, day)
);
CREATE TABLE IF NOT EXISTS closing (
    store_id INTEGER NOT NULL, day TEXT NOT NULL, item_id TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (store_id, day, item_id)
);
CREATE TABLE IF NOT EXISTS received (
    store_id INTEGER NOT NULL, day TEXT NOT NULL, item_id TEXT NOT NULL, qty DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (store_id, day, item_id)
);
CREATE TABLE IF NOT EXISTS note (
    id {auto_id},
    store_id INTEGER NOT NULL,
    day TEXT NOT NULL, text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '사장님',  -- 사장님 / 날씨 / 명절
    author TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS note_research (
    note_id INTEGER PRIMARY KEY,
    confirmed INTEGER NOT NULL,
    summary TEXT NOT NULL,
    items TEXT NOT NULL,    -- 영향받을 품목 id 목록 (JSON)
    sources TEXT NOT NULL,  -- [{{"title", "url"}}] (JSON)
    researched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS order_plan (
    store_id INTEGER NOT NULL, order_day TEXT NOT NULL,
    arrival TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    approved INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (store_id, order_day)
);
CREATE TABLE IF NOT EXISTS order_line (
    store_id INTEGER NOT NULL, order_day TEXT NOT NULL, item_id TEXT NOT NULL,
    base INTEGER NOT NULL,
    recommended INTEGER NOT NULL,
    final INTEGER NOT NULL,
    factors TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (store_id, order_day, item_id)
);
CREATE TABLE IF NOT EXISTS sheet_image (
    store_id INTEGER NOT NULL, day TEXT NOT NULL, data {blob} NOT NULL,
    PRIMARY KEY (store_id, day)
);
CREATE TABLE IF NOT EXISTS sheet_row (
    store_id INTEGER NOT NULL, day TEXT NOT NULL, idx INTEGER NOT NULL,
    raw_name TEXT NOT NULL, raw_qty TEXT NOT NULL,
    item_id_read TEXT, qty_read DOUBLE PRECISION, flags TEXT NOT NULL,
    item_id_final TEXT, qty_final DOUBLE PRECISION,
    PRIMARY KEY (store_id, day, idx)
);
CREATE TABLE IF NOT EXISTS catalog_supplier (
    store_id INTEGER NOT NULL, id TEXT NOT NULL, pos INTEGER NOT NULL,
    name TEXT NOT NULL, channel TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (store_id, id)
);
CREATE TABLE IF NOT EXISTS message_sent (
    -- 거래처 문자·전화 버튼을 누른 시각 (주문을 두 번 보내지 않게 화면에 표시)
    store_id INTEGER NOT NULL, order_day TEXT NOT NULL, supplier_id TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY (store_id, order_day, supplier_id)
);
CREATE TABLE IF NOT EXISTS catalog_item (
    store_id INTEGER NOT NULL, id TEXT NOT NULL, pos INTEGER NOT NULL,
    name TEXT NOT NULL, category TEXT NOT NULL, unit TEXT NOT NULL, supplier_id TEXT NOT NULL,
    base_demand DOUBLE PRECISION NOT NULL, price INTEGER NOT NULL, cost INTEGER NOT NULL, shelf_life INTEGER NOT NULL,
    active INTEGER NOT NULL,  -- 0: 취급을 그만둔 품목 (지난 기록용으로 남긴다)
    PRIMARY KEY (store_id, id)
);
CREATE TABLE IF NOT EXISTS store_setting (
    store_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (store_id, key)
);
CREATE TABLE IF NOT EXISTS sheet_buy (
    -- 사입 칸이 있는 재고표의 사입 수량·가격 (sheet_row와 같은 줄 번호). 모델이 읽은 값과 사람이 확정한 값
    store_id INTEGER NOT NULL, day TEXT NOT NULL, idx INTEGER NOT NULL,
    raw_qty TEXT NOT NULL DEFAULT '', raw_price TEXT NOT NULL DEFAULT '',
    qty_read DOUBLE PRECISION, price_read INTEGER, qty_final DOUBLE PRECISION, price_final INTEGER,
    PRIMARY KEY (store_id, day, idx)
);
CREATE TABLE IF NOT EXISTS received_price (
    -- 그날 들어온 품목을 단위당 얼마에 샀는지 (재고표 사입 가격)
    store_id INTEGER NOT NULL, day TEXT NOT NULL, item_id TEXT NOT NULL, price INTEGER NOT NULL,
    PRIMARY KEY (store_id, day, item_id)
)
"""
DIALECT_TYPES = {
    "sqlite": {"auto_id": "INTEGER PRIMARY KEY AUTOINCREMENT", "blob": "BLOB"},
    "postgres": {"auto_id": "BIGSERIAL PRIMARY KEY", "blob": "BYTEA"},
}
# 데이터를 옮길 때의 테이블 순서와 열
TABLES = {
    "stores": ("id", "name", "created_at"),
    "members": ("store_id", "email", "role"),
    "day_record": ("store_id", "day", "cash", "card"),
    "closing": ("store_id", "day", "item_id", "qty"),
    "received": ("store_id", "day", "item_id", "qty"),
    "note": ("id", "store_id", "day", "text", "source", "author", "created_at"),
    "note_research": ("note_id", "confirmed", "summary", "items", "sources", "researched_at"),
    "order_plan": ("store_id", "order_day", "arrival", "summary", "approved"),
    "order_line": ("store_id", "order_day", "item_id", "base", "recommended", "final", "factors", "reason"),
    "sheet_image": ("store_id", "day", "data"),
    "sheet_row": (
        "store_id", "day", "idx", "raw_name", "raw_qty", "item_id_read", "qty_read", "flags", "item_id_final",
        "qty_final",
    ),
    "catalog_supplier": ("store_id", "id", "pos", "name", "channel", "phone"),
    "message_sent": ("store_id", "order_day", "supplier_id", "sent_at"),
    "catalog_item": (
        "store_id", "id", "pos", "name", "category", "unit", "supplier_id", "base_demand", "price", "cost",
        "shelf_life", "active",
    ),
    "store_setting": ("store_id", "key", "value"),
    "sheet_buy": ("store_id", "day", "idx", "raw_qty", "raw_price", "qty_read", "price_read", "qty_final", "price_final"),
    "received_price": ("store_id", "day", "item_id", "price"),
}  # fmt: skip
SERIAL_TABLES = ("stores", "note")  # 옮긴 뒤 다음 id를 맞춰야 하는 테이블
OWNER, STAFF = "owner", "staff"
OWNER_NOTE = "사장님"


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
    # 사입 칸이 있는 재고표만 (sheet_buy)
    raw_buy_qty: str = ""
    raw_buy_price: str = ""
    buy_qty_read: float | None = None
    buy_price_read: int | None = None
    buy_qty_final: float | None = None
    buy_price_final: int | None = None


@dataclass(frozen=True)
class Research:
    """사장님 메모를 AI가 웹 검색으로 확인한 결과."""

    confirmed: bool
    summary: str
    items: tuple[str, ...]
    sources: tuple[tuple[str, str], ...]  # (제목, 주소)
    researched_at: str = ""


@dataclass(frozen=True)
class Note:
    id: int
    day: date
    text: str
    source: str  # 사장님 / 날씨 / 명절
    author: str
    research: Research | None


@dataclass(frozen=True)
class Membership:
    store_id: int
    store_name: str
    role: str


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


def _now() -> str:
    return now_kst().isoformat(timespec="seconds")


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


class Database:
    def __init__(self, url: Path | str):
        self.url = str(url)
        self.postgres = is_postgres_url(self.url)
        if self.postgres:
            from psycopg_pool import ConnectionPool

            # 원격 DB(Supabase)는 연결이 끊길 수 있어서 풀이 연결 상태를 확인하고 다시 잇는다
            # prepare_threshold=None: Supabase 트랜잭션 모드 풀러(포트 6543)는 prepared statement를 지원하지 않는다
            self._pool = ConnectionPool(
                self.url,
                min_size=1,
                max_size=4,
                check=ConnectionPool.check_connection,
                kwargs={"prepare_threshold": None},
                open=True,
            )
        else:
            if self.url != ":memory:":
                Path(self.url).parent.mkdir(parents=True, exist_ok=True)
            self._sqlite = sqlite3.connect(self.url, check_same_thread=False)
            # 웹 요청과 AI 조사가 여러 스레드에서 같은 연결을 쓰므로 한 번에 하나씩만 쓴다
            self._lock = threading.RLock()
        self._check_old_schema()
        types = DIALECT_TYPES["postgres" if self.postgres else "sqlite"]
        with self.tx() as tx:
            for statement in SCHEMA.format(**types).split(";"):
                if statement.strip():
                    tx.execute(statement)
            tx.execute(
                "INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT (key) DO NOTHING",
                (SCHEMA_VERSION,),
            )
        self._add_missing_columns()

    # 나중에 생긴 열: 이미 만들어진 DB에는 CREATE TABLE IF NOT EXISTS가 열을 더하지 않는다
    ADDED_COLUMNS = (("catalog_supplier", "phone", "TEXT NOT NULL DEFAULT ''"),)

    def _add_missing_columns(self) -> None:
        for table, column, ddl in self.ADDED_COLUMNS:
            try:
                with self.tx() as tx:  # PostgreSQL은 실패한 트랜잭션을 더 못 쓰므로 확인은 따로
                    tx.one(f"SELECT {column} FROM {table} LIMIT 1")
            except Exception:
                with self.tx() as tx:
                    tx.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def _check_old_schema(self) -> None:
        """가게 구분이 없던 예전 DB면 알아보기 쉬운 오류를 낸다."""
        # PostgreSQL은 오류가 난 트랜잭션을 더 쓸 수 없어서, 확인용 트랜잭션 밖에서 오류를 잡는다
        try:
            with self.tx() as tx:
                tx.one("SELECT store_id FROM day_record LIMIT 1")
        except Exception as e:  # 테이블이 없으면 새 DB, 열이 없으면 예전 DB
            if "store_id" in str(e):
                raise RuntimeError(
                    f"{mask_url(self.url)}은 가게 구분이 없던 예전 형식입니다. 새 DB 파일로 시작해 주세요."
                ) from e

    @contextmanager
    def tx(self) -> Iterator[_Tx]:
        if self.postgres:
            with self._pool.connection() as conn, conn.transaction():
                yield _Tx(conn, postgres=True)
        else:
            with self._lock, self._sqlite:
                yield _Tx(self._sqlite, postgres=False)

    def close(self) -> None:
        if self.postgres:
            self._pool.close()
        else:
            self._sqlite.close()

    # 가게와 멤버 -----------------------------------------------------------------

    def create_store(self, name: str, owner_email: str) -> int:
        with self.tx() as tx:
            row = tx.one("INSERT INTO stores (name, created_at) VALUES (?, ?) RETURNING id", (name.strip(), _now()))
            store_id = int(row[0])
            tx.execute(
                "INSERT INTO members (store_id, email, role) VALUES (?, ?, ?)", (store_id, owner_email.lower(), OWNER)
            )
        return store_id

    def store_name(self, store_id: int) -> str | None:
        with self.tx() as tx:
            row = tx.one("SELECT name FROM stores WHERE id = ?", (store_id,))
        return row[0] if row else None

    def rename_store(self, store_id: int, name: str) -> None:
        with self.tx() as tx:
            tx.execute("UPDATE stores SET name = ? WHERE id = ?", (name.strip(), store_id))

    def store_ids(self) -> list[int]:
        with self.tx() as tx:
            return [int(r[0]) for r in tx.all("SELECT id FROM stores ORDER BY id")]

    def memberships(self, email: str) -> list[Membership]:
        with self.tx() as tx:
            rows = tx.all(
                "SELECT s.id, s.name, m.role FROM members m JOIN stores s ON s.id = m.store_id"
                " WHERE m.email = ? ORDER BY s.id",
                (email.lower(),),
            )
        return [Membership(int(i), n, r) for i, n, r in rows]

    def role(self, store_id: int, email: str) -> str | None:
        with self.tx() as tx:
            row = tx.one("SELECT role FROM members WHERE store_id = ? AND email = ?", (store_id, email.lower()))
        return row[0] if row else None

    def members(self, store_id: int) -> list[tuple[str, str]]:
        with self.tx() as tx:
            return tx.all(
                "SELECT email, role FROM members WHERE store_id = ?"
                " ORDER BY CASE WHEN role = 'owner' THEN 0 ELSE 1 END, email",
                (store_id,),
            )

    def add_member(self, store_id: int, email: str, role: str = STAFF) -> None:
        with self.tx() as tx:
            tx.execute(
                "INSERT INTO members (store_id, email, role) VALUES (?, ?, ?)"
                " ON CONFLICT (store_id, email) DO UPDATE SET role = excluded.role",
                (store_id, email.strip().lower(), role),
            )

    def remove_member(self, store_id: int, email: str) -> None:
        """멤버를 뺀다. 가게에 사장님이 한 명도 안 남게 되면 빼지 않는다."""
        email = email.lower()
        with self.tx() as tx:
            owners = [e for e, r in tx.all("SELECT email, role FROM members WHERE store_id = ?", (store_id,)) if r == OWNER]
            if owners == [email]:
                raise ValueError("가게에 사장님이 한 명은 있어야 합니다.")
            tx.execute("DELETE FROM members WHERE store_id = ? AND email = ?", (store_id, email))

    # 다른 DB로 옮기기 -------------------------------------------------------------

    def is_empty(self) -> bool:
        with self.tx() as tx:
            return all(tx.one(f"SELECT COUNT(*) FROM {table}")[0] == 0 for table in TABLES)

    def dump(self) -> dict[str, list[tuple]]:
        with self.tx() as tx:
            return {table: tx.all(f"SELECT {', '.join(cols)} FROM {table}") for table, cols in TABLES.items()}

    def load(self, data: dict[str, list[tuple]]) -> dict[str, int]:
        """dump()로 뽑은 데이터를 빈 DB에 넣는다. 테이블별로 넣은 행 수를 돌려준다."""
        if not self.is_empty():
            raise ValueError("데이터를 넣을 저장소가 비어 있지 않습니다.")
        with self.tx() as tx:
            for table, cols in TABLES.items():
                marks = ", ".join("?" for _ in cols)
                tx.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})", data.get(table, []))
            if self.postgres:
                # id를 직접 넣었으므로 다음 id가 겹치지 않게 순번을 맞춘다
                for table in SERIAL_TABLES:
                    tx.execute(
                        f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), COALESCE(MAX(id), 0) + 1, false)"
                        f" FROM {table}"
                    )
        return {table: len(data.get(table, [])) for table in TABLES}


class Store:
    """가게 하나의 데이터. Store(":memory:")처럼 주소만 주면 새 DB에 가게 하나를 만들어 쓴다 (테스트·실험용)."""

    def __init__(self, db: "Database | Path | str", store_id: int | None = None):
        self.db = db if isinstance(db, Database) else Database(db)
        if store_id is None:
            ids = self.db.store_ids()
            store_id = ids[0] if ids else self.db.create_store("우리가게", "owner@local")
        self.store_id = store_id

    @property
    def postgres(self) -> bool:
        return self.db.postgres

    def _tx(self):
        return self.db.tx()

    def close(self) -> None:
        self.db.close()

    # 하루 기록 ----------------------------------------------------------------

    def save_day(self, day: date, closing: dict[str, float], cash: int, card: int) -> None:
        s, d = self.store_id, day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO day_record (store_id, day, cash, card) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (store_id, day) DO UPDATE SET cash = excluded.cash, card = excluded.card",
                (s, d, cash, card),
            )
            tx.execute("DELETE FROM closing WHERE store_id = ? AND day = ?", (s, d))
            tx.executemany(
                "INSERT INTO closing (store_id, day, item_id, qty) VALUES (?, ?, ?, ?)",
                [(s, d, i, q) for i, q in closing.items()],
            )

    def save_received(self, day: date, received: dict[str, float]) -> None:
        s, d = self.store_id, day.isoformat()
        with self._tx() as tx:
            tx.execute("DELETE FROM received WHERE store_id = ? AND day = ?", (s, d))
            tx.executemany(
                "INSERT INTO received (store_id, day, item_id, qty) VALUES (?, ?, ?, ?)",
                [(s, d, i, q) for i, q in received.items() if q > 0],
            )

    def save_prices(self, day: date, prices: dict[str, int]) -> None:
        """그날 들어온 품목의 단위당 사입 가격. 그날 기록을 통째로 바꾼다."""
        s, d = self.store_id, day.isoformat()
        with self._tx() as tx:
            tx.execute("DELETE FROM received_price WHERE store_id = ? AND day = ?", (s, d))
            tx.executemany(
                "INSERT INTO received_price (store_id, day, item_id, price) VALUES (?, ?, ?, ?)",
                [(s, d, i, int(p)) for i, p in prices.items() if p is not None and p > 0],
            )

    def prices(self, day: date) -> dict[str, int]:
        with self._tx() as tx:
            return dict(
                tx.all(
                    "SELECT item_id, price FROM received_price WHERE store_id = ? AND day = ?",
                    (self.store_id, day.isoformat()),
                )
            )

    def last_prices(self, before: date | None = None) -> dict[str, int]:
        """품목별 가장 최근 사입 가격 (before가 있으면 그 전날까지)."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT item_id, day, price FROM received_price WHERE store_id = ? AND day < ? ORDER BY day",
                (self.store_id, before.isoformat() if before else "9999-12-31"),
            )
        return {item_id: price for item_id, _, price in rows}

    def purchase_totals(self) -> dict[date, int]:
        """날짜별 사입 금액 (입고 수량 × 사입 가격). 사입 가격이 적힌 날만."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT r.day, SUM(r.qty * p.price) FROM received r JOIN received_price p"
                " ON p.store_id = r.store_id AND p.day = r.day AND p.item_id = r.item_id"
                " WHERE r.store_id = ? GROUP BY r.day",
                (self.store_id,),
            )
        return {date.fromisoformat(d): round(total) for d, total in rows}

    def received(self, day: date) -> dict[str, float]:
        with self._tx() as tx:
            return dict(
                tx.all("SELECT item_id, qty FROM received WHERE store_id = ? AND day = ?", (self.store_id, day.isoformat()))
            )

    def closing(self, day: date) -> dict[str, float]:
        with self._tx() as tx:
            return dict(
                tx.all("SELECT item_id, qty FROM closing WHERE store_id = ? AND day = ?", (self.store_id, day.isoformat()))
            )

    def last_closed_day(self) -> date | None:
        with self._tx() as tx:
            row = tx.one("SELECT MAX(day) FROM day_record WHERE store_id = ?", (self.store_id,))
        return date.fromisoformat(row[0]) if row and row[0] else None

    def ledger(self) -> list[DayRecord]:
        """마감한 날들의 기록 (날짜순). 원격 DB에서 느리지 않게 테이블마다 한 번씩만 읽는다."""
        s = (self.store_id,)
        with self._tx() as tx:
            days = tx.all("SELECT day, cash, card FROM day_record WHERE store_id = ? ORDER BY day", s)
            closing_rows = tx.all("SELECT day, item_id, qty FROM closing WHERE store_id = ?", s)
            received_rows = tx.all("SELECT day, item_id, qty FROM received WHERE store_id = ?", s)
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

    def add_note(self, day: date, text: str, source: str = OWNER_NOTE, author: str = "") -> int:
        with self._tx() as tx:
            row = tx.one(
                "INSERT INTO note (store_id, day, text, source, author, created_at) VALUES (?, ?, ?, ?, ?, ?)"
                " RETURNING id",
                (self.store_id, day.isoformat(), text.strip(), source, author, _now()),
            )
        return int(row[0])

    def delete_note(self, note_id: int) -> None:
        with self._tx() as tx:
            # note_research에는 가게 구분이 없으므로, 이 가게의 메모일 때만 조사 결과를 지운다
            tx.execute(
                "DELETE FROM note_research WHERE note_id IN (SELECT id FROM note WHERE id = ? AND store_id = ?)",
                (note_id, self.store_id),
            )
            tx.execute("DELETE FROM note WHERE id = ? AND store_id = ?", (note_id, self.store_id))

    def notes(self, start: date | None = None, end: date | None = None) -> list[tuple[int, date, str]]:
        return [(n.id, n.day, n.text) for n in self.note_records(start, end)]

    def note_records(self, start: date | None = None, end: date | None = None) -> list[Note]:
        with self._tx() as tx:
            rows = tx.all(
                "SELECT n.id, n.day, n.text, n.source, n.author,"
                " r.confirmed, r.summary, r.items, r.sources, r.researched_at"
                " FROM note n LEFT JOIN note_research r ON r.note_id = n.id"
                " WHERE n.store_id = ? ORDER BY n.day, n.id",
                (self.store_id,),
            )
        notes = []
        for nid, d, text, source, author, confirmed, summary, items, sources, at in rows:
            day = date.fromisoformat(d)
            if (start is not None and day < start) or (end is not None and day > end):
                continue
            research = None
            if summary is not None:
                research = Research(
                    bool(confirmed),
                    summary,
                    tuple(json.loads(items)),
                    tuple((s["title"], s["url"]) for s in json.loads(sources)),
                    at,
                )
            notes.append(Note(int(nid), day, text, source, author, research))
        return notes

    def save_research(self, note_id: int, research: Research) -> None:
        with self._tx() as tx:
            if tx.one("SELECT 1 FROM note WHERE id = ? AND store_id = ?", (note_id, self.store_id)) is None:
                return  # 조사하는 동안 지워진 메모
            tx.execute(
                "INSERT INTO note_research (note_id, confirmed, summary, items, sources, researched_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (note_id) DO UPDATE SET confirmed = excluded.confirmed,"
                " summary = excluded.summary, items = excluded.items, sources = excluded.sources,"
                " researched_at = excluded.researched_at",
                (
                    note_id,
                    int(research.confirmed),
                    research.summary,
                    json.dumps(list(research.items)),
                    json.dumps([{"title": t, "url": u} for t, u in research.sources], ensure_ascii=False),
                    research.researched_at or _now(),
                ),
            )

    # 발주 ----------------------------------------------------------------------

    def save_plan(self, plan: OrderPlan) -> None:
        s, d = self.store_id, plan.order_day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO order_plan (store_id, order_day, arrival, summary, approved) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (store_id, order_day) DO UPDATE SET"
                " arrival = excluded.arrival, summary = excluded.summary, approved = excluded.approved",
                (s, d, plan.arrival.isoformat(), plan.summary, int(plan.approved)),
            )
            tx.execute("DELETE FROM order_line WHERE store_id = ? AND order_day = ?", (s, d))
            tx.executemany(
                "INSERT INTO order_line (store_id, order_day, item_id, base, recommended, final, factors, reason)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(s, d, ln.item_id, ln.base, ln.recommended, ln.final, ln.factors, ln.reason) for ln in plan.lines],
            )

    def plan(self, order_day: date) -> OrderPlan | None:
        s, d = self.store_id, order_day.isoformat()
        with self._tx() as tx:
            row = tx.one(
                "SELECT arrival, summary, approved FROM order_plan WHERE store_id = ? AND order_day = ?", (s, d)
            )
            if row is None:
                return None
            lines = tx.all(
                "SELECT item_id, base, recommended, final, factors, reason FROM order_line"
                " WHERE store_id = ? AND order_day = ? ORDER BY item_id",
                (s, d),
            )
        return OrderPlan(
            order_day, date.fromisoformat(row[0]), row[1], bool(row[2]), tuple(OrderLineRecord(*ln) for ln in lines)
        )

    def delete_unapproved_plan(self, order_day: date) -> None:
        """승인 전 발주 추천을 지운다 (다음에 열 때 새로 만든다). 승인한 발주는 그대로 둔다."""
        s, d = self.store_id, order_day.isoformat()
        with self._tx() as tx:
            if tx.one("SELECT approved FROM order_plan WHERE store_id = ? AND order_day = ?", (s, d)) != (0,):
                return
            tx.execute("DELETE FROM order_line WHERE store_id = ? AND order_day = ?", (s, d))
            tx.execute("DELETE FROM order_plan WHERE store_id = ? AND order_day = ?", (s, d))

    def approved_finals(self, item_id: str, limit: int = 10) -> list[tuple[date, int]]:
        """승인된 지난 발주 (입고일, 수량). 추천 없이 넣는 발주의 가드레일에 쓴다."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT p.arrival, l.final FROM order_line l"
                " JOIN order_plan p ON p.store_id = l.store_id AND p.order_day = l.order_day"
                " WHERE l.store_id = ? AND p.approved = 1 AND l.item_id = ? ORDER BY p.arrival DESC LIMIT ?",
                (self.store_id, item_id, limit),
            )
        return [(date.fromisoformat(a), f) for a, f in reversed(rows)]

    # 재고표 --------------------------------------------------------------------

    def save_sheet(self, day: date, image: bytes, rows: list[SheetRowRecord]) -> None:
        s, d = self.store_id, day.isoformat()
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO sheet_image (store_id, day, data) VALUES (?, ?, ?)"
                " ON CONFLICT (store_id, day) DO UPDATE SET data = excluded.data",
                (s, d, image),
            )
            tx.execute("DELETE FROM sheet_row WHERE store_id = ? AND day = ?", (s, d))
            tx.execute("DELETE FROM sheet_buy WHERE store_id = ? AND day = ?", (s, d))
            buys = [
                (s, d, r.idx, r.raw_buy_qty, r.raw_buy_price, r.buy_qty_read, r.buy_price_read, r.buy_qty_final,
                 r.buy_price_final)
                for r in rows
                if r.raw_buy_qty or r.raw_buy_price or r.buy_qty_read is not None or r.buy_price_read is not None
            ]  # fmt: skip
            tx.executemany(
                "INSERT INTO sheet_buy (store_id, day, idx, raw_qty, raw_price, qty_read, price_read, qty_final,"
                " price_final) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                buys,
            )
            tx.executemany(
                "INSERT INTO sheet_row (store_id, day, idx, raw_name, raw_qty, item_id_read, qty_read, flags,"
                " item_id_final, qty_final) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        s,
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
            row = tx.one("SELECT data FROM sheet_image WHERE store_id = ? AND day = ?", (self.store_id, day.isoformat()))
        return bytes(row[0]) if row else None

    def sheet_rows(self, day: date) -> list[SheetRowRecord]:
        with self._tx() as tx:
            rows = tx.all(
                "SELECT r.idx, r.raw_name, r.raw_qty, r.item_id_read, r.qty_read, r.flags, r.item_id_final, r.qty_final,"
                " b.raw_qty, b.raw_price, b.qty_read, b.price_read, b.qty_final, b.price_final"
                " FROM sheet_row r LEFT JOIN sheet_buy b ON b.store_id = r.store_id AND b.day = r.day AND b.idx = r.idx"
                " WHERE r.store_id = ? AND r.day = ? ORDER BY r.idx",
                (self.store_id, day.isoformat()),
            )
        return [
            SheetRowRecord(
                idx, name, qty_text, iid, q, tuple(json.loads(flags)), fid, fq, braw or "", praw or "", bq, bp, bqf, bpf
            )
            for idx, name, qty_text, iid, q, flags, fid, fq, braw, praw, bq, bp, bqf, bpf in rows
        ]

    def finalize_sheet_rows(self, day: date, finals: dict[int, tuple[str | None, float | None]]) -> None:
        with self._tx() as tx:
            tx.executemany(
                "UPDATE sheet_row SET item_id_final = ?, qty_final = ? WHERE store_id = ? AND day = ? AND idx = ?",
                [(iid, q, self.store_id, day.isoformat(), idx) for idx, (iid, q) in finals.items()],
            )

    def finalize_sheet_buys(self, day: date, buys: dict[int, tuple[float | None, int | None]]) -> None:
        """사람이 확정한 사입 수량·가격 (줄 번호 → (수량, 가격)). 확인 화면에서 더한 줄도 넣는다."""
        with self._tx() as tx:
            tx.executemany(
                "INSERT INTO sheet_buy (store_id, day, idx, qty_final, price_final) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (store_id, day, idx) DO UPDATE SET qty_final = excluded.qty_final,"
                " price_final = excluded.price_final",
                [(self.store_id, day.isoformat(), idx, q, p) for idx, (q, p) in buys.items()],
            )

    def confirmed_names(self) -> dict[str, str]:
        """재고표에 적힌 이름 → 사람이 확정한 품목 id. 같은 이름이 여러 품목으로 확정됐으면 가장 많은 쪽."""
        with self._tx() as tx:
            rows = tx.all(
                "SELECT raw_name, item_id_final, COUNT(*) AS n FROM sheet_row"
                " WHERE store_id = ? AND item_id_final IS NOT NULL"
                " GROUP BY raw_name, item_id_final ORDER BY n DESC, raw_name",
                (self.store_id,),
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
                " FROM sheet_row WHERE store_id = ? AND item_id_final IS NOT NULL",
                (self.store_id,),
            )
        return {"확정한 줄": total or 0, "사람이 고친 줄": n_changed or 0, "그중 확인 표시가 있던 줄": n_flagged or 0}

    # 가게 설정 ------------------------------------------------------------------

    def catalog(self) -> Catalog | None:
        """이 가게의 품목·거래처. 아직 고친 적이 없으면 None이고, 그동안은 기본 목록을 쓴다."""
        s = (self.store_id,)
        with self._tx() as tx:
            suppliers = tx.all(
                "SELECT id, name, channel, phone FROM catalog_supplier WHERE store_id = ? ORDER BY pos", s
            )
            rows = tx.all(
                "SELECT id, name, category, unit, supplier_id, base_demand, price, cost, shelf_life, active"
                " FROM catalog_item WHERE store_id = ? ORDER BY pos",
                s,
            )
        if not rows:
            return None
        items, retired = [], []
        for iid, name, category, unit, supplier_id, demand, price, cost, shelf_life, active in rows:
            item = Item(iid, name, Category(category), unit, supplier_id, float(demand), price, cost, shelf_life)
            (items if active else retired).append(item)
        return Catalog(tuple(items), tuple(Supplier(*r) for r in suppliers), tuple(retired))

    def save_catalog(self, catalog: Catalog) -> None:
        """품목·거래처 목록을 통째로 바꾼다 (100줄 안팎이라 고친 것만 골라 쓰지 않는다)."""
        s = self.store_id
        with self._tx() as tx:
            tx.execute("DELETE FROM catalog_supplier WHERE store_id = ?", (s,))
            tx.execute("DELETE FROM catalog_item WHERE store_id = ?", (s,))
            tx.executemany(
                "INSERT INTO catalog_supplier (store_id, id, pos, name, channel, phone) VALUES (?, ?, ?, ?, ?, ?)",
                [(s, sup.id, n, sup.name, sup.channel, sup.phone) for n, sup in enumerate(catalog.suppliers)],
            )
            rows = [
                (s, i.id, n, i.name, i.category.value, i.unit, i.supplier_id, i.base_demand, i.price, i.cost,
                 i.shelf_life, int(n < len(catalog.items)))
                for n, i in enumerate(catalog.all_items)
            ]  # fmt: skip
            tx.executemany(
                "INSERT INTO catalog_item (store_id, id, pos, name, category, unit, supplier_id, base_demand, price,"
                " cost, shelf_life, active) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

    def mark_sent(self, order_day: date, supplier_id: str) -> None:
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO message_sent (store_id, order_day, supplier_id, sent_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (store_id, order_day, supplier_id) DO UPDATE SET sent_at = excluded.sent_at",
                (self.store_id, order_day.isoformat(), supplier_id, _now()),
            )

    def sent(self, order_day: date) -> dict[str, str]:
        """거래처 id → 문자·전화 버튼을 누른 시각 (ISO)."""
        with self._tx() as tx:
            return dict(
                tx.all(
                    "SELECT supplier_id, sent_at FROM message_sent WHERE store_id = ? AND order_day = ?",
                    (self.store_id, order_day.isoformat()),
                )
            )

    def settings(self) -> dict[str, str]:
        with self._tx() as tx:
            return dict(tx.all("SELECT key, value FROM store_setting WHERE store_id = ?", (self.store_id,)))

    def save_setting(self, key: str, value: str) -> None:
        with self._tx() as tx:
            tx.execute(
                "INSERT INTO store_setting (store_id, key, value) VALUES (?, ?, ?)"
                " ON CONFLICT (store_id, key) DO UPDATE SET value = excluded.value",
                (self.store_id, key, value),
            )
