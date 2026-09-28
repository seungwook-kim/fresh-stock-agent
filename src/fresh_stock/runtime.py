"""웹 서버와 MCP 서버가 같은 방식으로 가게 서비스를 만들도록 모은 설정."""

import os
from collections.abc import Callable
from pathlib import Path

from .auto_notes import SEOUL, WeatherSource
from .catalog import load_catalog
from .llm import LLMClient, make_llm
from .market_calendar import MarketCalendar
from .memory import DayMemoryBank, FastEmbedEmbedder
from .service import StoreService
from .store import Database, Store

_embedder = None


def memory_bank() -> DayMemoryBank:
    """과거 비슷한 날 검색. 임베딩 모델은 한 번만 불러 여러 가게가 같이 쓴다."""
    global _embedder
    if _embedder is None:
        _embedder = FastEmbedEmbedder()
    return DayMemoryBank(load_catalog(), MarketCalendar(), _embedder)


def weather_source() -> WeatherSource:
    """가게 위치(FRESH_STOCK_LAT, FRESH_STOCK_LON, 기본 서울)의 날씨 예보."""
    lat = float(os.environ.get("FRESH_STOCK_LAT", SEOUL[0]))
    lon = float(os.environ.get("FRESH_STOCK_LON", SEOUL[1]))
    return WeatherSource(lat, lon)


def service_factory(
    db: Database,
    *,
    backend: str,
    model: str | None,
    effort: str | None,
    log_dir: Path | None,
    rag: bool = True,
    auto_notes: bool = True,
) -> Callable[[int], StoreService]:
    """store_id를 받아 그 가게의 StoreService를 만드는 함수. AI 호출 기록은 가게별 파일로 남긴다."""
    catalog, calendar = load_catalog(), MarketCalendar()
    weather = weather_source()

    def make_service(store_id: int) -> StoreService:
        log = log_dir / f"store-{store_id}.jsonl" if log_dir else None

        def llm_factory() -> LLMClient:
            return make_llm(backend, model, effort, log)

        def research_llm_factory() -> LLMClient:
            # 웹 검색은 low에서 일찍 포기하는 경우가 있어 medium으로 둔다
            return make_llm(backend, model, "medium", log, web_search=True)

        return StoreService(
            Store(db, store_id),
            catalog,
            calendar,
            llm_factory,
            db.store_name(store_id) or "우리가게",
            memory_bank() if rag else None,
            auto_notes=auto_notes,
            weather=weather,
            research_llm_factory=research_llm_factory if backend == "claude-code" else None,
        )

    return make_service
