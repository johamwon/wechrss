import base64
import json
import sqlite3
import threading
import time
from dataclasses import replace
from urllib.parse import urlencode

import pytest

from experience import diagnose_feed, normalize_public_url, source_health
from service import APP_SCHEMA, AppDB, CredentialStore, Scheduler, SyncResult, SyncService
from test_web import call_wsgi
from web_app import WebApp
from wechat_mp_fetcher import Article, AuthExpiredError, FetcherError, RiskControlError, save_article
from weread_auth import WeReadCredentials


@pytest.fixture
def app(tmp_path, monkeypatch):
    for key in ("WEREAD_ACCESS_TOKEN", "WEREAD_VID", "ADMIN_PASSWORD", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
    db = AppDB(tmp_path / "db.sqlite")
    credentials = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, credentials)
    return WebApp(db, credentials, service, Scheduler(db, service))


def connect(app):
    app.service.save_credentials(WeReadCredentials(vid="123", accessToken="test-token"))


def post(app, path, **fields):
    return call_wsgi(app, path, "POST", urlencode({"csrf": app.csrf_token, **fields}).encode(),
                     {"HTTP_REFERER": "http://localhost/onboarding", "HTTP_HOST": "localhost"})


def wait_idle(service):
    deadline = time.monotonic() + 4
    while service._worker is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert service._worker is None


def save_article_and_success(app, source):
    with app.db.connect() as conn:
        save_article(conn, Article("demo", source.book_id, "示例文章", url="https://example.com/article"))
        conn.commit()
    run = app.db.mark_sync_start(source.id)
    app.db.mark_sync_finish(source, run, SyncResult(source.id, 1, 1, 0, "ok"))


@pytest.mark.parametrize("value", [
    "https://user:pass@example.com", "https://example.com/path", "https://example.com?token=secret",
    "https://example.com/#fragment", "file:///etc/passwd", "http://0.0.0.0:8080",
    "http://[::]:8080", "http://example.com:0", "http://example.com:99999",
    "https://example.com\\@localhost", "https://exa mple.com", "https://-invalid.com",
])
def test_invalid_feed_base_urls_are_rejected(value):
    with pytest.raises(FetcherError):
        normalize_public_url(value)


@pytest.mark.parametrize("value, expected", [
    (" https://rss.example.com/ ", "https://rss.example.com"),
    ("http://192.168.1.20:8080", "http://192.168.1.20:8080"),
    ("http://[::1]:8080/", "http://[::1]:8080"), ("", ""),
])
def test_feed_base_url_normalization(value, expected):
    assert normalize_public_url(value) == expected


@pytest.mark.parametrize("base, target, blocked", [
    ("http://127.0.0.1:8080", "local", False),
    ("http://127.0.0.1:8080", "lan", True),
    ("http://[::1]:8080", "cloud", True),
    ("http://[::ffff:127.0.0.1]:8080", "lan", True),
    ("http://nas.local:8080", "cloud", True),
    ("http://192.168.1.8:8080", "cloud", True),
    ("http://192.168.1.8:8080", "lan", False),
    ("https://rss.example.com", "cloud", False),
])
def test_diagnosis_distinguishes_reader_locations(base, target, blocked):
    result = diagnose_feed(base, target, password_required=True, token_available=True)
    assert result["blocked"] is blocked
    assert result["label"] != "已连通"


def test_password_needs_reader_token():
    result = diagnose_feed("https://rss.example.com", "cloud", password_required=True, token_available=False)
    assert result["blocked"]
    assert any("管理密码" in note for note in result["notes"])


