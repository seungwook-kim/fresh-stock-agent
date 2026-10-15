"""모바일 웹 화면 (FastAPI + Jinja2).

    로그인(구글) → 가게 선택 → 홈 → (입고 확인) → 재고표 사진 → 확인·수정 → 발주 추천 → 승인 → 거래처 문자

여러 가게가 함께 쓴다. 로그인한 구글 계정 이메일이 가게 멤버일 때만 그 가게 화면을 볼 수 있고,
모든 데이터는 가게별로 나뉜다. 멤버 추가·삭제와 가게 설정(품목·거래처, 장날·입고 요일, 경험 노트)은
사장님(owner)만 할 수 있다.

화면 디자인은 디자인 회의 결정(작업파일/디자인회의/결정사항.md)을 따른다: 청과 가게 주제는 틀(간판 머리띠·봉투·
가격표·주문 쪽지)이 맡고, 노랑은 "확인 필요", 빨강은 "저장 불가", 보라는 "AI 판단"에만 쓴다.
"""

import json
import math
import random
from difflib import SequenceMatcher
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .demo import demo_sheet_image
from .domain import CHANNELS, UNITS, Category, Item
from .llm import LLMError
from .market_calendar import today_kst
from .recognition import parse_price, parse_qty
from .service import ORDER, SHEET, SIMPLE, STORE_LAYOUT, StoreService
from .store import OWNER, OWNER_NOTE, STAFF, Database
from .visuals import STALL, item_pic, weather_pic

WEEKDAYS = "월화수목금토일"
GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
SESSION_DAYS = 30
# 5일장 날짜 (날짜 끝자리)
MARKET_CHOICES = (
    ("", "장 없음"), ("1,6", "1·6일"), ("2,7", "2·7일"), ("3,8", "3·8일"), ("4,9", "4·9일"), ("0,5", "5·10일"),
)  # fmt: skip
# AI 기록 화면에 보이는 호출 이름
PURPOSES = {"sheet": "재고표 읽기", "order": "발주 추천", "research": "메모 확인", "greeting": "문자 인사말"}
# 재고표 형식 고르기: (값, 이름, 설명)
SHEET_LAYOUTS = (
    (SIMPLE, "품목 · 남은 수량", "품목과 남은 수량만 적는 재고표"),
    (STORE_LAYOUT, "두 묶음 × 30줄 (사입 칸 있음)", "품목 | 남은 수량 | 사입 수량 | 사입 가격"),
)
# 기다림 화면 단계
READ_STEPS = "사진 받음|글자 읽는 중|품목 맞춰 보기"
ORDER_STEPS = "마감 재고 확정|가게 메모 확인|지난 비슷한 날 찾기|품목별 수량 계산"


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


def _comma(v: float | None) -> str:
    return "" if v is None else f"{round(v):,}"


def qty_text(value: float | None) -> str:
    """종이에 적는 식으로: 4.5 → "4반", 0.5 → "반", 3 → "3"."""
    whole, half = half_split(value)
    if half:
        return f"{whole}반" if whole != "0" else "반"
    return whole


def half_split(value: float | None) -> tuple[str, bool]:
    """수량을 [정수 칸][반 칩]으로 나눈다. 4.5 → ("4", True). 반이 아닌 소수는 칸에 그대로."""
    if value is None:
        return "", False
    whole = math.floor(value + 1e-9)
    frac = value - whole
    if abs(frac - 0.5) < 1e-9:
        return str(whole), True
    if abs(frac) < 1e-9:
        return str(whole), False
    return f"{value:g}", False


def read_num(form, name: str) -> tuple[str, bool, float | None, bool]:
    """수량 칸 하나 읽기: (적은 글자, 반 켬, 값, 해석 실패). 빈칸이고 반도 꺼져 있으면 값 None (실패 아님).
    칸에 "4반"·"4.5"처럼 적어도 읽고, 정수에 반 칩을 켰으면 0.5를 더한다."""
    text = str(form.get(name) or "").strip()
    half = bool(form.get(f"{name}_half"))
    if not text:
        return text, half, (0.5 if half else None), False
    value = parse_qty(text)
    if value is None:
        return text, half, None, True
    if half and float(value).is_integer():
        value += 0.5
    return text, half, value, False


