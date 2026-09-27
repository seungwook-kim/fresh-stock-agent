"""저장소 테스트. SQLite는 항상, PostgreSQL은 FRESH_STOCK_TEST_PG에 주소가 있을 때 같은 테스트를 돌린다.

    FRESH_STOCK_TEST_PG=postgresql://postgres:@127.0.0.1:5432/postgres uv run pytest tests/test_store.py
"""

import os
from datetime import date

import pytest

from fresh_stock.store import OWNER, STAFF, TABLES, Database, OrderLineRecord, OrderPlan, Research, SheetRowRecord, Store

PG_URL = os.environ.get("FRESH_STOCK_TEST_PG")
D1, D2 = date(2026, 7, 1), date(2026, 7, 2)


def _fresh_pg(url: str) -> Database:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for table in [*TABLES, "meta"]:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    return Database(url)


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="FRESH_STOCK_TEST_PG 없음"))]


@pytest.fixture(params=BACKENDS)
def db(request):
    d = _fresh_pg(PG_URL) if request.param == "postgres" else Database(":memory:")
    yield d
    d.close()


@pytest.fixture
def store(db):
    return Store(db, db.create_store("싱싱청과", "boss@gmail.com"))


def _fill(s: Store) -> None:
    s.save_received(D1, {"tomato": 8, "apple": 0})
    s.save_day(D1, {"tomato": 4.5, "apple": 0.0}, cash=100, card=200)
    s.save_day(D2, {"tomato": 1.0}, cash=50, card=60)
    s.save_day(D2, {"tomato": 2.0}, cash=70, card=80)  # 같은 날 다시 저장하면 덮어쓴다
    s.add_note(D2, " 비 예보 ", source="날씨")
    s.add_note(D1, "TV에서 양배추 소개", author="boss@gmail.com")
    lines = (OrderLineRecord("tomato", 5, 7, 7, "07-02 ×1.4", "TV"), OrderLineRecord("apple", 2, 2, 3, "", ""))
    s.save_plan(OrderPlan(D1, D2, "요약", False, lines))
    s.save_plan(OrderPlan(D1, D2, "승인", True, lines))
    rows = [
        SheetRowRecord(0, "토마", "4반", "tomato", 4.5, (), None, None),
        SheetRowRecord(1, "방토", "2", None, 2.0, ("품목을 찾지 못함",), None, None),
    ]
    s.save_sheet(D1, b"\xff\xd8jpg", rows)
    s.finalize_sheet_rows(D1, {0: ("tomato", 4.5), 1: ("cherry_tomato", 2.0)})


def test_day_records_and_ledger(store):
    _fill(store)
    assert store.received(D1) == {"tomato": 8}  # 0은 저장하지 않는다
    assert store.closing(D2) == {"tomato": 2.0}
    assert store.last_closed_day() == D2
    ledger = store.ledger()
    assert [r.day for r in ledger] == [D1, D2]
    assert ledger[0].received == {"tomato": 8} and ledger[0].closing == {"tomato": 4.5, "apple": 0.0}
    assert ledger[1].revenue == 150


def test_notes_and_research(store):
    _fill(store)
    notes = store.note_records()
    assert [(n.day, n.text, n.source) for n in notes] == [(D1, "TV에서 양배추 소개", "사장님"), (D2, "비 예보", "날씨")]
    assert notes[0].author == "boss@gmail.com" and notes[0].research is None
    research = Research(True, "9/27 방송에서 양배추 소개 확인", ("cabbage",), (("방송 기사", "https://example.com"),))
    store.save_research(notes[0].id, research)
    got = store.note_records()[0].research
    assert got.confirmed and got.items == ("cabbage",) and got.sources == (("방송 기사", "https://example.com"),)
    assert [t for _, _, t in store.notes(start=D2)] == ["비 예보"]
    store.delete_note(notes[0].id)
    assert len(store.notes()) == 1
    store.save_research(notes[0].id, research)  # 지워진 메모의 조사 결과는 저장하지 않는다
    assert store.note_records()[0].research is None


def test_plans(store):
    _fill(store)
    plan = store.plan(D1)
    assert plan.approved and plan.summary == "승인" and plan.arrival == D2
    assert {ln.item_id: ln.final for ln in plan.lines} == {"tomato": 7, "apple": 3}
    assert store.plan(D2) is None
    assert store.approved_finals("apple") == [(D2, 3)]


