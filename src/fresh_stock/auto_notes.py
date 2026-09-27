"""가게 메모 자동 수집: 날씨 예보와 명절.

- 날씨: Open-Meteo (API 키 불필요). 앞으로의 날은 예보 API, 지난 날은 그 시점 예보를 보관한
  historical forecast API를 쓴다 (데모처럼 과거 날짜로 돌릴 때).
- 명절: holidays 패키지의 한국 공휴일 (음력 설·추석 포함).

메모 문구는 에이전트와 과거 기록 검색이 이미 쓰는 표현("비 예보 (강수확률 80%)",
"추석 2일 전 (명절 대목)")에 맞춘다. 그래야 과거 비슷한 날이 검색된다.
"""

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

import holidays

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
HISTORY_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
RAIN_PROBABILITY = 60  # 이 강수확률(%) 이상이면 비 예보 메모
HEAT_TEMPERATURE = 33  # 낮 최고기온이 이 이상이면 폭염 메모
SEOUL = (37.57, 126.98)
HOLIDAY_NAMES = ("설날", "추석")
RAIN_PREFIX = "비 예보 ("
HEAT_PREFIX = "폭염 ("


@dataclass(frozen=True)
class AutoNote:
    day: date
    text: str
    source: str  # "날씨" 또는 "명절"


def _http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


@dataclass
class WeatherSource:
    lat: float = SEOUL[0]
    lon: float = SEOUL[1]
    fetch: Callable[[str], dict] = _http_json  # 테스트에서 가짜로 바꾼다

    def daily(self, start: date, end: date, today: date) -> list[tuple[date, float | None, float | None]]:
        """날짜별 (최대 강수확률 %, 낮 최고기온). 지난 날과 앞으로의 날을 나눠 요청한다."""
        out = []
        ranges = []
        if start < today:
            ranges.append((HISTORY_URL, start, min(end, today - timedelta(days=1))))
        if end >= today:
            ranges.append((FORECAST_URL, max(start, today), end))
        for base, s, e in ranges:
            query = urllib.parse.urlencode(
                {
                    "latitude": self.lat,
                    "longitude": self.lon,
                    "daily": "precipitation_probability_max,temperature_2m_max",
                    "timezone": "Asia/Seoul",
                    "start_date": s.isoformat(),
                    "end_date": e.isoformat(),
                }
            )
            data = self.fetch(f"{base}?{query}")["daily"]
            out += zip(
                (date.fromisoformat(d) for d in data["time"]),
                data["precipitation_probability_max"],
                data["temperature_2m_max"],
            )
        return out


def weather_notes(rows: list[tuple[date, float | None, float | None]]) -> list[AutoNote]:
    notes = []
    for day, rain, tmax in rows:
        if rain is not None and rain >= RAIN_PROBABILITY:
            notes.append(AutoNote(day, f"{RAIN_PREFIX}강수확률 {rain:.0f}%)", "날씨"))
        if tmax is not None and tmax >= HEAT_TEMPERATURE:
            notes.append(AutoNote(day, f"{HEAT_PREFIX}낮 최고 {tmax:.0f}도)", "날씨"))
    return notes


def holiday_notes(start: date, end: date) -> list[AutoNote]:
    """start~end 사이 날짜에 붙일 명절 메모. 명절 3일 전부터 당일까지 대목, 다음날은 연휴."""
    kr = holidays.KR(years=range(start.year, end.year + 2), language="ko")
    notes = []
    for day, name in kr.items():
        if name not in HOLIDAY_NAMES:
            continue
        for before in (3, 2, 1, 0):
            d = day - timedelta(days=before)
            label = f"{name} 당일" if before == 0 else f"{name} {before}일 전"
            notes.append(AutoNote(d, f"{label} (명절 대목)", "명절"))
        notes.append(AutoNote(day + timedelta(days=1), f"{name} 다음날 (연휴, 손님 적음)", "명절"))
    return sorted((n for n in notes if start <= n.day <= end), key=lambda n: (n.day, n.text))


def collect(start: date, end: date, today: date, weather: WeatherSource | None) -> tuple[list[AutoNote], str | None]:
    """(메모 목록, 날씨를 못 가져왔을 때의 오류). 날씨가 실패해도 명절 메모는 돌려준다."""
    notes = holiday_notes(start, end)
    error = None
    if weather is not None:
        try:
            notes += weather_notes(weather.daily(start, end, today))
        except (OSError, KeyError, ValueError) as e:
            error = f"날씨를 가져오지 못했습니다: {e}"
    return sorted(notes, key=lambda n: (n.day, n.text)), error
