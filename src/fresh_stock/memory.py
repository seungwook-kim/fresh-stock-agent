"""과거 비슷한 날 검색 (RAG).

기록이 쌓이면 "가게 메모가 있던 날 실제로 무엇이 평소의 몇 배 팔렸는지"가 남는다.
오늘 메모와 비슷한 과거 날을 찾아 발주 에이전트에게 근거로 준다. 사장님 경험 노트가
"명절엔 2~3배"처럼 말로 된 기억이라면, 이건 기록에서 나온 숫자다.

검색은 글자 2-gram BM25(키워드)와 다국어 임베딩(의미)을 RRF로 합친 하이브리드다.
짧은 한국어 메모 13개 질의로 비교했을 때 상위 3개 안에 같은 유형의 날이 들어온 비율이
BM25 11/13, 임베딩 12/13, 하이브리드 13/13이었다 (fresh-stock eval-retrieval).
"""

import json
import math
import zlib
from collections import Counter
from importlib import resources
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol

import numpy as np

from .accounting import DayRecord
from .catalog import Catalog
from .domain import Category
from .market_calendar import MarketCalendar
from .ordering import OrderContext, SeasonalPolicy

WEEKDAYS = "월화수목금토일"
EMBED_MODEL = "minishlab/potion-multilingual-128M"
TOP_ITEMS = 5  # 문서에 적을 "평소와 가장 달랐던 품목" 수
RRF_K = 10


@dataclass(frozen=True)
class DayMemory:
    """가게 메모가 있던 하루와 그날 실제 결과."""

    day: date
    notes: tuple[str, ...]
    category_ratio: dict[str, float]  # 분류별 실제 정가 판매 / 평소 예측
    item_ratio: tuple[tuple[str, float], ...]  # 평소와 가장 달랐던 품목들
    sold_out: dict[str, tuple[int, int]]  # 분류별 (마감 때 다 팔린 품목 수, 평소 팔리는 품목 수)

    @property
    def text(self) -> str:
        """검색에 쓰는 글 (메모)."""
        return " / ".join(self.notes)

    def describe(self, catalog: Catalog, calendar: MarketCalendar) -> str:
        market = "·장날" if calendar.is_market_day(self.day) else ""
        cats = ", ".join(f"{c} {r:.1f}배" for c, r in self.category_ratio.items())
        items = ", ".join(f"{catalog.item(i).name} {r:.1f}배" for i, r in self.item_ratio)
        text = (
            f"{self.day:%Y-%m-%d}({WEEKDAYS[self.day.weekday()]}{market}) 메모: {self.text}\n"
            f"  실제 판매(평소 예측 대비): {cats}" + (f" / 평소와 가장 달랐던 품목: {items}" if items else "")
        )
        heavy = [f"{c} {n}/{total}" for c, (n, total) in self.sold_out.items() if total and n / total >= 0.3]
        if heavy:
            text += f"\n  품절 많음({', '.join(heavy)}개 품목 다 팔림): 실제 수요는 판매보다 컸다"
        return text


def day_outcome(
    ledger: list[DayRecord], idx: int, catalog: Catalog, calendar: MarketCalendar
) -> tuple[dict[str, float], list[tuple[str, float]], dict[str, tuple[int, int]]]:
    """ledger[idx]날 정가 판매가 평소 예측(요일·장날 보정)의 몇 배였는지. 분류별, 품목별, 분류별 품절 수.

    기록에 남는 건 수요가 아니라 판매다. 다 팔린 품목은 실제 수요가 더 컸으므로 품절 수를 함께 남긴다.
    """
    day = ledger[idx].day
    before = OrderContext(day - timedelta(days=1), day, [day], ledger[:idx], catalog, calendar)
    upto = OrderContext(day, day + timedelta(days=1), [day], ledger[: idx + 1], catalog, calendar)
    policy = SeasonalPolicy()
    expected_by_cat: dict[str, float] = {c.value: 0.0 for c in Category}
    actual_by_cat: dict[str, float] = {c.value: 0.0 for c in Category}
    items = []
    sold_out = {c.value: [0, 0] for c in Category}
    for item in catalog.items:
        expected = policy.forecast(before, item, day)
        actual = upto.demand_history(item.id)[-1][1]
        # 금액 기준으로 합쳐서 봉 단위 품목이 분류 배율을 좌우하지 않게 한다
        expected_by_cat[item.category] += expected * item.price
        actual_by_cat[item.category] += actual * item.price
        if expected >= 1:
            gone = ledger[idx].closing.get(item.id, 0) == 0
            sold_out[item.category][1] += 1
            sold_out[item.category][0] += gone
            # 다 팔려서 적게 팔린 품목은 수요가 적었던 게 아니므로 "평소와 달랐던 품목"에서 뺀다
            if not (gone and actual < expected):
                items.append((item.id, actual / expected))
    ratios = {c: actual_by_cat[c] / e for c, e in expected_by_cat.items() if e > 0}
    items.sort(key=lambda x: abs(math.log(max(x[1], 0.05))), reverse=True)
    return ratios, items[:TOP_ITEMS], {c: (n, t) for c, (n, t) in sold_out.items()}


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> np.ndarray: ...


class FastEmbedEmbedder:
    """로컬 다국어 임베딩 (API 키 불필요). 처음 쓸 때 모델(약 0.5GB)을 .cache/models에 받는다."""

    def __init__(self, model: str = EMBED_MODEL, cache_dir: str = ".cache/models"):
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model, cache_dir=cache_dir)
        self._cache: dict[str, np.ndarray] = {}

    def embed(self, texts: list[str]) -> np.ndarray:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            for t, v in zip(missing, self.model.embed(missing)):
                self._cache[t] = np.asarray(v) / (np.linalg.norm(v) or 1.0)
        return np.array([self._cache[t] for t in texts])


