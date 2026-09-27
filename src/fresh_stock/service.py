"""매장 하루 업무 흐름. 웹 화면과 무관한 업무 로직이다.

마감 재고표 사진 → 확인·수정 → 발주 추천 → 승인 → 거래처 문자
"""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from .accounting import clearance_estimate, regular_sales_series
from .agent import AgentPolicy
from .auto_notes import HEAT_PREFIX, RAIN_PREFIX, AutoNote, WeatherSource, collect
from .catalog import Catalog
from .llm import ImageInput, LLMClient, LLMError
from .market_calendar import MarketCalendar
from .memory import DayMemoryBank
from .research import describe_for_agent, research_note
from .messages import LLMGreetingWriter, OrderMessage, build_messages
from .ordering import OrderContext, SeasonalPolicy, check_order
from .recognition import recognize_sheet
from .simulator import MARKDOWN_RATE
from .store import OWNER_NOTE, Note, OrderLineRecord, OrderPlan, Research, SheetRowRecord, Store

SHEET, ORDER = "sheet", "order"


@dataclass(frozen=True)
class RevenueRow:
    day: date
    actual: int  # 돈통 현금 + 카드 합계
    estimated: int  # 재고로 역산한 판매량 × 가격 (떨이는 할인가)

    @property
    def gap(self) -> int:
        return self.actual - self.estimated


