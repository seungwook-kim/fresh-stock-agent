"""명령줄 진입점.

    fresh-stock simulate --policy seasonal --days 90 --out out/
    fresh-stock compare --days 90
    fresh-stock compare --days 30 --policies seasonal,agent
    fresh-stock messages --date 2026-09-21 --llm
    fresh-stock eval-sheets --n 10
    fresh-stock serve --demo
"""

import argparse
import csv
import os
import socket
from datetime import date
from pathlib import Path

from .agent import AgentPolicy
from .catalog import load_catalog
from .llm import LLMClient, make_llm
from .market_calendar import MarketCalendar
from .messages import LLMGreetingWriter, build_messages
from .ordering import POLICIES, OrderPolicy
from .sheet_eval import build_cases, run_eval, summarize
from .sheets import synthetic_aliases
from .simulator import SimulationResult, Summary, run_simulation

POLICY_NAMES = [*POLICIES, AgentPolicy.name]


def _add_sim_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--start", type=date.fromisoformat, default=date(2026, 7, 1))
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--typo-rate", type=float, default=0.005, help="발주 입력 실수 확률 (줄마다)")
    p.add_argument("--no-guardrail", action="store_true", help="발주 수량 경고를 끈다")
    p.add_argument("--no-events", action="store_true", help="가게 메모(날씨·명절·단체 주문)를 끈다")
    p.add_argument(
        "--backend",
        choices=["claude-code", "anthropic"],
        default=os.environ.get("FRESH_STOCK_LLM", "claude-code"),
        help="LLM 백엔드: 로컬 Claude Code CLI(기본) 또는 Anthropic API. 환경변수 FRESH_STOCK_LLM으로도 정한다",
    )
    p.add_argument("--model", default=None, help="Claude 모델 (기본: claude-code는 sonnet, anthropic은 claude-opus-5)")
    p.add_argument("--effort", default="low", help="에이전트 추론 강도 (low, medium, high, xhigh, max)")
    p.add_argument("--llm-log", type=Path, default=Path("out/llm_calls.jsonl"), help="LLM 호출 기록 (JSONL)")


def _llm(args: argparse.Namespace) -> LLMClient:
    return make_llm(args.backend, args.model, args.effort, args.llm_log)


def _make_policy(name: str, args: argparse.Namespace) -> OrderPolicy:
    if name == AgentPolicy.name:
        return AgentPolicy(_llm(args))
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


def _print_messages(args: argparse.Namespace) -> None:
    args.days = (args.date - args.start).days + 2
    if args.days < 2:
        raise SystemExit("--date는 --start 이후여야 합니다.")
    result = _run(args, args.policy)
    todays = [o for o in result.orders if o.day == args.date]
    if not todays:
        print(f"{args.date}에는 발주가 없습니다 (다음 날 입고가 없는 날).")
        return
    arrival = todays[0].arrival
    notes = [n for d in MarketCalendar().cover_days(arrival) for n in result.notes.get(d, [])]
    writer = None
    if args.llm:
        llm = _llm(args)
        writer = LLMGreetingWriter(llm, args.store_name)
    messages = build_messages(
        result.catalog, arrival, {o.item_id: o.final for o in todays}, notes, writer
    )
    for m in messages:
        print(f"===== {m.supplier.name} ({m.supplier.channel}) · 인사말: {m.greeting_source} =====")
        print(m.text)
        print()


def _eval_sheets(args: argparse.Namespace) -> None:
    result = _run(args, "seasonal")
    cases = build_cases(result, args.n, args.seed, args.store_abbr)
    if args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        for case in cases:
            (args.out / f"{case.day}.jpg").write_bytes(case.image)
        print(f"재고표 이미지 {len(cases)}장 저장: {args.out.resolve()}")
        return
    llm = _llm(args)
    aliases = synthetic_aliases(result.catalog, args.store_abbr) if args.with_aliases else None
    scores = run_eval(llm, result.catalog, cases, args.out, args.workers, aliases)
    print(f"합성 재고표 인식 평가 ({args.backend}, 모델 {llm.model}, 추론 강도 {args.effort})\n")
    for label, value in summarize(scores).items():
        print(f"{label:<24}{value:.1%}" if isinstance(value, float) else f"{label:<24}{value}")
    calls = llm.calls
    if calls:
        print(
            f"\nLLM 호출 {len(calls)}회, 평균 {sum(c.seconds for c in calls) / len(calls):.1f}초, "
            f"입력 {sum(c.input_tokens for c in calls):,} / 출력 {sum(c.output_tokens for c in calls):,} 토큰"
        )
    print(f"장별 결과: {args.out.resolve()}")


