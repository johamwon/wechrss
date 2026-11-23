import base64
import json
import re
import threading
import time
from urllib.parse import urlencode

import pytest
import requests

from service import AppDB, CredentialStore, Scheduler, SyncService
from test_auth import FakeResponse, InstantAuth
from test_web import call_wsgi
from web_app import WebApp
from wechat_mp_fetcher import Article, AuthExpiredError, RiskControlError, WeReadMobileClient, save_article
from weread_auth import PollResult, RefreshExpiredError, RefreshRiskError, WeReadAuthClient, WeReadCredentials


@pytest.fixture
def setup_pool(tmp_path, monkeypatch):
    db = AppDB(tmp_path / "app.db")
    legacy = CredentialStore(tmp_path / "credentials.json")
    legacy.save("legacy-private-token", "101")
    service = SyncService(db, legacy, account_pool=True)
    service.save_credentials(WeReadCredentials(vid="202", accessToken="second-private-token"))
    monkeypatch.setattr(service._pool_limiter, "wait", lambda: None)
    source = db.add_source(source_value="123", name="测试公众号")
    return service, source


def test_expired_session_falls_back_and_is_skipped_after_restart(setup_pool, monkeypatch):
    service, source = setup_pool
    calls = []

    class Client:
        def __init__(self, vid):
            self.vid = vid

        def get_articles(self, book_id, **kwargs):
            calls.append(self.vid)
            if self.vid == "101":
                raise AuthExpiredError("expired")
            return [Article("one", book_id, "测试文章")]

    monkeypatch.setattr(service, "_client_for", lambda record: Client(record.vid))
    assert service.sync_source(source.id).status == "ok"
    assert calls == ["101", "202"]
    reopened = SyncService(service.db, service.credentials, account_pool=True)
    assert [item["state"] for item in reopened.credential_status()["accounts"]] == ["expired", "valid"]
    assert [record.vid for record in reopened.pool.candidates()] == ["202"]
    service.remove_pool_account("101")
    assert [item["vid"] for item in SyncService(service.db, service.credentials, account_pool=True)
            .credential_status()["accounts"]] == ["202"]


@pytest.mark.parametrize("error, expected", [(RiskControlError("429"), "risk_control"),
                                             (requests.Timeout("timeout"), "error")])
def test_limits_and_transient_errors_never_switch(setup_pool, monkeypatch, error, expected):
    service, source = setup_pool
    calls = []

    class Client:
        def get_articles(self, *args, **kwargs):
            raise error

    def get_client(record):
        calls.append(record.vid)
        return Client()

    monkeypatch.setattr(service, "_client_for", get_client)
    assert service.sync_source(source.id).status == expected
    if expected == "risk_control":
        service.save_credentials(WeReadCredentials(vid="303", accessToken="new-token"))
        assert service.sync_source(source.id).status == "risk_control"
        assert service.account_issue() == "risk_control"
        service.resume_pool()
    assert service.account_issue() == ""
    assert calls == ["101"]
    assert all(item["state"] == "pending" for item in service.credential_status()["accounts"])


@pytest.mark.parametrize("refresh_error, result, candidates", [
    (RefreshExpiredError("expired"), "ok", ["202"]),
    (RefreshRiskError("limited"), "risk_control", ["101", "202"]),
    (None, "ok", ["101", "202"]),
])
def test_pool_refresh_outcome(setup_pool, monkeypatch, refresh_error, result, candidates):
    service, source = setup_pool
    service.save_credentials(WeReadCredentials(vid="101", accessToken="expired", refreshToken="refresh",
                                               deviceId="device", profile="eink"))
    calls = []

    class Client:
        def __init__(self, token):
            self.token = token

        def get_articles(self, *args, **kwargs):
            if self.token == "expired":
                raise AuthExpiredError("expired")
            return []

    def refresh(record):
        calls.append(record.vid)
        if refresh_error:
            raise refresh_error
        return WeReadCredentials(vid=record.vid, accessToken="renewed")

    monkeypatch.setattr(service, "_client_for", lambda record: Client(record.accessToken))
    monkeypatch.setattr(service.auth_client, "refresh", refresh)
    assert service.sync_source(source.id).status == result
    assert calls == ["101"]
    assert sorted(record.vid for record in service.pool.candidates()) == candidates
    if refresh_error is None:
        assert service.pool.candidates()[0].accessToken == "renewed"


