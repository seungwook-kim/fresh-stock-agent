"""발주 에이전트.

규칙(요일·장날 보정)의 수요 예측을 출발점으로, 가게 메모와 판매 기록을 보고 수요 배율을 정한다.
LLM은 "무엇이 평소의 몇 배 팔릴지"만 판단하고, 발주 수량은 코드가 계산한다.

배율의 근거는 두 가지다.
- 매장 경험 노트: 사장님이 말로 알려준 경험 ("명절엔 2~3배")
- 과거 비슷한 날 (RAG): 오늘 메모와 비슷한 메모가 있던 날의 실제 결과를 기록에서 검색 (memory.py)

도구: 판매 기록 조회(history), 과거 기록 검색(search), 배율 조정안 제출(submit).
"""

import math
import re
from dataclasses import dataclass
from datetime import date
from importlib import resources
from statistics import mean

from .domain import Category, Item
from .llm import LLMClient, LLMError
from .memory import DayMemoryBank
from .ordering import OrderContext, SeasonalPolicy

WEEKDAYS = "월화수목금토일"
HISTORY_DAYS = 14
MAX_HISTORY_ITEMS = 8
ALL = "전체"
FACTOR_RANGE = (0.2, 4.0)  # 판단이 틀려도 피해가 한정되게 배율을 이 범위로 자른다

STORE_KNOWLEDGE = resources.files("fresh_stock").joinpath("data", "store_knowledge.md").read_text(encoding="utf-8")


def build_system_prompt(use_knowledge: bool = True, use_memory: bool = False) -> str:
    sources = []
    if use_memory:
        sources.append("과거 비슷한 날의 실제 결과 (기록에서 검색, 프롬프트의 [과거 비슷한 날])")
    if use_knowledge:
        sources.append("아래 매장 경험 노트")
    basis = " 와 ".join(sources) if sources else "메모 내용에 대한 너의 상식"
    lines = [
        "너는 과일·야채 매장의 발주 담당 보조다. 매일 마감 후 다음 입고분 발주를 돕는다.",
        "",
        "- 기본 추천은 최근 같은 요일 정가 판매량과 장날 배율로 수요를 예측하고, 안전재고 20%를 더하고, "
        "남은 재고를 뺀 값이다. 평소 숫자 패턴은 이미 반영돼 있다.",
        "- 기본 추천이 모르는 것은 가게 메모다 (날씨, 명절, TV 방송 등). 메모를 읽고 수요가 평소의 몇 배가 될지 판단한다.",
        f"- 배율의 근거는 {basis}다.",
    ]
    if use_memory:
        lines += [
            "- 과거 기록은 실제로 일어난 일이다. 비슷한 날의 분류별·품목별 배율을 우선 참고하고, "
            "메모가 딱 맞지 않으면 차이를 감안한다 (예: 다른 품목의 TV 방송이면 그 품목에 같은 배율을 적용).",
            '- 다른 표현으로 더 찾아보려면 action을 "search"로 하고 query에 검색어를 넣는다.',
        ]
    lines += [
        '- 판단에 판매 기록이 더 필요하면 action을 "history"로 하고 item_ids에 품목 id를 최대 8개 넣는다.',
        "- 조회와 검색은 합쳐서 두 번까지 한다.",
        '- adjustments에는 수요 배율만 넣는다. 발주 수량은 시스템이 "예측 수요 × 배율 × 1.2 - 재고"로 계산한다.',
        '  - target: 품목 id, 분류("과일", "야채", "기타"), 또는 "전체"',
        '  - day: 배율을 적용할 날 ("MM-DD") 또는 "전체" (버틸 날 모두)',
        "  - factor: 평소 대비 수요 배율 (예: 1.0이면 변화 없음)",
        "  - reason: 사장님이 읽을 짧은 이유 (과거 기록을 근거로 했으면 그 날짜를 적는다)",
        "  - 한 품목에 여러 조정이 겹치면 가장 구체적인 것 하나만 쓴다 (품목 > 분류 > 전체, 특정 날 > 전체 날)",
        "- 메모가 수요에 영향을 주지 않는다고 보면 adjustments를 비워서 제출한다.",
        "- 남은 물건은 떨이로 원가 이하에 팔게 되므로, 근거 없이 배율을 올리지 않는다.",
        "- 쓰지 않는 필드(item_ids, query)는 비워 둔다.",
        "- summary는 이번 발주에 대해 사장님께 드리는 한두 문장 요약이다.",
    ]
    if use_knowledge:
        lines += ["", "아래는 사장님이 직접 알려준 매장 경험이다.", "", STORE_KNOWLEDGE]
    return "\n".join(lines)


RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["history", "search", "submit"]},
        "item_ids": {"type": "array", "items": {"type": "string"}},
        "query": {"type": "string"},
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "target": {"type": "string"},
                    "day": {"type": "string"},
                    "factor": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["target", "day", "factor", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["action", "item_ids", "query", "adjustments", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AgentDecision:
    day: date
    item_id: str
    base: int  # 규칙 추천
    qty: int  # 배율을 반영해 코드가 계산한 수량
    factors: str  # 적용한 배율 (날짜별)
    reason: str


def _fmt_day(d: date, ctx: OrderContext) -> str:
    market = "·장날" if ctx.calendar.is_market_day(d) else ""
    return f"{d:%m-%d}({WEEKDAYS[d.weekday()]}{market})"


def _specificity(adj: dict, item: Item) -> int | None:
    """이 조정이 품목에 적용되면 구체성 점수(클수록 우선), 아니면 None."""
    if adj["target"] == item.id:
        score = 4
    elif adj["target"] == item.category:
        score = 2
    elif adj["target"] == ALL:
        score = 0
    else:
        return None
    return score + (0 if adj["day"] == ALL else 1)


class AgentPolicy:
    name = "agent"

    def __init__(
        self,
        llm: LLMClient,
        memory: DayMemoryBank | None = None,
        use_knowledge: bool = True,
        max_lookups: int = 2,
        skip_quiet_days: bool = True,
    ):
        self.llm = llm
        self.memory = memory
        self.use_knowledge = use_knowledge
        self.max_lookups = max_lookups
        # 메모가 없는 날은 규칙이 이미 평소 패턴을 반영하므로 LLM을 부르지 않는다 (비용·지연 절감)
        self.skip_quiet_days = skip_quiet_days
        self.system_prompt = build_system_prompt(use_knowledge, memory is not None)
        self.label = "발주 에이전트" + (" + 과거 기록 검색" if memory else "") + ("" if use_knowledge else " (경험 노트 없음)")
        self.base_policy = SeasonalPolicy()
        self.decisions: list[AgentDecision] = []
        self.summaries: list[tuple[date, str]] = []
        self.failures = 0

    def decide(self, ctx: OrderContext) -> dict[str, int]:
        base = self.base_policy.decide(ctx)
        if self.skip_quiet_days and not ctx.notes:
            return base
        recalled = self._recall(ctx)
        lookups: list[str] = []
        while True:
            prompt = self._prompt(ctx, base, lookups, recalled)
            try:
                resp = self.llm.generate_json(self.system_prompt, prompt, RESPONSE_SCHEMA, purpose=f"order {ctx.today}")
            except LLMError:
                # 에이전트가 실패해도 발주는 나가야 한다. 규칙 추천으로 대신한다
                self.failures += 1
                return base
            if len(lookups) < self.max_lookups:
                if resp["action"] == "history" and resp["item_ids"]:
                    lookups.append(self._history(ctx, resp["item_ids"][:MAX_HISTORY_ITEMS]))
                    continue
                if resp["action"] == "search" and resp["query"].strip():
                    lookups.append(self._search(ctx, resp["query"].strip()))
                    continue
            return self._apply(ctx, base, resp)

    def _recall(self, ctx: OrderContext) -> str | None:
        """오늘 메모로 과거 비슷한 날을 자동으로 검색한다."""
        if self.memory is None:
            return None
        query = " / ".join(n for _, notes in sorted(ctx.notes.items()) for n in notes)
        return self.memory.describe(self.memory.search(ctx, query))

    def _search(self, ctx: OrderContext, query: str) -> str:
        """에이전트의 검색 도구: 검색어로 과거 기록을 찾는다."""
        if self.memory is None:
            return "과거 기록 검색을 쓸 수 없다."
        return f"검색어: {query}\n" + self.memory.describe(self.memory.search(ctx, query))

    def _factor(self, adjustments: list[dict], item: Item, day: date) -> tuple[float, str | None]:
        """품목·날짜에 적용할 배율과 그 이유. 가장 구체적인 조정 하나만 쓴다."""
        day_key = f"{day:%m-%d}"
        best, best_score = None, -1
        for adj in adjustments:
            if adj["day"] not in (ALL, day_key):
                continue
            score = _specificity(adj, item)
            if score is not None and score > best_score:
                best, best_score = adj, score
        if best is None:
            return 1.0, None
        lo, hi = FACTOR_RANGE
        return min(max(float(best["factor"]), lo), hi), best["reason"]

    def _apply(self, ctx: OrderContext, base: dict[str, int], resp: dict) -> dict[str, int]:
        orders = dict(base)
        policy = self.base_policy
        for item in ctx.catalog.items:
            need, reasons, factors = 0.0, [], []
            for d in ctx.cover_days:
                factor, reason = self._factor(resp["adjustments"], item, d)
                need += policy.forecast(ctx, item, d) * factor
                factors.append(f"{d:%m-%d} ×{factor:g}")
                if reason and reason not in reasons:
                    reasons.append(reason)
            if not reasons:
                continue
            qty = max(0, math.ceil(need * policy.safety_factor - ctx.stock(item.id) - 1e-9))
            orders[item.id] = qty
            self.decisions.append(
                AgentDecision(ctx.today, item.id, base[item.id], qty, ", ".join(factors), " / ".join(reasons))
            )
        # 모델이 요약 끝에 태그 조각(</summary> 등)을 붙이는 경우가 있어 지운다
        self.summaries.append((ctx.today, re.sub(r"\s*</?[a-z_]+>\s*$", "", resp["summary"].strip())))
        return orders

    def _prompt(self, ctx: OrderContext, base: dict[str, int], lookups: list[str], recalled: str | None) -> str:
        lines = [
            f"발주일: {_fmt_day(ctx.today, ctx)}  입고일: {_fmt_day(ctx.arrival, ctx)}",
            "이번 입고분으로 버틸 날: " + ", ".join(_fmt_day(d, ctx) for d in ctx.cover_days),
            "",
            "가게 메모:",
        ]
        if ctx.notes:
            for d, notes in sorted(ctx.notes.items()):
                lines += [f"- {_fmt_day(d, ctx)}: {n}" for n in notes]
        else:
            lines.append("- 없음")
        categories = ", ".join(c.value for c in Category)
        lines += [
            "",
            f"품목별 기본 추천 (id | 이름 | 분류({categories}) | 단위 | 현재 재고 | 최근 7일 하루 평균 정가 판매 | 기본 추천):",
        ]
        for item in ctx.catalog.items:
            recent = [q for _, q in ctx.demand_history(item.id)[-7:]]
            avg = mean(recent) if recent else 0.0
            lines.append(
                f"{item.id} | {item.name} | {item.category} | {item.unit} | {ctx.stock(item.id):g} | "
                f"{avg:.1f} | {base[item.id]}"
            )
        if recalled is not None:
            lines += ["", "[과거 비슷한 날] 오늘 메모로 기록에서 찾은 날과 그날 실제 결과:", recalled]
        for n, result in enumerate(lookups, 1):
            lines += ["", f"[조회·검색 {n}]", result]
        if len(lookups) >= self.max_lookups:
            lines += ["", "조회는 더 할 수 없다. 이제 submit으로 제출한다."]
        return "\n".join(lines)

    def _history(self, ctx: OrderContext, item_ids: list[str]) -> str:
        """에이전트의 조회 도구: 품목별 최근 정가 판매 기록."""
        known = {i.id for i in ctx.catalog.items}
        closing = {rec.day: rec.closing for rec in ctx.ledger}
        lines = []
        for item_id in item_ids:
            if item_id not in known:
                lines.append(f"{item_id}: 없는 품목")
                continue
            item = ctx.catalog.item(item_id)
            days = ctx.demand_history(item_id)[-HISTORY_DAYS:]
            cells = [
                f"{_fmt_day(d, ctx)} {q:.1f}{'*' if closing[d].get(item_id, 0) == 0 else ''}" for d, q in days
            ]
            lines.append(f"{item.name}({item.unit}): " + ", ".join(cells))
        lines.append("(* 표시는 마감 때 다 팔린 날이다. 실제 수요가 더 컸을 것으로 보고 1.3배로 올려 잡은 값이다)")
        return "\n".join(lines)
