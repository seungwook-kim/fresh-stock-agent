"""웹 화면 흐름 테스트. 실제 LLM 대신 가짜 클라이언트를 쓴다."""

import json
from datetime import timedelta

from conftest import WebEnv

from fresh_stock.llm import LLMError


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
                "query": "",
                "adjustments": [{"target": "cabbage", "day": "전체", "factor": 3.0, "reason": "TV 효능 소개"}],
                "summary": "TV 방송을 반영했습니다.",
            }
        return {"greetings": []}


def test_home_shows_next_step():
    env = WebEnv(FakeLLM())
    env.login()
    page = env.client.get("/")
    assert page.status_code == 200
    assert f"{env.first.month}/{env.first.day}" in page.text
    assert "마감 재고표를 올릴 차례" in page.text and "싱싱청과" in page.text
    assert "효능 소개" in page.text  # 데모 가게 메모


def test_full_day_through_web():
    env = WebEnv(FakeLLM())
    env.login()
    client, day = env.client, env.first
    r = client.post("/sheet", files={"photo": ("sheet.jpg", b"jpg-bytes", "image/jpeg")}, follow_redirects=True)
    assert r.status_code == 200 and "재고표 확인" in r.text
    assert "품목을 찾지 못함" in r.text and 'class="flag"' in r.text
    assert client.get(f"/sheet/image/{day.isoformat()}").content == b"jpg-bytes"

    form = {"item_0": "tomato", "qty_0": "4반", "item_1": "tangerine", "qty_1": "2",
            "item_2": "apple", "qty_2": "1", "item_3": "", "qty_3": "", "item_4": "", "qty_4": "",
            "cash": "450,000", "card": "820000"}  # fmt: skip
    r = client.post("/sheet/confirm", data=form, follow_redirects=True)
    assert r.status_code == 200 and "입고분 발주" in r.text
    store = env.store
    assert store.closing(day)["tomato"] == 4.5 and store.closing(day)["tangerine"] == 2.0
    assert store.correction_stats()["사람이 고친 줄"] == 1
    assert "TV 방송을 반영했습니다." in r.text and "TV 효능 소개" in r.text
    assert "평소 추천에서 수량이 바뀐 품목" in r.text

    plan = store.plan(day)
    finals = {f"final_{ln.item_id}": str(ln.recommended) for ln in plan.lines}
    tomato = next(ln.recommended for ln in plan.lines if ln.item_id == "tomato")
    finals["final_tomato"] = str(max(tomato, 3) * 11)
    r = client.post("/order", data=finals)
    assert "확인이 필요한 수량이 1개" in r.text and 'class="warn"' in r.text
    assert not store.plan(day).approved

    r = client.post("/order", data={**finals, "confirmed": "yes"}, follow_redirects=True)
    assert r.status_code == 200 and "거래처 문자" in r.text and "구매 목록" in r.text
    assert store.plan(day).approved
    assert store.last_closed_day() == day


def test_sheet_confirm_keeps_edits_on_error():
    env = WebEnv(FakeLLM())
    env.login()
    env.client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")})
    r = env.client.post("/sheet/confirm", data={"item_0": "tomato", "qty_0": "??", "item_1": "tangerine", "qty_1": "7"})
    assert "빨간 줄(토마 4반)의 품목이나 수량을 확인해 주세요." in r.text
    assert 'value="7"' in r.text  # 사람이 고친 다른 줄의 값은 그대로 남는다
    assert '<option value="tangerine" selected>' in r.text


def test_sheet_read_failure_shows_error():
    env = WebEnv(FakeLLM(fail_sheet=True))
    env.login()
    r = env.client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True)
    assert "재고표를 읽지 못했습니다" in r.text


def test_notes_add_and_delete():
    env = WebEnv(FakeLLM())
    env.login()
    env.client.post("/notes", data={"day": (env.first + timedelta(days=3)).isoformat(), "text": "추석 3일 전"})
    note = next(n for n in env.store.note_records() if n.text == "추석 3일 전")
    assert note.author == "boss@gmail.com" and note.source == "사장님"
    assert "추석 3일 전" in env.client.get("/").text
    env.client.post(f"/notes/{note.id}/delete")
    assert all(t != "추석 3일 전" for _, _, t in env.store.notes())


def test_llm_log_pages_are_per_store(tmp_path):
    env = WebEnv(FakeLLM(), log_dir=tmp_path)
    calls = [
        {"purpose": "sheet 2026-07-31", "system": "S", "prompt": "품목 목록", "images": 1, "output": {"rows": []},
         "error": None, "input_tokens": 4000, "output_tokens": 2000, "seconds": 20.5, "at": "2026-07-31T21:00:00"},
        {"purpose": "order 2026-07-31", "system": "S", "prompt": "가게 메모", "images": 0, "output": None,
         "error": "timeout", "input_tokens": 0, "output_tokens": 0, "seconds": 300.0, "at": "2026-07-31T21:01:00"},
    ]  # fmt: skip
    log = tmp_path / f"store-{env.store_id}.jsonl"
    log.write_text("\n".join(json.dumps(c, ensure_ascii=False) for c in calls) + "\n{broken", encoding="utf-8")
    env.login()
    page = env.client.get("/logs").text
    assert "재고표 읽기" in page and "발주 에이전트" in page and "실패: timeout" in page
    detail = env.client.get("/logs/0").text  # 최신이 먼저
    assert "order 2026-07-31" in detail and "가게 메모" in detail
    assert env.client.get("/logs/99", follow_redirects=False).status_code == 303

    # 다른 가게는 이 기록을 볼 수 없다
    env.client.get("/logout")
    env.login("other@gmail.com")
    env.client.post("/stores", data={"name": "옆집청과"})
    assert "아직 기록이 없어요" in env.client.get("/logs").text


def test_messages_page():
    env = WebEnv(FakeLLM())
    env.login()
    day = env.first - timedelta(days=1)
    r = env.client.get(f"/messages?day={day.isoformat()}")
    assert r.status_code == 200 and "거래처 문자" in r.text


def test_skip_banner_when_behind():
    env = WebEnv(FakeLLM())
    env.login()
    page = env.client.get("/")
    assert "쉬는 날로 건너뛰기" in page.text  # 데모 기록은 7월에 끝나 오늘보다 앞선다
    env.client.post("/skip")
    assert env.store.last_closed_day() == env.first
