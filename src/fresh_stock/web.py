"""모바일 웹 화면 (FastAPI + Jinja2).

    로그인(구글) → 가게 선택 → 홈 → (입고 확인) → 재고표 사진 → 확인·수정 → 발주 추천 → 승인 → 거래처 문자

여러 가게가 함께 쓴다. 로그인한 구글 계정 이메일이 가게 멤버일 때만 그 가게 화면을 볼 수 있고,
모든 데이터는 가게별로 나뉜다. 멤버 추가·삭제와 가게 설정(품목·거래처, 장날·입고 요일, 경험 노트)은
사장님(owner)만 할 수 있다.
"""

import json
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .demo import demo_sheet_image
from .domain import CHANNELS, UNITS, Category
from .llm import LLMError
from .recognition import parse_price, parse_qty
from .market_calendar import today_kst
from .service import ORDER, SHEET, SIMPLE, STORE_LAYOUT, StoreService
from .store import OWNER, OWNER_NOTE, STAFF, Database

WEEKDAYS = "월화수목금토일"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SESSION_DAYS = 30
# 5일장 날짜 (날짜 끝자리)
MARKET_CHOICES = (
    ("", "장 없음"), ("1,6", "1·6일"), ("2,7", "2·7일"), ("3,8", "3·8일"), ("4,9", "4·9일"), ("0,5", "5·10일"),
)  # fmt: skip


@dataclass
class AuthConfig:
    secret_key: str  # 세션 쿠키 서명 키
    google_client_id: str | None = None
    google_client_secret: str | None = None
    dev_login: bool = False  # 개발용: 이메일만 넣어 로그인. PC에서 확인할 때만 켠다
    demo_store_id: int | None = None  # 데모: 가게가 없는 사람은 데모 가게에 넣는다
    secure_cookies: bool = False  # https로 배포하면 켠다

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@dataclass(frozen=True)
class Session:
    email: str
    store_id: int
    role: str
    service: StoreService


class Redirect(Exception):
    """로그인이나 가게 선택이 필요할 때 그 화면으로 보낸다."""

    def __init__(self, url: str):
        self.url = url


def _fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def _won(v: float) -> str:
    return f"{round(v):,}원"


def read_llm_log(path: Path | None, limit: int = 200) -> list[dict]:
    """LLM 호출 기록(JSONL)의 최근 limit건. 최신이 앞에 온다."""
    if path is None or not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()[-limit:]
    calls = []
    for line in lines:
        try:
            calls.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 쓰는 중에 읽힌 마지막 줄
    return calls[::-1]


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def _google_client(auth: AuthConfig):
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    # openid 범위를 쓰지 않고 userinfo로 이메일을 받아 id_token 검증(키 내려받기)을 피한다
    oauth.register(
        name="google",
        client_id=auth.google_client_id,
        client_secret=auth.google_client_secret,
        authorize_url=GOOGLE_AUTHORIZE_URL,
        access_token_url=GOOGLE_TOKEN_URL,
        client_kwargs={"scope": "email profile", "prompt": "select_account"},
    )
    return oauth.google


