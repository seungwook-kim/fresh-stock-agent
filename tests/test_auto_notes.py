"""날씨·명절 자동 메모와 AI 건강 기사·방송 제안 테스트. 외부 API는 가짜 응답으로 대신한다."""

import time
from datetime import date

from fastapi.testclient import TestClient

from fresh_stock.auto_notes import FORECAST_URL, HISTORY_URL, WeatherSource, collect, holiday_notes, weather_notes
from fresh_stock.catalog import load_catalog
from fresh_stock.demo import seed_demo
from fresh_stock.llm import LLMError
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.news import find_food_news
from fresh_stock.service import StoreService
from fresh_stock.store import Store
from fresh_stock.web import create_app

catalog = load_catalog()
calendar = MarketCalendar()


class FakeWeather:
    """날짜별 (강수확률, 최고기온)을 돌려주는 가짜 Open-Meteo."""

    def __init__(self, table, fail=False):
        self.table = table
        self.fail = fail
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        if self.fail:
            raise OSError("network down")
        days = sorted(self.table)
        return {
            "daily": {
                "time": [d.isoformat() for d in days],
                "precipitation_probability_max": [self.table[d][0] for d in days],
                "temperature_2m_max": [self.table[d][1] for d in days],
            }
        }


def test_weather_notes_thresholds():
    rows = [(date(2026, 8, 1), 80, 30), (date(2026, 8, 2), 40, 35.2), (date(2026, 8, 3), None, None)]
    assert [n.text for n in weather_notes(rows)] == ["비 예보 (강수확률 80%)", "폭염 (낮 최고 35도)"]


def test_weather_uses_history_for_past_days_and_forecast_for_future():
    fetch = FakeWeather({})
    WeatherSource(fetch=fetch).daily(date(2026, 9, 25), date(2026, 9, 29), today=date(2026, 9, 27))
    assert fetch.urls[0].startswith(HISTORY_URL) and "end_date=2026-09-26" in fetch.urls[0]
    assert fetch.urls[1].startswith(FORECAST_URL) and "start_date=2026-09-27" in fetch.urls[1]


def test_holiday_notes_match_simulator_wording():
    notes = holiday_notes(date(2026, 9, 21), date(2026, 9, 27))
    texts = {n.day: n.text for n in notes}
    assert texts[date(2026, 9, 22)] == "추석 3일 전 (명절 대목)"
    assert texts[date(2026, 9, 25)] == "추석 당일 (명절 대목)"
    assert texts[date(2026, 9, 26)] == "추석 다음날 (연휴, 손님 적음)"
    seollal = holiday_notes(date(2027, 2, 1), date(2027, 2, 10))
    assert any(n.text == "설날 당일 (명절 대목)" and n.day == date(2027, 2, 7) for n in seollal)


def test_collect_keeps_holidays_when_weather_fails():
    notes, error = collect(date(2026, 9, 23), date(2026, 9, 25), date(2026, 9, 22), WeatherSource(fetch=FakeWeather({}, fail=True)))
    assert error and "날씨" in error
    assert notes and all(n.source == "명절" for n in notes)


def _service(weather=None, search=None):
    store = Store(":memory:")
    first = seed_demo(store, catalog, calendar, date(2026, 7, 1), days=30, seed=1)
    return StoreService(store, catalog, calendar, weather=weather, search_llm_factory=search, auto_notes=True), first


def test_refresh_adds_once_and_replaces_old_forecast():
    fetch = FakeWeather({date(2026, 8, 1): (70, 30), date(2026, 8, 2): (20, 28), date(2026, 8, 3): (90, 34)})
    service, first = _service(weather=WeatherSource(fetch=fetch))
    added, error = service.refresh_auto_notes(first)
    assert error is None
    assert {(n.day, n.text) for n in added} >= {(date(2026, 8, 1), "비 예보 (강수확률 70%)"), (date(2026, 8, 3), "폭염 (낮 최고 34도)")}
    assert service.refresh_auto_notes(first)[0] == []  # 같은 메모는 다시 넣지 않는다
    # 새 예보가 오면 이전 날씨 메모를 바꾼다 (데모의 8/2 "비 예보 (강수확률 80%)"도 새 예보에 없으면 지워진다)
    fetch.table[date(2026, 8, 1)] = (95, 30)
    service.refresh_auto_notes(first)
    rain = [(d, t) for _, d, t in service.store.notes(date(2026, 8, 1), date(2026, 8, 3)) if t.startswith("비 예보")]
    assert rain == [(date(2026, 8, 1), "비 예보 (강수확률 95%)"), (date(2026, 8, 3), "비 예보 (강수확률 90%)")]


class FakeSearchLLM:
    def __init__(self, found=None, fail=False):
        self.found = found or []
        self.fail = fail
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.calls.append(prompt)
        if self.fail:
            raise LLMError("timeout")
        return {"found": self.found}


def _news(food, when="2026-07-29", kind="방송"):
    return {"food": food, "kind": kind, "title": "건강 프로그램", "date": when, "url": "https://example.com/a"}


def test_find_food_news_keeps_only_our_items_in_range():
    llm = FakeSearchLLM([_news("양배추"), _news("방토"), _news("아보카도"), _news("당근", when="2026-06-01"), _news("양배추")])
    found = find_food_news(llm, catalog, date(2026, 7, 31))
    assert [f.name for f in found] == ["양배추", "방울토마토"]  # 없는 품목·기간 밖·중복은 버린다
    assert found[0].note() == "7/29 방송에서 양배추 효능 소개 (손님 문의 많을 듯)"
    assert "2026-07-24부터 2026-07-31까지" in llm.calls[0]


def test_web_auto_notes_and_news_suggestions():
    fetch = FakeWeather({date(2026, 8, 1): (85, 30), date(2026, 8, 2): (10, 27), date(2026, 8, 3): (10, 27)})
    llm = FakeSearchLLM([_news("양배추")])
    service, first = _service(weather=WeatherSource(fetch=fetch), search=lambda: llm)
    client = TestClient(create_app(service))
    page = client.post("/notes/auto", follow_redirects=True).text
    assert "날씨·명절 메모" in page and "비 예보 (강수확률 85%)" in page

    client.post("/notes/news")
    for _ in range(50):
        page = client.get("/").text
        if "AI가 찾은 제안" in page:
            break
        time.sleep(0.05)
    assert "7/29 방송에서 양배추 효능 소개" in page
    client.post("/notes", data={"day": "2026-08-01", "text": "7/29 방송에서 양배추 효능 소개 (손님 문의 많을 듯)"})
    assert any("양배추 효능" in t for _, _, t in service.store.notes())


def test_web_news_failure_is_shown():
    service, _ = _service(search=lambda: FakeSearchLLM(fail=True))
    client = TestClient(create_app(service))
    client.post("/notes/news")
    for _ in range(50):
        page = client.get("/").text
        if "찾지 못했어요" in page:
            break
        time.sleep(0.05)
    assert "건강 기사·방송을 찾지 못했어요" in page


def test_news_button_hidden_without_search_llm():
    service, _ = _service()
    assert "AI로 건강 기사·방송 찾기" not in TestClient(create_app(service)).get("/").text
