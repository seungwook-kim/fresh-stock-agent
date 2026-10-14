"""매장 하루 업무 흐름. 웹 화면과 무관한 업무 로직이다.

(입고 확인) → 마감 재고표 사진 → 확인·수정 → 발주 추천 → 승인 → 거래처 문자

가게 설정(품목·거래처, 장날·입고 요일, 경험 노트)도 여기서 고친다. 가게마다 DB에 저장되고,
고치기 전까지는 기본값(가상 매장 목록, 2·7일 장, 일요일 입고 없음, 기본 경험 노트)을 쓴다.
"""

import math
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import mean

from .accounting import clearance_estimate, regular_sales_series
from .agent import STORE_KNOWLEDGE, AgentPolicy
from .auto_notes import HEAT_PREFIX, RAIN_PREFIX, AutoNote, WeatherSource, collect
from .catalog import Catalog
from .domain import CHANNELS, UNITS, Category, Item, Supplier
from .llm import ImageInput, LLMClient, LLMError
from .market_calendar import MarketCalendar, today_kst
from .memory import DayMemoryBank
from .research import describe_for_agent, research_note
from .messages import LLMGreetingWriter, OrderMessage, build_messages
from .ordering import OrderContext, SeasonalPolicy, check_order
from .recognition import recognize_sheet
from .simulator import MARKDOWN_RATE
from .store import OWNER_NOTE, Note, OrderLineRecord, OrderPlan, Research, SheetRowRecord, Store

SHEET, ORDER = "sheet", "order"
# 가게 설정 키 (store_setting)
MARKET_DAYS, CLOSED_WEEKDAYS, KNOWLEDGE = "market_days", "closed_weekdays", "knowledge"
# 재고표 형식: simple은 품목·남은 수량, store는 품목·남은 수량·사입 수량·사입 가격 (실제 가게 종이 형식)
SHEET_LAYOUT, SIMPLE, STORE_LAYOUT = "sheet_layout", "simple", "store"
MAX_KNOWLEDGE_LEN = 4000  # 경험 노트는 발주 에이전트 프롬프트에 그대로 들어간다


def _ints(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in text.split(",") if x.strip())


def calendar_from_settings(settings: dict[str, str], default: MarketCalendar) -> MarketCalendar:
    """가게 설정의 장날(날짜 끝자리)·입고 없는 요일. 정하지 않은 것은 default를 쓴다."""
    return MarketCalendar(
        market_day_digits=_ints(settings[MARKET_DAYS]) if MARKET_DAYS in settings else default.market_day_digits,
        no_supply_weekdays=(
            _ints(settings[CLOSED_WEEKDAYS]) if CLOSED_WEEKDAYS in settings else default.no_supply_weekdays
        ),
    )


def _new_id(prefix: str, taken: set[str]) -> str:
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


