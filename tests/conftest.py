"""웹 테스트 공통 준비: 데모 기록이 든 가게 하나와 개발용 로그인."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from fresh_stock.catalog import load_catalog
from fresh_stock.demo import seed_demo
from fresh_stock.market_calendar import MarketCalendar
from fresh_stock.service import StoreService
from fresh_stock.store import Database, Store
from fresh_stock.web import AuthConfig, create_app

CATALOG = load_catalog()
CALENDAR = MarketCalendar()
OWNER_EMAIL = "boss@gmail.com"


class WebEnv:
    def __init__(self, llm=None, research_llm=None, weather=None, google_exchange=None, log_dir=None):
        self.db = Database(":memory:")
        self.store_id = self.db.create_store("싱싱청과", OWNER_EMAIL)
        # 7/1부터 30일: 7/31(금) 마감 → 토요일 입고 발주가 있는 날
        self.first = seed_demo(Store(self.db, self.store_id), CATALOG, CALENDAR, date(2026, 7, 1), days=30, seed=1)

        def make_service(store_id: int) -> StoreService:
            return StoreService(
                Store(self.db, store_id),
                CATALOG,
                CALENDAR,
                llm_factory=(lambda: llm) if llm else None,
                store_name=self.db.store_name(store_id),
                auto_notes=weather is not None,
                weather=weather,
                research_llm_factory=(lambda: research_llm) if research_llm else None,
            )

        auth = AuthConfig(secret_key="test-secret", dev_login=True)
        self.client = TestClient(create_app(self.db, make_service, auth, log_dir, google_exchange))

    @property
    def store(self) -> Store:
        return Store(self.db, self.store_id)

    def login(self, email: str = OWNER_EMAIL):
        return self.client.post("/login/dev", data={"email": email}, follow_redirects=True)


@pytest.fixture
def web_env():
    return WebEnv