class HashEmbedder:
    """테스트용 임베딩: 글자 2-gram을 해시해 만든 벡터. 모델을 받지 않아도 된다."""

    def __init__(self, dim: int = 256):
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim))
        for row, text in enumerate(texts):
            for gram in _bigrams(text):
                out[row, zlib.crc32(gram.encode()) % self.dim] += 1
        norms = np.linalg.norm(out, axis=1, keepdims=True)
        return out / np.where(norms == 0, 1, norms)


def _bigrams(text: str) -> list[str]:
    t = "".join(text.split())
    return [t[i : i + 2] for i in range(len(t) - 1)] or [t]


def bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    """글자 2-gram BM25. 한국어 형태소 분석기 없이 짧은 메모의 키워드를 맞춘다."""
    tokens = [_bigrams(d) for d in docs]
    if not tokens:
        return np.zeros(0)
    avg = sum(map(len, tokens)) / len(tokens)
    df = Counter(t for ts in tokens for t in set(ts))
    n = len(docs)
    scores = []
    for ts in tokens:
        tf = Counter(ts)
        s = 0.0
        for t in _bigrams(query):
            if t in tf:
                idf = math.log(1 + (n - df[t] + 0.5) / (df[t] + 0.5))
                s += idf * tf[t] * (k1 + 1) / (tf[t] + k1 * (1 - b + b * len(ts) / avg))
        scores.append(s)
    return np.array(scores)


def dense_rank(query: str, docs: list[str], embedder: Embedder) -> list[int]:
    vectors = embedder.embed(docs + [query])
    return [int(i) for i in np.argsort(-(vectors[:-1] @ vectors[-1]))]


def bm25_rank(query: str, docs: list[str]) -> list[int]:
    return [int(i) for i in np.argsort(-bm25_scores(query, docs))]


def evaluate_retrieval(embedder: Embedder | None) -> dict[str, tuple[int, int, int, list[str]]]:
    """data/retrieval_eval.json으로 검색 방식을 비교한다. 방식별 (1위 적중, 3위 안 적중, 질의 수, 1위 오답)."""
    data = json.loads(resources.files("fresh_stock").joinpath("data", "retrieval_eval.json").read_text(encoding="utf-8"))
    docs = [d["text"] for d in data["docs"]]
    types = [d["type"] for d in data["docs"]]
    methods = {"BM25 (글자 2-gram)": lambda q: bm25_rank(q, docs)}
    if embedder is not None:
        methods["임베딩"] = lambda q: dense_rank(q, docs, embedder)
        methods["하이브리드 (BM25 + 임베딩, RRF)"] = lambda q: hybrid_rank(q, docs, embedder)
    results = {}
    for name, rank in methods.items():
        top1 = hit3 = 0
        misses = []
        for q in data["queries"]:
            order = rank(q["text"])
            found = [types[i] for i in order[:3]]
            top1 += found[0] == q["type"]
            hit3 += q["type"] in found
            if found[0] != q["type"]:
                misses.append(f"{q['text']} -> {docs[order[0]]}")
        results[name] = (top1, hit3, len(data["queries"]), misses)
    return results


def hybrid_rank(query: str, docs: list[str], embedder: Embedder | None) -> list[int]:
    """BM25와 임베딩 순위를 RRF로 합친 문서 순서. embedder가 없으면 BM25만 쓴다."""
    if not docs:
        return []
    rankings = [bm25_scores(query, docs)]
    if embedder is not None:
        vectors = embedder.embed(docs + [query])
        rankings.append(vectors[:-1] @ vectors[-1])
    total = np.zeros(len(docs))
    for scores in rankings:
        ranks = np.argsort(np.argsort(-scores))
        total += 1 / (RRF_K + ranks + 1)
    return [int(i) for i in np.argsort(-total)]


class DayMemoryBank:
    """가게 메모가 있던 과거 날들의 결과를 모아 검색한다. 지나간 날의 결과는 바뀌지 않으므로 캐시한다."""

    def __init__(self, catalog: Catalog, calendar: MarketCalendar, embedder: Embedder | None = None):
        self.catalog = catalog
        self.calendar = calendar
        self.embedder = embedder
        self._cache: dict[tuple[date, tuple[str, ...]], DayMemory] = {}

    def memories(self, ledger: list[DayRecord], note_log: dict[date, list[str]]) -> list[DayMemory]:
        index = {rec.day: i for i, rec in enumerate(ledger)}
        out = []
        for day, notes in sorted(note_log.items()):
            idx = index.get(day)
            if idx is None or idx == 0 or not notes:
                continue
            key = (day, tuple(notes))
            if key not in self._cache:
                ratios, items, sold_out = day_outcome(ledger, idx, self.catalog, self.calendar)
                self._cache[key] = DayMemory(day, tuple(notes), ratios, tuple(items), sold_out)
            out.append(self._cache[key])
        return out

    def search(self, ctx: OrderContext, query: str, k: int = 3) -> list[DayMemory]:
        memories = self.memories(ctx.ledger, ctx.note_log)
        order = hybrid_rank(query, [m.text for m in memories], self.embedder)
        return [memories[i] for i in order[:k]]

    def describe(self, found: list[DayMemory]) -> str:
        if not found:
            return "검색된 과거 기록이 없다."
        return "\n".join(m.describe(self.catalog, self.calendar) for m in found)
