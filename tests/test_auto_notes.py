"""날씨·명절 자동 메모 테스트. 날씨 API는 가짜 응답으로 대신한다."""

from datetime import date

from conftest import CALENDAR, CATALOG, WebEnv

from fresh_stock.auto_notes import FORECAST_URL, HISTORY_URL, WeatherSource, collect, holiday_notes, weather_notes
from fresh_stock.demo import seed_demo
from fresh_stock.service import StoreService
from fresh_stock.store import Store


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
    weather = WeatherSource(fetch=FakeWeather({}, fail=True))
    notes, error = collect(date(2026, 9, 23), date(2026, 9, 25), date(2026, 9, 22), weather)
    assert error and "날씨" in error
    assert notes and all(n.source == "명절" for n in notes)


def _service(weather=None):
    store = Store(":memory:")
    first = seed_demo(store, CATALOG, CALENDAR, date(2026, 7, 1), days=30, seed=1)
    return StoreService(store, CATALOG, CALENDAR, weather=weather, auto_notes=True), first


def test_refresh_adds_once_and_replaces_old_forecast():
    fetch = FakeWeather({date(2026, 8, 1): (70, 30), date(2026, 8, 2): (20, 28), date(2026, 8, 3): (90, 34)})
    service, first = _service(weather=WeatherSource(fetch=fetch))
    added, error = service.refresh_auto_notes(first)
    assert error is None
    got = {(n.day, n.text) for n in added}
    assert got >= {(date(2026, 8, 1), "비 예보 (강수확률 70%)"), (date(2026, 8, 3), "폭염 (낮 최고 34도)")}
    assert service.refresh_auto_notes(first)[0] == []  # 같은 메모는 다시 넣지 않는다
    # 새 예보가 오면 이전 날씨 메모를 바꾼다 (데모의 8/2 "비 예보 (강수확률 80%)"도 새 예보에 없으면 지워진다)
    fetch.table[date(2026, 8, 1)] = (95, 30)
    service.refresh_auto_notes(first)
    rain = [(d, t) for _, d, t in service.store.notes(date(2026, 8, 1), date(2026, 8, 3)) if t.startswith("비 예보")]
    assert rain == [(date(2026, 8, 1), "비 예보 (강수확률 95%)"), (date(2026, 8, 3), "비 예보 (강수확률 90%)")]


def test_auto_notes_keep_their_source():
    service, first = _service(weather=WeatherSource(fetch=FakeWeather({date(2026, 8, 1): (70, 30)})))
    service.refresh_auto_notes(first)
    sources = {n.text: n.source for n in service.store.note_records()}
    assert sources["비 예보 (강수확률 70%)"] == "날씨"


def test_web_auto_notes_button():
    fetch = FakeWeather({date(2026, 8, 1): (85, 30), date(2026, 8, 2): (10, 27), date(2026, 8, 3): (10, 27)})
    env = WebEnv(weather=WeatherSource(fetch=fetch))
    env.login()
    page = env.client.post("/notes/auto", follow_redirects=True).text
    assert "날씨·명절 메모" in page and "비 예보 (강수확률 85%)" in page and "날씨 자동" in page
