"""MCP 서버 테스트. 도구 호출만으로 하루 업무를 끝까지 진행해 본다 (가짜 AI 사용)."""

import asyncio
import json
from datetime import date, timedelta

import pytest
from conftest import CALENDAR, CATALOG, OWNER_EMAIL
from mcp.server.mcpserver.exceptions import ToolError

from fresh_stock.demo import seed_demo
from fresh_stock.mcp_server import build_server
from fresh_stock.service import StoreService
from fresh_stock.store import Database, Store


class FakeLLM:
    """재고표 읽기, 발주 에이전트, 메모 확인에 정해진 답을 준다."""

    def __init__(self):
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.calls.append(purpose)
        if purpose.startswith("sheet"):
            return {
                "rows": [
                    {"raw_name": "토마", "raw_qty": "4반", "item_id": "tomato", "corrected": False, "confident": True},
                    {"raw_name": "사과", "raw_qty": "x", "item_id": "apple", "corrected": False, "confident": False},
                ]
            }
        if purpose.startswith("research"):
            return {"confirmed": True, "summary": "방송 확인", "items": ["양배추"], "sources": []}
        if purpose.startswith("order"):
            return {"action": "submit", "item_ids": [], "query": "", "adjustments": [], "summary": "평소대로"}
        return {"greetings": []}


def _setup(email=OWNER_EMAIL):
    db = Database(":memory:")
    store_id = db.create_store("싱싱청과", OWNER_EMAIL)
    first = seed_demo(Store(db, store_id), CATALOG, CALENDAR, date(2026, 7, 1), days=30, seed=1)
    llm = FakeLLM()

    def make_service(sid):
        return StoreService(
            Store(db, sid), CATALOG, CALENDAR, llm_factory=lambda: llm, research_llm_factory=lambda: llm
        )

    return build_server(db, make_service, email), db, store_id, first, llm


def call(server, name, **args):
    """도구를 부르고 결과(JSON 글)를 읽는다. AI 클라이언트도 이 글을 그대로 받는다."""
    result = asyncio.run(server.call_tool(name, args))
    return json.loads(result.content[0].text)


def test_tools_are_listed_with_hints():
    server, *_ = _setup()
    tools = {t.name: t for t in asyncio.run(server.list_tools())}
    assert set(tools) >= {"today", "add_note", "read_sheet", "confirm_sheet", "get_order_plan", "approve_order", "get_messages"}
    assert tools["today"].annotations.read_only_hint is True
    assert tools["approve_order"].annotations.read_only_hint is False


def test_full_day_through_mcp(tmp_path):
    server, db, store_id, day, llm = _setup()
    assert call(server, "list_stores")["stores"] == [{"store_id": store_id, "name": "싱싱청과", "role": "owner"}]
    t = call(server, "today")
    assert t["step"] == "재고표" and t["day"] == day.isoformat()

    note = call(server, "add_note", day=(day + timedelta(days=1)).isoformat(), text="어제 TV에서 양배추가 몸에 좋다고 나왔대")
    photo = tmp_path / "sheet.jpg"
    photo.write_bytes(b"\xff\xd8jpg")
    sheet = call(server, "read_sheet", image_path=str(photo))
    assert sheet["needs_check_count"] == 1 and sheet["rows"][0]["written"] == "토마 4반"

    assert [r["line"] for r in sheet["rows"]] == [0, 1]
    with pytest.raises(ToolError, match="아무것도 저장하지 않았습니다"):
        call(server, "confirm_sheet", cash=0, card=0, corrections=[{"item": "두리안", "qty": "1"}])
    # 확인 표시된 줄(사과 x)을 사람에게 확인받지 않으면 저장하지 않는다
    with pytest.raises(ToolError, match="1번 '사과 x'"):
        call(server, "confirm_sheet", cash=100000, card=0)
    # 고친 줄만 넘겨도 나머지 읽은 줄(토마토 4반)은 그대로 저장된다
    done = call(
        server, "confirm_sheet", cash=100000, card=0, corrections=[{"line": 1, "qty": "0"}, {"item": "방토", "qty": "2"}]
    )
    assert done["next_action"].startswith("get_order_plan")
    closing = Store(db, store_id).closing(day)
    assert closing["tomato"] == 4.5 and closing["cherry_tomato"] == 2.0 and closing["apple"] == 0

    plan = call(server, "get_order_plan")
    assert plan["arrival"] == (day + timedelta(days=1)).isoformat() and plan["agent_summary"] == "평소대로"
    checked = next(n for n in plan["notes"] if n["note_id"] == note["note_id"])
    assert checked["ai_check"]["confirmed"] and checked["ai_check"]["items"] == ["양배추"]

    with pytest.raises(ToolError, match="승인을 받은 뒤"):
        call(server, "approve_order", user_confirmed=False)
    tomato = next(ln["recommended"] for ln in plan["lines"] if ln["item"] == "토마토")
    warned = call(server, "approve_order", user_confirmed=True, changes={"토마토": max(tomato, 3) * 11})
    assert not warned["approved"] and "토마토" in warned["warnings"]
    approved = call(server, "approve_order", user_confirmed=True, changes={"토마토": tomato})
    assert approved["approved"] and any("구매 목록" in m["text"] for m in approved["messages"])
    assert call(server, "get_messages")["order_day"] == day.isoformat()
    assert call(server, "today")["step"] == "재고표"


def test_non_member_is_blocked():
    server, db, store_id, *_ = _setup(email="stranger@gmail.com")
    assert call(server, "list_stores")["stores"] == []
    with pytest.raises(ToolError, match="가게를 정해 주세요"):
        call(server, "today")
    with pytest.raises(ToolError, match="멤버가 아닙니다"):
        call(server, "today", store_id=store_id)


def test_member_with_two_stores_must_choose():
    server, db, store_id, *_ = _setup()
    second = db.create_store("2호점", OWNER_EMAIL)
    with pytest.raises(ToolError, match="2호점"):
        call(server, "today")
    assert call(server, "today", store_id=second)["store"] == "2호점"


def test_days_behind_and_skip_day():
    server, db, store_id, day, _ = _setup()
    t = call(server, "today")
    assert t["days_behind"] > 0 and "skip_day" in t["warning"]  # 데모 기록은 7월에 끝난다
    with pytest.raises(ToolError, match="확인한 뒤"):
        call(server, "skip_day", user_confirmed=False)
    skipped = call(server, "skip_day", user_confirmed=True)
    assert skipped["skipped"].startswith(day.isoformat()) and skipped["now"]["step"] == "발주"
    assert call(server, "skip_day", user_confirmed=True)["now"] == {"step": "재고표", "day": (day + timedelta(days=1)).isoformat()}


def test_step_guards():
    server, *_ = _setup()
    with pytest.raises(ToolError, match="재고표를 올릴 차례"):
        call(server, "get_order_plan")
    with pytest.raises(ToolError, match="파일이 없습니다"):
        call(server, "read_sheet", image_path="없는파일.jpg")
