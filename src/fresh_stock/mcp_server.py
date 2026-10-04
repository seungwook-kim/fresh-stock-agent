"""MCP 서버: Claude Code, Claude Desktop 같은 AI 도구가 가게 업무를 도구로 쓰게 한다.

    FRESH_STOCK_EMAIL=boss@gmail.com uv run fresh-stock mcp      # stdio

FRESH_STOCK_EMAIL이 멤버인 가게만 다룬다 (웹 화면과 같은 규칙). 도구는 웹 화면의 업무 흐름을 그대로 따른다:
오늘 할 일 → 메모 → 재고표 읽기·확정 → 발주 추천 → 사람 승인 → 거래처 문자.

발주 승인은 사용자에게 추천을 보여주고 승인을 받았다는 표시(user_confirmed)가 있어야만 하고,
입력 실수 가드레일도 웹과 똑같이 걸린다. 수량 계산은 모두 코드가 한다.
"""

from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .recognition import parse_qty
from .research import match_item
from .market_calendar import today_kst
from .service import ORDER, SHEET, StoreService
from .store import OWNER_NOTE, Database, Note

INSTRUCTIONS = """청과 매장 재고·발주 도구다. 하루 업무 순서: today로 할 일 확인 → 사장님 메모 추가(add_note) →
마감 재고표 사진 읽기(read_sheet) → 확인 필요한 줄만 사용자에게 확인받아 확정(confirm_sheet) → 발주 추천 보기(get_order_plan) →
사용자에게 보여주고 승인받은 뒤 approve_order → 거래처 문자(get_messages).
today의 days_behind가 1 이상이면 할 일 날짜가 오늘보다 앞선 것이다. 그날 가게를 쉬었는지 물어보고 skip_day로 넘긴다.
수량은 모두 코드가 계산한다. 발주 수량을 직접 지어내지 말고 추천을 그대로 보여준 뒤 사용자가 바꾸라는 것만 바꾼다."""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITES = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
WEB = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MEDIA_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


def _note_dict(n: Note, service: StoreService) -> dict:
    d = {"note_id": n.id, "day": n.day.isoformat(), "text": n.text, "source": n.source}
    if n.research is not None:
        d["ai_check"] = {
            "confirmed": n.research.confirmed,
            "summary": n.research.summary,
            "items": [service.catalog.item(i).name for i in n.research.items],
            "sources": [{"title": t, "url": u} for t, u in n.research.sources],
        }
    elif n.source == OWNER_NOTE:
        d["ai_check"] = "아직 확인 전 (발주 추천을 만들 때 AI가 웹 검색으로 확인)"
    return d


