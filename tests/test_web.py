"""웹 화면 흐름 테스트. 실제 LLM 대신 가짜 클라이언트를 쓴다."""

import json
import re
from datetime import timedelta

from conftest import CATALOG, WebEnv

from fresh_stock.llm import LLMError
from fresh_stock.web import similar_items


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
    assert "재고표 찍어 올릴 차례" in page.text and "싱싱청과" in page.text
    assert "효능 소개" in page.text  # 데모 가게 메모


def test_full_day_through_web():
    env = WebEnv(FakeLLM())
    env.login()
    client, day = env.client, env.first
    r = client.post("/sheet", files={"photo": ("sheet.jpg", b"jpg-bytes", "image/jpeg")}, follow_redirects=True)
    assert r.status_code == 200 and "재고표 확인" in r.text
    assert "어느 품목인지 몰라요" in r.text and 'class="fixrow err' in r.text  # 못 맞춘 품목은 빨간 줄
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
    assert "평소와 바뀐 품목" in r.text

    plan = store.plan(day)
    finals = {f"final_{ln.item_id}": str(ln.recommended) for ln in plan.lines}
    tomato = next(ln.recommended for ln in plan.lines if ln.item_id == "tomato")
    finals["final_tomato"] = str(max(tomato, 3) * 11)
    r = client.post("/order", data=finals)
    assert "<b>1개</b>가 평소와 크게 달라요" in r.text and 'id="w-tomato"' in r.text
    assert not store.plan(day).approved
    # [추천값으로 고치기]는 그 값을 넣고 다시 보여준다
    r = client.post("/order", data={**finals, "fix": f"tomato:{tomato}"})
    assert f'name="final_tomato" value="{tomato}"' in r.text and not store.plan(day).approved
    # [88 맞아요]는 그 값일 때만 통한다
    r = client.post("/order", data={**finals, "ok_tomato": "1"})
    assert 'id="w-tomato"' in r.text and not store.plan(day).approved

    r = client.post("/order", data={**finals, "ok_tomato": finals["final_tomato"]}, follow_redirects=True)
    assert r.status_code == 200 and "주문 보내기" in r.text and "구매 목록" in r.text
    assert store.plan(day).approved
    assert store.last_closed_day() == day


def test_sheet_confirm_keeps_edits_on_error():
    env = WebEnv(FakeLLM())
    env.login()
    env.client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")})
    r = env.client.post("/sheet/confirm", data={"item_0": "tomato", "qty_0": "??", "item_1": "tangerine", "qty_1": "7"})
    assert "빨간 줄(토마 4반)의 품목이나 숫자를 고쳐야 확정돼요." in r.text
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
    assert "재고표 읽기" in page and "발주 추천" in page and "실패" in page and "timeout" in page
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
    assert r.status_code == 200 and "주문 보내기" in r.text


def test_skip_banner_when_behind():
    env = WebEnv(FakeLLM())
    env.login()
    page = env.client.get("/")
    assert "쉬는 날로 건너뛰기" in page.text  # 데모 기록은 7월에 끝나 오늘보다 앞선다
    env.client.post("/skip")
    assert env.store.last_closed_day() == env.first


NEW_ITEM_FORM = {"name": "아보카도", "category": "과일", "unit": "개", "supplier_id": "garak", "base_demand": "3",
                 "price": "2,500", "cost": "1500", "shelf_life": "4"}  # fmt: skip


def test_owner_edits_items_and_store_settings():
    env = WebEnv(FakeLLM())
    env.login()
    c = env.client
    page = c.get("/settings").text
    assert "기본 품목 목록" in page and "사과" in page and "가락시장" in page

    r = c.post("/settings/item", data=NEW_ITEM_FORM, follow_redirects=True)
    assert "아보카도 저장했어요" in r.text and "기본 품목 목록" not in r.text
    assert env.store.catalog().item("item1").price == 2500
    r = c.post("/settings/item", data={**NEW_ITEM_FORM, "name": "망고", "price": "비쌈"})
    assert "판매가에 숫자를 적어 주세요" in r.text and 'value="망고"' in r.text  # 입력한 값은 그대로 남는다
    r = c.post("/settings/item", data={**NEW_ITEM_FORM, "item_id": "item1", "price": "3000"}, follow_redirects=True)
    assert env.store.catalog().item("item1").price == 3000

    assert 'value="사과"' in c.get("/settings/item?item_id=apple").text
    r = c.post("/settings/item/apple/active", data={"active": "0"}, follow_redirects=True)
    assert "사과: 취급을 그만뒀어요" in r.text and "그만둔 품목" in r.text
    review = c.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True).text
    assert 'value="apple"' not in review and 'value="item1"' in review  # 재고표 품목 선택에서도 빠진다

    r = c.post("/settings/supplier", data={"name": "과일 도매", "channel": "전화"}, follow_redirects=True)
    assert "과일 도매 저장했어요" in r.text
    r = c.post("/settings/supplier/garak/delete", follow_redirects=True)
    assert "지울 수 없습니다" in r.text

    r = c.post("/settings/calendar", data={"market": "1,6", "closed": ["0", "6"]}, follow_redirects=True)
    assert "장날·입고 요일을 저장했어요" in r.text and 'value="1,6" checked' in r.text
    assert env.store.settings()["closed_weekdays"] == "0,6"
    r = c.post("/settings/calendar", data={"supply_form": "1", "market": "", "supply": ["0", "1", "2", "3", "4", "5"]},
               follow_redirects=True)
    assert env.store.settings()["closed_weekdays"] == "6"
    r = c.post("/settings/calendar", data={"market": "9,9"}, follow_redirects=True)
    assert "잘못됐습니다" in r.text
    r = c.post("/settings/knowledge", data={"text": "비 오면 배달 주문이 늘어요"}, follow_redirects=True)
    assert "경험 노트를 저장했어요" in r.text and "비 오면 배달 주문이 늘어요" in r.text


