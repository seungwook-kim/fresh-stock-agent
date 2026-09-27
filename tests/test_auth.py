"""로그인과 가게 분리 테스트."""

from conftest import OWNER_EMAIL, WebEnv

from fresh_stock.store import Store


def test_pages_require_login():
    env = WebEnv()
    for path in ("/", "/sheet", "/order", "/logs", "/members", "/sheet/review"):
        r = env.client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login", path
    assert "구글 로그인이 아직 설정되지 않았어요" in env.client.get("/login").text
    assert "개발용 로그인" in env.client.get("/login").text


def test_member_lands_on_their_store():
    env = WebEnv()
    r = env.login()
    assert "싱싱청과" in r.text and "차례예요" in r.text
    assert "boss@gmail.com" in r.text and "로그아웃" in r.text


def test_new_user_without_store_creates_one_and_is_isolated():
    env = WebEnv()
    r = env.login("new@gmail.com")
    assert "아직 속한 가게가 없어요" in r.text
    assert env.client.get("/", follow_redirects=False).headers["location"] == "/stores"
    r = env.client.post("/stores", data={"name": "새청과"}, follow_redirects=True)
    assert "새청과" in r.text and "싱싱청과" not in r.text
    assert "앞으로 예정된 메모가 없어요" in r.text  # 다른 가게의 메모는 보이지 않는다
    # 남의 가게로 바꾸려 해도 멤버가 아니면 바뀌지 않는다
    env.client.post(f"/stores/{env.store_id}/select")
    assert "싱싱청과" not in env.client.get("/").text


def test_cannot_touch_other_store_notes():
    env = WebEnv()
    note_id = env.store.note_records()[0].id
    env.login("stranger@gmail.com")
    env.client.post("/stores", data={"name": "남의가게"})
    env.client.post(f"/notes/{note_id}/delete")
    assert any(n.id == note_id for n in env.store.note_records())


def test_owner_manages_members_and_staff_cannot():
    env = WebEnv()
    env.login()
    env.client.post("/members", data={"email": "Staff@Gmail.com", "role": "staff"})
    assert "staff@gmail.com" in env.client.get("/members").text
    r = env.client.post("/members/remove", data={"email": OWNER_EMAIL}, follow_redirects=True)
    assert "사장님이 한 명은 있어야" in r.text

    env.client.get("/logout")
    r = env.login("staff@gmail.com")
    assert "싱싱청과" in r.text  # 추가된 직원은 바로 그 가게로 들어간다
    r = env.client.post("/members", data={"email": "x@gmail.com"}, follow_redirects=True)
    assert "사장님만 추가할 수 있습니다" in r.text
    assert env.db.role(env.store_id, "x@gmail.com") is None


def test_removed_member_loses_access():
    env = WebEnv()
    env.db.add_member(env.store_id, "staff@gmail.com")
    env.login("staff@gmail.com")
    assert env.client.get("/", follow_redirects=False).status_code == 200
    env.db.remove_member(env.store_id, "staff@gmail.com")
    assert env.client.get("/", follow_redirects=False).headers["location"] == "/stores"


def test_multiple_stores_choose_on_login():
    env = WebEnv()
    second = env.db.create_store("2호점", OWNER_EMAIL)
    r = env.login()
    assert "가게 선택" in r.text and "2호점" in r.text
    r = env.client.post(f"/stores/{second}/select", follow_redirects=True)
    assert "2호점" in r.text


def test_google_login_flow():
    async def exchange(request):
        return {"email": "Boss@Gmail.com", "email_verified": True}

    env = WebEnv(google_exchange=exchange)
    r = env.client.get("/auth/google", follow_redirects=True)
    assert "싱싱청과" in r.text


def test_google_login_rejects_unverified_email():
    async def exchange(request):
        return {"email": OWNER_EMAIL, "email_verified": False}

    env = WebEnv(google_exchange=exchange)
    r = env.client.get("/auth/google", follow_redirects=True)
    assert "이메일이 확인된 구글 계정" in r.text


def test_google_button_redirects_to_google():
    from fastapi.testclient import TestClient

    from fresh_stock.web import AuthConfig, create_app

    env = WebEnv()
    auth = AuthConfig(secret_key="s", google_client_id="cid.apps.googleusercontent.com", google_client_secret="sec")
    client = TestClient(create_app(env.db, lambda sid: None, auth))
    r = client.get("/login/google", follow_redirects=False)
    assert r.status_code in (302, 303, 307)
    assert r.headers["location"].startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "client_id=cid.apps.googleusercontent.com" in r.headers["location"]
    assert "redirect_uri=http%3A%2F%2Ftestserver%2Fauth%2Fgoogle" in r.headers["location"]


def test_dev_login_is_off_unless_enabled():
    from fastapi.testclient import TestClient

    from fresh_stock.web import AuthConfig, create_app

    env = WebEnv()
    client = TestClient(create_app(env.db, lambda sid: None, AuthConfig(secret_key="s")))
    client.post("/login/dev", data={"email": OWNER_EMAIL})
    assert client.get("/", follow_redirects=False).headers["location"] == "/login"
    assert "개발용 로그인" not in client.get("/login").text


def test_demo_mode_lets_anyone_look_around():
    from fastapi.testclient import TestClient

    from fresh_stock.web import AuthConfig, create_app

    env = WebEnv()
    auth = AuthConfig(secret_key="s", dev_login=True, demo_store_id=env.store_id)
    services = {}

    def make_service(sid):
        from conftest import CALENDAR, CATALOG

        from fresh_stock.service import StoreService

        return services.setdefault(sid, StoreService(Store(env.db, sid), CATALOG, CALENDAR))

    client = TestClient(create_app(env.db, make_service, auth))
    r = client.post("/login/dev", data={"email": "visitor@gmail.com"}, follow_redirects=True)
    assert "싱싱청과" in r.text