def test_all_expired_wait_for_admin_relogin(setup_pool, monkeypatch):
    service, source = setup_pool

    class Client:
        def get_articles(self, *args, **kwargs):
            raise AuthExpiredError("expired")

    monkeypatch.setattr(service, "_client_for", lambda _: Client())
    assert service.sync_source(source.id).status == "auth_expired"
    assert service.account_issue() == "auth_expired"
    assert not service.pool.candidates()
    service.save_credentials(WeReadCredentials(vid="202", accessToken="new"))
    assert service.account_issue() == ""
    assert [item.vid for item in service.pool.candidates()] == ["202"]


def test_public_mode_keeps_pool_and_management_private(setup_pool, monkeypatch):
    service, source = setup_pool
    sched = Scheduler(service.db, service)
    with pytest.raises(ValueError, match="ADMIN_PASSWORD"):
        WebApp(service.db, service.credentials, service, sched)

    monkeypatch.setenv("ADMIN_PASSWORD", "admin-test")
    monkeypatch.setenv("WECHAT_MP_PUBLIC_FEEDS", "1")
    app = WebApp(service.db, service.credentials, service, sched)
    feed_token = service.db.rotate_feed_token(source.id)
    with service.db.connect() as conn:
        save_article(conn, Article("one", source.book_id, "Test article", content_html="hidden-body"))
        conn.commit()

    meta, body = call_wsgi(app, "/")
    assert meta["status"].startswith("200")
    assert meta["headers"]["Vary"] == "Authorization"
    path = f"/public-feeds/{source.id}.xml"
    assert path.encode() in body
    assert feed_token.encode() not in body
    meta, xml = call_wsgi(app, path)
    assert meta["status"].startswith("200")
    assert b"Test article" in xml and b"hidden-body" not in xml
    assert call_wsgi(app, path, "HEAD")[1] == b""

    for protected in ("/admin", "/settings", "/settings/backup", "/api/login/status", "/api/health", "/inbox"):
        assert call_wsgi(app, protected)[0]["status"].startswith("401")
    encoded = urlencode({"csrf": app.csrf_token, "action": "remove", "vid": "101"}).encode()
    assert call_wsgi(app, "/settings/account-pool", "POST", encoded)[0]["status"].startswith("401")
    auth = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"admin:admin-test").decode()}
    meta, settings = call_wsgi(app, "/settings", environ_overrides=auth)
    assert meta["status"].startswith("200")
    assert "账号池".encode() in settings and b"legacy-private-token" not in settings
    assert call_wsgi(app, "/settings/account-pool", "POST", encoded, auth)[0]["status"].startswith("303")
    assert len(service.credential_status()["accounts"]) == 1


def test_qr_login_adds_account_without_overwriting_legacy(setup_pool, monkeypatch):
    service, _ = setup_pool
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-test")
    service.auth_client = InstantAuth()
    app = WebApp(service.db, service.credentials, service, Scheduler(service.db, service))
    auth = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"admin:admin-test").decode()}
    encoded = urlencode({"csrf": app.csrf_token}).encode()
    assert call_wsgi(app, "/settings/login/start", "POST", encoded, auth)[0]["status"].startswith("303")
    app.login_manager._thread.join(2)
    assert app.login_manager.status()["status"] == "success"
    assert [item["vid"] for item in service.credential_status()["accounts"]] == ["101", "202", "100"]
    assert service.credentials.get_record().vid == "101"


def test_unexplained_forbidden_is_not_an_expired_session(monkeypatch):
    mobile = WeReadMobileClient("test", "123", min_interval=0)
    monkeypatch.setattr(mobile.session, "get", lambda *a, **kw: FakeResponse({"errcode": -99}, 403))
    with pytest.raises(RiskControlError):
        mobile.get_info("MP_WXS_123")
    monkeypatch.setattr(mobile.session, "get", lambda *a, **kw: FakeResponse({"errcode": -2012}, 403))
    with pytest.raises(AuthExpiredError):
        mobile.get_info("MP_WXS_123")

    auth = WeReadAuthClient()
    record = WeReadCredentials(vid="123", accessToken="test", refreshToken="refresh", deviceId="device")
    monkeypatch.setattr(auth.session, "post", lambda *a, **kw: FakeResponse({}, 429))
    with pytest.raises(RefreshRiskError):
        auth.refresh(record)