def read_money(form, name: str) -> tuple[str, int | None, bool]:
    """금액 칸: (적은 글자, 원, 해석 실패)."""
    text = str(form.get(name) or "").strip()
    if not text:
        return text, None, False
    value = parse_price(text)
    return text, value, value is None


def repeated_digit_fix(qty: float | None) -> int | None:
    """88 → 8, 122 → 12: 마지막 숫자를 한 번 더 읽은 것 같을 때 고칠 값 (재고표에서 흔한 실수)."""
    if qty is None or qty < 10 or not float(qty).is_integer():
        return None
    s = str(int(qty))
    return int(s[:-1]) if s[-1] == s[-2] else None


def item_groups(items: list[Item]) -> list[tuple[str, list[Item]]]:
    """품목 고르기 목록: 재고표 순서 그대로, 분류가 바뀔 때마다 묶는다."""
    groups: list[tuple[str, list[Item]]] = []
    for item in items:
        if not groups or groups[-1][0] != item.category.value:
            groups.append((item.category.value, []))
        groups[-1][1].append(item)
    return groups


def _jamo(text: str) -> str:
    """한글을 자모로 풀어 비교한다 (굴·귤처럼 손글씨에서 헷갈리는 글자가 비슷하게 나오게)."""
    out = []
    for ch in text:
        code = ord(ch) - 0xAC00
        if 0 <= code < 11172:
            out += [chr(0x1100 + code // 588), chr(0x1161 + code % 588 // 28)]
            if code % 28:
                out.append(chr(0x11A7 + code % 28))
        else:
            out.append(ch)
    return "".join(out)


def similar_items(raw: str, items: list[Item], n: int = 3) -> list[Item]:
    """적힌 이름과 비슷한 품목 (빨간 줄 품목 고르기 맨 위). "토마" → 토마토·방울토마토, "굴" → 귤."""
    raw = "".join(raw.split())
    if not raw:
        return []
    scored = []
    for item in items:
        score = SequenceMatcher(None, _jamo(raw), _jamo(item.name)).ratio()
        if raw in item.name or item.name in raw:
            score += 1 - abs(len(item.name) - len(raw)) / 10  # 이름에 들어 있으면 길이가 비슷할수록 앞에
        scored.append((score, item))
    scored.sort(key=lambda x: -x[0])
    return [item for score, item in scored[:n] if score >= 0.4]


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
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["day"] = _fmt_day
    templates.env.filters["won"] = _won
    templates.env.filters["comma"] = _comma
    templates.env.filters["qty"] = qty_text
    templates.env.globals.update(
        item_pic=item_pic, half_split=half_split, weather_pic=weather_pic, item_groups=item_groups, WEEKDAYS=WEEKDAYS
    )
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
        base = {"user": request.session.get("email"), "auth": auth, "late": False}
        if s is not None:
            step, day = s.service.status()
            today = today_kst()
            base.update(
                catalog=s.service.catalog,
                step=step,
                status_day=day,
                today=today,
                late=day < today,
                store_name=db.store_name(s.store_id),
                role=s.role,
                sheet_layout=s.service.sheet_layout,
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
        return render(request, "login.html", error=error, stall=STALL)

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

    def _store_line(store_id: int) -> str:
        """가게 고르기 간판 오른쪽에 쓰는 지금 상태."""
        service = service_for(store_id)
        service.refresh()
        step, day = service.status()
        if step == ORDER:
            return f"{_fmt_day(day)} 발주 확인할 차례"
        if service.store.sheet_rows(day):
            return f"{_fmt_day(day)} 재고표 확인 중"
        return f"{_fmt_day(day)} 재고표 차례"

    @app.get("/stores")
    def stores(request: Request):
        email = request.session.get("email")
        if not email:
            return go("/login")
        memberships = db.memberships(email)
        lines = {m.store_id: _store_line(m.store_id) for m in memberships}
        return render(
            request, "stores.html", memberships=memberships, current=request.session.get("store_id"), lines=lines
        )

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

    @app.post("/store/rename")
    def rename_store(request: Request, name: str = Form("")):
        s = current(request)
        if s.role != OWNER:
            return go("/members?" + urlencode({"error": "가게 이름은 사장님만 바꿀 수 있습니다."}))
        if not name.strip():
            return go("/members?" + urlencode({"error": "가게 이름을 적어 주세요."}))
        db.rename_store(s.store_id, name)
        return go("/members?" + urlencode({"saved": "가게 이름을 바꿨어요."}))

    @app.get("/members")
    def members(request: Request, error: str = "", saved: str = ""):
        s = current(request)
        return render(
            request, "members.html", s, members=db.members(s.store_id), error=error, saved=saved, active="more"
        )

    @app.post("/members")
    def add_member(request: Request, email: str = Form(...), role: str = Form(STAFF)):
        s = current(request)
        if s.role != OWNER:
            return go("/members?" + urlencode({"error": "멤버는 사장님만 추가할 수 있습니다."}))
        if "@" not in email:
            return go("/members?" + urlencode({"error": "구글 계정 이메일을 입력해 주세요."}))
        db.add_member(s.store_id, email, OWNER if role == OWNER else STAFF)
        return go("/members?" + urlencode({"saved": f"{email.strip().lower()} 를 넣었어요."}))

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

    @app.get("/more")
    def more(request: Request):
        s = current(request)
        service = s.service
        step, day = service.status()
        others = [m for m in db.memberships(s.email) if m.store_id != s.store_id]
        return render(
            request,
            "more.html",
            s,
            active="more",
            n_items=len(service.catalog.items),
            n_suppliers=len(service.catalog.suppliers),
            n_members=len(db.members(s.store_id)),
            others=others,
            received_day=day if step == SHEET else None,
        )

    # 홈 ---------------------------------------------------------------------------

    def _recent_plan(service: StoreService):
        """가장 최근에 승인한 발주 (시뮬레이션으로 넣은 데모 기록은 빼고)."""
        last = service.store.last_closed_day()
        if last is None:
            return None
        for d in (last, last - timedelta(days=1)):
            plan = service.store.plan(d)
            if plan and plan.approved and plan.summary != "시뮬레이션 기록":
                return plan
        return None

    @app.get("/")
    def home(request: Request, auto: int | None = None, auto_error: str = ""):
        s = current(request)
        service = s.service
        step, day = service.status()
        rows = service.store.sheet_rows(day) if step == SHEET else []
        flagged = [r for r in rows if r.flags]
        missing = service.missing_items(day, rows) if rows else []  # 확인 화면의 "재고표에 없음" 노란 줄
        plan = _recent_plan(service)
        unsent = []
        if plan is not None:
            sent = service.store.sent(plan.order_day)
            unsent = [m.supplier for m in service.messages(plan.order_day) if m.supplier.id not in sent]
        # 봉투 카드: 지금 할 일 하나
        if step == SHEET and plan is not None and plan.order_day == day - timedelta(days=1) and unsent:
            bag = "messages"  # 방금 승인한 발주의 거래처 문자가 남았다
        elif step == ORDER:
            bag = "order"
        elif rows:
            bag = "review"
        else:
            bag = "sheet"
        catalog = service.catalog
        known = {i.id for i in catalog.all_items}
        active = {i.id for i in catalog.items}
        revenue = service.revenue(days=7)[::-1]
        flagged_items = [catalog.item(r.item_id_read) for r in flagged if r.item_id_read in known]
        flagged_items += [catalog.item(i) for i in missing]
        return render(
            request,
            "home.html",
            s,
            active="home",
            bag=bag,
            day=day,
            # 확인 화면과 같은 셈: 품목을 모르는 줄은 빨간 줄, 못 읽었거나 표시가 붙은 줄과 빠진 품목은 노란 줄
            n_err=sum(r.item_id_read not in active for r in rows),
            n_check=sum(r.item_id_read in active and (r.qty_read is None or bool(r.flags)) for r in rows) + len(missing),
            flagged_items=flagged_items[:8],
            rows=rows,
            plan=plan,
            unsent=unsent,
            notes=service.store.note_records(start=day),
            owner_note=OWNER_NOTE,
            revenue=revenue,
            market={r.day for r in revenue if service.calendar.is_market_day(r.day)},
            note_days=[day + timedelta(days=k) for k in range(8)],
            default_note_day=day + timedelta(days=1),
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
        rows = s.service.store.sheet_rows(day)
        return render(
            request,
            "sheet.html",
            s,
            active="sheet",
            day=day,
            rows=rows,
            flagged=sum(bool(r.flags) for r in rows),
            error=error,
            n_received=sum(1 for q in received.values() if q > 0),
            n_differs=sum(1 for i in ordered.keys() | received.keys() if ordered.get(i, 0) != received.get(i, 0)),
            saved=saved,
            store_layout=s.service.sheet_layout == STORE_LAYOUT,
            read_steps=READ_STEPS,
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
        entered: dict[int, dict] | None = None,
        bad: frozenset[int] = frozenset(),
        unresolved: frozenset[int] = frozenset(),
        error: str = "",
        money: tuple[str, str] = ("", ""),
    ):
        """재고표 확인 화면. 노란·빨간 줄은 맨 위 카드로, 나머지는 종이 순서 그대로 아래에.
        확정에 실패하면 사람이 입력한 값(entered)을 그대로 다시 보여준다."""
        service = s.service
        catalog = service.catalog
        known = {i.id for i in catalog.items}
        rows = service.store.sheet_rows(day)
        entered = entered or {}
        max_possible, expected = service.sheet_bounds(day)
        prev = service.store.closing(day - timedelta(days=1))
        came = service.store.received(day)
        fixes = []
        for r in rows:
            typed = entered.get(r.idx, {})
            item_now = typed.get("item") if typed else r.item_id_read
            if r.idx in bad or item_now not in known:
                kind = "err"
            elif r.qty_read is None:
                kind = "need"
            elif r.flags:
                kind = "check"
            else:
                continue
            too_big = any("보다 많음" in f for f in r.flags)
            limit = max_possible.get(r.item_id_read or "", 0)
            fix = repeated_digit_fix(r.qty_read) if too_big else None
            if fix is not None and fix > limit:
                fix = None  # 고쳐도 가능한 양보다 많으면 고치기 버튼을 내지 않는다
            fixes.append({
                "r": r, "kind": kind, "why": " · ".join(r.flags) or "품목이나 수량을 확인해 주세요.",
                "fix": fix,
                "similar": similar_items(r.raw_name, catalog.items) if kind == "err" else [],
                "limit": limit, "unresolved": r.idx in unresolved,
            })  # fmt: skip
        next_idx = max((r.idx for r in rows), default=-1) + 1
        missing = []
        for k, item_id in enumerate(service.missing_items(day, rows)):
            missing.append({
                "idx": next_idx + k, "item": catalog.item(item_id), "prev": prev.get(item_id, 0), "came": came.get(item_id, 0),
                "expected": expected.get(item_id, 0), "unresolved": next_idx + k in unresolved,
            })  # fmt: skip
        add_start = next_idx + len(missing)
        n_check = sum(f["kind"] != "err" for f in fixes) + len(missing)
        return render(
            request,
            "review.html",
            s,
            day=day,
            rows=rows,
            fixes=fixes,
            fix_idx={f["r"].idx for f in fixes},
            known=known,
            missing=missing,
            add_rows=range(add_start, add_start + 3),
            n_check=n_check,
            n_err=sum(f["kind"] == "err" for f in fixes),
            entered=entered,
            bad=bad,
            error=error,
            cash=money[0],
            card=money[1],
            store_layout=service.sheet_layout == STORE_LAYOUT,
            order_steps=ORDER_STEPS,
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
        """재고표 확정. 칸 이름: item_N, qty_N(+_half), 사입 칸 buyq_N(+_half)·buyp_N, 노란 줄 확인 ok_N,
        재고표에 없는 품목 miss_N(+ zero_N "0 맞아요"). 빨간 줄(저장 불가)이나 확인하지 않은 노란 줄이 있으면 확정하지 않는다."""
        s = current(request)
        service = s.service
        _, day = service.status()
        form = await request.form()
        rows = {r.idx: r for r in service.store.sheet_rows(day)}
        store_layout = service.sheet_layout == STORE_LAYOUT
        entered: dict[int, dict] = {}
        finals: dict[int, tuple[str | None, float | None]] = {}
        buys: dict[int, tuple[float | None, int | None]] | None = {} if store_layout else None
        bad, unresolved = set(), set()
        for key in list(form.keys()):
            if not key.startswith("item_"):
                continue
            idx = int(key.removeprefix("item_"))
            item_id = str(form.get(key) or "") or None
            qty_text, half, qty, qty_bad = read_num(form, f"qty_{idx}")
            buy_text, buy_half, buy_qty, buy_bad = read_num(form, f"buyq_{idx}")
            price_text, price, price_bad = read_money(form, f"buyp_{idx}")
            ok, zero = bool(form.get(f"ok_{idx}")), bool(form.get(f"zero_{idx}"))
            entered[idx] = {"item": item_id or "", "qty": qty_text, "half": half, "buyq": buy_text, "buyh": buy_half,
                            "buyp": price_text, "ok": ok, "zero": zero}  # fmt: skip
            row = rows.get(idx)
            missing_row = bool(form.get(f"miss_{idx}"))
            if missing_row:
                if qty is None and not qty_bad:
                    if not zero:
                        unresolved.add(idx)
                        continue
                    qty = 0.0
            elif row is None and item_id is None and not qty_text and not buy_text and not half:
                continue  # 비워 둔 추가 줄
            if qty is None and not qty_bad:
                if row is not None and row.qty_read is None:
                    unresolved.add(idx)  # 못 읽은 칸은 숫자를 넣어야 확인된다
                qty = 0.0
            if item_id is None or qty_bad or buy_bad or price_bad:
                bad.add(idx)
            finals[idx] = (item_id, qty)
            if buys is not None:
                buys[idx] = (buy_qty, price)
            if row is not None and row.flags and not ok and idx not in unresolved:
                changed = (item_id, qty) != (row.item_id_read, row.qty_read) or (
                    store_layout and (buy_qty, price) != (row.buy_qty_read, row.buy_price_read)
                )
                if not changed:
                    unresolved.add(idx)
        cash_text = str(form.get("cash") or "").strip()
        card_text = str(form.get("card") or "").strip()
        cash, card = parse_price(cash_text) if cash_text else None, parse_price(card_text) if card_text else None
        money = (cash_text, card_text)
        if bad:
            names = [f"{rows[i].raw_name} {rows[i].raw_qty}" if i in rows else "추가한 줄" for i in sorted(bad)]
            error = f"빨간 줄({', '.join(names)})의 품목이나 숫자를 고쳐야 확정돼요."
            return _review_page(request, s, day, entered, frozenset(bad), frozenset(unresolved), error, money)
        if unresolved:
            error = f"노란 줄 {len(unresolved)}개를 확인해 주세요. 맞으면 [맞아요], 아니면 숫자를 고치면 돼요."
            return _review_page(request, s, day, entered, frozenset(), frozenset(unresolved), error, money)
        if cash is None or card is None:
            error = "돈통 현금과 카드 합계를 넣어 주세요. 없는 날은 0을 넣으면 돼요."
            return _review_page(request, s, day, entered, error=error, money=money)
        service.confirm_sheet(day, finals, cash, card, buys)
        step, _ = service.status()
        return go("/order" if step == ORDER else "/")

    # 발주 ---------------------------------------------------------------------------

    def _order_page(
        request: Request,
        s: Session,
        day: date,
        entered: dict[str, int] | None = None,
        warnings: dict[str, str] | None = None,
        accepted: set[str] | None = None,
        error: str = "",
    ):
        service = s.service
        catalog = service.catalog
        plan = service.plan_for(day)
        ctx = service.order_context(day)
        stock = service.store.closing(day)
        entered = entered or {ln.item_id: ln.final for ln in plan.lines}
        warnings = warnings or {}
        order = {item.id: n for n, item in enumerate(catalog.items)}
        all_lines = sorted(plan.lines, key=lambda ln: order.get(ln.item_id, len(order)))
        groups = []
        for supplier in catalog.suppliers:
            lines = [ln for ln in all_lines if catalog.item(ln.item_id).supplier_id == supplier.id]
            # 추천도 재고도 없는 품목은 접어 둔다 (60~90개를 다 보이면 찾기 어렵다)
            shown = [ln for ln in lines if ln.recommended > 0 or stock.get(ln.item_id, 0) > 0
                     or entered.get(ln.item_id, 0) > 0 or ln.item_id in warnings]  # fmt: skip
            zero = [ln for ln in lines if ln not in shown]
            if lines:
                groups.append((supplier, shown, zero))
        # 에이전트 요약은 LLM이 쓴 글이라 실제 결과와 다를 수 있다. 수량이 바뀐 품목은 코드로 따로 보여준다
        changed = [ln for ln in all_lines if ln.recommended != ln.base]
        cover = service.calendar.cover_days(plan.arrival)
        n_items = sum(1 for q in entered.values() if q > 0)
        n_suppliers = sum(1 for _, shown, _ in groups if any(entered.get(ln.item_id, 0) > 0 for ln in shown))
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
            entered=entered,
            warnings=warnings,
            accepted=accepted or set(),
            n_items=n_items,
            n_suppliers=n_suppliers,
            error=error,
            order_steps=ORDER_STEPS,
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
        """발주 승인. 입력 실수 경고가 난 품목은 [추천값으로 고치기](fix=품목:값) 또는 [이 값 맞아요](ok_품목=값)로 확인한다."""
        s = current(request)
        step, day = s.service.status()
        if step != ORDER:
            return go("/")
        form = await request.form()
        finals = {}
        for key, value in form.items():
            if key.startswith("final_"):
                text = str(value).strip().replace(",", "") or "0"
                finals[key.removeprefix("final_")] = int(text) if text.isdigit() else -1
        fix = str(form.get("fix") or "")
        if ":" in fix:
            item_id, value = fix.split(":", 1)
            if item_id in finals and value.isdigit():
                finals[item_id] = int(value)
            return _order_page(request, s, day, finals, s.service.check_finals(day, finals))
        invalid = {i for i, q in finals.items() if q < 0}
        if invalid:
            names = ", ".join(s.service.catalog.item(i).name for i in invalid)
            finals = {i: max(q, 0) for i, q in finals.items()}
            return _order_page(request, s, day, finals, error=f"{names}: 발주는 0 이상의 정수로 넣어 주세요.")
        warnings = s.service.check_finals(day, finals)
        # "88 맞아요"는 그 값에만 유효하다. 확인한 뒤 숫자를 바꾸면 다시 묻는다
        accepted = {i for i in warnings if form.get(f"ok_{i}") == str(finals[i])}
        if set(warnings) - accepted:
            return _order_page(request, s, day, finals, warnings, accepted)
        s.service.approve(day, finals)
        return go(f"/messages?day={day.isoformat()}")

    # 거래처 문자 ----------------------------------------------------------------------

    @app.get("/messages")
    def messages(request: Request, day: date, greeting: bool = False):
        s = current(request)
        msgs = s.service.messages(day, greeting=greeting)
        sent = s.service.store.sent(day)
        links = {}
        for m in msgs:
            phone = "".join(c for c in m.supplier.phone if c.isdigit() or c == "+")
            links[m.supplier.id] = {
                "sms": f"sms:{phone}?&body={quote(m.text)}",
                "tel": f"tel:{phone}" if phone else "",
            }
        return render(
            request,
            "messages.html",
            s,
            active="order",
            day=day,
            messages=msgs,
            greeting=greeting,
            links=links,
            sent={k: datetime.fromisoformat(v).strftime("%H:%M") for k, v in sent.items()},
        )

    @app.post("/messages/sent")
    def message_sent(request: Request, day: date = Form(...), supplier: str = Form(...)):
        """문자·전화 버튼을 누르면 보낸 것으로 남긴다 (두 번 보내지 않게)."""
        s = current(request)
        if supplier in {sup.id for sup in s.service.catalog.suppliers}:
            s.service.store.mark_sent(day, supplier)
        return Response(status_code=204)

    # 입고 확인 -------------------------------------------------------------------------

    def _received_page(request: Request, s: Session, day: date, entered: dict | None = None, error: str = ""):
        service = s.service
        catalog = service.catalog
        ordered, received = service.arrivals(day)
        groups = []
        for supplier in catalog.suppliers:
            lines = [i for i in catalog.items if i.supplier_id == supplier.id
                     and (ordered.get(i.id, 0) > 0 or received.get(i.id, 0) > 0)]  # fmt: skip
            if lines:
                groups.append((supplier, lines))
        plan = service.store.plan(day - timedelta(days=1))
        differs = [(catalog.item(i), ordered.get(i, 0), received.get(i, 0)) for i in ordered.keys() | received.keys()
                   if i in {x.id for x in catalog.items} and ordered.get(i, 0) != received.get(i, 0)]  # fmt: skip
        return render(
            request,
            "received.html",
            s,
            day=day,
            groups=groups,
            ordered=ordered,
            received=received,
            ordered_on=plan.order_day if plan and plan.approved else None,
            differs=differs,
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
        """실제로 들어온 양으로 고친다. 칸 이름: recv_<품목 id>(+_half), 추가 줄은 add_item_<n> + add_qty_<n>(+_half)."""
        s = current(request)
        step, day = s.service.status()
        if step != SHEET:
            return go("/")
        if s.service.sheet_layout == STORE_LAYOUT:
            return go("/sheet")
        form = await request.form()
        entered = {k: str(v).strip() for k, v in form.items()}
        catalog = s.service.catalog
        known = {i.id for i in catalog.items}
        changes: dict[str, float] = {}
        bad = []
        fields = [(k.removeprefix("recv_"), k) for k in form if k.startswith("recv_") and not k.endswith("_half")]
        for key in form:
            if key.startswith("add_item_") and entered.get(key):
                fields.append((entered[key], f"add_qty_{key.removeprefix('add_item_')}"))
        for item_id, field in fields:
            text, _, qty, failed = read_num(form, field)
            if item_id not in known or failed:
                bad.append(f"{catalog.item(item_id).name if item_id in known else item_id} '{text}'")
                continue
            # 목록의 칸은 들어온 양 그대로, 추가 줄에서 목록에 있는 품목을 또 고르면 더한다
            changes[item_id] = changes.get(item_id, 0.0) + (qty or 0.0)
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
            active="more",
            groups=groups,
            retired=catalog.retired,
            channels=CHANNELS,
            market_choices=MARKET_CHOICES,
            market_value=",".join(str(d) for d in sorted(service.calendar.market_day_digits)),
            closed=set(service.calendar.no_supply_weekdays),
            knowledge=service.knowledge,
            layouts=SHEET_LAYOUTS,
            customized=service.store.catalog() is not None,
            error=error,
            saved=saved,
        )

    @app.get("/settings/print")
    def print_sheet(request: Request):
        """인쇄용 빈 재고표: 품목 이름은 찍혀 있고 숫자 칸은 비어 있다 (두 묶음)."""
        s = current(request)
        items = list(s.service.catalog.items)
        rows = max(30, -(-len(items) // 2))
        return render(
            request,
            "print_sheet.html",
            s,
            left=items[:rows],
            right=items[rows : rows * 2],
            rows=rows,
            store_layout=s.service.sheet_layout == STORE_LAYOUT,
        )

    def _item_page(request: Request, s: Session, item_id: str | None, values: dict[str, str], error: str = ""):
        catalog = s.service.catalog
        return render(
            request,
            "item.html",
            s,
            item_id=item_id,
            item=catalog.item(item_id) if item_id else None,
            active_now=item_id in {i.id for i in catalog.items},
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
                      "base_demand": f"{i.base_demand:g}", "price": f"{i.price:,}", "cost": f"{i.cost:,}",
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
    def save_supplier(
        request: Request,
        name: str = Form(""),
        channel: str = Form(""),
        phone: str = Form(""),
        supplier_id: str = Form(""),
    ):
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        try:
            supplier = s.service.save_supplier(supplier_id or None, name=name, channel=channel, phone=phone)
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
        """장날과 입고 요일. 화면은 "입고 있는 요일"을 고르고, 저장은 입고 없는 요일로 한다."""
        s = current(request)
        denied = _owner_only(s)
        if denied:
            return denied
        form = await request.form()
        market = str(form.get("market") or "")
        try:
            if form.get("supply_form"):  # 화면: 입고 있는 요일에 체크
                supply = [str(v) for v in form.getlist("supply")]
                closed = [w for w in range(7) if str(w) not in supply]
            else:  # 입고 없는 요일을 바로 받는 형식
                values = [str(v) for v in form.getlist("closed")]
                if not all(c.isdigit() for c in values):
                    raise ValueError("요일 값이 잘못됐습니다.")
                closed = [int(c) for c in values]
            if market not in {v for v, _ in MARKET_CHOICES}:
                raise ValueError("장날 값이 잘못됐습니다.")
            s.service.save_calendar(tuple(int(d) for d in market.split(",") if d), tuple(closed))
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
        today = today_kst().isoformat()
        days: dict[str, list[dict]] = {}  # 날짜별로 묶는다 (최신이 앞)
        for n, c in enumerate(calls):
            c["n"] = n
            kind = c["purpose"].split(" ")[0] or "기타"
            c["label"] = PURPOSES.get(kind, kind)
            at = c.get("at") or ""
            c["day"] = "오늘" if at[:10] == today else (at[5:10].replace("-", "/") or "날짜 없음")
            c["time"] = at[11:16]
            out = c.get("output") if isinstance(c.get("output"), dict) else {}
            took = f"{c.get('seconds') or 0:.0f}초"
            if c.get("error"):
                c["detail"], c["result"] = f"{c['error'][:40]} · {took}", "실패"
            elif kind == "sheet":
                rows = out.get("rows") or []
                c["detail"], c["result"] = f"사진 {c.get('images') or 0}장 · {took}", f"{len(rows)}줄 읽음"
            elif kind == "research":
                c["detail"] = took
                c["result"] = "확인됨" if out.get("confirmed") else "확인 못 함"
            else:
                c["detail"], c["result"] = f"{c['purpose'].partition(' ')[2]} · {took}".lstrip(" ·"), ""
            days.setdefault(c["day"], []).append(c)
        catalog = s.service.catalog
        known = {i.id for i in catalog.all_items}
        aliases = [(raw, catalog.item(i).name) for raw, i in sorted(s.service.learned_aliases().items()) if i in known]
        return render(
            request,
            "logs.html",
            s,
            active="more",
            calls=calls,
            days=days,
            corrections=s.service.store.correction_stats(),
            aliases=aliases,
            llm_log=path,
        )

    @app.get("/logs/{n}")
    def log_detail(request: Request, n: int):
        s = current(request)
        calls = read_llm_log(log_path(s.store_id))
        if not 0 <= n < len(calls):
            return go("/logs")
        call = calls[n]
        kind = call["purpose"].split(" ")[0]
        return render(
            request,
            "log_detail.html",
            s,
            active="more",
            call=call,
            label=PURPOSES.get(kind, kind),
            n=n,
            output=json.dumps(call["output"], ensure_ascii=False, indent=2) if call["output"] is not None else "",
        )

    return app
