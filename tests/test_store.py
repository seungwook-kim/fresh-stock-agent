"""저장소 테스트. SQLite는 항상, PostgreSQL은 FRESH_STOCK_TEST_PG에 주소가 있을 때 같은 테스트를 돌린다.

    FRESH_STOCK_TEST_PG=postgresql://postgres:@127.0.0.1:5432/postgres uv run pytest tests/test_store.py
"""

import os
from datetime import date

import pytest

from fresh_stock.store import TABLES, OrderLineRecord, OrderPlan, SheetRowRecord, Store

PG_URL = os.environ.get("FRESH_STOCK_TEST_PG")
D1, D2 = date(2026, 7, 1), date(2026, 7, 2)


def _fresh_pg(url: str) -> Store:
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    return Store(url)


BACKENDS = ["sqlite", pytest.param("postgres", marks=pytest.mark.skipif(not PG_URL, reason="FRESH_STOCK_TEST_PG 없음"))]


@pytest.fixture(params=BACKENDS)
def store(request):
    s = _fresh_pg(PG_URL) if request.param == "postgres" else Store(":memory:")
    yield s
    s.close()


def _fill(s: Store) -> None:
    s.save_received(D1, {"tomato": 8, "apple": 0})
    s.save_day(D1, {"tomato": 4.5, "apple": 0.0}, cash=100, card=200)
    s.save_day(D2, {"tomato": 1.0}, cash=50, card=60)
    s.save_day(D2, {"tomato": 2.0}, cash=70, card=80)  # 같은 날 다시 저장하면 덮어쓴다
    s.add_note(D2, " 비 예보 ")
    s.add_note(D1, "TV에서 양배추 소개")
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


def test_notes(store):
    _fill(store)
    notes = store.notes()
    assert [(d, t) for _, d, t in notes] == [(D1, "TV에서 양배추 소개"), (D2, "비 예보")]
    assert [t for _, _, t in store.notes(start=D2)] == ["비 예보"]
    store.delete_note(notes[0][0])
    assert len(store.notes()) == 1


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


def test_dump_and_load_between_backends(store):
    _fill(store)
    target = Store(":memory:")
    counts = target.load(store.dump())
    assert counts["closing"] == 3 and counts["note"] == 2
    assert target.ledger() == store.ledger()
    assert target.plan(D1) == store.plan(D1)
    assert target.sheet_image(D1) == store.sheet_image(D1)
    target.add_note(D2, "새 메모")  # 옮긴 뒤에도 메모 id가 겹치지 않는다
    assert len({i for i, _, _ in target.notes()}) == 3
    with pytest.raises(ValueError):
        target.load(store.dump())


@pytest.mark.skipif(not PG_URL, reason="FRESH_STOCK_TEST_PG 없음")
def test_sqlite_to_postgres():
    source = Store(":memory:")
    _fill(source)
    target = _fresh_pg(PG_URL)
    target.load(source.dump())
    assert target.ledger() == source.ledger()
    target.add_note(D2, "새 메모")
    assert len({i for i, _, _ in target.notes()}) == 3
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
