"""사장님 메모를 AI가 웹 검색으로 확인하는 기능 테스트. 웹 검색 LLM은 가짜로 대신한다."""

from datetime import date, timedelta

from conftest import CATALOG, WebEnv

from fresh_stock.llm import LLMError
from fresh_stock.research import describe_for_agent, match_item, research_note
from fresh_stock.store import Note, Research


class FakeResearchLLM:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.calls.append((purpose, prompt))
        if self.fail:
            raise LLMError("timeout")
        return {
            "confirmed": True,
            "summary": "KBS 생로병사의 비밀(7/30)에서 양배추 위 건강 효능 소개",
            "items": ["양배추", "방토", "아보카도"],
            "sources": [{"title": "방송 기사", "url": "https://news.example.com/1"}, {"title": "메모", "url": "없음"}],
        }


class FakeAgentLLM:
    def __init__(self):
        self.prompts = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        self.prompts.append((purpose, prompt))
        return {"action": "submit", "item_ids": [], "query": "", "adjustments": [], "summary": "반영"}


NOTE = Note(1, date(2026, 8, 1), "어제 TV에서 양배추가 몸에 좋다고 나왔대", "사장님", "boss@gmail.com", None)


def test_research_note_keeps_only_our_items_and_real_urls():
    llm = FakeResearchLLM()
    r = research_note(llm, CATALOG, NOTE)
    assert r.confirmed and r.items == ("cabbage", "cherry_tomato")  # 없는 품목은 버리고 줄임말은 품목으로
    assert r.sources == (("방송 기사", "https://news.example.com/1"),)
    assert "어제 TV에서 양배추" in llm.calls[0][1] and "2026-08-01" in llm.calls[0][1]


def test_match_item():
    assert match_item("방울토마토", CATALOG) == "cherry_tomato"
    assert match_item(" 샤인 ", CATALOG) == "shine_muscat"
    assert match_item("아보카도", CATALOG) is None


def test_describe_for_agent():
    assert describe_for_agent(NOTE, CATALOG) == NOTE.text
    confirmed = Note(**{**NOTE.__dict__, "research": Research(True, "방송 확인", ("cabbage",), ())})
    assert describe_for_agent(confirmed, CATALOG) == f"{NOTE.text} (AI 확인: 방송 확인 / 관련 품목: 양배추)"
    unconfirmed = Note(**{**NOTE.__dict__, "research": Research(False, "찾지 못함", (), ())})
    assert "AI가 확인하지 못함: 찾지 못함" in describe_for_agent(unconfirmed, CATALOG)


def _env(research_llm, agent_llm=None):
    env = WebEnv(llm=agent_llm or FakeAgentLLM(), research_llm=research_llm)
    return env


def test_plan_researches_owner_notes_first_and_passes_to_agent():
    research, agent = FakeResearchLLM(), FakeAgentLLM()
    env = _env(research, agent)
    day = env.first  # 7/31(금) 마감 → 8/1(토)·8/2(일) 입고분
    store = env.store
    owner_note = store.add_note(day + timedelta(days=1), "어제 TV에서 양배추가 몸에 좋다고 나왔대", author="boss@gmail.com")
    env.store.save_day(day, {}, 0, 0)  # 마감을 끝낸 상태로 만든다
    env.login()
    page = env.client.get("/order").text

    researched = {n.id: n.research for n in store.note_records()}
    assert researched[owner_note].confirmed
    # 날씨·명절 같은 자동 메모와 데모의 기존 메모는 사장님 메모가 아니면 조사하지 않는다
    assert all(p.startswith("research") for p, _ in research.calls)
    assert len(research.calls) == sum(
        1 for n in store.note_records(day + timedelta(days=1), day + timedelta(days=2)) if n.source == "사장님"
    )
    order_prompt = next(p for purpose, p in agent.prompts if purpose.startswith("order"))
    assert "AI 확인: KBS 생로병사의 비밀" in order_prompt and "관련 품목: 양배추, 방울토마토" in order_prompt
    assert "AI가 확인한 메모" in page and "https://news.example.com/1" in page

    # 한 번 조사한 메모는 추천을 다시 만들어도 다시 조사하지 않는다
    n_calls = len(research.calls)
    env.client.post("/order/refresh")
    assert len(research.calls) == n_calls


def test_research_failure_does_not_block_order():
    env = _env(FakeResearchLLM(fail=True))
    day = env.first
    note_id = env.store.add_note(day + timedelta(days=1), "옆 동네 축제 한대")
    env.store.save_day(day, {}, 0, 0)
    env.login()
    page = env.client.get("/order").text
    assert "입고분 발주" in page
    r = next(n.research for n in env.store.note_records() if n.id == note_id)
    assert not r.confirmed and "AI가 조사하지 못했습니다" in r.summary


def test_home_shows_research_and_manual_recheck():
    research = FakeResearchLLM()
    env = _env(research)
    note_id = env.store.add_note(env.first + timedelta(days=1), "어제 TV에서 양배추가 몸에 좋다고 나왔대")
    env.login()
    page = env.client.get("/").text
    assert "발주 추천을 만들 때 AI가 확인해요" in page and "지금 확인" in page
    page = env.client.post(f"/notes/{note_id}/research", follow_redirects=True).text
    assert "AI 확인: KBS 생로병사의 비밀" in page and "관련 품목: 양배추" in page and "다시 확인" in page


def test_no_research_button_without_search_llm():
    env = WebEnv(llm=FakeAgentLLM())
    env.login()
    assert "지금 확인" not in env.client.get("/").text
