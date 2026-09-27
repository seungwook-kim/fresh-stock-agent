"""발주 에이전트.

규칙(요일·장날 보정)이 낸 추천을 출발점으로, 가게 메모와 판매 기록을 보고 필요한 품목만 조정한다.
에이전트가 쓰는 도구는 판매 기록 조회(history)이고, 끝에 조정안을 제출(submit)한다.
숫자 계산은 코드가 하고, 에이전트는 무엇을 얼마나 바꿀지 판단하고 이유를 남긴다.
"""

from dataclasses import dataclass
from datetime import date
from statistics import mean

from .llm import LLMClient, LLMError
from .ordering import OrderContext, SeasonalPolicy

WEEKDAYS = "월화수목금토일"
HISTORY_DAYS = 14
MAX_HISTORY_ITEMS = 8

SYSTEM_PROMPT = """너는 과일·야채 매장의 발주 담당 보조다. 매일 마감 후 다음 입고분 발주 수량을 정한다.

- 기본 추천은 최근 같은 요일 정가 판매량, 장날 배율, 안전재고 20%로 계산한 값이다. 평소 숫자 패턴은 이미 반영돼 있다.
- 기본 추천이 모르는 것은 가게 메모다 (날씨, 명절, 단체 주문 등). 메모를 읽고 영향을 받는 품목만 조정한다.
- 판단에 판매 기록이 더 필요하면 action을 "history"로 하고 item_ids에 품목 id를 최대 8개 넣는다.
- 조정할 품목만 adjustments에 넣는다. qty는 최종 발주 수량(정수), reason은 사장님이 읽을 짧은 이유다.
- 메모가 없고 특별한 이유도 없으면 adjustments를 비워서 제출한다.
- 남은 물건은 떨이로 팔리지만 원가 이하로 팔게 되므로, 근거 없이 늘리지 않는다.
- summary는 이번 발주에 대해 사장님께 드리는 한두 문장 요약이다."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["history", "submit"]},
        "item_ids": {"type": "array", "items": {"type": "string"}},
        "adjustments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "item_id": {"type": "string"},
                    "qty": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["item_id", "qty", "reason"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["action", "item_ids", "adjustments", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AgentDecision:
    day: date
    item_id: str
    base: int  # 규칙 추천
    qty: int  # 에이전트가 정한 수량 (상한 적용 후)
    reason: str


def _fmt_day(d: date, ctx: OrderContext) -> str:
    market = "·장날" if ctx.calendar.is_market_day(d) else ""
    return f"{d:%m-%d}({WEEKDAYS[d.weekday()]}{market})"


def _cap(base: int) -> int:
    """에이전트가 올릴 수 있는 상한. 판단이 틀려도 피해가 한정되게 한다."""
    return max(base * 3, base + 10)


class AgentPolicy:
    name = "agent"
    label = "발주 에이전트"

    def __init__(self, llm: LLMClient, max_lookups: int = 2, skip_quiet_days: bool = True):
        self.llm = llm
        self.max_lookups = max_lookups
        # 메모가 없는 날은 규칙이 이미 평소 패턴을 반영하므로 LLM을 부르지 않는다 (비용·지연 절감)
        self.skip_quiet_days = skip_quiet_days
        self.base_policy = SeasonalPolicy()
        self.decisions: list[AgentDecision] = []
        self.summaries: list[tuple[date, str]] = []
        self.failures = 0

    def decide(self, ctx: OrderContext) -> dict[str, int]:
        base = self.base_policy.decide(ctx)
        if self.skip_quiet_days and not ctx.notes:
            return base
        lookups: list[str] = []
        while True:
            prompt = self._prompt(ctx, base, lookups)
            try:
                resp = self.llm.generate_json(SYSTEM_PROMPT, prompt, RESPONSE_SCHEMA, purpose=f"order {ctx.today}")
            except LLMError:
                # 에이전트가 실패해도 발주는 나가야 한다. 규칙 추천으로 대신한다
                self.failures += 1
                return base
            if resp["action"] == "history" and resp["item_ids"] and len(lookups) < self.max_lookups:
                lookups.append(self._history(ctx, resp["item_ids"][:MAX_HISTORY_ITEMS]))
                continue
            return self._apply(ctx, base, resp)

    def _apply(self, ctx: OrderContext, base: dict[str, int], resp: dict) -> dict[str, int]:
        orders = dict(base)
        for adj in resp["adjustments"]:
            item_id = adj["item_id"]
            if item_id not in base:
                continue
            qty = min(max(0, int(adj["qty"])), _cap(base[item_id]))
            orders[item_id] = qty
            self.decisions.append(AgentDecision(ctx.today, item_id, base[item_id], qty, adj["reason"]))
        self.summaries.append((ctx.today, resp["summary"]))
        return orders

    def _prompt(self, ctx: OrderContext, base: dict[str, int], lookups: list[str]) -> str:
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
        lines += ["", "품목별 기본 추천 (id | 이름 | 단위 | 현재 재고 | 최근 7일 하루 평균 정가 판매 | 기본 추천):"]
        for item in ctx.catalog.items:
            recent = [q for _, q in ctx.demand_history(item.id)[-7:]]
            avg = mean(recent) if recent else 0.0
            lines.append(
                f"{item.id} | {item.name} | {item.unit} | {ctx.stock(item.id):g} | {avg:.1f} | {base[item.id]}"
            )
        for n, result in enumerate(lookups, 1):
            lines += ["", f"[판매 기록 조회 {n}]", result]
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