@dataclass(frozen=True)
class RevenueRow:
    day: date
    actual: int  # 돈통 현금 + 카드 합계
    estimated: int  # 재고로 역산한 판매량 × 가격 (떨이는 할인가)
    purchase: int | None = None  # 그날 사입 금액 (재고표 사입 가격이 있는 날만)

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
        self.default_catalog = catalog
        self.default_calendar = calendar
        self.llm_factory = llm_factory
        self.store_name = store_name
        self.memory = memory  # 과거 비슷한 날 검색 (RAG). 없으면 경험 노트만 쓴다
        self.auto_notes = auto_notes  # 발주 추천을 만들 때 날씨·명절 메모를 먼저 가져온다
        self.weather = weather
        self.research_llm_factory = research_llm_factory  # 웹 검색이 되는 LLM (사장님 메모 확인)
        # 발주 추천은 1~3분 걸린다. 새로고침·두 사람이 동시에 열어도 한 번만 만들게 한다
        self._plan_lock = threading.Lock()
        self.refresh()

    def refresh(self) -> None:
        """가게 설정을 DB에서 다시 읽는다. 웹·MCP는 요청마다 부른다 (다른 화면·프로세스에서 고쳤을 수 있다)."""
        settings = self.store.settings()
        self.catalog = self.store.catalog() or self.default_catalog
        self.calendar = calendar_from_settings(settings, self.default_calendar)
        self.knowledge = settings.get(KNOWLEDGE, STORE_KNOWLEDGE)
        self.sheet_layout = settings.get(SHEET_LAYOUT, SIMPLE)
        if self.memory is not None:
            self.memory.use(self.catalog, self.calendar)

    # 지금 할 일 ------------------------------------------------------------------

    def status(self) -> tuple[str, date]:
        """(할 일, 날짜). 마감한 날의 발주가 남았으면 발주, 아니면 다음 날 재고표."""
        last = self.store.last_closed_day()
        if last is None:
            return SHEET, today_kst()
        if self.calendar.is_supply_day(last + timedelta(days=1)):
            plan = self.store.plan(last)
            if plan is None or not plan.approved:
                return ORDER, last
        return SHEET, last + timedelta(days=1)

    def days_behind(self) -> int:
        """할 일 날짜가 오늘(한국 시간)보다 며칠 전인지. 가게를 쉬었거나 기록을 빠뜨린 날이 있으면 1 이상."""
        _, day = self.status()
        return max(0, (today_kst() - day).days)

    def skip(self) -> tuple[str, date]:
        """지금 할 일을 건너뛴다 (가게를 쉰 날). 건너뛴 (할 일, 날짜)를 돌려준다.

        재고표 차례면 그날은 아무것도 안 팔린 날로 기록한다 (마감 재고 = 전날 재고 + 그날 입고, 매출 0).
        발주 차례면 발주 없이 넘어간다 (입고 0). 오늘 이후의 날은 건너뛸 수 없다.
        건너뛰지 않으면 다음 재고표가 쉰 날의 날짜로 저장돼 이후 요일·장날 계산이 모두 하루씩 밀린다.
        """
        step, day = self.status()
        if day > today_kst():
            raise ValueError(f"{day}은(는) 아직 오지 않은 날이라 건너뛸 수 없습니다.")
        if step == SHEET:
            prev = self.store.closing(day - timedelta(days=1))
            received = self.store.received(day)
            closing = {i.id: prev.get(i.id, 0) + received.get(i.id, 0) for i in self.catalog.items}
            self.store.save_day(day, closing, 0, 0)
        else:
            plan = self.store.plan(day)
            lines = tuple(
                OrderLineRecord(ln.item_id, ln.base, ln.recommended, 0, ln.factors, ln.reason) for ln in plan.lines
            ) if plan else tuple(OrderLineRecord(i.id, 0, 0, 0, "", "") for i in self.catalog.items)
            arrival = day + timedelta(days=1)
            self.store.save_plan(OrderPlan(day, arrival, "발주 안 함 (건너뜀)", True, lines))
            self.store.save_received(arrival, {})
        return step, day

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
        purchases = self.sheet_layout == STORE_LAYOUT
        ordered, _ = self.arrivals(day)
        rows = recognize_sheet(
            self.llm_factory(),
            ImageInput(image, media_type),
            self.catalog,
            max_possible,
            expected,
            f"sheet {day}",
            aliases=self.learned_aliases(),
            purchases=purchases,
            prev_closing=self.store.closing(day - timedelta(days=1)) if purchases else None,
            ordered=ordered,
            ref_prices=self.store.last_prices(before=day) if purchases else None,
        )
        records = [
            SheetRowRecord(
                n, r.raw_name, r.raw_qty, r.item_id, r.qty, tuple(r.flags), None, None,
                r.raw_buy_qty, r.raw_buy_price, r.buy_qty, r.buy_price,
            )
            for n, r in enumerate(rows)
        ]  # fmt: skip
        self.store.save_sheet(day, image, records)
        return records

    def missing_items(self, day: date, rows: list[SheetRowRecord]) -> list[str]:
        """재고표에 없어서 0으로 저장될 품목 중 예상 재고가 남아 있어야 하는 것. 적는 걸 빠뜨렸을 수 있다."""
        _, expected = self.sheet_bounds(day)
        on_sheet = {r.item_id_read for r in rows}
        return [i.id for i in self.catalog.items if i.id not in on_sheet and expected.get(i.id, 0) >= 1]

    def confirm_sheet(
        self,
        day: date,
        finals: dict[int, tuple[str | None, float | None]],
        cash: int,
        card: int,
        buys: dict[int, tuple[float | None, int | None]] | None = None,
    ) -> dict[str, float]:
        """사람이 확인한 재고표로 그날 마감 재고를 저장한다. 표에 없는 품목은 0, 같은 품목 두 줄은 더한다 (창고 + 진열대).

        buys(줄 번호 → (사입 수량, 사입 가격))를 주면 사입 칸이 있는 재고표다. 사입 칸이 그날 실제로 들어온 양이므로
        승인한 발주 수량 대신 그 값으로 그날 입고를 바꾸고, 사입 가격을 기록한다. 빈 칸은 안 들어온 것이다.
        """
        closing = {i.id: 0.0 for i in self.catalog.items}
        for item_id, qty in finals.values():
            if item_id in closing and qty is not None:
                closing[item_id] += qty
        self.store.finalize_sheet_rows(day, finals)
        if buys is not None:
            received: dict[str, float] = {}
            prices: dict[str, int] = {}
            for idx, (qty, price) in buys.items():
                item_id = finals.get(idx, (None, None))[0]
                if item_id not in closing:
                    continue
                if qty:
                    received[item_id] = received.get(item_id, 0.0) + qty
                if price:
                    prices[item_id] = price
            self.store.finalize_sheet_buys(day, buys)
            self.store.save_received(day, received)
            self.store.save_prices(day, prices)
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
        notes, error = collect(start, end, today_kst(), self.weather)
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
            agent = AgentPolicy(
                self.llm_factory(),
                memory=self.memory,
                use_knowledge=bool(self.knowledge.strip()),
                knowledge=self.knowledge,
            )
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
        """저장된 추천, 없으면 새로 만든다. 다른 요청이 만드는 중이면 끝날 때까지 기다렸다가 그 결과를 쓴다."""
        plan = self.store.plan(day)
        if plan is not None:
            return plan
        with self._plan_lock:
            return self.store.plan(day) or self.make_plan(day)

    def remake_plan(self, day: date) -> OrderPlan | None:
        """다시 추천 (메모를 고친 뒤 등). 다른 요청이 만드는 중이면 그 결과를 쓰고 또 만들지 않는다."""
        if not self._plan_lock.acquire(blocking=False):
            with self._plan_lock:
                return self.store.plan(day)
        try:
            return self.make_plan(day)
        finally:
            self._plan_lock.release()

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
        purchases = self.store.purchase_totals()
        rows = []
        for idx in range(max(1, len(ledger) - days), len(ledger)):
            rec = ledger[idx]
            estimated = 0.0
            for item in self.catalog.items:
                cleared = clearance_estimate(ledger, idx, item)
                estimated += sales[idx].get(item.id, 0) * item.price + cleared * item.price * MARKDOWN_RATE
            rows.append(RevenueRow(rec.day, rec.revenue, round(estimated, -2), purchases.get(rec.day)))
        return rows

    # 입고 ------------------------------------------------------------------------

    def arrivals(self, day: date) -> tuple[dict[str, int], dict[str, float]]:
        """day에 들어온 물건: (승인한 발주 수량, 입고로 기록된 양). 발주를 승인하면 그 수량이 입고로 기록된다."""
        plan = self.store.plan(day - timedelta(days=1))
        ordered = {}
        if plan is not None and plan.approved and plan.arrival == day:
            ordered = {ln.item_id: ln.final for ln in plan.lines if ln.final > 0}
        return ordered, self.store.received(day)

    def fix_received(self, day: date, changes: dict[str, float]) -> dict[str, float]:
        """입고를 실제로 들어온 양으로 고친다 (덜 왔거나, 시장에 물건이 없었거나, 더 사 온 경우).

        판매량은 "어제 재고 + 오늘 입고 - 오늘 재고"로 역산하므로, 입고가 틀리면 판매량과 다음 발주가 같이 틀린다.
        마감 재고표를 확정하기 전, 지금 재고표를 쓸 날 것만 고칠 수 있다. 고친 뒤의 입고 기록을 돌려준다.
        """
        if self.sheet_layout == STORE_LAYOUT:
            raise ValueError("이 가게는 재고표의 사입 칸으로 그날 입고를 기록합니다. 재고표에 실제로 들어온 양을 적어 주세요.")
        step, current = self.status()
        if step != SHEET or day != current:
            now = f"지금은 {current} {'재고표' if step == SHEET else '발주'} 차례"
            raise ValueError(f"입고는 그날 마감 재고표를 확정하기 전에만 고칠 수 있습니다 ({now}).")
        known = {i.id for i in self.catalog.items}
        received = self.store.received(day)
        for item_id, qty in changes.items():
            if item_id not in known:
                raise ValueError(f"취급하는 품목이 아닙니다: {item_id}")
            if not math.isfinite(qty) or qty < 0:
                raise ValueError(f"{self.catalog.item(item_id).name}: 입고 수량은 0 이상이어야 합니다.")
            received[item_id] = round(qty, 2)
        self.store.save_received(day, received)
        return self.store.received(day)

    # 가게 설정 --------------------------------------------------------------------

    def _drop_pending_plan(self) -> None:
        """승인 전 발주 추천을 지워 바뀐 설정으로 다시 만들게 한다 (품목·입고 요일이 바뀌면 추천이 맞지 않는다)."""
        last = self.store.last_closed_day()
        if last is not None:
            self.store.delete_unapproved_plan(last)

    def _save_catalog(self, catalog: Catalog) -> None:
        before = {i.id for i in self.catalog.items}
        self.store.save_catalog(catalog)
        self.refresh()
        if {i.id for i in catalog.items} != before:
            self._drop_pending_plan()

    def save_item(
        self,
        item_id: str | None,
        *,
        name: str,
        category: str,
        unit: str,
        supplier_id: str,
        base_demand: float,
        price: int,
        cost: int,
        shelf_life: int,
    ) -> Item:
        """품목을 고치거나(item_id), item_id가 None이면 새로 더한다. 입력이 잘못되면 ValueError."""
        catalog = self.catalog
        name = name.strip()
        if item_id is not None and item_id not in {i.id for i in catalog.all_items}:
            raise ValueError("없는 품목입니다.")
        if not name or len(name) > 20:
            raise ValueError("품목 이름을 1~20자로 적어 주세요.")
        same = next((i for i in catalog.all_items if i.name == name and i.id != item_id), None)
        if same is not None:
            if same in catalog.retired:
                raise ValueError(f"취급을 그만둔 품목에 '{name}'이(가) 있습니다. 그 품목을 다시 취급으로 바꿔 주세요.")
            raise ValueError(f"'{name}'은(는) 이미 있는 품목입니다.")
        if category not in {c.value for c in Category}:
            raise ValueError("분류를 골라 주세요.")
        if unit not in UNITS:
            raise ValueError("단위를 골라 주세요.")
        if supplier_id not in {s.id for s in catalog.suppliers}:
            raise ValueError("거래처를 골라 주세요.")
        if not (math.isfinite(base_demand) and base_demand >= 0) or price < 0 or cost < 0:
            raise ValueError("판매량·가격은 0 이상으로 적어 주세요.")
        if not 1 <= shelf_life <= 30:
            raise ValueError("정가로 파는 기간은 1~30일로 적어 주세요.")
        item = Item(
            id=item_id or _new_id("item", {i.id for i in catalog.all_items}),
            name=name,
            category=Category(category),
            unit=unit,
            supplier_id=supplier_id,
            base_demand=float(base_demand),
            price=int(price),
            cost=int(cost),
            shelf_life=int(shelf_life),
        )
        self._save_catalog(catalog.with_item(item))
        return item

    def set_item_active(self, item_id: str, active: bool) -> None:
        """품목 취급을 그만두거나 다시 취급한다. 지난 기록에 남아 있어서 지우지는 않는다."""
        catalog = self.catalog
        if item_id not in {i.id for i in catalog.all_items}:
            raise ValueError("없는 품목입니다.")
        if not active and [i.id for i in catalog.items] == [item_id]:
            raise ValueError("취급하는 품목이 하나는 있어야 합니다.")
        self._save_catalog(catalog.with_active(item_id, active))

    def save_supplier(self, supplier_id: str | None, *, name: str, channel: str) -> Supplier:
        """거래처를 고치거나(supplier_id), None이면 새로 더한다."""
        catalog = self.catalog
        name = name.strip()
        if supplier_id is not None and supplier_id not in {s.id for s in catalog.suppliers}:
            raise ValueError("없는 거래처입니다.")
        if not name or len(name) > 30:
            raise ValueError("거래처 이름을 1~30자로 적어 주세요.")
        if any(s.name == name and s.id != supplier_id for s in catalog.suppliers):
            raise ValueError(f"'{name}'은(는) 이미 있는 거래처입니다.")
        if channel not in CHANNELS:
            raise ValueError("주문 방식을 골라 주세요.")
        supplier = Supplier(supplier_id or _new_id("supplier", {s.id for s in catalog.suppliers}), name, channel)
        self._save_catalog(catalog.with_supplier(supplier))
        return supplier

    def delete_supplier(self, supplier_id: str) -> None:
        """거래처를 지운다. 이 거래처에서 받는 품목이 있으면(취급을 그만둔 품목 포함) 지우지 않는다."""
        catalog = self.catalog
        using = [i.name for i in catalog.all_items if i.supplier_id == supplier_id]
        if using:
            more = f" 외 {len(using) - 3}개" if len(using) > 3 else ""
            raise ValueError(f"이 거래처에서 받는 품목이 있어 지울 수 없습니다: {', '.join(using[:3])}{more}")
        self._save_catalog(catalog.without_supplier(supplier_id))

    def save_calendar(self, market_digits: tuple[int, ...], closed_weekdays: tuple[int, ...]) -> None:
        """장날(날짜 끝자리, 2·7장이면 (2, 7))과 입고가 없는 요일(월=0 ... 일=6)."""
        if any(not 0 <= d <= 9 for d in market_digits) or any(not 0 <= w <= 6 for w in closed_weekdays):
            raise ValueError("장날이나 요일 값이 잘못됐습니다.")
        if len(set(closed_weekdays)) >= 7:
            raise ValueError("입고가 있는 요일이 하루는 있어야 합니다.")
        # 입고 요일이 바뀌면 지금 발주가 버틸 날도 달라진다
        self._drop_pending_plan()
        self.store.save_setting(MARKET_DAYS, ",".join(str(d) for d in sorted(set(market_digits))))
        self.store.save_setting(CLOSED_WEEKDAYS, ",".join(str(w) for w in sorted(set(closed_weekdays))))
        self.refresh()

    def save_sheet_layout(self, layout: str) -> None:
        """재고표 형식. store면 사입 수량·가격 칸까지 읽고, 그 칸으로 그날 입고와 사입 가격을 기록한다."""
        if layout not in (SIMPLE, STORE_LAYOUT):
            raise ValueError("재고표 형식을 골라 주세요.")
        self.store.save_setting(SHEET_LAYOUT, layout)
        self.refresh()

    def save_knowledge(self, text: str) -> None:
        """발주 에이전트가 참고할 사장님 경험 노트. 비우면 경험 노트 없이 판단한다."""
        text = text.strip().replace("\r\n", "\n")
        if len(text) > MAX_KNOWLEDGE_LEN:
            raise ValueError(f"경험 노트는 {MAX_KNOWLEDGE_LEN:,}자까지 적을 수 있습니다.")
        self.store.save_setting(KNOWLEDGE, text)
        self.refresh()

    def import_catalog(self, catalog: Catalog) -> Catalog:
        """품목·거래처 목록을 통째로 바꾼다 (CSV로 한꺼번에 정할 때). 목록에서 빠진 품목은 취급을 그만둔 것으로 남긴다."""
        ids, names = [i.id for i in catalog.all_items], [i.name for i in catalog.all_items]
        problems = [f"id {x} 중복" for x in sorted({x for x in ids if ids.count(x) > 1})]
        problems += [f"이름 {x} 중복" for x in sorted({x for x in names if names.count(x) > 1})]
        problems += [f"{i.name}: 단위 {i.unit}" for i in catalog.all_items if i.unit not in UNITS]
        problems += [f"{i.name}: 정가 판매 일수 {i.shelf_life}" for i in catalog.all_items if i.shelf_life < 1]
        problems += [f"{s.name}: 주문 방식 {s.channel}" for s in catalog.suppliers if s.channel not in CHANNELS]
        if problems:
            raise ValueError("목록을 확인해 주세요: " + ", ".join(problems[:10]))
        keep = set(ids)
        dropped = tuple(i for i in self.catalog.all_items if i.id not in keep)
        suppliers = {s.id for s in catalog.suppliers}
        # 남겨 두는 품목의 거래처가 새 목록에 없으면 같이 남겨 둔다 (지난 발주를 거래처별로 보여줄 때 쓴다)
        extra = tuple(
            s for s in self.catalog.suppliers if s.id not in suppliers and any(i.supplier_id == s.id for i in dropped)
        )
        merged = Catalog(catalog.items, (*catalog.suppliers, *extra), (*catalog.retired, *dropped))
        if not merged.items:
            raise ValueError("취급하는 품목이 하나는 있어야 합니다.")
        self._save_catalog(merged)
        return merged
