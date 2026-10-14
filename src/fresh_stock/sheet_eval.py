"""재고표 인식 평가.

정답이 있는 합성 재고표로 인식 파이프라인을 잰다. 가장 중요한 숫자는
"확인 표시 없이 틀린 값이 들어간 줄"이다. 표시된 줄은 사람이 고치지만, 표시가 없으면 그대로 발주에 쓰인다.

재고표 형식은 두 가지다.
- simple: 품목과 남은 수량만 세 단으로 적은 표 (처음 만든 합성 재고표)
- store: 실제 가게 재고표 형식. [품목 | 남은 수량 | 사입 수량 | 사입 가격] 네 칸 묶음이 두 개, 30줄이 꽉 찬 표
- excel: store와 같은 칸을 엑셀로 인쇄하고(칸 선·머리글·품목 이름) 숫자만 손으로 적어 휴대폰으로 찍은 사진
"""

import json
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .accounting import regular_sales_series
from .catalog import Catalog
from .llm import ImageInput, LLMClient, LLMError
from .recognition import RecognizedRow, recognize_sheet
from .sheets import (
    WRITERS,
    SheetEntry,
    SheetTruth,
    Writer,
    render_excel_sheet,
    render_sheet,
    render_store_sheet,
)
from .simulator import SimulationResult


@dataclass(frozen=True)
class SheetScore:
    day: date
    written: int  # 종이에 적힌 품목 줄 수
    correct: int  # 품목과 수량이 모두 맞은 줄
    wrong_qty: int  # 품목은 맞았는데 수량이 틀린 줄
    extra: int  # 적혀 있지 않은 품목으로 인식한 줄 (잘못 매칭 포함)
    missed: int  # 적혀 있는데 인식 결과에 없는 품목
    errors_flagged: int  # 틀린 줄(수량 틀림 + 잘못 매칭) 중 확인 표시가 붙은 줄
    correct_flagged: int  # 맞았는데 확인 표시가 붙은 줄
    failed: bool = False  # LLM 호출 실패
    # 사입 칸 (store 형식을 사입 칸까지 읽었을 때): 품목을 찾은 줄 중 사입 수량·가격이 모두 맞은 줄
    buy_checked: int = 0
    buy_correct: int = 0
    buy_errors_flagged: int = 0

    @property
    def errors(self) -> int:
        return self.wrong_qty + self.extra

    @property
    def silent_errors(self) -> int:
        """확인 표시 없이 틀린 값이 들어가는 줄 (누락 포함)."""
        return self.errors - self.errors_flagged + self.missed


def _buy_ok(row: RecognizedRow, entry: SheetEntry) -> bool:
    return (row.buy_qty or 0.0) == entry.buy_qty and (row.buy_price or None) == entry.buy_price


def score_sheet(rows: list[RecognizedRow], truth: SheetTruth, purchases: bool = False) -> SheetScore:
    expected = truth.written
    entries = {e.item_id: e for e in truth.entries}
    matched: set[str] = set()
    correct = wrong_qty = extra = errors_flagged = correct_flagged = 0
    buy_checked = buy_correct = buy_errors_flagged = 0
    for row in rows:
        if row.item_id in expected and row.item_id not in matched:
            matched.add(row.item_id)
            if purchases:
                buy_checked += 1
                if _buy_ok(row, entries[row.item_id]):
                    buy_correct += 1
                else:
                    buy_errors_flagged += row.needs_review
            if row.qty is not None and abs(row.qty - expected[row.item_id]) < 1e-9:
                correct += 1
                correct_flagged += row.needs_review
            else:
                wrong_qty += 1
                errors_flagged += row.needs_review
        else:
            extra += 1
            errors_flagged += row.needs_review
    return SheetScore(
        day=truth.day,
        written=len(expected),
        correct=correct,
        wrong_qty=wrong_qty,
        extra=extra,
        missed=len(expected) - len(matched),
        errors_flagged=errors_flagged,
        correct_flagged=correct_flagged,
        buy_checked=buy_checked,
        buy_correct=buy_correct,
        buy_errors_flagged=buy_errors_flagged,
    )


def store_purchases(result: SimulationResult, idx: int, seed: int) -> dict[str, tuple[float, int]]:
    """그날 사 온 양(입고)과 단위당 사입 가격. 가격은 평소 매입가에서 ±15% 오르내린다."""
    rng = random.Random(seed)
    purchases = {}
    for item in result.catalog.items:
        qty = result.ledger[idx].received.get(item.id, 0)
        price = int(round(item.cost * rng.uniform(0.85, 1.15), -2 if item.cost >= 1000 else -1))
        if qty > 0:
            purchases[item.id] = (qty, price)
    return purchases


@dataclass(frozen=True)
class SheetCase:
    day: date
    image: bytes
    truth: SheetTruth
    max_possible: dict[str, float]
    expected: dict[str, float]
    # 사입 칸이 있는 형식만: 어제 재고와 그날 들어오기로 한 양 (실제 서비스처럼 확인 표시를 붙이는 데 쓴다)
    prev_closing: dict[str, float] = field(default_factory=dict)
    ordered: dict[str, float] = field(default_factory=dict)


