"""사장님 메모를 AI가 웹 검색으로 확인한다.

매일 TV·건강 기사를 뒤지는 건 결과가 들쭉날쭉해서, 사장님이 들은 이야기를 메모로 남기면
("어제 TV에서 양배추가 몸에 좋다고 나왔대") 그 메모를 기준으로 AI가 사실을 확인한다.
발주 추천을 만들 때 아직 확인하지 않은 메모만 조사하고, 결과를 에이전트에게 메모와 함께 준다.

확인하지 못한 내용은 짐작하지 않는다. 확인 여부는 에이전트와 사장님이 모두 볼 수 있게 남긴다.
"""

from .catalog import Catalog
from .llm import LLMClient
from .sheets import ALIASES
from .store import Note, Research

SYSTEM_PROMPT = """너는 과일·야채 매장 사장님이 남긴 메모를 확인하는 조사원이다. 웹 검색으로 메모 내용을 확인하고,
발주에 영향을 줄 정보를 정리한다.

- 메모가 TV 방송, 건강 기사, 지역 행사, 날씨처럼 바깥에서 일어난 일이면 웹 검색으로 확인한다
  (언제, 어떤 방송·기사·행사인지, 어떤 식재료인지).
- 단골 주문, 거래처 사정처럼 가게 안의 일이라 검색으로 확인할 수 없는 메모는 검색하지 않는다.
  confirmed는 false로 하고 summary에 "가게 안의 일이라 검색으로 확인하지 않음"이라고 쓴다.
- 출처로 확인했을 때만 confirmed를 true로 한다. 확인하지 못한 내용은 짐작해서 쓰지 않는다.
- items에는 아래 품목 목록 중 수요가 달라질 품목 이름만 넣는다. 없으면 비운다.
- summary는 사장님이 읽을 한두 문장이다. 확인한 사실과 출처 이름을 쓴다.
- sources에는 확인에 쓴 출처의 제목과 주소를 넣는다."""

SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"},
        "summary": {"type": "string"},
        "items": {"type": "array", "items": {"type": "string"}},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "url": {"type": "string"}},
                "required": ["title", "url"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["confirmed", "summary", "items", "sources"],
    "additionalProperties": False,
}


def match_item(name: str, catalog: Catalog) -> str | None:
    """품목 이름이나 줄임말을 품목 id로 바꾼다. 우리 품목이 아니면 None."""
    name = name.strip()
    by_name = {i.name: i.id for i in catalog.items}
    if name in by_name:
        return by_name[name]
    for full, aliases in ALIASES.items():
        if name in aliases and full in by_name:
            return by_name[full]
    return None


def research_note(llm: LLMClient, catalog: Catalog, note: Note) -> Research:
    items = ", ".join(i.name for i in catalog.items)
    prompt = f"메모 날짜: {note.day.isoformat()}\n메모: {note.text}\n\n품목 목록: {items}"
    resp = llm.generate_json(SYSTEM_PROMPT, prompt, SCHEMA, purpose=f"research {note.day} #{note.id}")
    item_ids = []
    for name in resp["items"]:
        item_id = match_item(name, catalog)
        if item_id and item_id not in item_ids:
            item_ids.append(item_id)
    sources = tuple((s["title"], s["url"]) for s in resp["sources"] if s["url"].startswith(("http://", "https://")))
    return Research(bool(resp["confirmed"]), resp["summary"].strip(), tuple(item_ids), sources)


def describe_for_agent(note: Note, catalog: Catalog) -> str:
    """에이전트에게 줄 메모 문장. 조사 결과가 있으면 확인 여부와 함께 붙인다."""
    r = note.research
    if r is None:
        return note.text
    names = ", ".join(catalog.item(i).name for i in r.items)
    status = "AI 확인" if r.confirmed else "AI가 확인하지 못함"
    return f"{note.text} ({status}: {r.summary}" + (f" / 관련 품목: {names}" if names else "") + ")"