class StoreService:
    def __init__(
        self,
        store: Store,
        catalog: Catalog,
        calendar: MarketCalendar,
        llm_factory: Callable[[], LLMClient] | None = None,
        store_name: str = "우리가게",
        memory: DayMemoryBank | None = None,
        auto_notes: bool = False,
        weather: WeatherSource | None = None,
        research_llm_factory: Callable[[], LLMClient] | None = None,
    ):
        self.store = store
        self.catalog = catalog
        self.calendar = calendar
        self.llm_factory = llm_factory
        self.store_name = store_name
        self.memory = memory  # 과거 비슷한 날 검색 (RAG). 없으면 경험 노트만 쓴다
        self.auto_notes = auto_notes  # 발주 추천을 만들 때 날씨·명절 메모를 먼저 가져온다
        self.weather = weather
        self.research_llm_factory = research_llm_factory  # 웹 검색이 되는 LLM (사장님 메모 확인)

    # 지금 할 일 ------------------------------------------------------------------

    def status(self) -> tuple[str, date]:
        """(할 일, 날짜). 마감한 날의 발주가 남았으면 발주, 아니면 다음 날 재고표."""
        last = self.store.last_closed_day()
        if last is None:
            return SHEET, date.today()
        if self.calendar.is_supply_day(last + timedelta(days=1)):
            plan = self.store.plan(last)
            if plan is None or not plan.approved:
                return ORDER, last
        return SHEET, last + timedelta(days=1)

    # 재고표 ----------------------------------------------------------------------

    def sheet_bounds(self, day: date) -> tuple[dict[str, float], dict[str, float]]:
        """품목별 (어제 재고 + 오늘 입고, 예상 재고). 재고표 확인 표시에 쓴다."""
        prev = self.store.closing(day - timedelta(days=1))
        received = self.store.received(day)
        max_possible = {i.id: prev.get(i.id, 0) + received.get(i.id, 0) for i in self.catalog.items}
        ledger = self.store.ledger()
        recent = regular_sales_series(ledger, self.catalog)[-7:]
        expected = {
            i: q - (mean(s.get(i, 0) for s in recent) if recent else 0) for i, q in max_possible.items()
        }
        return max_possible, expected

    def learned_aliases(self) -> dict[str, str]:
        """확인 화면에서 사람이 확정한 기록으로 익힌 이 매장의 줄임말 (적힌 이름 → 품목 id)."""
        known = {i.id for i in self.catalog.items}
        aliases = {}
        for raw, item_id in self.store.confirmed_names().items():
            if item_id not in known:
                continue
            # 정식 이름 그대로이거나, 숫자·지운 자국이 섞인 긴 글자는 줄임말로 쓰지 않는다
            if raw == self.catalog.item(item_id).name or not raw or len(raw) > 8 or any(c.isdigit() for c in raw):
                continue
            aliases[raw] = item_id
        return aliases

    def read_sheet(self, day: date, image: bytes, media_type: str = "image/jpeg") -> list[SheetRowRecord]:
        if self.llm_factory is None:
            raise RuntimeError("재고표를 읽으려면 LLM이 필요합니다.")
        max_possible, expected = self.sheet_bounds(day)
        rows = recognize_sheet(
            self.llm_factory(),
            ImageInput(image, media_type),
            self.catalog,
            max_possible,
            expected,
            f"sheet {day}",
            aliases=self.learned_aliases(),
        )
        records = [
            SheetRowRecord(n, r.raw_name, r.raw_qty, r.item_id, r.qty, tuple(r.flags), None, None)
            for n, r in enumerate(rows)
        ]
        self.store.save_sheet(day, image, records)
        return records

    def missing_items(self, day: date, rows: list[SheetRowRecord]) -> list[str]:
        """재고표에 없어서 0으로 저장될 품목 중 예상 재고가 남아 있어야 하는 것. 적는 걸 빠뜨렸을 수 있다."""
        _, expected = self.sheet_bounds(day)
        on_sheet = {r.item_id_read for r in rows}
        return [i.id for i in self.catalog.items if i.id not in on_sheet and expected.get(i.id, 0) >= 1]

    def confirm_sheet(
        self, day: date, finals: dict[int, tuple[str | None, float | None]], cash: int, card: int
    ) -> dict[str, float]:
        """사람이 확인한 재고표로 그날 마감 재고를 저장한다. 표에 없는 품목은 0, 같은 품목 두 줄은 더한다 (창고 + 진열대)."""
        closing = {i.id: 0.0 for i in self.catalog.items}
        for item_id, qty in finals.values():
            if item_id in closing and qty is not None:
                closing[item_id] += qty
        self.store.finalize_sheet_rows(day, finals)
        self.store.save_day(day, closing, cash, card)
        return closing

    # 발주 ------------------------------------------------------------------------

    def order_context(self, day: date) -> OrderContext | None:
        arrival = day + timedelta(days=1)
        if not self.calendar.is_supply_day(arrival):
            return None
        cover = self.calendar.cover_days(arrival)
        notes: dict[date, list[str]] = {}
        for note in self.store.note_records(cover[0], cover[-1]):
            # 사장님 메모는 AI 조사 결과(확인 여부, 관련 품목)와 함께 에이전트에게 준다
            notes.setdefault(note.day, []).append(describe_for_agent(note, self.catalog))
        note_log: dict[date, list[str]] = {}
        for _, d, text in self.store.notes(end=day):
            note_log.setdefault(d, []).append(text)
        return OrderContext(
            today=day,
            arrival=arrival,
            cover_days=cover,
            ledger=self.store.ledger(),
            catalog=self.catalog,
            calendar=self.calendar,
            notes=notes,
            note_log=note_log,
        )

    def refresh_auto_notes(self, day: date, days_ahead: int = 3) -> tuple[list[AutoNote], str | None]:
        """day 다음 날부터 days_ahead일 동안의 날씨·명절 메모를 넣는다. (새로 넣은 메모, 날씨 오류)

        같은 메모는 다시 넣지 않는다. 날씨를 가져왔으면 그 기간의 이전 날씨 메모는 새 예보로 바꾼다.
        """
        start, end = day + timedelta(days=1), day + timedelta(days=days_ahead)
        notes, error = collect(start, end, date.today(), self.weather)
        if self.weather is not None and error is None:
            fresh = {(n.day, n.text) for n in notes if n.source == "날씨"}
            for note_id, d, text in self.store.notes(start, end):
                if text.startswith((RAIN_PREFIX, HEAT_PREFIX)) and (d, text) not in fresh:
                    self.store.delete_note(note_id)
        existing = {(d, t) for _, d, t in self.store.notes(start, end)}
        added = [n for n in notes if (n.day, n.text) not in existing]
        for n in added:
            self.store.add_note(n.day, n.text, source=n.source)
        return added, error

    def research_notes(self, notes: list[Note]) -> dict[int, Research]:
        """사장님 메모를 AI가 웹 검색으로 확인하고 저장한다. 여러 메모는 동시에 조사한다."""
        if self.research_llm_factory is None or not notes:
            return {}
        llm = self.research_llm_factory()

        def one(note: Note) -> tuple[int, Research]:
            try:
                return note.id, research_note(llm, self.catalog, note)
            except LLMError as e:
                # 실패해도 발주 추천은 나가야 한다. 실패로 남기고, 화면에서 다시 조사할 수 있다
                return note.id, Research(False, f"AI가 조사하지 못했습니다: {str(e)[:80]}", (), ())

        with ThreadPoolExecutor(max_workers=3) as pool:
            results = dict(pool.map(one, notes))
        for note_id, research in results.items():
            self.store.save_research(note_id, research)
        return results

    def pending_research(self, day: date) -> list[Note]:
        """day 발주가 버틸 날들의 사장님 메모 중 아직 AI가 확인하지 않은 것."""
        arrival = day + timedelta(days=1)
        if not self.calendar.is_supply_day(arrival):
            return []
        cover = self.calendar.cover_days(arrival)
        return [
            n for n in self.store.note_records(cover[0], cover[-1]) if n.source == OWNER_NOTE and n.research is None
        ]

    def make_plan(self, day: date) -> OrderPlan | None:
        """발주 추천을 새로 만든다. 가게 메모가 있으면 에이전트가 조정한다."""
        if self.auto_notes:
            self.refresh_auto_notes(day)
        # 사장님 메모는 발주 추천을 만들 때 AI가 웹 검색으로 확인한다 (스케줄러 없이)
        self.research_notes(self.pending_research(day))
        ctx = self.order_context(day)
        if ctx is None:
            return None
        base = SeasonalPolicy().decide(ctx)
        decisions = {}
        if ctx.notes and self.llm_factory is not None:
            agent = AgentPolicy(self.llm_factory(), memory=self.memory)
            recommended = agent.decide(ctx)
            decisions = {d.item_id: d for d in agent.decisions}
            if agent.summaries:
                summary = agent.summaries[-1][1]
            else:
                summary = "에이전트 호출에 실패해서 평소 패턴(요일·장날) 기준 추천을 그대로 씁니다."
        else:
            recommended = base
            summary = "가게 메모가 없어 평소 패턴(요일·장날) 기준으로 추천했습니다."
        lines = []
        for item in self.catalog.items:
            dec = decisions.get(item.id)
            qty = recommended[item.id]
            lines.append(
                OrderLineRecord(item.id, base[item.id], qty, qty, dec.factors if dec else "", dec.reason if dec else "")
            )
        plan = OrderPlan(day, ctx.arrival, summary, False, tuple(lines))
        self.store.save_plan(plan)
        return plan

    def similar_days(self, day: date) -> list[str]:
        """발주 화면에 보여줄 과거 비슷한 날 (에이전트가 받은 것과 같은 검색)."""
        ctx = self.order_context(day)
        if ctx is None or not ctx.notes or self.memory is None:
            return []
        query = " / ".join(n for _, notes in sorted(ctx.notes.items()) for n in notes)
        return [m.describe(self.catalog, self.calendar) for m in self.memory.search(ctx, query)]

    def plan_for(self, day: date) -> OrderPlan | None:
        return self.store.plan(day) or self.make_plan(day)

    def check_finals(self, day: date, finals: dict[str, int]) -> dict[str, str]:
        """입력한 발주 수량 중 확인이 필요한 것 (입력 실수 가드레일)."""
        plan = self.store.plan(day)
        recommended = {ln.item_id: ln.recommended for ln in plan.lines} if plan else {}
        cover = len(self.calendar.cover_days(day + timedelta(days=1)))
        warnings = {}
        for item_id, qty in finals.items():
            past_daily = [q / len(self.calendar.cover_days(a)) for a, q in self.store.approved_finals(item_id)]
            item = self.catalog.item(item_id)
            warning = check_order(item, qty, cover, past_daily, recommended=recommended.get(item_id) or None)
            if warning:
                warnings[item_id] = warning
        return warnings

    def approve(self, day: date, finals: dict[str, int]) -> OrderPlan:
        plan = self.store.plan(day)
        if plan is None:
            raise ValueError(f"{day} 발주 추천이 없습니다.")
        lines = tuple(
            OrderLineRecord(
                ln.item_id, ln.base, ln.recommended, finals.get(ln.item_id, ln.final), ln.factors, ln.reason
            )
            for ln in plan.lines
        )
        approved = OrderPlan(plan.order_day, plan.arrival, plan.summary, True, lines)
        self.store.save_plan(approved)
        self.store.save_received(plan.arrival, {ln.item_id: ln.final for ln in lines})
        return approved

    def messages(self, day: date, greeting: bool = False) -> list[OrderMessage]:
        plan = self.store.plan(day)
        if plan is None or not plan.approved:
            return []
        cover = self.calendar.cover_days(plan.arrival)
        notes = [t for _, _, t in self.store.notes(cover[0], cover[-1])]
        writer = None
        if greeting and self.llm_factory is not None:
            writer = LLMGreetingWriter(self.llm_factory(), self.store_name)
        finals = {ln.item_id: ln.final for ln in plan.lines}
        return build_messages(self.catalog, plan.arrival, finals, notes, writer)

    # 매출 ------------------------------------------------------------------------

    def revenue(self, days: int = 7) -> list[RevenueRow]:
        """실제 매출(현금+카드)과 재고로 역산한 추정 매출. 차이는 할인·기록 실수 등이다."""
        ledger = self.store.ledger()
        sales = regular_sales_series(ledger, self.catalog)
        rows = []
        for idx in range(max(1, len(ledger) - days), len(ledger)):
            rec = ledger[idx]
            estimated = 0.0
            for item in self.catalog.items:
                cleared = clearance_estimate(ledger, idx, item)
                estimated += sales[idx].get(item.id, 0) * item.price + cleared * item.price * MARKDOWN_RATE
            rows.append(RevenueRow(rec.day, rec.revenue, round(estimated, -2)))
        return rows
