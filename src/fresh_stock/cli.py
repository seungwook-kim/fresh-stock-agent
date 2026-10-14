"""명령줄 진입점.

    fresh-stock simulate --policy seasonal --days 90 --out out/
    fresh-stock compare --days 90
    fresh-stock compare --days 30 --policies seasonal,agent
    fresh-stock messages --date 2026-09-21 --llm
    fresh-stock eval-sheets --n 10
    fresh-stock serve --demo
    fresh-stock export-catalog --store 1 --out out/catalog
    fresh-stock import-catalog --store 1 items.csv suppliers.csv
"""

import argparse
import csv
import os
import socket
from datetime import date, timedelta
from pathlib import Path

from .agent import AgentPolicy
from .catalog import Catalog, load_catalog, sample_store_catalog
from .llm import LLMClient, make_llm
from .market_calendar import MarketCalendar, today_kst
from .messages import LLMGreetingWriter, build_messages
from .memory import FastEmbedEmbedder, evaluate_retrieval
from .runtime import memory_bank, service_factory, weather_source
from .ordering import POLICIES, OrderPolicy, OwnerPolicy, SwitchPolicy
from .sheet_eval import ReplayLLM, build_cases, build_writer_cases, run_eval, summarize
from .sheets import WRITERS
from .sheets import synthetic_aliases
from .simulator import SimulationResult, Summary, run_simulation