def test_old_database_migrates_without_losing_success_history(tmp_path):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.executescript(APP_SCHEMA)
        conn.execute("""INSERT INTO sources(book_id,created_at,updated_at,last_status,last_sync_at)
                        VALUES('MP_WXS_12',100,300,'error',300)""")
        conn.execute("""INSERT INTO sync_runs(source_id,started_at,finished_at,status,new_count)
                        VALUES(1,100,200,'ok',2)""")
    db = AppDB(path)
    source = db.get_source(1)
    assert source.last_success_at == 200
    assert source.last_new_at == 200
    assert source.last_sync_at == 300
    assert source.feed_token == ""
    assert AppDB(path).get_source(1).last_success_at == 200


def test_first_run_progress_survives_reopening_and_requires_reader_confirmation(app):
    assert app._onboarding()["step"] == 1
    connect(app)
    assert app._onboarding()["step"] == 2
    source = app.db.add_source(source_value="MP_WXS_13")
    app.db.set_setting("onboarding_source_id", str(source.id))
    assert app._onboarding()["step"] == 3
    save_article_and_success(app, source)
    assert app._onboarding()["step"] == 4
    # Merely opening the feed must never finish onboarding.
    call_wsgi(app, f"/feeds/{source.id}.xml")
    assert app._onboarding()["step"] == 4
    post(app, f"/sources/{source.id}/verify", next="onboarding")
    assert app._onboarding()["step"] == 5
    reopened = WebApp(AppDB(app.db.path), app.credentials, app.service, app.scheduler)
    assert reopened._onboarding()["step"] == 5


def test_empty_feed_cannot_be_confirmed(app):
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_14")
    meta, _ = post(app, f"/sources/{source.id}/verify")
    assert "error=" in meta["headers"]["Location"]
    assert not app.db.get_source(source.id).feed_verified_at


def test_adding_source_queues_first_sync_once(app, monkeypatch):
    connect(app)
    started, release = threading.Event(), threading.Event()
    calls = []

    def fetch(source):
        calls.append(source.id)
        started.set()
        assert release.wait(3)
        return [Article("first", source.book_id, "第一篇")]

    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", fetch)
    try:
        meta, _ = post(app, "/sources", source="MP_WXS_15", next="onboarding")
        assert meta["headers"]["Location"].startswith("/onboarding")
        assert started.wait(2)
        source = app.db.list_sources()[0]
        app.service.queue_sync(source.id)
        _, data = call_wsgi(app, f"/api/sources/{source.id}/status")
        assert json.loads(data)["health"]["busy"]
    finally:
        release.set()
        wait_idle(app.service)
    assert calls == [source.id]
    assert app.db.get_source(source.id).article_count == 1
    assert app.db.get_source(source.id).last_success_at


def test_manual_first_sync_works_with_scheduler_paused(app, monkeypatch):
    connect(app)
    app.scheduler.enabled = False
    source = app.db.add_source(source_value="MP_WXS_16")
    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", lambda _: [])
    post(app, f"/sources/{source.id}/sync")
    wait_idle(app.service)
    assert app.db.get_source(source.id).last_status == "ok"


@pytest.mark.parametrize("exception, expected", [
    (AuthExpiredError("expired"), "auth_expired"), (RiskControlError("-2041"), "risk_control"),
])
def test_account_failure_persists_and_new_login_clears_it(app, monkeypatch, exception, expected):
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_17")
    save_article_and_success(app, source)
    previous_success = app.db.get_source(source.id).last_success_at
    def fail(_):
        raise exception
    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", fail)
    app.service.sync_source(source.id)
    assert app.service.account_issue() == expected
    assert app.db.get_source(source.id).last_success_at == previous_success
    restarted = SyncService(AppDB(app.db.path), app.credentials)
    assert restarted.account_issue() == expected
    _, data = call_wsgi(app, "/api/experience")
    assert json.loads(data)["alerts"][0]["key"] == expected
    app.service.save_credentials(WeReadCredentials(vid="123", accessToken="new-session"))
    assert app.service.account_issue() == ""


