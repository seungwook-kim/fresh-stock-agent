"""재고표 인식 평가.

정답이 있는 합성 재고표로 인식 파이프라인을 잰다. 가장 중요한 숫자는
"확인 표시 없이 틀린 값이 들어간 줄"이다. 표시된 줄은 사람이 고치지만, 표시가 없으면 그대로 발주에 쓰인다.
"""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

from .accounting import regular_sales_series
from .catalog import Catalog
from .llm import ImageInput, LLMClient, LLMError
from .recognition import RecognizedRow, recognize_sheet
from .sheets import SheetTruth, render_sheet
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

    @property
    def errors(self) -> int:
        return self.wrong_qty + self.extra

    @property
    def silent_errors(self) -> int:
        """확인 표시 없이 틀린 값이 들어가는 줄 (누락 포함)."""
        return self.errors - self.errors_flagged + self.missed


def score_sheet(rows: list[RecognizedRow], truth: SheetTruth) -> SheetScore:
    expected = truth.written
    matched: set[str] = set()
    correct = wrong_qty = extra = errors_flagged = correct_flagged = 0
    for row in rows:
        if row.item_id in expected and row.item_id not in matched:
            matched.add(row.item_id)
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
    )


@dataclass(frozen=True)
class SheetCase:
    day: date
    image: bytes
    truth: SheetTruth
    max_possible: dict[str, float]
    expected: dict[str, float]


def build_cases(result: SimulationResult, n: int, seed: int, store_abbr: bool = False) -> list[SheetCase]:
    """시뮬레이션 기록에서 n일을 골라 재고표 이미지를 만든다."""
    ledger = result.ledger
    sales = regular_sales_series(ledger, result.catalog)
    step = max(1, (len(ledger) - 1) // n)
    cases = []
    for k, idx in enumerate(range(1, len(ledger), step)):
        if len(cases) == n:
            break
        rec, prev = ledger[idx], ledger[idx - 1]
        image, truth = render_sheet(result.catalog, rec.day, rec.closing, seed=seed * 1000 + k, store_abbr=store_abbr)
        max_possible = {
            i.id: prev.closing.get(i.id, 0) + rec.received.get(i.id, 0) for i in result.catalog.items
        }
        recent = sales[max(0, idx - 7) : idx]
        expected = {
            i: max_possible[i] - sum(s.get(i, 0) for s in recent) / len(recent) for i in max_possible
        }
        cases.append(SheetCase(rec.day, image, truth, max_possible, expected))
    return cases


def run_eval(
    llm: LLMClient,
    catalog: Catalog,
    cases: list[SheetCase],
    out: Path,
    workers: int = 3,
    aliases: dict[str, str] | None = None,
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
            )
        except LLMError as e:
            (out / f"{case.day}.json").write_text(json.dumps({"error": str(e)}, ensure_ascii=False), encoding="utf-8")
            n = len(case.truth.entries)
            return SheetScore(case.day, n, 0, 0, 0, n, 0, 0, failed=True)
        score = score_sheet(rows, case.truth)
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


def summarize(scores: list[SheetScore]) -> dict[str, float]:
    ok = [s for s in scores if not s.failed]
    written = sum(s.written for s in ok)
    correct = sum(s.correct for s in ok)
    errors = sum(s.errors for s in ok)
    return {
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