def build_cases(
    result: SimulationResult,
    n: int,
    seed: int,
    store_abbr: bool = False,
    layout: str = "simple",
    writer: Writer | None = None,
    first: int = 1,
) -> list[SheetCase]:
    """시뮬레이션 기록에서 n일을 골라 재고표 이미지를 만든다. layout은 simple, store, excel (모듈 설명 참고)."""
    ledger = result.ledger
    sales = regular_sales_series(ledger, result.catalog)
    step = max(1, (len(ledger) - 1) // n)
    cases = []
    for k, idx in enumerate(range(first, len(ledger), step)):
        if len(cases) == n:
            break
        rec, prev = ledger[idx], ledger[idx - 1]
        if layout in ("store", "excel"):
            purchases = store_purchases(result, idx, seed * 1000 + k)
            if layout == "store":
                image, truth = render_store_sheet(result.catalog, rec.day, rec.closing, purchases, seed=seed * 1000 + k)
            else:
                image, truth = render_excel_sheet(
                    result.catalog, rec.day, rec.closing, purchases, seed=seed * 1000 + k, writer=writer
                )
        else:
            image, truth = render_sheet(result.catalog, rec.day, rec.closing, seed=seed * 1000 + k, store_abbr=store_abbr)
        max_possible = {
            i.id: prev.closing.get(i.id, 0) + rec.received.get(i.id, 0) for i in result.catalog.items
        }
        recent = sales[max(0, idx - 7) : idx]
        expected = {
            i: max_possible[i] - sum(s.get(i, 0) for s in recent) / len(recent) for i in max_possible
        }
        cases.append(SheetCase(rec.day, image, truth, max_possible, expected, dict(prev.closing), dict(rec.received)))
    return cases


def build_writer_cases(result: SimulationResult, n: int, seed: int) -> list[tuple[Writer, SheetCase]]:
    """쓰는 사람(WRITERS)마다 엑셀 양식 재고표 n장. 사람마다 다른 날을 고른다 (날짜가 겹치지 않게)."""
    cases = []
    for k, writer in enumerate(WRITERS):
        for case in build_cases(result, n, seed + k, layout="excel", writer=writer, first=1 + 2 * k):
            cases.append((writer, case))
    return cases


def run_eval(
    llm: LLMClient,
    catalog: Catalog,
    cases: list[SheetCase],
    out: Path,
    workers: int = 3,
    aliases: dict[str, str] | None = None,
    purchases: bool = False,
) -> list[SheetScore]:
    out.mkdir(parents=True, exist_ok=True)

    def one(case: SheetCase) -> SheetScore:
        (out / f"{case.day}.jpg").write_bytes(case.image)
        try:
            rows = recognize_sheet(
                llm,
                ImageInput(case.image),
                catalog,
                case.max_possible,
                case.expected,
                purpose=f"sheet {case.day}",
                aliases=aliases,
                purchases=purchases,
                prev_closing=case.prev_closing if purchases else None,
                ordered=case.ordered if purchases else None,
            )
        except LLMError as e:
            (out / f"{case.day}.json").write_text(json.dumps({"error": str(e)}, ensure_ascii=False), encoding="utf-8")
            n = len(case.truth.entries)
            return SheetScore(case.day, n, 0, 0, 0, n, 0, 0, failed=True)
        score = score_sheet(rows, case.truth, purchases)
        report = {
            "score": asdict(score),
            "truth": [asdict(e) for e in case.truth.entries],
            "rows": [asdict(r) for r in rows],
        }
        (out / f"{case.day}.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8"
        )
        return score

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, cases))


class ReplayLLM:
    """LLM 호출 기록(JSONL)에 남은 응답을 그대로 돌려준다. 수량 해석·확인 표시 코드를 고친 뒤 LLM을 다시 부르지 않고 다시 채점한다."""

    model = "replay"

    def __init__(self, log_path: Path):
        self.outputs: dict[str, dict] = {}
        for line in log_path.read_text(encoding="utf-8").splitlines():
            call = json.loads(line)
            if call.get("output") is not None:
                self.outputs[call["purpose"]] = call["output"]
        self.calls: list = []

    def generate_json(self, system, prompt, schema, purpose="", images=()):
        if purpose not in self.outputs:
            raise LLMError(f"기록에 없는 호출: {purpose}")
        return self.outputs[purpose]


def summarize(scores: list[SheetScore]) -> dict[str, float]:
    ok = [s for s in scores if not s.failed]
    written = sum(s.written for s in ok)
    correct = sum(s.correct for s in ok)
    errors = sum(s.errors for s in ok)
    summary: dict[str, float] = {
        "재고표 수": len(scores),
        "호출 실패": len(scores) - len(ok),
        "적힌 줄": written,
        "줄 정확도": correct / written if written else 0.0,
        "틀린 줄": errors,
        "틀린 줄 중 확인 표시": sum(s.errors_flagged for s in ok) / errors if errors else 1.0,
        "누락 줄": sum(s.missed for s in ok),
        "표시 없이 틀린 줄": sum(s.silent_errors for s in ok),
        "맞았는데 표시된 줄 비율": sum(s.correct_flagged for s in ok) / correct if correct else 0.0,
    }
    checked = sum(s.buy_checked for s in ok)
    if checked:
        buy_errors = checked - sum(s.buy_correct for s in ok)
        summary.update({
            "사입 칸 확인한 줄": checked,
            "사입 칸 정확도": sum(s.buy_correct for s in ok) / checked,
            "사입 칸 틀린 줄": buy_errors,
            "사입 칸 표시 없이 틀린 줄": buy_errors - sum(s.buy_errors_flagged for s in ok),
        })  # fmt: skip
    return summary