def test_scheduler_does_not_enqueue_when_account_is_blocked(app, monkeypatch):
    connect(app)
    app.db.add_source(source_value="MP_WXS_18")
    app.db.set_setting("account_issue", "risk_control")
    calls = []
    monkeypatch.setattr(app.service, "queue_sync", calls.append)
    monkeypatch.setattr(app.scheduler._stop, "wait", lambda _: app.scheduler._stop.set())
    app.scheduler._run()
    assert calls == []


def test_health_staleness_uses_last_success_not_article_age(app):
    source = app.db.add_source(source_value="MP_WXS_19")
    source = replace(source, last_status="ok", last_success_at=1000, last_new_at=1)
    assert source_health(source, configured=True, scheduler_enabled=True, now=1100)["code"] == "ok"
    assert source_health(source, configured=True, scheduler_enabled=True, now=100000)["code"] == "stale"
    assert source_health(source, configured=True, scheduler_enabled=False, now=100000)["code"] == "paused"
    assert source_health(source, configured=False, scheduler_enabled=True)["code"] == "disconnected"


def test_zero_new_articles_remains_success_and_preserves_last_new_time(app, monkeypatch):
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_20")
    save_article_and_success(app, source)
    with app.db.connect() as conn:
        conn.execute("UPDATE sources SET last_new_at=123 WHERE id=?", (source.id,))
        conn.commit()
    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", lambda _: [])
    result = app.service.sync_source(source.id)
    assert result.status == "ok" and result.new_count == 0
    assert app.db.get_source(source.id).last_new_at == 123


def test_reader_token_is_source_scoped_read_only_and_revocable(app):
    source = app.db.add_source(source_value="MP_WXS_21")
    other = app.db.add_source(source_value="MP_WXS_22")
    token = app.db.rotate_feed_token(source.id)
    app.admin_password = "admin-secret"
    query = {"QUERY_STRING": urlencode({"token": token})}
    for method in ("GET", "HEAD"):
        meta, data = call_wsgi(app, f"/feeds/{source.id}.xml", method=method, environ_overrides=query)
        assert meta["status"].startswith("200")
        assert bool(data) is (method == "GET")
    for path in ("/", "/api/sources", f"/feeds/{other.id}.xml"):
        meta, _ = call_wsgi(app, path, environ_overrides=query)
        assert meta["status"].startswith("401")
    meta, _ = call_wsgi(app, f"/feeds/{source.id}.xml", method="POST", environ_overrides=query)
    assert meta["status"].startswith("401")
    app.db.rotate_feed_token(source.id)
    meta, _ = call_wsgi(app, f"/feeds/{source.id}.xml", environ_overrides=query)
    assert meta["status"].startswith("401")
    meta, _ = call_wsgi(app, f"/feeds/{source.id}.xml", environ_overrides={"QUERY_STRING": "token=%E4%B8%AD"})
    assert meta["status"].startswith("401")


def test_admin_password_and_csrf_still_protect_token_rotation(app):
    source = app.db.add_source(source_value="MP_WXS_23")
    app.admin_password = "中文密码"
    auth = "Basic " + base64.b64encode("admin:中文密码".encode()).decode()
    meta, _ = call_wsgi(app, f"/sources/{source.id}/feed-token", method="POST",
                        environ_overrides={"HTTP_AUTHORIZATION": auth})
    assert meta["status"].startswith("303")
    assert "error=" in meta["headers"]["Location"]
    assert not app.db.get_source(source.id).feed_token


def test_tokens_are_not_returned_by_general_apis(app):
    source = app.db.add_source(source_value="MP_WXS_24")
    token = app.db.rotate_feed_token(source.id)
    for path in ("/api/sources", "/api/experience", f"/api/sources/{source.id}/status"):
        meta, data = call_wsgi(app, path)
        assert meta["status"].startswith("200")
        assert token not in data.decode()
        assert "feed_token" not in data.decode()