def test_staff_sees_settings_but_cannot_change():
    env = WebEnv(FakeLLM())
    env.db.add_member(env.store_id, "staff@gmail.com")
    env.login("staff@gmail.com")
    c = env.client
    page = c.get("/settings").text
    assert "사장님만 고칠 수 있어요" in page and 'href="/settings/item' not in page
    for url, data in [("/settings/item", NEW_ITEM_FORM), ("/settings/calendar", {"market": ""}),
                      ("/settings/knowledge", {"text": "x"}), ("/settings/item/apple/active", {"active": "0"})]:  # fmt: skip
        r = c.post(url, data=data, follow_redirects=True)
        assert "사장님만 고칠 수 있습니다" in r.text
    assert env.store.catalog() is None and env.store.settings() == {}


def test_received_page_fixes_arrivals():
    env = WebEnv(FakeLLM())
    env.login()
    c, day = env.client, env.first
    assert 'href="/received"' in c.get("/sheet").text
    ordered = env.store.received(day)  # 승인한 발주가 그대로 입고로 기록돼 있다
    item = max(ordered, key=ordered.get)
    extra = next(i.id for i in CATALOG.items if i.id not in ordered)
    assert f'name="recv_{item}"' in c.get("/received").text

    r = c.post("/received", data={f"recv_{item}": "1반", "add_item_0": extra, "add_qty_0": "2"}, follow_redirects=True)
    assert "입고를 저장했어요" in r.text and "주문과 다른 품목 2개" in r.text
    got = env.store.received(day)
    assert got[item] == 1.5 and got[extra] == 2.0

    r = c.post("/received", data={f"recv_{item}": "많이"})
    assert "수량을 알 수 없습니다" in r.text and 'value="많이"' in r.text
    assert env.store.received(day)[item] == 1.5


def test_store_layout_sheet_saves_purchases():
    env = WebEnv(FakeLLM())
    env.login()
    c, day = env.client, env.first
    r = c.post("/settings/sheet-layout", data={"layout": "store"}, follow_redirects=True)
    assert "재고표 형식을 저장했어요" in r.text
    page = c.get("/sheet").text
    assert "사입 수량·가격 칸이" in page and 'href="/received"' not in page
    assert c.get("/received", follow_redirects=False).headers["location"] == "/sheet"

    review = c.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True).text
    assert 'name="buyq_0"' in review and 'name="buyp_0"' in review and "그날 입고와 사입 가격으로" in review
    form = {"item_0": "tomato", "qty_0": "4반", "buyq_0": "3", "buyp_0": "15,000",
            "item_1": "tangerine", "qty_1": "2", "buyq_1": "", "buyp_1": "", "cash": "1", "card": "1"}  # fmt: skip
    bad = c.post("/sheet/confirm", data={**form, "buyp_0": "비쌈"})
    assert "빨간 줄" in bad.text and 'value="비쌈"' in bad.text
    c.post("/sheet/confirm", data=form, follow_redirects=True)
    assert env.store.received(day) == {"tomato": 3.0}  # 주문 수량 대신 재고표 사입 칸
    assert env.store.prices(day) == {"tomato": 15000}
    assert "45,000원" in c.get("/").text  # 홈 매출 표의 사입 금액


class BigNumberLLM(FakeLLM):
    """토마토 남은 수량을 88로 읽는다 (8을 두 번 읽은 흔한 실수)."""

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        if purpose.startswith("sheet"):
            self.calls.append(purpose)
            return {"rows": [{"raw_name": "토마토", "raw_qty": "88", "item_id": "tomato", "corrected": False, "confident": True}]}
        return super().generate_json(system, prompt, schema, purpose, images)