def create_app(
    db: Database,
    make_service: Callable[[int], StoreService],
    auth: AuthConfig,
    llm_log_dir: Path | None = None,
    google_exchange: Callable[[Request], Awaitable[dict]] | None = None,
) -> FastAPI:
    """make_service(store_id)는 그 가게의 StoreService를 만든다. google_exchange는 테스트에서 구글 응답을 대신한다."""
    app = FastAPI(title="fresh-stock")
    app.add_middleware(
        SessionMiddleware,
        secret_key=auth.secret_key,
        max_age=SESSION_DAYS * 24 * 3600,
        same_site="lax",
        https_only=auth.secure_cookies,
    )
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["day"] = _fmt_day
    templates.env.filters["won"] = _won
    services: dict[int, StoreService] = {}
    google = _google_client(auth) if auth.google_enabled else None

    def service_for(store_id: int) -> StoreService:
        if store_id not in services:
            services[store_id] = make_service(store_id)
        return services[store_id]

    def log_path(store_id: int) -> Path | None:
        return llm_log_dir / f"store-{store_id}.jsonl" if llm_log_dir else None

    @app.exception_handler(Redirect)
    async def _redirect(request: Request, exc: Redirect):
        return go(exc.url)

    def current(request: Request) -> Session:
        email = request.session.get("email")
        if not email:
            raise Redirect("/login")
        store_id = request.session.get("store_id")
        role = db.role(store_id, email) if store_id else None
        if role is None:
            request.session.pop("store_id", None)
            raise Redirect("/stores")
        service = service_for(store_id)
        service.refresh()  # 다른 화면이나 MCP에서 가게 설정을 고쳤을 수 있다
        return Session(email, store_id, role, service)

    def render(request: Request, name: str, s: Session | None = None, **context):
        base = {"user": request.session.get("email"), "auth": auth}
        if s is not None:
            step, day = s.service.status()
            base.update(
                catalog=s.service.catalog,
                step=step,
                status_day=day,
                today=today_kst(),
                store_name=db.store_name(s.store_id),
                role=s.role,
            )
        return templates.TemplateResponse(request, name, {**base, **context})

    # 로그인 ---------------------------------------------------------------------------

    def _after_login(request: Request, email: str) -> RedirectResponse:
        email = email.strip().lower()
        request.session.clear()
        request.session["email"] = email
        memberships = db.memberships(email)
        if not memberships and auth.demo_store_id is not None:
            db.add_member(auth.demo_store_id, email, OWNER)  # 데모에서는 누구나 둘러볼 수 있게
            memberships = db.memberships(email)
        if len(memberships) == 1:
            request.session["store_id"] = memberships[0].store_id
            return go("/")
        return go("/stores")

    @app.get("/login")
    def login(request: Request, error: str = ""):
        if request.session.get("email"):
            return go("/")
        return render(request, "login.html", error=error)

    @app.get("/login/google")
    async def login_google(request: Request):
        if google is None:
            return go("/login?" + urlencode({"error": "구글 로그인이 설정되지 않았습니다."}))
        return await google.authorize_redirect(request, str(request.url_for("auth_google")))

    @app.get("/auth/google", name="auth_google")
    async def auth_google(request: Request):
        try:
            if google_exchange is not None:
                info = await google_exchange(request)
            else:
                token = await google.authorize_access_token(request)
                info = (await google.get(GOOGLE_USERINFO_URL, token=token)).json()
        except Exception as e:  # 사용자가 취소했거나 구글 응답이 이상한 경우
            return go("/login?" + urlencode({"error": f"구글 로그인에 실패했습니다: {str(e)[:80]}"}))
        if not info.get("email") or not info.get("email_verified", False):
            return go("/login?" + urlencode({"error": "이메일이 확인된 구글 계정으로 로그인해 주세요."}))
        return _after_login(request, info["email"])

    @app.post("/login/dev")
    def login_dev(request: Request, email: str = Form(...)):
        if not auth.dev_login:
            return go("/login")
        if "@" not in email:
            return go("/login?" + urlencode({"error": "이메일 형식으로 입력해 주세요."}))
        return _after_login(request, email)

    @app.get("/logout")
    def logout(request: Request):
        request.session.clear()
        return go("/login")

    # 가게 ---------------------------------------------------------------------------

    @app.get("/stores")
    def stores(request: Request):
        email = request.session.get("email")
        if not email:
            return go("/login")
        return render(request, "stores.html", memberships=db.memberships(email), current=request.session.get("store_id"))

    @app.post("/stores")
    def create_store(request: Request, name: str = Form(...)):
        email = request.session.get("email")
        if not email:
            return go("/login")
        if not name.strip():
            return go("/stores")
        request.session["store_id"] = db.create_store(name, email)
        return go("/")

    @app.post("/stores/{store_id}/select")
    def select_store(request: Request, store_id: int):
        email = request.session.get("email")
        if not email:
            return go("/login")
        if db.role(store_id, email) is not None:
            request.session["store_id"] = store_id
        return go("/")

    @app.get("/members")
    def members(request: Request, error: str = ""):
        s = current(request)
        return render(request, "members.html", s, members=db.members(s.store_id), error=error)

    @app.post("/members")
    def add_member(request: Request, email: str = Form(...), role: str = Form(STAFF)):
        s = current(request)
        if s.role != OWNER:
            return go("/members?" + urlencode({"error": "멤버는 사장님만 추가할 수 있습니다."}))
        if "@" not in email:
            return go("/members?" + urlencode({"error": "구글 계정 이메일을 입력해 주세요."}))
        db.add_member(s.store_id, email, OWNER if role == OWNER else STAFF)
        return go("/members")

    @app.post("/members/remove")
    def remove_member(request: Request, email: str = Form(...)):
        s = current(request)
        if s.role != OWNER:
            return go("/members?" + urlencode({"error": "멤버는 사장님만 뺄 수 있습니다."}))
        try:
            db.remove_member(s.store_id, email)
        except ValueError as e:
            return go("/members?" + urlencode({"error": str(e)}))
        return go("/members")

    # 홈 ---------------------------------------------------------------------------

    @app.get("/")
    def home(request: Request, auto: int | None = None, auto_error: str = ""):
        s = current(request)
        service = s.service
        _, day = service.status()
        last = service.store.last_closed_day()
        last_plan = None
        if last is not None:
            for d in (last, last - timedelta(days=1)):
                plan = service.store.plan(d)
                if plan and plan.approved and plan.summary != "시뮬레이션 기록":
                    last_plan = plan
                    break
        return render(
            request,
            "home.html",
            s,
            notes=service.store.note_records(start=day),
            owner_note=OWNER_NOTE,
            revenue=service.revenue(days=7),
            corrections=service.store.correction_stats(),
            aliases=service.learned_aliases(),
            last_plan=last_plan,
            default_note_day=(day + timedelta(days=1)).isoformat(),
            auto=auto,
            auto_error=auto_error,
            can_research=service.research_llm_factory is not None,
        )

    @app.post("/notes/auto")
    def auto_notes(request: Request):
        s = current(request)
        _, day = s.service.status()
        added, error = s.service.refresh_auto_notes(day)
        params = {"auto": len(added)}
        if error:
            params["auto_error"] = error
        return go("/?" + urlencode(params))

    @app.post("/notes")
    def add_note(request: Request, day: date = Form(...), text: str = Form(...)):
        s = current(request)
        if text.strip():
            s.service.store.add_note(day, text, author=s.email)
        return go("/")

    @app.post("/notes/{note_id}/delete")
    def delete_note(request: Request, note_id: int):
        s = current(request)
        s.service.store.delete_note(note_id)
        return go("/")

    @app.post("/notes/{note_id}/research")
    def research_now(request: Request, note_id: int):
        """메모 하나를 지금 바로 AI가 다시 확인한다 (보통은 발주 추천을 만들 때 확인한다)."""
        s = current(request)
        note = next((n for n in s.service.store.note_records() if n.id == note_id), None)
        if note is not None and note.source == OWNER_NOTE:
            s.service.research_notes([note])
        return go("/")

    @app.post("/skip")
    def skip(request: Request):
        """가게를 쉰 날: 재고표나 발주를 건너뛰어 날짜를 오늘로 맞춘다."""
        s = current(request)
        try:
            s.service.skip()
        except ValueError:
            pass
        return go("/")

    # 재고표 -------------------------------------------------------------------------

    @app.get("/sheet")
    def sheet(request: Request, error: str = "", saved: int = 0):
        s = current(request)
        _, day = s.service.status()
        ordered, received = s.service.arrivals(day)
        return render(
            request,
            "sheet.html",
            s,
            day=day,
            has_rows=bool(s.service.store.sheet_rows(day)),
            error=error,
            n_received=sum(1 for q in received.values() if q > 0),
            n_differs=sum(1 for i in ordered.keys() | received.keys() if ordered.get(i, 0) != received.get(i, 0)),
            saved=saved,
            store_layout=s.service.sheet_layout == STORE_LAYOUT,
        )

    def _read(service: StoreService, day: date, image: bytes, media_type: str):
        try:
            service.read_sheet(day, image, media_type)
        except LLMError as e:
            return go("/sheet?" + urlencode({"error": f"재고표를 읽지 못했습니다: {str(e)[:80]}"}))
        return go("/sheet/review")

    @app.post("/sheet")
    async def upload_sheet(request: Request, photo: UploadFile = File(...)):
        s = current(request)
        _, day = s.service.status()
        data = await photo.read()
        return _read(s.service, day, data, photo.content_type or "image/jpeg")

    @app.post("/sheet/demo")
    def demo_sheet(request: Request):
        s = current(request)
        _, day = s.service.status()
        return _read(s.service, day, demo_sheet_image(s.service, day, seed=random.randint(0, 10**6)), "image/jpeg")

    @app.get("/sheet/image/{day}")
    def sheet_image(request: Request, day: date):
        s = current(request)
        data = s.service.store.sheet_image(day)
        return Response(data or b"", media_type="image/jpeg")

    def _review_page(
        request: Request,
        s: Session,
        day: date,
        entered: dict[int, tuple[str, str, str, str]] | None = None,
        bad: frozenset[int] = frozenset(),
        error: str = "",
        money: tuple[str, str] = ("", ""),
    ):
        """재고표 확인 화면. 확정에 실패하면 사람이 입력한 값(entered: 품목, 재고, 사입 수량, 사입 가격)을 그대로 다시 보여준다."""
        service = s.service
        rows = service.store.sheet_rows(day)
        max_possible, _ = service.sheet_bounds(day)
        return render(
            request,
            "review.html",
            s,
            day=day,
            rows=rows,
            flagged=sum(bool(r.flags) for r in rows),
            missing=[service.catalog.item(i) for i in service.missing_items(day, rows)],
            max_possible=max_possible,
            entered=entered or {},
            bad=bad,
            error=error,
            cash=money[0],
            card=money[1],
            store_layout=service.sheet_layout == STORE_LAYOUT,
        )

    @app.get("/sheet/review")
    def review(request: Request):
        s = current(request)
        _, day = s.service.status()
        if not s.service.store.sheet_rows(day):
            return go("/sheet")
        return _review_page(request, s, day)

    @app.post("/sheet/confirm")
    async def confirm(request: Request):
        s = current(request)
        service = s.service
        _, day = service.status()
        form = await request.form()
        rows = {r.idx: r for r in service.store.sheet_rows(day)}
        entered: dict[int, tuple[str, str, str, str]] = {}
        finals: dict[int, tuple[str | None, float | None]] = {}
        # 사입 칸이 있는 재고표면 줄마다 사입 수량·가격 (빈 칸은 안 들어온 것)
        buys: dict[int, tuple[float | None, int | None]] | None = (
            {} if service.sheet_layout == STORE_LAYOUT else None
        )
        bad = set()
        for key, value in form.items():
            if not key.startswith("item_"):
                continue
            idx = int(key.removeprefix("item_"))
            item_id = str(value) or None
            qty_text = str(form.get(f"qty_{idx}", "")).strip()
            buy_qty_text = str(form.get(f"buyq_{idx}", "")).strip()
            buy_price_text = str(form.get(f"buyp_{idx}", "")).strip()
            entered[idx] = (item_id or "", qty_text, buy_qty_text, buy_price_text)
            if item_id is None and not qty_text and not buy_qty_text:
                continue
            qty = parse_qty(qty_text) if qty_text else 0.0
            if item_id is None or qty is None:
                bad.add(idx)
            finals[idx] = (item_id, qty)
            if buys is not None:
                buy_qty = parse_qty(buy_qty_text) if buy_qty_text else None
                buy_price = parse_price(buy_price_text) if buy_price_text else None
                if (buy_qty_text and buy_qty is None) or (buy_price_text and buy_price is None):
                    bad.add(idx)
                buys[idx] = (buy_qty, buy_price)
        cash_text = str(form.get("cash") or "0").replace(",", "").strip() or "0"
        card_text = str(form.get("card") or "0").replace(",", "").strip() or "0"
        if not (cash_text.isdigit() and card_text.isdigit()):
            money = (str(form.get("cash") or ""), str(form.get("card") or ""))
            return _review_page(request, s, day, entered, frozenset(bad), "현금·카드 합계는 숫자로 입력해 주세요.", money)
        if bad:
            names = [f"{rows[i].raw_name} {rows[i].raw_qty}" if i in rows else "추가한 줄" for i in sorted(bad)]
            error = f"빨간 줄({', '.join(names)})의 품목이나 수량을 확인해 주세요."
            return _review_page(request, s, day, entered, frozenset(bad), error, (cash_text, card_text))
        service.confirm_sheet(day, finals, int(cash_text), int(card_text), buys)
        step, _ = service.status()
        return go("/order" if step == ORDER else "/")

    # 발주 ---------------------------------------------------------------------------

    def _order_page(request: Request, s: Session, day: date, entered: dict[str, int] | None = None, warnings=None):
        service = s.service
        catalog = service.catalog
        plan = service.plan_for(day)
        ctx = service.order_context(day)
        stock = service.store.closing(day)
        order = {item.id: n for n, item in enumerate(catalog.items)}
        all_lines = sorted(plan.lines, key=lambda ln: order.get(ln.item_id, len(order)))
        groups = []
        for supplier in catalog.suppliers:
            lines = [ln for ln in all_lines if catalog.item(ln.item_id).supplier_id == supplier.id]
            if lines:
                groups.append((supplier, lines))
        # 에이전트 요약은 LLM이 쓴 글이라 실제 결과와 다를 수 있다. 수량이 바뀐 품목은 코드로 따로 보여준다
        changed = [ln for ln in all_lines if ln.recommended != ln.base]
        cover = service.calendar.cover_days(plan.arrival)
        return render(
            request,
            "order.html",
            s,
            day=day,
            plan=plan,
            groups=groups,
            changed=changed,
            stock=stock,
            notes=ctx.notes if ctx else {},
            researched=[n for n in service.store.note_records(cover[0], cover[-1]) if n.research is not None],
            similar=service.similar_days(day),
            entered=entered or {ln.item_id: ln.final for ln in plan.lines},
            warnings=warnings or {},
        )

    @app.get("/order")
    def order(request: Request):
        s = current(request)
        step, day = s.service.status()
        if step != ORDER:
            return go("/")
        return _order_page(request, s, day)

    @app.post("/order/refresh")
    def refresh(request: Request):
        s = current(request)
        step, day = s.service.status()
        if step == ORDER:
            s.service.remake_plan(day)
        return go("/order")

    @app.post("/order")
    async def approve(request: Request):
        s = current(request)
        step, day = s.service.status()
        if step != ORDER:
            return go("/")
        form = await request.form()
        finals = {}
        for key, value in form.items():
            if key.startswith("final_"):
                text = str(value).strip() or "0"
                finals[key.removeprefix("final_")] = int(text) if text.isdigit() else -1
        invalid = {i: "0 이상의 정수로 입력해 주세요." for i, q in finals.items() if q < 0}
        warnings = invalid or s.service.check_finals(day, finals)
        if invalid or (warnings and form.get("confirmed") != "yes"):
            return _order_page(request, s, day, finals, warnings)
        s.service.approve(day, finals)
        return go(f"/messages?day={day.isoformat()}")

    # 거래처 문자 ----------------------------------------------------------------------

    @app.get("/messages")
    def messages(request: Request, day: date, greeting: bool = False):
        s = current(request)
        return render(
            request, "messages.html", s, day=day, messages=s.service.messages(day, greeting=greeting), greeting=greeting
        )

    # 입고 확인 -------------------------------------------------------------------------

    def _received_page(request: Request, s: Session, day: date, entered: dict[str, str] | None = None, error: str = ""):
        service = s.service
        ordered, received = service.arrivals(day)
        lines = [i for i in service.catalog.items if ordered.get(i.id, 0) > 0 or received.get(i.id, 0) > 0]
        return render(
            request,
            "received.html",
            s,
            day=day,
            lines=lines,
            ordered=ordered,
            received=received,
            entered=entered or {},
            error=error,
            has_rows=bool(service.store.sheet_rows(day)),
        )

    @app.get("/received")
    def received_page(request: Request):
        s = current(request)
        step, day = s.service.status()
        if step != SHEET:
            return go("/")
        if s.service.sheet_layout == STORE_LAYOUT:
            return go("/sheet")  # 재고표의 사입 칸으로 입고를 기록한다
        return _received_page(request, s, day)

    @app.post("/received")
    async def fix_received(request: Request):
        """실제로 들어온 양으로 고친다. 칸 이름: recv_<품목 id>, 추가 줄은 add_item_<n> + add_qty_<n>."""
        s = current(request)
        step, day = s.service.status()
        if step != SHEET:
            return go("/")
        if s.service.sheet_layout == STORE_LAYOUT:
            return go("/sheet")
        form = await request.form()
        entered = {k: str(v).strip() for k, v in form.items()}
        changes: dict[str, float] = {}
        bad = []
        pairs = [(k.removeprefix("recv_"), v) for k, v in entered.items() if k.startswith("recv_")]
        pairs += [(entered[k], entered.get(f"add_qty_{k.removeprefix('add_item_')}", ""))
                  for k in entered if k.startswith("add_item_") and entered[k]]  # fmt: skip
        known = {i.id for i in s.service.catalog.items}
        for item_id, text in pairs:
            qty = parse_qty(text) if text else 0.0
            if item_id not in known or qty is None:
                bad.append(f"{s.service.catalog.item(item_id).name if item_id in known else item_id} '{text}'")
            else:
                # 목록의 칸은 들어온 양 그대로, 추가 줄에서 목록에 있는 품목을 또 고르면 더한다
                changes[item_id] = changes.get(item_id, 0.0) + qty
        if bad:
            return _received_page(request, s, day, entered, f"수량을 알 수 없습니다: {', '.join(bad)}")
        try:
            s.service.fix_received(day, changes)
        except ValueError as e:
            return _received_page(request, s, day, entered, str(e))
        return go("/sheet?saved=1")

    # 가게 설정 (품목·거래처, 장날·입고 요일, 경험 노트) ---------------------------------------

    def _owner_only(s: Session) -> RedirectResponse | None:
        if s.role != OWNER:
            return go("/settings?" + urlencode({"error": "가게 설정은 사장님만 고칠 수 있습니다."}))
        return None

    @app.get("/settings")
    def settings(request: Request, error: str = "", saved: str = ""):
        s = current(request)
        service = s.service
        catalog = service.catalog
        groups = [(sup, [i for i in catalog.items if i.supplier_id == sup.id]) for sup in catalog.suppliers]
        return render(
            request,
            "settings.html",
            s,
            groups=groups,
            retired=catalog.retired,
            channels=CHANNELS,
            market_choices=MARKET_CHOICES,
            market_value=",".join(str(d) for d in sorted(service.calendar.market_day_digits)),
            closed=set(service.calendar.no_supply_weekdays),
            weekdays=WEEKDAYS,
            knowledge=service.knowledge,
            sheet_layout=service.sheet_layout,
            layouts=((SIMPLE, "품목 · 남은 수량"), (STORE_LAYOUT, "품목 · 남은 수량 · 사입 수량 · 사입 가격 (두 묶음)")),
            customized=service.store.catalog() is not None,
            error=error,
            saved=saved,
        )

    def _item_page(request: Request, s: Session, item_id: str | None, values: dict[str, str], error: str = ""):
        return render(
            request,
            "item.html",
            s,
            item_id=item_id,
            values=values,
            categories=[c.value for c in Category],
            units=UNITS,
            error=error,
        )

    @app.get("/settings/item")
    def item_form(request: Request, item_id: str | None = None):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        catalog = s.service.catalog
        if item_id is not None and item_id not in {i.id for i in catalog.all_items}:
            return go("/settings")
        if item_id is None:
            values = {"category": Category.VEGETABLE.value, "unit": UNITS[0], "supplier_id": catalog.suppliers[0].id,
                      "shelf_life": "2"}  # fmt: skip
        else:
            i = catalog.item(item_id)
            values = {"name": i.name, "category": i.category.value, "unit": i.unit, "supplier_id": i.supplier_id,
                      "base_demand": f"{i.base_demand:g}", "price": str(i.price), "cost": str(i.cost),
                      "shelf_life": str(i.shelf_life)}  # fmt: skip
        return _item_page(request, s, item_id, values)

    @app.post("/settings/item")
    async def save_item(request: Request):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        form = await request.form()
        values = {k: str(v).strip() for k, v in form.items()}
        item_id = values.get("item_id") or None
        try:
            numbers = {}
            for key, label, cast in (("base_demand", "하루 판매량", float), ("price", "판매가", int),
                                     ("cost", "매입가", int), ("shelf_life", "정가로 파는 기간", int)):  # fmt: skip
                text = values.get(key, "").replace(",", "")
                try:
                    numbers[key] = cast(text)
                except ValueError:
                    raise ValueError(f"{label}에 숫자를 적어 주세요.") from None
            item = s.service.save_item(
                item_id,
                name=values.get("name", ""),
                category=values.get("category", ""),
                unit=values.get("unit", ""),
                supplier_id=values.get("supplier_id", ""),
                **numbers,
            )
        except ValueError as e:
            return _item_page(request, s, item_id, values, str(e))
        return go("/settings?" + urlencode({"saved": f"{item.name} 저장했어요."}))

    @app.post("/settings/item/{item_id}/active")
    def set_item_active(request: Request, item_id: str, active: int = Form(...)):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            s.service.set_item_active(item_id, bool(active))
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        name = s.service.catalog.item(item_id).name
        return go("/settings?" + urlencode({"saved": f"{name}: {'다시 취급해요' if active else '취급을 그만뒀어요'}."}))

    @app.post("/settings/supplier")
    def save_supplier(request: Request, name: str = Form(""), channel: str = Form(""), supplier_id: str = Form("")):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            supplier = s.service.save_supplier(supplier_id or None, name=name, channel=channel)
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        return go("/settings?" + urlencode({"saved": f"{supplier.name} 저장했어요."}))

    @app.post("/settings/supplier/{supplier_id}/delete")
    def delete_supplier(request: Request, supplier_id: str):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            s.service.delete_supplier(supplier_id)
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        return go("/settings?" + urlencode({"saved": "거래처를 지웠어요."}))

    @app.post("/settings/calendar")
    async def save_calendar(request: Request):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        form = await request.form()
        market = str(form.get("market") or "")
        closed = [str(v) for v in form.getlist("closed")]
        try:
            if market not in {v for v, _ in MARKET_CHOICES} or not all(c.isdigit() for c in closed):
                raise ValueError("장날이나 요일 값이 잘못됐습니다.")
            s.service.save_calendar(tuple(int(d) for d in market.split(",") if d), tuple(int(c) for c in closed))
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        return go("/settings?" + urlencode({"saved": "장날·입고 요일을 저장했어요."}))

    @app.post("/settings/sheet-layout")
    def save_sheet_layout(request: Request, layout: str = Form("")):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            s.service.save_sheet_layout(layout)
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        return go("/settings?" + urlencode({"saved": "재고표 형식을 저장했어요."}))

    @app.post("/settings/knowledge")
    def save_knowledge(request: Request, text: str = Form("")):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            s.service.save_knowledge(text)
        except ValueError as e:
            return go("/settings?" + urlencode({"error": str(e)}))
        return go("/settings?" + urlencode({"saved": "경험 노트를 저장했어요."}))

    # AI 호출 기록 (가게별) -------------------------------------------------------------

    @app.get("/logs")
    def logs(request: Request):
        s = current(request)
        path = log_path(s.store_id)
        calls = read_llm_log(path)
        kinds: dict[str, dict] = {}
        for c in calls:
            k = kinds.setdefault(c["purpose"].split(" ")[0] or "기타", {"n": 0, "sec": 0.0, "in": 0, "out": 0, "err": 0})
            k["n"] += 1
            k["sec"] += c["seconds"]
            k["in"] += c["input_tokens"]
            k["out"] += c["output_tokens"]
            k["err"] += bool(c["error"])
        return render(request, "logs.html", s, calls=calls, kinds=kinds, llm_log=path)

    @app.get("/logs/{n}")
    def log_detail(request: Request, n: int):
        s = current(request)
        calls = read_llm_log(log_path(s.store_id))
        if not 0 <= n < len(calls):
            return go("/logs")
        call = calls[n]
        return render(
            request,
            "log_detail.html",
            s,
            call=call,
            n=n,
            output=json.dumps(call["output"], ensure_ascii=False, indent=2) if call["output"] is not None else "",
        )

    return app