def test_public_address_is_used_and_invalid_update_preserves_previous_value(app):
    source = app.db.add_source(source_value="MP_WXS_25")
    post(app, "/settings/feed-address", public_base_url="https://rss.example.com/")
    meta, data = call_wsgi(app, f"/sources/{source.id}", environ_overrides={"QUERY_STRING": "reader=cloud"})
    assert meta["status"].startswith("200")
    assert "https://rss.example.com/feeds/" in data.decode()
    post(app, "/settings/feed-address", public_base_url="https://user:password@example.com")
    assert app.db.get_setting("public_base_url") == "https://rss.example.com"


def test_login_return_is_allowlisted_and_idle_login_does_not_poll(app):
    _, data = call_wsgi(app, "/settings", environ_overrides={"QUERY_STRING": "next=onboarding"})
    assert 'data-login-return="/onboarding"' in data.decode()
    assert 'data-login-poll="0"' in data.decode()
    _, data = call_wsgi(app, "/settings", environ_overrides={"QUERY_STRING": "next=https://evil.example&login=1"})
    assert "evil.example" not in data.decode()
    assert 'data-login-poll="1"' in data.decode()


def test_source_form_keeps_input_after_resolve_failure(app, monkeypatch):
    def fail(*args, **kwargs):
        raise FetcherError("链接无法识别")
    monkeypatch.setattr(app.db, "add_source", fail)
    meta, data = post(app, "/sources", source="https://mp.weixin.qq.com/s/example", name="自定义名称")
    assert meta["status"].startswith("400")
    assert "https://mp.weixin.qq.com/s/example" in data.decode()
    assert "自定义名称" in data.decode()


def test_account_failure_cancels_remaining_queue_without_requests(app, monkeypatch):
    connect(app)
    first = app.db.add_source(source_value="MP_WXS_26")
    second = app.db.add_source(source_value="MP_WXS_27")
    started, release = threading.Event(), threading.Event()
    calls = []
    def fetch(source):
        calls.append(source.id)
        started.set()
        assert release.wait(3)
        raise RiskControlError("verification required")
    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", fetch)
    try:
        app.service.queue_sync(first.id)
        assert started.wait(2)
        app.service.queue_sync(second.id)
    finally:
        release.set()
        wait_idle(app.service)
    assert calls == [first.id]
    assert not app.service.is_queued(second.id)
    assert app.db.get_source(second.id).last_status == "never"


def test_expired_session_without_refresh_fields_is_not_generic_network_error(app, monkeypatch):
    import service as service_module
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_28")
    class ExpiredClient:
        def __init__(self, *args, **kwargs):
            pass
        def get_articles(self, *args, **kwargs):
            raise AuthExpiredError("expired")
    monkeypatch.setattr(service_module, "WeReadMobileClient", ExpiredClient)
    monkeypatch.setattr(app.service.auth_client, "version_headers", lambda: {})
    result = app.service.sync_source(source.id)
    assert result.status == "auth_expired"
    assert app.service.account_issue() == "auth_expired"


def test_queue_can_be_used_again_after_unexpected_worker_error(app, monkeypatch):
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_29")
    original = app.service.sync_source
    def fail(*args, **kwargs):
        raise RuntimeError("local failure")
    monkeypatch.setattr(app.service, "sync_source", fail)
    app.service.queue_sync(source.id)
    wait_idle(app.service)
    assert app.db.get_source(source.id).last_status == "error"
    monkeypatch.setattr(app.service, "sync_source", original)
    monkeypatch.setattr(app.service, "_get_articles_with_auto_refresh", lambda _: [])
    app.service.queue_sync(source.id)
    wait_idle(app.service)
    assert app.db.get_source(source.id).last_status == "ok"