AGENT_VARIANTS = {
    # 이름: (과거 기록 검색 사용, 경험 노트 사용)
    "agent": (False, True),
    "agent-rag": (True, True),
    "agent-rag-only": (True, False),
}
POLICY_NAMES = [*POLICIES, OwnerPolicy.name, *AGENT_VARIANTS]
def load_dotenv(path: Path = Path(".env")) -> None:
    """프로젝트 폴더의 .env에서 KEY=VALUE를 읽어 환경변수로 둔다. 이미 있는 환경변수가 우선한다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:  # 값을 비워 둔 줄은 설정하지 않은 것으로 본다
            os.environ.setdefault(key.strip(), value)


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
    p.add_argument(
        "--switch",
        type=date.fromisoformat,
        default=None,
        help="이 날짜 전까지는 모든 방식이 사장님 감(owner)으로 발주해 같은 과거 기록을 쌓는다",
    )


def _llm(args: argparse.Namespace) -> LLMClient:
    return make_llm(args.backend, args.model, args.effort, args.llm_log)


def _make_policy(name: str, args: argparse.Namespace) -> OrderPolicy:
    if name in AGENT_VARIANTS:
        use_memory, use_knowledge = AGENT_VARIANTS[name]
        policy: OrderPolicy = AgentPolicy(_llm(args), memory=memory_bank() if use_memory else None, use_knowledge=use_knowledge)
    elif name == OwnerPolicy.name:
        policy = OwnerPolicy()
    else:
        policy = POLICIES[name]()
    if args.switch is not None:
        policy = SwitchPolicy(OwnerPolicy(), policy, args.switch)
    return policy


def _agent(policy: OrderPolicy) -> AgentPolicy | None:
    inner = policy.after if isinstance(policy, SwitchPolicy) else policy
    return inner if isinstance(inner, AgentPolicy) else None


def _run(args: argparse.Namespace, policy_name: str, catalog: Catalog | None = None) -> SimulationResult:
    return run_simulation(
        catalog=catalog or load_catalog(),
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
    if args.writers:
        _eval_writers(args)
        return
    catalog = sample_store_catalog() if args.layout in ("store", "excel") else None
    result = _run(args, "seasonal", catalog)
    cases = build_cases(result, args.n, args.seed, args.store_abbr, args.layout)
    if args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)
        for case in cases:
            (args.out / f"{case.day}.jpg").write_bytes(case.image)
        print(f"재고표 이미지 {len(cases)}장 저장: {args.out.resolve()}")
        return
    llm = ReplayLLM(args.replay) if args.replay else _llm(args)
    aliases = synthetic_aliases(result.catalog, args.store_abbr) if args.with_aliases else None
    scores = run_eval(llm, result.catalog, cases, args.out, args.workers, aliases, args.read_purchases)
    reader = "사입 칸까지 읽기" if args.read_purchases else "품목·남은 수량만 읽기"
    source = f"{args.replay.name}의 응답으로 다시 채점" if args.replay else f"{args.backend}, 모델 {llm.model}, 추론 강도 {args.effort}"
    print(f"합성 재고표 인식 평가 ({args.layout} 형식, {reader}, {source})\n")
    for label, value in summarize(scores).items():
        print(f"{label:<24}{value:.1%}" if isinstance(value, float) else f"{label:<24}{value}")
    calls = llm.calls
    if calls:
        print(
            f"\nLLM 호출 {len(calls)}회, 평균 {sum(c.seconds for c in calls) / len(calls):.1f}초, "
            f"입력 {sum(c.input_tokens for c in calls):,} / 출력 {sum(c.output_tokens for c in calls):,} 토큰"
        )
    print(f"장별 결과: {args.out.resolve()}")


def _eval_writers(args: argparse.Namespace) -> None:
    """손글씨가 다른 10명이 엑셀 양식 재고표를 적었을 때의 인식 결과를 사람별로 낸다 (사입 칸까지 읽기)."""
    result = _run(args, "seasonal", sample_store_catalog())
    pairs = build_writer_cases(result, args.n, args.seed)
    if args.dry_run:
        for writer, case in pairs:
            folder = args.out / writer.name.replace(" ", "_")
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{case.day}.jpg").write_bytes(case.image)
        print(f"재고표 이미지 {len(pairs)}장 저장: {args.out.resolve()}")
        return
    llm = ReplayLLM(args.replay) if args.replay else _llm(args)
    scores = run_eval(llm, result.catalog, [c for _, c in pairs], args.out, args.workers, purchases=True)
    source = f"{args.replay.name}의 응답으로 다시 채점" if args.replay else f"{args.backend}, 모델 {llm.model}, 추론 강도 {args.effort}"
    print(f"손글씨 {len(WRITERS)}명 × 재고표 {args.n}장 (엑셀 양식, 사입 칸까지 읽기, {source})\n")
    head = f"{'쓰는 사람':<10}{'글씨체':<22}{'줄':>5}{'정확도':>9}{'표시 없이 틀림':>12}{'사입 칸':>9}{'맞았는데 표시':>12}"
    print(head)
    for writer in WRITERS:
        mine = [s for (w, _), s in zip(pairs, scores) if w is writer]
        m = summarize(mine)
        print(f"{writer.name:<10}{writer.font.removesuffix('-Regular.ttf'):<22}{m['적힌 줄']:>5}{m['줄 정확도']:>9.1%}"
              f"{m['표시 없이 틀린 줄'] + m.get('사입 칸 표시 없이 틀린 줄', 0):>12}{m.get('사입 칸 정확도', 0):>9.1%}"
              f"{m['맞았는데 표시된 줄 비율']:>12.1%}")  # fmt: skip
    print()
    for label, value in summarize(scores).items():
        print(f"{label:<24}{value:.1%}" if isinstance(value, float) else f"{label:<24}{value}")
    calls = llm.calls
    if calls:
        print(f"\nLLM 호출 {len(calls)}회, 평균 {sum(c.seconds for c in calls) / len(calls):.1f}초")
    print(f"장별 결과: {args.out.resolve()}")


def _serve(args: argparse.Namespace) -> None:
    import secrets

    import uvicorn

    from .demo import seed_demo
    from .store import Database, Store, mask_url
    from .web import AuthConfig, create_app

    if args.port is None:
        port = _free_port(args.host, DEFAULT_PORT)
        if port is None:
            raise SystemExit(f"{DEFAULT_PORT}번부터 빈 포트를 찾지 못했습니다. --port로 지정해 주세요.")
    elif _port_in_use(args.host, args.port):
        raise SystemExit(f"{args.port}번 포트를 다른 프로그램이 쓰고 있습니다. --port를 빼면 빈 포트를 찾아 씁니다.")
    else:
        port = args.port

    db = Database(args.db)
    print(f"DB: {mask_url(args.db)}")
    catalog, calendar = load_catalog(), MarketCalendar()
    demo_store_id = None
    if args.demo:
        ids = db.store_ids()
        if ids:
            demo_store_id = ids[0]
        else:
            demo_store_id = db.create_store("데모 가게", os.environ.get("FRESH_STOCK_DEMO_OWNER", "demo@local"))
            # 기록이 오늘 전날에 끝나도록 넣어 오늘 마감 재고표부터 시작한다
            start = args.start or today_kst() - timedelta(days=args.days)
            first = seed_demo(Store(db, demo_store_id), catalog, calendar, start, args.days, args.seed)
            print(f"데모 가게에 기록 {args.days}일을 넣었습니다. {first} 마감 재고표부터 시작합니다.")

    log_dir = args.llm_log
    make_service = service_factory(
        db,
        backend=args.backend,
        model=args.model,
        effort=args.effort,
        log_dir=log_dir,
        rag=not args.no_rag,
        auto_notes=not args.no_auto_notes,
    )

    secret = os.environ.get("FRESH_STOCK_SECRET")
    if not secret:
        secret = secrets.token_urlsafe(32)
        print("FRESH_STOCK_SECRET이 없어 임시 세션 키를 씁니다. 서버를 다시 켜면 다시 로그인해야 합니다.")
    auth = AuthConfig(
        secret_key=secret,
        google_client_id=os.environ.get("GOOGLE_CLIENT_ID"),
        google_client_secret=os.environ.get("GOOGLE_CLIENT_SECRET"),
        dev_login=args.dev_login or os.environ.get("FRESH_STOCK_DEV_LOGIN") == "1",
        demo_store_id=demo_store_id,
        secure_cookies=os.environ.get("FRESH_STOCK_SECURE_COOKIES") == "1",
    )
    print(f"로그인: 구글 {'켜짐' if auth.google_enabled else '꺼짐 (GOOGLE_CLIENT_ID/SECRET 없음)'}"
          f", 개발용 로그인 {'켜짐' if auth.dev_login else '꺼짐'}")
    app = create_app(db, make_service, auth, log_dir)
    print(f"http://{args.host}:{port} 에서 열립니다.", flush=True)
    uvicorn.run(app, host=args.host, port=port, log_level="warning")


DEFAULT_PORT = 8799


def _fetch_notes(args: argparse.Namespace) -> None:
    from .service import StoreService
    from .store import Database, Store

    db = Database(args.db)
    weather = weather_source()
    day = args.day or today_kst()
    for store_id in db.store_ids():
        service = StoreService(Store(db, store_id), load_catalog(), MarketCalendar(), weather=weather)
        added, error = service.refresh_auto_notes(day, args.days)
        print(f"[{db.store_name(store_id)}] {day} 다음 날부터 {args.days}일: 메모 {len(added)}개 추가")
        for n in added:
            print(f"  {n.day} [{n.source}] {n.text}")
        if error:
            print(f"  {error}")


def _mcp(args: argparse.Namespace) -> None:
    from .mcp_server import build_server
    from .store import Database

    email = args.email or os.environ.get("FRESH_STOCK_EMAIL")
    if not email:
        raise SystemExit("FRESH_STOCK_EMAIL(또는 --email)에 가게 멤버인 구글 계정 이메일을 넣어 주세요.")
    db = Database(args.db)
    make_service = service_factory(
        db,
        backend=args.backend,
        model=args.model,
        effort=args.effort,
        log_dir=args.llm_log,
        rag=not args.no_rag,
        auto_notes=not args.no_auto_notes,
    )
    # stdio 방식이라 표준 출력에는 아무것도 찍지 않는다 (MCP 메시지와 섞이면 연결이 깨진다)
    build_server(db, make_service, email).run("stdio")


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


def _eval_retrieval(args: argparse.Namespace) -> None:
    embedder = None if args.bm25_only else FastEmbedEmbedder()
    print("가게 메모 검색 평가 (data/retrieval_eval.json)\n")
    for name, (top1, hit3, n, misses) in evaluate_retrieval(embedder).items():
        print(f"{name:<32} 1위 적중 {top1:>2}/{n}   3위 안 적중 {hit3:>2}/{n}")
        for miss in misses:
            print(f"    1위 오답: {miss}")


def _copy_db(args: argparse.Namespace) -> None:
    from .store import Database, mask_url

    source, target = Database(args.source), Database(args.target)
    counts = target.load(source.dump())
    print(f"{mask_url(args.source)} -> {mask_url(args.target)}")
    for table, n in counts.items():
        print(f"  {table:<16} {n:>6}행")
    source.close()
    target.close()


def _catalog_service(db_url: str, store_id: int):
    from .service import StoreService
    from .store import Database, Store

    db = Database(db_url)
    if db.store_name(store_id) is None:
        ids = ", ".join(f"{i}: {db.store_name(i)}" for i in db.store_ids()) or "없음"
        raise SystemExit(f"가게 {store_id}이(가) 없습니다. 가게 목록: {ids}")
    return StoreService(Store(db, store_id), load_catalog(), MarketCalendar(), store_name=db.store_name(store_id))


def _export_catalog(args: argparse.Namespace) -> None:
    from .catalog import catalog_csv

    service = _catalog_service(args.db, args.store)
    items, suppliers = catalog_csv(service.catalog)
    args.out.mkdir(parents=True, exist_ok=True)
    # 엑셀에서 바로 열리도록 BOM을 붙인다
    (args.out / "items.csv").write_text(items, encoding="utf-8-sig")
    (args.out / "suppliers.csv").write_text(suppliers, encoding="utf-8-sig")
    print(f"품목 {len(service.catalog.items)}개(취급 그만둔 품목 {len(service.catalog.retired)}개 별도), "
          f"거래처 {len(service.catalog.suppliers)}곳 -> {args.out.resolve()}")


def _import_catalog(args: argparse.Namespace) -> None:
    service = _catalog_service(args.db, args.store)
    try:
        merged = service.import_catalog(load_catalog(args.items, args.suppliers))
    except (ValueError, KeyError) as e:
        raise SystemExit(f"목록을 바꾸지 않았습니다: {e}") from e
    print(f"[{service.store_name}] 품목 {len(merged.items)}개, 거래처 {len(merged.suppliers)}곳으로 바꿨습니다."
          f" 취급을 그만둔 품목 {len(merged.retired)}개는 지난 기록용으로 남깁니다.")


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
        agent = _agent(r.policy)
        if agent is None or not agent.llm.calls:
            continue
        calls = agent.llm.calls
        print(
            f"\n{agent.label}: LLM 호출 {len(calls)}회, 평균 {sum(c.seconds for c in calls) / len(calls):.1f}초, "
            f"입력 {sum(c.input_tokens for c in calls):,} / 출력 {sum(c.output_tokens for c in calls):,} 토큰, "
            f"실패 후 규칙으로 대체 {agent.failures}회, 조정 {len(agent.decisions)}건"
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
    agent = _agent(result.policy)
    if agent is not None:
        with open(out / "agent_decisions.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["발주일", "품목", "규칙 추천", "에이전트", "배율", "이유"])
            for dec in agent.decisions:
                w.writerow([dec.day, names[dec.item_id], dec.base, dec.qty, dec.factors, dec.reason])
        with open(out / "agent_summaries.csv", "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["발주일", "요약"])
            w.writerows(agent.summaries)


def main() -> None:
    load_dotenv()
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
    ev.add_argument(
        "--layout", choices=["simple", "store", "excel"], default="simple",
        help="simple: 품목·수량 세 단 / store: 실제 가게 형식 (품목·남은 수량·사입 수량·사입 가격 × 2, 30줄, 예시 가게 60품목)"
        " / excel: store 칸을 엑셀로 인쇄하고 숫자만 손으로 적어 휴대폰으로 찍은 사진",
    )  # fmt: skip
    ev.add_argument("--read-purchases", action="store_true", help="사입 수량·가격 칸이 있는 형식으로 알려주고 함께 읽는다")
    ev.add_argument("--replay", type=Path, default=None, help="LLM을 부르지 않고 이 호출 기록(JSONL)의 응답으로 다시 채점한다")
    ev.add_argument("--writers", action="store_true", help="손글씨가 다른 10명이 각자 n장씩 적은 엑셀 양식 재고표로 평가한다")
    _add_sim_args(ev)

    srv = sub.add_parser("serve", help="모바일 웹 화면을 띄운다")
    srv.add_argument(
        "--db",
        default=os.environ.get("FRESH_STOCK_DB", "out/store.db"),
        help="SQLite 파일 경로 또는 postgresql:// 주소 (환경변수 FRESH_STOCK_DB, .env로도 정한다)",
    )
    srv.add_argument("--demo", action="store_true", help="빈 DB면 시뮬레이션 기록을 넣고 시작한다")
    srv.add_argument("--dev-login", action="store_true", help="개발용 로그인(이메일만 입력)을 켠다. PC에서 확인할 때만")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=None, help="지정하지 않으면 8799번부터 빈 포트를 찾아 쓴다")
    srv.add_argument("--no-rag", action="store_true", help="과거 비슷한 날 검색을 끈다 (임베딩 모델을 받지 않는다)")
    srv.add_argument("--no-auto-notes", action="store_true", help="발주 추천 때 날씨·명절 메모를 자동으로 가져오지 않는다")
    _add_sim_args(srv)
    srv.set_defaults(days=60, start=None, llm_log=Path("out/llm"))  # AI 호출 기록은 가게별 파일로 남긴다

    ret = sub.add_parser("eval-retrieval", help="가게 메모 검색 방식(BM25, 임베딩, 하이브리드)을 비교한다")
    ret.add_argument("--bm25-only", action="store_true", help="임베딩 모델 없이 BM25만 잰다")

    mc = sub.add_parser("mcp", help="MCP 서버(stdio)를 띄운다. Claude Code·Claude Desktop에서 가게 업무를 도구로 쓴다")
    mc.add_argument("--db", default=os.environ.get("FRESH_STOCK_DB", "out/store.db"))
    mc.add_argument("--email", default=None, help="가게 멤버인 구글 계정 이메일 (기본: FRESH_STOCK_EMAIL)")
    mc.add_argument("--backend", choices=["claude-code", "anthropic"], default=os.environ.get("FRESH_STOCK_LLM", "claude-code"))
    mc.add_argument("--model", default=None)
    mc.add_argument("--effort", default="low")
    mc.add_argument("--llm-log", type=Path, default=Path("out/llm"))
    mc.add_argument("--no-rag", action="store_true")
    mc.add_argument("--no-auto-notes", action="store_true")

    fn = sub.add_parser("fetch-notes", help="날씨·명절 메모를 가져와 넣는다 (매일 예약 실행용)")
    fn.add_argument("--db", default=os.environ.get("FRESH_STOCK_DB", "out/store.db"))
    fn.add_argument("--day", type=date.fromisoformat, default=None, help="기준일 (기본: 오늘)")
    fn.add_argument("--days", type=int, default=3, help="기준일 다음 날부터 며칠치")

    ex = sub.add_parser(
        "export-catalog", help="가게 품목·거래처 목록을 CSV로 뽑는다 (엑셀에서 고쳐 import-catalog로 넣는다)"
    )
    ex.add_argument("--db", default=os.environ.get("FRESH_STOCK_DB", "out/store.db"))
    ex.add_argument("--store", type=int, required=True, help="가게 번호")
    ex.add_argument("--out", type=Path, default=Path("out/catalog"))

    im = sub.add_parser(
        "import-catalog", help="CSV로 가게 품목·거래처 목록을 통째로 바꾼다. 빠진 품목은 취급 그만둔 품목으로 남긴다"
    )
    im.add_argument("--db", default=os.environ.get("FRESH_STOCK_DB", "out/store.db"))
    im.add_argument("--store", type=int, required=True, help="가게 번호")
    im.add_argument("items", type=Path, help="품목 CSV (export-catalog의 items.csv 형식)")
    im.add_argument("suppliers", type=Path, help="거래처 CSV (export-catalog의 suppliers.csv 형식)")

    cp = sub.add_parser("copy-db", help="저장소 데이터를 다른 DB로 옮긴다 (예: SQLite -> Supabase)")
    cp.add_argument("source", help="옮길 DB (SQLite 파일 경로 또는 postgresql:// 주소)")
    cp.add_argument("target", help="받을 빈 DB")

    args = parser.parse_args()
    if args.command == "mcp":
        _mcp(args)
    elif args.command == "fetch-notes":
        _fetch_notes(args)
    elif args.command == "copy-db":
        _copy_db(args)
    elif args.command == "export-catalog":
        _export_catalog(args)
    elif args.command == "import-catalog":
        _import_catalog(args)
    elif args.command == "eval-retrieval":
        _eval_retrieval(args)
    elif args.command == "serve":
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
        _print_summaries([r.summary(args.switch) for r in results])
        _print_agent_stats(results)
