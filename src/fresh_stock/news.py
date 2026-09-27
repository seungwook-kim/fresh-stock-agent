"""건강 기사·방송에서 효능이 소개된 품목 찾기 (메모 제안).

TV 건강 프로그램 방송 내용은 공식 API가 없어서, AI가 웹 검색으로 최근 기사·방송 정보를 찾는다.
실제로 해보면 TV 방송보다 인터넷 건강 기사가 주로 잡히고 몇 분이 걸린다. 틀린 정보로 발주가
늘어나면 안 되므로 자동으로 메모에 넣지 않고, 사장님이 확인하고 추가하는 "제안"으로만 쓴다.
우리 가게 품목(정식 이름이나 줄임말)과 맞는 것만 남긴다.
"""

from dataclasses import dataclass
from datetime import date, timedelta

from .catalog import Catalog
from .llm import LLMClient
from .sheets import ALIASES

SEARCH_DAYS = 7

SYSTEM_PROMPT = """너는 과일·야채 매장을 위한 조사원이다. 웹 검색으로 최근 한국 TV 건강·교양 프로그램이나
건강 기사에서 효능을 소개한 과일·채소를 찾는다.

- 주어진 기간에 방송되거나 게시된 것만 넣는다. 날짜를 확인할 수 없으면 넣지 않는다.
- food에는 품목 목록에 있는 이름 하나만 쓴다 ("과일", "채소"처럼 뭉뚱그린 것은 넣지 않는다).
- kind는 "방송" 또는 "기사", title은 프로그램 이름이나 기사 제목, url은 확인한 출처 주소다.
- 찾은 것이 없으면 found를 비운다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "found": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "food": {"type": "string"},
                    "kind": {"type": "string", "enum": ["방송", "기사"]},
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "url": {"type": "string"},
                },
                "required": ["food", "kind", "title", "date", "url"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["found"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class FoodNews:
    item_id: str
    name: str
    kind: str  # 방송 / 기사
    title: str
    published: date | None
    url: str

    def note(self) -> str:
        """메모로 추가할 때의 문구. 과거 기록 검색이 TV 효능 소개 메모와 비슷하게 찾도록 맞춘다."""
        when = f"{self.published.month}/{self.published.day} " if self.published else ""
        return f"{when}{self.kind}에서 {self.name} 효능 소개 (손님 문의 많을 듯)"


def _match_item(food: str, catalog: Catalog) -> str | None:
    food = food.strip()
    by_name = {i.name: i.id for i in catalog.items}
    if food in by_name:
        return by_name[food]
    for name, aliases in ALIASES.items():
        if food in aliases and name in by_name:
            return by_name[name]
    return None


def _parse_date(text: str) -> date | None:
    try:
        return date.fromisoformat(text.strip()[:10])
    except ValueError:
        return None


def find_food_news(llm: LLMClient, catalog: Catalog, day: date, days: int = SEARCH_DAYS) -> list[FoodNews]:
    start = day - timedelta(days=days)
    items = ", ".join(i.name for i in catalog.items if i.category in ("과일", "야채"))
    prompt = (
        f"기준일은 {day.isoformat()}이다. {start.isoformat()}부터 {day.isoformat()}까지 방송되거나 게시된 것을 찾아라.\n"
        f"품목 목록: {items}"
    )
    resp = llm.generate_json(SYSTEM_PROMPT, prompt, SCHEMA, purpose=f"news {day}")
    found: dict[str, FoodNews] = {}
    for f in resp["found"]:
        item_id = _match_item(f["food"], catalog)
        published = _parse_date(f["date"])
        if item_id is None or (published is not None and not start <= published <= day):
            continue  # 우리 품목이 아니거나 기간 밖이면 버린다
        found.setdefault(
            item_id, FoodNews(item_id, catalog.item(item_id).name, f["kind"], f["title"], published, f["url"])
        )
    return list(found.values())