def test_feed_request_log_redacts_query_token(monkeypatch):
    from unittest.mock import Mock
    from wsgiref.simple_server import WSGIRequestHandler
    from web_app import QuietHandler
    monkeypatch.delenv("WECHAT_MP_QUIET", raising=False)
    logger = Mock()
    monkeypatch.setattr(WSGIRequestHandler, "log_message", logger)
    handler = object.__new__(QuietHandler)
    handler.log_message('"%s" %s %s', 'GET /feeds/1.xml?token=sensitive-token&other=1 HTTP/1.1', '200', '123')
    assert "sensitive-token" not in repr(logger.call_args)
    assert "[redacted]" in repr(logger.call_args)


def test_feed_token_rotation_resets_reader_evidence(app):
    connect(app)
    source = app.db.add_source(source_value="MP_WXS_30")
    save_article_and_success(app, source)
    app.db.verify_feed(source.id)
    app.db.record_feed_request(source.id)
    app.db.rotate_feed_token(source.id)
    source = app.db.get_source(source.id)
    assert source.feed_verified_at == 0
    assert source.last_feed_request_at == 0


def test_invalid_forwarded_host_cannot_break_diagnosis(app):
    source = app.db.add_source(source_value="MP_WXS_31")
    meta, data = call_wsgi(app, f"/sources/{source.id}", environ_overrides={"HTTP_X_FORWARDED_HOST": "[invalid"})
    assert meta["status"].startswith("200")
    assert "http://127.0.0.1/feeds/" in data.decode()


def test_returning_to_active_login_resumes_polling(app, monkeypatch):
    monkeypatch.setattr(app.login_manager, "status", lambda: {"status": "waiting", "message": "等待扫码"})
    meta, data = call_wsgi(app, "/settings")
    assert meta["status"].startswith("200")
    assert 'data-login-poll="1"' in data.decode()


@pytest.mark.parametrize("failure, expected", [(RiskControlError("-2041"), "risk_control"),
                                              (AuthExpiredError("expired"), "auth_expired")])
def test_resolving_original_link_also_persists_account_alert(app, monkeypatch, failure, expected):
    connect(app)
    class FakeClient:
        def resolve_article_url(self, review_id):
            raise failure
    monkeypatch.setattr(app.service, "_client_for", lambda _: FakeClient())
    with pytest.raises((RiskControlError, AuthExpiredError)):
        app.service._resolve_url_with_auto_refresh("demo")
    assert app.service.account_issue() == expected


def test_container_probe_uses_management_auth_and_does_not_require_connected_account(monkeypatch):
    from io import BytesIO
    from scripts import healthcheck
    monkeypatch.setenv("ADMIN_PASSWORD", "测试密码")
    request = healthcheck.build_request()
    auth = request.get_header("Authorization")
    assert base64.b64decode(auth.split(" ")[1]).decode() == "admin:测试密码"
    class Response(BytesIO):
        status = 200
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda *args, **kwargs: Response(
        b'{"ok":true,"credentials":false,"account_issue":"auth_expired"}'))
    assert healthcheck.main() == 0


def test_container_probe_fails_when_process_is_unavailable(monkeypatch):
    from scripts import healthcheck
    def unavailable(*args, **kwargs):
        raise OSError("connection refused")
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", unavailable)
    assert healthcheck.main() == 1


def test_container_probe_uses_custom_port_and_liveness_without_connected_account(monkeypatch):
    from io import BytesIO
    from scripts import healthcheck
    monkeypatch.setenv("PORT", "7860")
    assert healthcheck.build_request().full_url == "http://127.0.0.1:7860/api/health"
    class Response(BytesIO):
        status = 200
    monkeypatch.setattr(healthcheck.urllib.request, "urlopen", lambda *args, **kwargs: Response(
        b'{"alive":true,"ok":false,"credentials":{"configured":false}}'))
    assert healthcheck.main() == 0


def test_automatic_backfill_honors_account_failure_pause(app):
    app.scheduler.set_backfill_enabled(True)
    app.db.set_setting("account_issue", "risk_control")
    assert app.scheduler.maybe_backfill() is None
    assert not app.db.get_setting("backfill_last_run_at")