def build_server(db: Database, make_service: Callable[[int], StoreService], email: str) -> MCPServer:
    email = email.strip().lower()
    server = MCPServer("fresh-stock", instructions=INSTRUCTIONS)
    services: dict[int, StoreService] = {}

    def use(store_id: int | None) -> tuple[int, StoreService]:
        """다룰 가게. 가게가 하나면 store_id를 생략할 수 있다. 멤버가 아닌 가게는 막는다."""
        memberships = db.memberships(email)
        if store_id is None:
            if len(memberships) != 1:
                names = ", ".join(f"{m.store_id}: {m.store_name}" for m in memberships) or "없음"
                raise ToolError(f"가게를 정해 주세요 (store_id). {email}의 가게: {names}")
            store_id = memberships[0].store_id
        if db.role(store_id, email) is None:
            raise ToolError(f"{email}은(는) 가게 {store_id}의 멤버가 아닙니다.")
        if store_id not in services:
            services[store_id] = make_service(store_id)
        return store_id, services[store_id]

    @server.tool(annotations=READ_ONLY)
    def list_stores() -> dict:
        """로그인한 계정(FRESH_STOCK_EMAIL)이 속한 가게 목록."""
        return {
            "email": email,
            "stores": [{"store_id": m.store_id, "name": m.store_name, "role": m.role} for m in db.memberships(email)],
        }

    @server.tool(annotations=READ_ONLY)
    def today(store_id: int | None = None) -> dict:
        """오늘 할 일(재고표를 올릴 차례인지, 발주를 확인할 차례인지)과 앞으로의 가게 메모."""
        sid, service = use(store_id)
        step, day = service.status()
        action = "마감 재고표 사진을 읽고(read_sheet) 확정(confirm_sheet)" if step == SHEET else "발주 추천 확인(get_order_plan) 후 승인"
        result = {
            "store": db.store_name(sid),
            "today": today_kst().isoformat(),
            "step": "재고표" if step == SHEET else "발주",
            "day": day.isoformat(),
            "days_behind": service.days_behind(),
            "next_action": action,
            "notes": [_note_dict(n, service) for n in service.store.note_records(start=day)],
        }
        if result["days_behind"]:
            result["warning"] = (
                f"할 일 날짜({day})가 오늘보다 앞섭니다. 그날 가게를 쉬었다면 사용자 확인 후 skip_day로 넘기세요. "
                f"그대로 재고표를 올리면 오늘 재고가 {day} 기록으로 저장됩니다."
            )
        return result

    @server.tool(annotations=WRITES)
    def skip_day(user_confirmed: bool, store_id: int | None = None) -> dict:
        """가게를 쉰 날을 건너뛴다. 재고표 차례면 그날을 판매 없는 날로, 발주 차례면 발주 없이 넘긴다.
        사용자가 그날 쉬었다(또는 주문하지 않았다)고 확인했을 때만 user_confirmed=true."""
        if not user_confirmed:
            raise ToolError("사용자에게 그날 가게를 쉬었는지 확인한 뒤 user_confirmed=true로 불러 주세요.")
        _, service = use(store_id)
        try:
            step, day = service.skip()
        except ValueError as e:
            raise ToolError(str(e)) from e
        next_step, next_day = service.status()
        return {
            "skipped": f"{day} {'재고표 (쉬는 날)' if step == SHEET else '발주 (발주 없음)'}",
            "now": {"step": "재고표" if next_step == SHEET else "발주", "day": next_day.isoformat()},
            "days_behind": service.days_behind(),
        }

    @server.tool(annotations=READ_ONLY)
    def list_notes(from_day: str | None = None, store_id: int | None = None) -> dict:
        """가게 메모와 AI 확인 결과. from_day(YYYY-MM-DD)를 주지 않으면 오늘 할 일 날짜부터."""
        _, service = use(store_id)
        start = date.fromisoformat(from_day) if from_day else service.status()[1]
        return {"notes": [_note_dict(n, service) for n in service.store.note_records(start=start)]}

    @server.tool(annotations=WRITES)
    def add_note(day: str, text: str, store_id: int | None = None) -> dict:
        """사장님 메모를 남긴다. day는 그 일이 수요에 영향을 줄 날(YYYY-MM-DD). 예: 내일 입고분에 영향을 줄
        "어제 TV에서 양배추가 몸에 좋다고 나왔대". 발주 추천을 만들 때 AI가 웹 검색으로 확인한다."""
        _, service = use(store_id)
        if not text.strip():
            raise ToolError("메모 내용이 비어 있습니다.")
        note_id = service.store.add_note(date.fromisoformat(day), text, author=email)
        return {"note_id": note_id, "day": day, "text": text.strip()}

    @server.tool(annotations=WEB)
    def check_note(note_id: int, store_id: int | None = None) -> dict:
        """사장님 메모 하나를 지금 바로 AI가 웹 검색으로 확인한다 (30초~3분). 보통은 발주 추천 때 자동으로 한다."""
        _, service = use(store_id)
        note = next((n for n in service.store.note_records() if n.id == note_id), None)
        if note is None or note.source != OWNER_NOTE:
            raise ToolError("이 가게의 사장님 메모가 아닙니다.")
        if service.research_llm_factory is None:
            raise ToolError("웹 검색이 되는 AI가 설정되지 않았습니다 (claude-code 백엔드 필요).")
        service.research_notes([note])
        return _note_dict(next(n for n in service.store.note_records() if n.id == note_id), service)

    @server.tool(annotations=WRITES)
    def read_sheet(image_path: str, store_id: int | None = None) -> dict:
        """마감 재고표 사진(jpg/png 파일 경로)을 AI가 읽는다. needs_check가 있는 줄만 사용자에게 사진과 비교해
        달라고 한다. 읽은 결과는 confirm_sheet로 확정해야 저장된다."""
        _, service = use(store_id)
        step, day = service.status()
        if step != SHEET:
            raise ToolError(f"지금은 {day} 발주를 확인할 차례입니다. 재고표는 발주를 승인한 뒤 올립니다.")
        path = Path(image_path)
        if not path.is_file():
            raise ToolError(f"파일이 없습니다: {image_path}")
        media = MEDIA_TYPES.get(path.suffix.lower())
        if media is None:
            raise ToolError("jpg, png, webp 사진만 읽을 수 있습니다.")
        rows = service.read_sheet(day, path.read_bytes(), media)
        return {
            "day": day.isoformat(),
            "rows": [
                {
                    "line": r.idx,
                    "written": f"{r.raw_name} {r.raw_qty}",
                    "item": service.catalog.item(r.item_id_read).name if r.item_id_read else None,
                    "qty": r.qty_read,
                    "needs_check": list(r.flags),
                }
                for r in rows
            ],
            "needs_check_count": sum(bool(r.flags) for r in rows),
        }

    @server.tool(annotations=WRITES)
    def confirm_sheet(
        cash: int, card: int, corrections: list[dict] | None = None, store_id: int | None = None
    ) -> dict:
        """마감 재고를 확정한다. cash·card는 오늘 돈통 현금과 카드 합계 (사용자에게 물어서 넣는다).

        read_sheet로 읽은 줄은 그대로 쓰고, corrections에는 사용자가 확인하거나 고친 줄만 넣는다:
        [{"line": 3, "qty": "4반"}, {"line": 7, "item": "사과", "qty": "2"}, {"item": "참외", "qty": "1"}].
        line은 read_sheet의 줄 번호, line 없이 item만 주면 그 품목 줄을 고치거나 새 줄로 더한다.
        needs_check가 있는 줄은 모두 corrections로 확인받아야 저장된다 (읽은 값이 맞으면 그 값 그대로 넣는다).
        재고표를 읽지 않았으면 corrections가 재고표 전체이고, 적지 않은 품목은 재고 0이다."""
        _, service = use(store_id)
        step, day = service.status()
        if step != SHEET:
            raise ToolError(f"지금은 {day} 발주를 확인할 차례입니다.")
        if cash < 0 or card < 0:
            raise ToolError("현금·카드 합계는 0 이상이어야 합니다.")
        sheet = {r.idx: r for r in service.store.sheet_rows(day)}
        if not sheet and not corrections:
            raise ToolError("읽은 재고표가 없습니다. read_sheet로 사진을 읽거나 corrections에 재고 전체를 넣어 주세요.")
        known_ids = {i.id for i in service.catalog.items}
        finals: dict[int, tuple[str | None, float | None]] = {
            idx: (r.item_id_read, r.qty_read) for idx, r in sheet.items()
        }
        checked: set[int] = set()
        errors = []
        next_idx = max(sheet, default=-1) + 1
        for c in corrections or []:
            name, qty_text = str(c.get("item") or "").strip(), str(c.get("qty", "")).strip()
            qty = parse_qty(qty_text)
            item_id = (match_item(name, service.catalog) or (name if name in known_ids else None)) if name else None
            line = c.get("line")
            if line is not None:
                if int(line) not in sheet:
                    errors.append(f"없는 줄 번호 {line}")
                    continue
                idx = int(line)
                item_id = item_id or (None if name else sheet[idx].item_id_read)
            elif item_id is None:
                errors.append(f"{name} {qty_text}")
                continue
            else:
                # 읽은 줄 중 같은 품목을 고친다. 없으면 빠뜨린 품목을 새 줄로 더한다
                idx = next((i for i, r in sheet.items() if r.item_id_read == item_id and i not in checked), None)
                if idx is None:
                    idx, next_idx = next_idx, next_idx + 1
            if qty is None or (item_id is None and qty != 0):
                errors.append(f"{name or f'{line}번 줄'} {qty_text}")
                continue
            finals[idx] = (item_id, qty)
            checked.add(idx)
        if errors:
            raise ToolError(f"품목이나 수량을 알 수 없습니다: {', '.join(errors)}. 아무것도 저장하지 않았습니다.")
        unchecked = [r for idx, r in sheet.items() if r.flags and idx not in checked]
        if unchecked:
            lines = ", ".join(f"{r.idx}번 '{r.raw_name} {r.raw_qty}'" for r in unchecked)
            raise ToolError(
                f"확인이 필요한 줄을 아직 확인받지 않았습니다: {lines}. 사용자에게 사진과 비교해 맞는 값을 받아 "
                "corrections에 line과 함께 넣어 주세요. 아무것도 저장하지 않았습니다."
            )
        rows = list(sheet.values())
        missing = service.missing_items(day, rows) if rows else []
        closing = service.confirm_sheet(day, finals, cash, card)
        next_step, _ = service.status()
        return {
            "day": day.isoformat(),
            "saved_items": sum(1 for q in closing.values() if q > 0),
            "zero_but_expected": [service.catalog.item(i).name for i in missing if closing.get(i, 0) == 0],
            "next_action": "get_order_plan으로 발주 추천 확인" if next_step == ORDER else "내일 재고표",
        }

    @server.tool(annotations=WEB)
    def get_order_plan(store_id: int | None = None) -> dict:
        """발주 추천. 처음 부르면 날씨·명절 메모를 가져오고, 확인하지 않은 사장님 메모를 AI가 웹 검색으로 확인한 뒤
        추천을 만든다 (메모가 있으면 1~3분). 사용자에게 바뀐 품목과 이유를 보여주고 승인을 받아야 한다."""
        _, service = use(store_id)
        step, day = service.status()
        if step != ORDER:
            raise ToolError(f"지금은 {day} 재고표를 올릴 차례입니다.")
        plan = service.plan_for(day)
        catalog = service.catalog
        stock = service.store.closing(day)
        cover = service.calendar.cover_days(plan.arrival)
        order = {item.id: n for n, item in enumerate(catalog.items)}
        lines = sorted(plan.lines, key=lambda ln: order[ln.item_id])
        return {
            "order_day": day.isoformat(),
            "arrival": plan.arrival.isoformat(),
            "agent_summary": plan.summary,
            "notes": [_note_dict(n, service) for n in service.store.note_records(cover[0], cover[-1])],
            "changed_by_agent": [
                {"item": catalog.item(ln.item_id).name, "rule": ln.base, "recommended": ln.recommended,
                 "demand_factor": ln.factors, "reason": ln.reason}
                for ln in lines if ln.recommended != ln.base
            ],  # fmt: skip
            "similar_past_days": service.similar_days(day),
            "lines": [
                {"item": catalog.item(ln.item_id).name, "unit": catalog.item(ln.item_id).unit,
                 "stock": stock.get(ln.item_id, 0), "recommended": ln.recommended}
                for ln in lines if ln.recommended > 0 or stock.get(ln.item_id, 0) > 0
            ],  # fmt: skip
        }

    @server.tool(annotations=WRITES)
    def approve_order(
        user_confirmed: bool,
        changes: dict[str, int] | None = None,
        accept_warnings: bool = False,
        store_id: int | None = None,
    ) -> dict:
        """발주를 승인하고 거래처 문자를 돌려준다. 사용자가 추천을 보고 승인한다고 말했을 때만 user_confirmed=true.
        changes는 사용자가 바꾸라고 한 품목만 {품목 이름: 수량}. 입력 실수 경고가 나오면 사용자에게 보여주고,
        그대로 하겠다고 하면 accept_warnings=true로 다시 부른다."""
        if not user_confirmed:
            raise ToolError("발주 추천을 사용자에게 보여주고 승인을 받은 뒤 user_confirmed=true로 불러 주세요.")
        _, service = use(store_id)
        step, day = service.status()
        if step != ORDER:
            raise ToolError(f"지금은 {day} 재고표를 올릴 차례입니다.")
        plan = service.plan_for(day)
        finals = {ln.item_id: ln.recommended for ln in plan.lines}
        unknown = []
        for name, qty in (changes or {}).items():
            item_id = match_item(name, service.catalog)
            if item_id is None or int(qty) < 0:
                unknown.append(name)
            else:
                finals[item_id] = int(qty)
        if unknown:
            raise ToolError(f"알 수 없는 품목이나 음수 수량: {', '.join(unknown)}. 승인하지 않았습니다.")
        warnings = service.check_finals(day, finals)
        if warnings and not accept_warnings:
            return {
                "approved": False,
                "warnings": {service.catalog.item(i).name: w for i, w in warnings.items()},
                "next_action": "경고를 사용자에게 보여주고, 그대로 하겠다고 하면 accept_warnings=true로 다시 호출",
            }
        service.approve(day, finals)
        return {"approved": True, "arrival": plan.arrival.isoformat(), "messages": _messages(service, day)}

    def _messages(service: StoreService, day: date) -> list[dict]:
        return [{"supplier": m.supplier.name, "channel": m.supplier.channel, "text": m.text} for m in service.messages(day)]

    @server.tool(annotations=READ_ONLY)
    def get_messages(order_day: str | None = None, store_id: int | None = None) -> dict:
        """승인한 발주의 거래처별 문자·구매 목록. order_day(YYYY-MM-DD)를 주지 않으면 가장 최근 승인한 발주."""
        _, service = use(store_id)
        if order_day:
            day = date.fromisoformat(order_day)
        else:
            day = None
            last = service.store.last_closed_day()
            for back in range(8) if last else ():
                plan = service.store.plan(last - timedelta(days=back))
                if plan is not None and plan.approved:
                    day = plan.order_day
                    break
            if day is None:
                raise ToolError("최근 승인한 발주가 없습니다.")
        messages = _messages(service, day)
        if not messages:
            raise ToolError(f"{order_day or day} 에 승인한 발주가 없습니다.")
        return {"order_day": day.isoformat(), "messages": messages}

    @server.tool(annotations=READ_ONLY)
    def revenue(days: int = 7, store_id: int | None = None) -> dict:
        """최근 매출: 실제(현금+카드)와 재고로 역산한 추정 매출, 그 차이 (할인·기록 실수 추적용)."""
        _, service = use(store_id)
        return {
            "days": [
                {"day": r.day.isoformat(), "actual": r.actual, "estimated": r.estimated, "gap": r.gap}
                for r in service.revenue(days=days)
            ]
        }

    @server.tool(annotations=READ_ONLY)
    def list_items(store_id: int | None = None) -> dict:
        """취급 품목 목록 (이름, 단위, 분류, 거래처)."""
        _, service = use(store_id)
        return {
            "items": [
                {"name": i.name, "unit": i.unit, "category": i.category, "supplier": service.catalog.supplier(i.supplier_id).name}
                for i in service.catalog.items
            ]
        }

    return server