def _missing_rows(page: str) -> dict[str, str]:
    """확인 화면의 '재고표에 없음' 줄: 줄 번호 → 품목 id."""
    return dict(re.findall(r'name="miss_(\d+)" value="1"><input type="hidden" name="item_\1" value="([^"]+)"', page))


def test_review_needs_yellow_rows_and_money_before_confirm():
    env = WebEnv(BigNumberLLM())
    env.login()
    c, day, store = env.client, env.first, env.store
    page = c.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True).text
    assert 'data-value="8"' in page and "88 맞아요" in page  # [8로 고치기][88 맞아요]
    missing = _missing_rows(page)
    assert missing and "tomato" not in missing.values()  # 어제 재고가 남았는데 재고표에 없는 품목

    form = {"item_0": "tomato", "qty_0": "88", **{f"item_{i}": v for i, v in missing.items()},
            **{f"miss_{i}": "1" for i in missing}}  # fmt: skip
    r = c.post("/sheet/confirm", data={**form, "cash": "0", "card": "0"})
    assert f"노란 줄 {1 + len(missing)}개를 확인해 주세요" in r.text and store.last_closed_day() < day

    zeros = {f"zero_{i}": "1" for i in missing}
    r = c.post("/sheet/confirm", data={**form, **zeros, "ok_0": "1"})
    assert "돈통 현금과 카드 합계를 넣어 주세요" in r.text and store.last_closed_day() < day

    # 숫자를 고친 줄은 [맞아요]를 누르지 않아도 확인한 것으로 본다. 반 칩은 0.5를 더한다
    first, *rest = missing
    done = {**form, **zeros, "qty_0": "8", "qty_0_half": "1", f"qty_{first}": "2", "cash": "10,000", "card": "0"}
    del done[f"zero_{first}"]
    r = c.post("/sheet/confirm", data=done, follow_redirects=True)
    assert "입고분 발주" in r.text and store.last_closed_day() == day
    closing = store.closing(day)
    assert closing["tomato"] == 8.5 and closing[missing[first]] == 2.0
    assert all(closing[missing[i]] == 0 for i in rest)


def test_messages_mark_sent_and_supplier_phone():
    env = WebEnv(FakeLLM())
    env.login()
    c = env.client
    c.post("/settings/supplier", data={"supplier_id": "garak", "name": "가락시장", "channel": "문자", "phone": "010-1234-5678"})
    day = env.first - timedelta(days=1)
    page = c.get(f"/messages?day={day.isoformat()}").text
    assert "sms:01012345678?&amp;body=" in page and 'class="note sent"' not in page
    assert c.post("/messages/sent", data={"day": day.isoformat(), "supplier": "garak"}).status_code == 204
    c.post("/messages/sent", data={"day": day.isoformat(), "supplier": "nowhere"})  # 없는 거래처는 무시
    assert set(env.store.sent(day)) == {"garak"}
    assert 'class="note sent" data-supplier="garak"' in c.get(f"/messages?day={day.isoformat()}").text


def test_more_menu_rename_print_and_static():
    env = WebEnv(FakeLLM())
    env.login()
    c = env.client
    more = c.get("/more").text
    assert all(h in more for h in ('href="/settings"', 'href="/members"', 'href="/logs"', 'href="/stores"'))
    r = c.post("/store/rename", data={"name": "싱싱청과 본점"}, follow_redirects=True)
    assert "가게 이름을 바꿨어요" in r.text and env.db.store_name(env.store_id) == "싱싱청과 본점"
    sheet = c.get("/settings/print").text
    assert "싱싱청과 본점 마감 재고표" in sheet and "사과" in sheet
    assert c.get("/static/app.css").status_code == 200
    assert c.get("/static/produce/apple.svg").headers["content-type"].startswith("image/svg")

    env.db.add_member(env.store_id, "staff@gmail.com")
    c.get("/logout")
    env.login("staff@gmail.com")
    r = c.post("/store/rename", data={"name": "바꿈"}, follow_redirects=True)
    assert "사장님만 바꿀 수 있습니다" in r.text and env.db.store_name(env.store_id) == "싱싱청과 본점"


def test_similar_items_for_unknown_names():
    names = lambda raw: [i.name for i in similar_items(raw, CATALOG.items)]  # noqa: E731
    assert names("토마")[:2] == ["토마토", "방울토마토"]
    assert names("굴")[0] == "귤"  # 손글씨에서 헷갈리는 글자
    assert names("") == [] and names("ㅁㄴㅇㄹ") == []


def test_unknown_row_suggests_similar_items():
    env = WebEnv(FakeLLM())
    env.login()
    page = env.client.post("/sheet", files={"photo": ("sheet.jpg", b"x", "image/jpeg")}, follow_redirects=True).text
    similar = re.search(r'<optgroup label="비슷한 이름">(.*?)</optgroup>', page, re.S)
    assert similar and 'value="tangerine"' in similar.group(1)  # "굴" 줄에 귤
