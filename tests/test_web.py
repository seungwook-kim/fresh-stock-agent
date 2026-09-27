"""웹 화면 흐름 테스트. 실제 LLM 대신 가짜 클라이언트를 쓴다."""

from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from fresh_stock.catalog import load_catalog
from fresh_stock.demo import seed_demo
from fresh_stock.llm import LLMError
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.service import StoreService
from fresh_stock.store import Store
from fresh_stock.web import create_app

catalog = load_catalog()
calendar = MarketCalendar()


class FakeLLM:
    def __init__(self, fail_sheet=False):
        self.fail_sheet = fail_sheet
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.calls.append(purpose)
        if purpose.startswith("sheet"):
            if self.fail_sheet:
                raise LLMError("timeout")
            return {
                "rows": [
                    {"raw_name": "토마", "raw_qty": "4반", "item_id": "tomato", "corrected": False, "confident": True},
                    {"raw_name": "굴", "raw_qty": "2", "item_id": "", "corrected": False, "confident": False},
                ]
            }
        if purpose.startswith("order"):
            return {
                "action": "submit",
                "item_ids": [],
                "adjustments": [{"target": "cabbage", "day": "전체", "factor": 3.0, "reason": "TV 효능 소개"}],
                "summary": "TV 방송을 반영했습니다.",
            }
        return {"greetings": []}


@pytest.fixture
def app_env():
    store = Store(":memory:")
    # 7/1부터 30일: 7/31(금) 마감 → 토요일 입고 발주가 있는 날
    first = seed_demo(store, catalog, calendar, date(2026, 7, 1), days=30, seed=1)
    llm = FakeLLM()
    service = StoreService(store, catalog, calendar, llm_factory=lambda: llm)
    return TestClient(create_app(service)), service, first, llm


def test_home_shows_next_step(app_env):
    client, _, first, _ = app_env
    page = client.get("/")
    assert page.status_code == 200
    assert f"{first.month}/{first.day}" in page.text
    assert "마감 재고표를 올릴 차례" in page.text
    assert "효능 소개" in page.text  # 데모 가게 메모


def test_full_day_through_web(app_env):
    client, service, day, llm = app_env
    r = client.post("/sheet", files={"photo": ("sheet.jpg", b"jpg-bytes", "image/jpeg")}, follow_redirects=True)
    assert r.status_code == 200 and "재고표 확인" in r.text
    assert "품목을 찾지 못함" in r.text and 'class="flag"' in r.text
    assert client.get(f"/sheet/image/{day.isoformat()}").content == b"jpg-bytes"

    # 두 번째 줄(굴)을 귤로 고치고, 빠진 품목 하나를 추가해 확정
    form = {"item_0": "tomato", "qty_0": "4반", "item_1": "tangerine", "qty_1": "2",
            "item_2": "apple", "qty_2": "1", "item_3": "", "qty_3": "", "item_4": "", "qty_4": "",
            "cash": "450,000", "card": "820000"}  # fmt: skip
    r = client.post("/sheet/confirm", data=form, follow_redirects=True)
    assert r.status_code == 200 and "입고분 발주" in r.text
    assert service.store.closing(day)["tomato"] == 4.5
    assert service.store.closing(day)["tangerine"] == 2.0
    assert service.store.correction_stats()["사람이 고친 줄"] == 1
    assert "TV 방송을 반영했습니다." in r.text and "TV 효능 소개" in r.text
    assert "평소 추천에서 수량이 바뀐 품목" in r.text

    plan = service.store.plan(day)
    finals = {f"final_{ln.item_id}": str(ln.recommended) for ln in plan.lines}
    tomato = next(ln.recommended for ln in plan.lines if ln.item_id == "tomato")
    finals["final_tomato"] = str(max(tomato, 3) * 11)
    r = client.post("/order", data=finals)
    assert "확인이 필요한 수량이 1개" in r.text and 'class="warn"' in r.text
    assert not service.store.plan(day).approved

    r = client.post("/order", data={**finals, "confirmed": "yes"}, follow_redirects=True)
    assert r.status_code == 200 and "거래처 문자" in r.text
    assert "구매 목록" in r.text
    assert service.store.plan(day).approved
    assert service.status()[1] == day + timedelta(days=1)


def test_sheet_confirm_rejects_unreadable_qty(app_env):
    client, _, _, _ = app_env
    client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")})
    r = client.post("/sheet/confirm", data={"item_0": "tomato", "qty_0": "??", "item_1": "tangerine", "qty_1": "7"})
    assert "빨간 줄(토마 4반)의 품목이나 수량을 확인해 주세요." in r.text
    assert 'value="7"' in r.text  # 사람이 고친 다른 줄의 값은 그대로 남는다
    assert '<option value="tangerine" selected>' in r.text


def test_sheet_read_failure_shows_error():
    store = Store(":memory:")
    seed_demo(store, catalog, calendar, date(2026, 7, 1), days=30, seed=1)
    service = StoreService(store, catalog, calendar, llm_factory=lambda: FakeLLM(fail_sheet=True))
    client = TestClient(create_app(service))
    r = client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True)
    assert "재고표를 읽지 못했습니다" in r.text


def test_notes_add_and_delete(app_env):
    client, service, first, _ = app_env
    client.post("/notes", data={"day": (first + timedelta(days=3)).isoformat(), "text": "추석 3일 전"})
    note_id = next(i for i, _, t in service.store.notes() if t == "추석 3일 전")
    assert "추석 3일 전" in client.get("/").text
    client.post(f"/notes/{note_id}/delete")
    assert all(t != "추석 3일 전" for _, _, t in service.store.notes())