def _serve(args: argparse.Namespace) -> None:
    import uvicorn

    from .demo import seed_demo
    from .service import StoreService
    from .store import Store
    from .web import create_app

    if args.port is None:
        port = _free_port(args.host, DEFAULT_PORT)
        if port is None:
            raise SystemExit(f"{DEFAULT_PORT}번부터 빈 포트를 찾지 못했습니다. --port로 지정해 주세요.")
    elif _port_in_use(args.host, args.port):
        raise SystemExit(f"{args.port}번 포트를 다른 프로그램이 쓰고 있습니다. --port를 빼면 빈 포트를 찾아 씁니다.")
    else:
        port = args.port

    store = Store(args.db)
    catalog, calendar = load_catalog(), MarketCalendar()
    if args.demo and store.last_closed_day() is None:
        first = seed_demo(store, catalog, calendar, args.start, args.days, args.seed)
        print(f"데모 기록 {args.days}일을 넣었습니다. {first} 마감 재고표부터 시작합니다.")

    def llm_factory() -> LLMClient:
        return _llm(args)

    app = create_app(StoreService(store, catalog, calendar, llm_factory, args.store_name))
    print(f"http://{args.host}:{port} 에서 열립니다.", flush=True)
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


DEFAULT_PORT = 8799


def _port_in_use(host: str, port: int) -> bool:
    """이미 쓰이는 포트인지 본다.

    Windows는 같은 포트를 다른 주소(0.0.0.0과 127.0.0.1)로 겹쳐 열 수 있어서, 열 수 있는지(bind)만 보면
    다른 서버와 겹칠 수 있다. 접속이 되는지도 함께 본다.
    """
    try:
        with socket.create_connection(("127.0.0.1" if host == "0.0.0.0" else host, port), timeout=0.3):
            return True
    except OSError:
        pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def _free_port(host: str, start: int, tries: int = 50) -> int | None:
    return next((p for p in range(start, start + tries) if not _port_in_use(host, p)), None)


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
            w.writerow(["발주일", "품목", "규칙 추천", "에이전트", "배율", "이유"])
            for dec in result.policy.decisions:
                w.writerow([dec.day, names[dec.item_id], dec.base, dec.qty, dec.factors, dec.reason])
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

    msg = sub.add_parser("messages", help="시뮬레이션의 특정 발주일 주문으로 거래처별 발주 문자를 만든다")
    msg.add_argument("--date", type=date.fromisoformat, required=True, help="발주일 (YYYY-MM-DD)")
    msg.add_argument("--policy", choices=POLICY_NAMES, default="seasonal")
    msg.add_argument("--store-name", default="우리가게", help="인사말에 쓸 가게 이름 (--llm일 때)")
    msg.add_argument("--llm", action="store_true", help="문자·전화 거래처에 Claude가 쓴 인사말을 덧붙인다")
    _add_sim_args(msg)

    ev = sub.add_parser("eval-sheets", help="합성 재고표 이미지로 사진 인식 정확도를 잰다")
    ev.add_argument("--n", type=int, default=10, help="재고표 장 수")
    ev.add_argument("--workers", type=int, default=3, help="동시에 읽을 장 수")
    ev.add_argument("--out", type=Path, default=Path("out/eval_sheets"))
    ev.add_argument("--dry-run", action="store_true", help="이미지만 만들고 인식은 하지 않는다")
    ev.add_argument("--with-aliases", action="store_true", help="줄임말을 이미 익힌 상태로 읽는다")
    ev.add_argument("--store-abbr", action="store_true", help="매장 고유 한 글자 약어(양=양배추 등)로 재고표를 쓴다")
    _add_sim_args(ev)

    srv = sub.add_parser("serve", help="모바일 웹 화면을 띄운다")
    srv.add_argument("--db", type=Path, default=Path("out/store.db"))
    srv.add_argument("--demo", action="store_true", help="빈 DB면 시뮬레이션 기록을 넣고 시작한다")
    srv.add_argument("--store-name", default="우리가게")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=None, help="지정하지 않으면 8799번부터 빈 포트를 찾아 쓴다")
    _add_sim_args(srv)
    srv.set_defaults(days=60, llm_log=Path("out/llm_web.jsonl"))

    args = parser.parse_args()
    if args.command == "serve":
        _serve(args)
    elif args.command == "eval-sheets":
        _eval_sheets(args)
    elif args.command == "messages":
        _print_messages(args)
    elif args.command == "simulate":
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
