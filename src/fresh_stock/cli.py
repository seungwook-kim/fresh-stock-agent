"""명령줄 진입점.

    fresh-stock simulate --policy seasonal --days 90 --out out/
    fresh-stock compare --days 90
    fresh-stock compare --days 30 --policies seasonal,agent
"""

import argparse
import csv
from datetime import date
from pathlib import Path

from .agent import AgentPolicy
from .catalog import load_catalog
from .llm import ClaudeCodeClient
from .market_calendar import MarketCalendar
from .ordering import POLICIES, OrderPolicy
from .simulator import SimulationResult, Summary, run_simulation

POLICY_NAMES = [*POLICIES, AgentPolicy.name]


def _add_sim_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 7, 1))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--typo-rate", type=float, default=0.005, help="발주 입력 실수 확률 (줄마다)")
    p.add_argument("--no-guardrail", action="store_true", help="발주 수량 경고를 끈다")
    p.add_argument("--no-events", action="store_true", help="가게 메모(날씨·명절·단체 주문)를 끈다")
    p.add_argument("--model", default="sonnet", help="에이전트가 쓸 Claude 모델 (Claude Code 모델 이름)")
    p.add_argument("--effort", default="low", help="에이전트 추론 강도 (low, medium, high, xhigh, max)")
    p.add_argument("--llm-log", type=Path, default=Path("out/llm_calls.jsonl"), help="LLM 호출 기록 (JSONL)")


def _make_policy(name: str, args: argparse.Namespace) -> OrderPolicy:
    if name == AgentPolicy.name:
        return AgentPolicy(ClaudeCodeClient(model=args.model, effort=args.effort, log_path=args.llm_log))
    return POLICIES[name]()


def _run(args: argparse.Namespace, policy_name: str) -> SimulationResult:
    return run_simulation(
        catalog=load_catalog(),
        calendar=MarketCalendar(),
        policy=_make_policy(policy_name, args),
        start=args.start,
        days=args.days,
        seed=args.seed,
        typo_rate=args.typo_rate,
        guardrail=not args.no_guardrail,
        with_events=not args.no_events,
    )


def _won(v: int) -> str:
    return f"{v:,}원"


def _print_summaries(summaries: list[Summary]) -> None:
    rows = [
        ("매출", lambda s: _won(s.revenue)),
        ("이익 (매출 - 매입 + 남은 재고)", lambda s: _won(s.profit)),
        ("떨이 물량 (원가)", lambda s: _won(s.clearance_cost)),
        ("떨이 비중 (매입 대비)", lambda s: f"{s.clearance_rate:.1%}"),
        ("떨이 할인액", lambda s: _won(s.markdown_loss)),
        ("품절로 놓친 매출", lambda s: _won(s.lost_sales)),
        ("입력 실수 / 경고로 막음", lambda s: f"{s.typos} / {s.typos_caught}"),
        ("오경보", lambda s: str(s.false_alarms)),
    ]
    width = max(len(s.policy) for s in summaries) + 4
    print(f"{summaries[0].days}일 시뮬레이션\n")
    print(" " * 32 + "".join(s.policy.ljust(width) for s in summaries))
    for label, fmt in rows:
        print(label.ljust(32) + "".join(fmt(s).ljust(width) for s in summaries))


def _print_agent_stats(results: list[SimulationResult]) -> None:
    for r in results:
        if not isinstance(r.policy, AgentPolicy) or not r.policy.llm.calls:
            continue
        calls = r.policy.llm.calls
        print(
            f"\n에이전트: LLM 호출 {len(calls)}회, 평균 {sum(c.seconds for c in calls) / len(calls):.1f}초, "
            f"입력 {sum(c.input_tokens for c in calls):,} / 출력 {sum(c.output_tokens for c in calls):,} 토큰, "
            f"실패 후 규칙으로 대체 {r.policy.failures}회, 조정 {len(r.policy.decisions)}건"
        )


def _write_csv(result: SimulationResult, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    names = {i.id: i.name for i in result.catalog.items}
    with open(out / "ledger.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["날짜", "품목", "입고", "마감재고"])
        for rec in result.ledger:
            for item in result.catalog.items:
                w.writerow([rec.day, item.name, rec.received.get(item.id, 0), rec.closing[item.id]])
    with open(out / "payments.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["날짜", "현금", "카드", "합계"])
        for rec in result.ledger:
            w.writerow([rec.day, rec.cash, rec.card, rec.revenue])
    with open(out / "orders.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["발주일", "입고일", "품목", "추천", "입력", "최종", "입력실수", "경고"])
        for o in result.orders:
            w.writerow([o.day, o.arrival, names[o.item_id], o.proposed, o.entered, o.final, o.typo, o.warning or ""])
    with open(out / "truth.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["날짜", "품목", "정가수요", "정가판매", "떨이판매", "품절"])
        for t in result.truths:
            for item in result.catalog.items:
                i = item.id
                w.writerow([t.day, item.name, t.demand[i], t.sold[i], t.cleared[i], t.lost[i]])
    if isinstance(result.policy, AgentPolicy):
        with open(out / "agent_decisions.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["발주일", "품목", "규칙 추천", "에이전트", "이유"])
            for dec in result.policy.decisions:
                w.writerow([dec.day, names[dec.item_id], dec.base, dec.qty, dec.reason])
        with open(out / "agent_summaries.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["발주일", "요약"])
            w.writerows(result.policy.summaries)


def main() -> None:
    parser = argparse.ArgumentParser(prog="fresh-stock")
    sub = parser.add_subparsers(dest="command", required=True)

    sim = sub.add_parser("simulate", help="발주 방식 하나로 가상 매장을 돌리고 기록을 CSV로 남긴다")
    sim.add_argument("--policy", choices=POLICY_NAMES, default="yesterday")
    sim.add_argument("--out", type=Path, default=Path("out"))
    _add_sim_args(sim)

    cmp_ = sub.add_parser("compare", help="같은 수요로 발주 방식들을 비교한다")
    cmp_.add_argument("--policies", default=",".join(POLICIES), help=f"쉼표로 구분 ({', '.join(POLICY_NAMES)})")
    _add_sim_args(cmp_)

    args = parser.parse_args()
    if args.command == "simulate":
        result = _run(args, args.policy)
        _write_csv(result, args.out)
        _print_summaries([result.summary()])
        _print_agent_stats([result])
        print(f"\n기록 저장: {args.out.resolve()}")
    else:
        names = [n.strip() for n in args.policies.split(",")]
        unknown = [n for n in names if n not in POLICY_NAMES]
        if unknown:
            parser.error(f"알 수 없는 발주 방식: {', '.join(unknown)}")
        results = [_run(args, name) for name in names]
        _print_summaries([r.summary() for r in results])
        _print_agent_stats(results)