def test_public_qr_isolation_consent_and_revocation(setup_pool, monkeypatch):
    service, _ = setup_pool
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-test")
    app = WebApp(service.db, service.credentials, service, Scheduler(service.db, service))
    https = {"wsgi.url_scheme": "https"}
    released = threading.Event()
    waiting = threading.Event()

    class VisitorAuth(InstantAuth):
        def poll_qr_once(self, uuid, *, last=None, timeout=None):
            waiting.set()
            if not released.wait(5):
                raise RuntimeError("test login timed out")
            return PollResult("confirmed", 405, "wx-code")

        def initialize_feature(self, record):
            raise AssertionError("public QR must not perform a separate login probe")

    app.public_login.auth_factory = VisitorAuth

    def open_pool():
        meta, body = call_wsgi(app, "/account-pool", environ_overrides=https)
        assert meta["status"].startswith("200")
        assert meta["headers"]["Cache-Control"] == "no-store"
        cookie = meta["headers"]["Set-Cookie"]
        assert cookie.startswith("__Host-werss_pool=")
        assert "Secure" in cookie and "HttpOnly" in cookie
        csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', body)[1].decode()
        return {**https, "HTTP_COOKIE": cookie.split(";", 1)[0]}, csrf

    first, first_csrf = open_pool()
    second, second_csrf = open_pool()

    def post(headers, csrf, action, **fields):
        data = urlencode(dict(csrf=csrf, action=action, **fields)).encode()
        return call_wsgi(app, "/account-pool", "POST", data, headers)[0]["status"]

    assert post(first, first_csrf, "start").startswith("400")
    assert post(second, first_csrf, "start", consent="1").startswith("403")
    assert post(first, first_csrf, "start", consent="1").startswith("303")
    try:
        assert waiting.wait(2)
        first_state = json.loads(call_wsgi(app, "/account-pool/status", environ_overrides=first)[1])
        second_state = json.loads(call_wsgi(app, "/account-pool/status", environ_overrides=second)[1])
        assert first_state == {"status": "waiting", "qr": "data:image/png;base64,AA=="}
        assert second_state == {"status": "idle", "qr": ""}
    finally:
        released.set()
        for sign_in in app.public_login._sessions.values():
            sign_in.manager._thread.join(2)

    accounts = service.pool.public_status("", 1)["accounts"]
    donated = next(item for item in accounts if item["state"] == "valid")
    assert service.credential_status()["accounts"][-1]["name"] == "Alice"
    _, public = call_wsgi(app, "/account-pool", environ_overrides=second)
    assert donated["id"].encode() in public
    assert b"Alice" not in public and b"secret-access-token" not in public
    assert b'name="action" value="revoke"' not in public
    assert post(second, second_csrf, "revoke", id=donated["id"]).startswith("303")
    assert len(service.credential_status()["accounts"]) == 3
    assert post(first, first_csrf, "revoke", id=donated["id"]).startswith("303")
    assert len(service.credential_status()["accounts"]) == 2


def test_public_pool_paginates_without_an_account_limit(setup_pool):
    service, _ = setup_pool
    for i in range(52):
        service.save_credentials(WeReadCredentials(vid=str(300 + i), accessToken="test"))
    service.pool.mark("101", "expired")
    first = service.pool.public_status("", 1)
    second = service.pool.public_status("", 2)
    assert (len(first["accounts"]), len(second["accounts"])) == (50, 4)
    assert first["counts"] == {"valid": 0, "expired": 1, "pending": 53}
    assert all("vid" not in item and "name" not in item and "owner" not in item for item in first["accounts"])


def test_expired_candidate_checks_continue_on_next_sync(setup_pool, monkeypatch):
    service, source = setup_pool
    service.save_credentials(WeReadCredentials(vid="303", accessToken="expired"))
    service.save_credentials(WeReadCredentials(vid="404", accessToken="usable"))
    attempted = []

    class Client:
        def __init__(self, vid):
            self.vid = vid

        def get_articles(self, *args, **kwargs):
            attempted.append(self.vid)
            if self.vid != "404":
                raise AuthExpiredError("expired")
            return []

    monkeypatch.setattr(service, "_client_for", lambda record: Client(record.vid))
    assert service.sync_source(source.id).status == "error"
    assert service.account_issue() == ""
    assert attempted == ["101", "202", "303"]
    assert service.sync_source(source.id).status == "ok"
    assert attempted[-1] == "404"


def test_public_login_throttles_new_qr_sessions(setup_pool, monkeypatch):
    service, _ = setup_pool
    monkeypatch.setenv("ADMIN_PASSWORD", "admin-test")
    app = WebApp(service.db, service.credentials, service, Scheduler(service.db, service))
    app.public_login.auth_factory = InstantAuth
    assert app.public_login.start("first")
    assert not app.public_login.start("second")
    app.public_login._sessions["first"].manager._thread.join(2)
    app.public_login._last_start = time.monotonic() - 3
    assert app.public_login.start("second")
    app.public_login._sessions["second"].manager._thread.join(2)
