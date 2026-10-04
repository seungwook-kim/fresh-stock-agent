"""장날(5일장)과 입고일(가락시장 휴장) 계산."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

# 가게는 한국에 있다. 서버가 다른 시간대(UTC 클라우드 등)에서 돌아도 날짜는 한국 기준으로 센다 (서머타임 없음)
KST = timezone(timedelta(hours=9), "KST")


def now_kst() -> datetime:
    return datetime.now(KST)


def today_kst() -> date:
    return now_kst().date()


@dataclass(frozen=True)
class MarketCalendar:
    # 5일장은 날짜 끝자리로 정해진다. (2, 7)이면 2·7·12·17·22·27일
    market_day_digits: tuple[int, ...] = (2, 7)
    # 입고가 없는 요일 (월=0 ... 일=6). 가락시장이 일요일에 쉬어 일요일 입고가 없다
    no_supply_weekdays: tuple[int, ...] = (6,)

    def is_market_day(self, d: date) -> bool:
        return d.day % 10 in self.market_day_digits

    def is_supply_day(self, d: date) -> bool:
        return d.weekday() not in self.no_supply_weekdays

    def next_supply_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_supply_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def cover_days(self, arrival: date) -> list[date]:
        """arrival에 들어온 물건으로 버텨야 하는 날들 (다음 입고일 전날까지).

        토요일 입고분은 일요일까지 팔아야 하므로 [토, 일]이 된다.
        """
        end = self.next_supply_day(arrival)
        return [arrival + timedelta(days=i) for i in range((end - arrival).days)]