def test_sheets_and_corrections(store):
    _fill(store)
    assert store.sheet_image(D1) == b"\xff\xd8jpg"
    rows = store.sheet_rows(D1)
    assert rows[1].flags == ("품목을 찾지 못함",) and rows[1].item_id_final == "cherry_tomato"
    assert store.correction_stats() == {"확정한 줄": 2, "사람이 고친 줄": 1, "그중 확인 표시가 있던 줄": 1}
    assert store.confirmed_names() == {"토마": "tomato", "방토": "cherry_tomato"}


def test_stores_are_isolated(db, store):
    _fill(store)
    other = Store(db, db.create_store("옆집청과", "next@gmail.com"))
    assert other.ledger() == [] and other.notes() == [] and other.plan(D1) is None
    assert other.sheet_image(D1) is None and other.correction_stats()["확정한 줄"] == 0
    other.delete_note(store.notes()[0][0])  # 다른 가게 메모는 지울 수 없다
    assert len(store.notes()) == 2
    other.save_day(D1, {"tomato": 9.0}, 1, 1)
    assert store.closing(D1)["tomato"] == 4.5


def test_members(db, store):
    sid = store.store_id
    assert db.role(sid, "BOSS@gmail.com") == OWNER  # 이메일은 대소문자를 가리지 않는다
    db.add_member(sid, " Staff@Gmail.com ")
    assert db.role(sid, "staff@gmail.com") == STAFF
    assert [m.store_name for m in db.memberships("staff@gmail.com")] == ["싱싱청과"]
    assert db.members(sid) == [("boss@gmail.com", OWNER), ("staff@gmail.com", STAFF)]
    with pytest.raises(ValueError):
        db.remove_member(sid, "boss@gmail.com")  # 마지막 사장님은 뺄 수 없다
    db.remove_member(sid, "staff@gmail.com")
    assert db.memberships("staff@gmail.com") == []


def test_dump_and_load_between_backends(db, store):
    _fill(store)
    target = Database(":memory:")
    counts = target.load(db.dump())
    assert counts["closing"] == 3 and counts["note"] == 2 and counts["stores"] == 1
    copied = Store(target, store.store_id)
    assert copied.ledger() == store.ledger()
    assert copied.plan(D1) == store.plan(D1)
    assert copied.sheet_image(D1) == store.sheet_image(D1)
    copied.add_note(D2, "새 메모")  # 옮긴 뒤에도 id가 겹치지 않는다
    assert len({i for i, _, _ in copied.notes()}) == 3
    assert target.create_store("새 가게", "a@b.c") != store.store_id
    with pytest.raises(ValueError):
        target.load(db.dump())


def test_store_with_url_creates_default_store():
    s = Store(":memory:")
    assert s.db.store_name(s.store_id) == "우리가게"


def test_old_schema_is_reported(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE day_record (day TEXT PRIMARY KEY, cash INTEGER, card INTEGER)")
    with pytest.raises(RuntimeError, match="예전 형식"):
        Database(path)


@pytest.mark.skipif(not PG_URL, reason="FRESH_STOCK_TEST_PG 없음")
def test_sqlite_to_postgres():
    source = Database(":memory:")
    s = Store(source, source.create_store("싱싱청과", "boss@gmail.com"))
    _fill(s)
    target = _fresh_pg(PG_URL)
    target.load(source.dump())
    copied = Store(target, s.store_id)
    assert copied.ledger() == s.ledger()
    copied.add_note(D2, "새 메모")
    assert len({i for i, _, _ in copied.notes()}) == 3
    assert target.create_store("새 가게", "a@b.c") != s.store_id
    target.close()


def test_mask_url_hides_password():
    from fresh_stock.store import mask_url

    url = "postgresql://postgres.abc:s3cret@aws-0.pooler.supabase.com:5432/postgres"
    assert mask_url(url) == "postgresql://postgres.abc:***@aws-0.pooler.supabase.com:5432/postgres"
    assert mask_url("out/store.db") == "out/store.db"


def test_load_dotenv(tmp_path, monkeypatch):
    from fresh_stock.cli import load_dotenv

    env = tmp_path / ".env"
    env.write_text('# 주석\nFRESH_STOCK_DB="postgresql://u:p@h/db"\nFRESH_STOCK_LLM=anthropic\n', encoding="utf-8")
    monkeypatch.delenv("FRESH_STOCK_DB", raising=False)
    monkeypatch.setenv("FRESH_STOCK_LLM", "claude-code")  # 이미 있는 환경변수가 우선
    load_dotenv(env)
    assert os.environ["FRESH_STOCK_DB"] == "postgresql://u:p@h/db"
    assert os.environ["FRESH_STOCK_LLM"] == "claude-code"
