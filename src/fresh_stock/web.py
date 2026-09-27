"""모바일 웹 화면 (FastAPI + Jinja2).

    홈 → 재고표 사진 → 확인·수정 → 발주 추천 → 승인 → 거래처 문자
"""

import random
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from .demo import demo_sheet_image
from .llm import LLMError
from .recognition import parse_qty
from .service import ORDER, StoreService

WEEKDAYS = "월화수목금토일"


def _fmt_day(d: date) -> str:
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def _won(v: float) -> str:
    return f"{round(v):,}원"


def create_app(service: StoreService) -> FastAPI:
    app = FastAPI(title="fresh-stock")
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    templates.env.filters["day"] = _fmt_day
    templates.env.filters["won"] = _won
    catalog = service.catalog

    def render(request: Request, name: str, **context):
        step, day = service.status()
        return templates.TemplateResponse(
            request,
            name,
            {"catalog": catalog, "step": step, "status_day": day, "store_name": service.store_name, **context},
        )

    def go(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # 홈 ---------------------------------------------------------------------------

    @app.get("/")
    def home(request: Request):
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
            notes=service.store.notes(start=day),
            revenue=service.revenue(days=7),
            corrections=service.store.correction_stats(),
            last_plan=last_plan,
            default_note_day=(day + timedelta(days=1)).isoformat(),
        )

    @app.post("/notes")
    def add_note(day: date = Form(...), text: str = Form(...)):
        if text.strip():
            service.store.add_note(day, text)
        return go("/")

    @app.post("/notes/{note_id}/delete")
    def delete_note(note_id: int):
        service.store.delete_note(note_id)
        return go("/")

    # 재고표 -------------------------------------------------------------------------

    @app.get("/sheet")
    def sheet(request: Request, error: str = ""):
        _, day = service.status()
        return render(request, "sheet.html", day=day, has_rows=bool(service.store.sheet_rows(day)), error=error)

    def _read(day: date, image: bytes, media_type: str):
        try:
            service.read_sheet(day, image, media_type)
        except LLMError as e:
            return go("/sheet?" + urlencode({"error": f"재고표를 읽지 못했습니다: {str(e)[:80]}"}))
        return go("/sheet/review")

    @app.post("/sheet")
    async def upload_sheet(photo: UploadFile = File(...)):
        _, day = service.status()
        data = await photo.read()
        return _read(day, data, photo.content_type or "image/jpeg")

    @app.post("/sheet/demo")
    def demo_sheet():
        _, day = service.status()
        return _read(day, demo_sheet_image(service, day, seed=random.randint(0, 10**6)), "image/jpeg")

    @app.get("/sheet/image/{day}")
    def sheet_image(day: date):
        data = service.store.sheet_image(day)
        return Response(data or b"", media_type="image/jpeg")

    def _review_page(
        request: Request,
        day: date,
        entered: dict[int, tuple[str, str]] | None = None,
        bad: frozenset[int] = frozenset(),
        error: str = "",
        money: tuple[str, str] = ("", ""),
    ):
        """재고표 확인 화면. 확정에 실패하면 사람이 입력한 값(entered)을 그대로 다시 보여준다."""
        rows = service.store.sheet_rows(day)
        max_possible, _ = service.sheet_bounds(day)
        return render(
            request,
            "review.html",
            day=day,
            rows=rows,
            flagged=sum(bool(r.flags) for r in rows),
            missing=[catalog.item(i) for i in service.missing_items(day, rows)],
            max_possible=max_possible,
            entered=entered or {},
            bad=bad,
            error=error,
            cash=money[0],
            card=money[1],
        )

    @app.get("/sheet/review")
    def review(request: Request):
        _, day = service.status()
        if not service.store.sheet_rows(day):
            return go("/sheet")
        return _review_page(request, day)

    @app.post("/sheet/confirm")
    async def confirm(request: Request):
        _, day = service.status()
        form = await request.form()
        rows = {r.idx: r for r in service.store.sheet_rows(day)}
        entered: dict[int, tuple[str, str]] = {}
        finals: dict[int, tuple[str | None, float | None]] = {}
        bad = set()
        for key, value in form.items():
            if not key.startswith("item_"):
                continue
            idx = int(key.removeprefix("item_"))
            item_id = str(value) or None
            qty_text = str(form.get(f"qty_{idx}", "")).strip()
            entered[idx] = (item_id or "", qty_text)
            if item_id is None and not qty_text:
                continue
            qty = parse_qty(qty_text) if qty_text else 0.0
            if item_id is None or qty is None:
                bad.add(idx)
            finals[idx] = (item_id, qty)
        cash_text = str(form.get("cash") or "0").replace(",", "").strip() or "0"
        card_text = str(form.get("card") or "0").replace(",", "").strip() or "0"
        if not (cash_text.isdigit() and card_text.isdigit()):
            money = (str(form.get("cash") or ""), str(form.get("card") or ""))
            return _review_page(request, day, entered, frozenset(bad), "현금·카드 합계는 숫자로 입력해 주세요.", money)
        if bad:
            names = [f"{rows[i].raw_name} {rows[i].raw_qty}" if i in rows else "추가한 줄" for i in sorted(bad)]
            error = f"빨간 줄({', '.join(names)})의 품목이나 수량을 확인해 주세요."
            return _review_page(request, day, entered, frozenset(bad), error, (cash_text, card_text))
        cash, card = int(cash_text), int(card_text)
        service.confirm_sheet(day, finals, cash, card)
        step, _ = service.status()
        return go("/order" if step == ORDER else "/")

    # 발주 ---------------------------------------------------------------------------

    def _order_page(request: Request, day: date, entered: dict[str, int] | None = None, warnings=None):
        plan = service.plan_for(day)
        ctx = service.order_context(day)
        stock = service.store.closing(day)
        order = {item.id: n for n, item in enumerate(catalog.items)}
        all_lines = sorted(plan.lines, key=lambda ln: order[ln.item_id])
        groups = []
        for supplier in catalog.suppliers:
            lines = [ln for ln in all_lines if catalog.item(ln.item_id).supplier_id == supplier.id]
            if lines:
                groups.append((supplier, lines))
        # 에이전트 요약은 LLM이 쓴 글이라 실제 결과와 다를 수 있다. 수량이 바뀐 품목은 코드로 따로 보여준다
        changed = [ln for ln in all_lines if ln.recommended != ln.base]
        return render(
            request,
            "order.html",
            day=day,
            plan=plan,
            groups=groups,
            changed=changed,
            stock=stock,
            notes=ctx.notes if ctx else {},
            entered=entered or {ln.item_id: ln.final for ln in plan.lines},
            warnings=warnings or {},
        )

    @app.get("/order")
    def order(request: Request):
        step, day = service.status()
        if step != ORDER:
            return go("/")
        return _order_page(request, day)

    @app.post("/order/refresh")
    def refresh():
        step, day = service.status()
        if step == ORDER:
            service.make_plan(day)
        return go("/order")

    @app.post("/order")
    async def approve(request: Request):
        step, day = service.status()
        if step != ORDER:
            return go("/")
        form = await request.form()
        finals = {}
        for key, value in form.items():
            if key.startswith("final_"):
                text = str(value).strip() or "0"
                finals[key.removeprefix("final_")] = int(text) if text.isdigit() else -1
        invalid = {i: "0 이상의 정수로 입력해 주세요." for i, q in finals.items() if q < 0}
        warnings = invalid or service.check_finals(day, finals)
        if invalid or (warnings and form.get("confirmed") != "yes"):
            return _order_page(request, day, finals, warnings)
        service.approve(day, finals)
        return go(f"/messages?day={day.isoformat()}")

    # 거래처 문자 ----------------------------------------------------------------------

    @app.get("/messages")
    def messages(request: Request, day: date, greeting: bool = False):
        return render(
            request, "messages.html", day=day, messages=service.messages(day, greeting=greeting), greeting=greeting
        )

    return app

