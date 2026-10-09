from io import BytesIO
from pathlib import Path
from wsgiref.util import setup_testing_defaults

import web_app
from service import AppDB, CredentialStore, Scheduler, SyncService


def call_wsgi(app, path="/", method="GET", body=b"", environ_overrides=None):
    env = {}
    setup_testing_defaults(env)
    env["PATH_INFO"] = path
    env["REQUEST_METHOD"] = method
    env["CONTENT_LENGTH"] = str(len(body))
    env["wsgi.input"] = BytesIO(body)
    if environ_overrides:
        env.update(environ_overrides)
    captured = {}
    def start_response(status, headers):
        captured["status"] = status
        captured["headers"] = dict(headers)
    data = b"".join(app(env, start_response))
    return captured, data


def test_health_and_dashboard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(web_app, "DATA_DIR", tmp_path)
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    # Configure credentials so health check returns ok=true
    from weread_auth import WeReadCredentials
    creds.save_record(WeReadCredentials(
        vid="123", accessToken="token", refreshToken="refresh", deviceId="dev"
    ))
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)
    meta, data = call_wsgi(app, "/api/health")
    assert meta["status"].startswith("200")
    assert b'"ok": true' in data
    assert f'"version": "{web_app.APP_VERSION}"'.encode("utf-8") in data
    # Check new health check fields
    assert b'"database"' in data
    assert b'"integrity_ok": true' in data
    assert b'"sync_24h"' in data
    assert b'"disk"' in data
    assert meta["headers"]["X-Frame-Options"] == "DENY"
    meta, data = call_wsgi(app, "/")
    assert meta["status"].startswith("200")
    assert "公众号订阅" in data.decode("utf-8")


def test_static_assets_and_security_headers(tmp_path: Path):
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    meta, data = call_wsgi(app, "/static/app.js")
    assert meta["status"].startswith("200")
    assert meta["headers"]["Content-Type"].startswith("text/javascript")
    assert b"clipboard" in data
    assert "frame-ancestors 'none'" in meta["headers"]["Content-Security-Policy"]


def test_post_error_does_not_redirect_to_external_referer(tmp_path: Path):
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    body = b"csrf=wrong&source=MP_WXS_123"
    meta, _ = call_wsgi(
        app,
        "/sources",
        method="POST",
        body=body,
        environ_overrides={"HTTP_HOST": "localhost", "HTTP_REFERER": "https://evil.example/phish"},
    )
    assert meta["status"].startswith("303")
    assert meta["headers"]["Location"].startswith("/?error=")


def test_settings_and_login_status_api(tmp_path: Path):
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    meta, data = call_wsgi(app, "/settings")
    assert meta["status"].startswith("200")
    assert "微信扫码登录" in data.decode("utf-8")

    meta, data = call_wsgi(app, "/api/login/status")
    text = data.decode("utf-8")
    assert meta["status"].startswith("200")
    assert '"status": "idle"' in text
    assert "accessToken" not in text
    assert "refreshToken" not in text


def test_content_fetch_option_is_hidden_and_forced_off(tmp_path: Path):
    from urllib.parse import urlencode

    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    meta, data = call_wsgi(app, "/sources/new")
    assert meta["status"].startswith("200")
    assert "保存文章正文" not in data.decode("utf-8")

    body = urlencode({
        "csrf": app.csrf_token,
        "source": "MP_WXS_9988",
        "name": "隐藏选项测试",
        "fetch_content": "1",
    }).encode("utf-8")
    meta, _ = call_wsgi(app, "/sources", method="POST", body=body)
    assert meta["status"].startswith("303")
    assert db.list_sources()[0].fetch_content is False
    _, data = call_wsgi(app, "/api/sources")
    assert "fetch_content" not in data.decode("utf-8")


def test_source_page_displays_full_article_url(tmp_path: Path):
    from wechat_mp_fetcher import Article, save_article

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_321", name="URL展示", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article(
            "r-visible", source.book_id, "文章",
            url="https://mp.weixin.qq.com/s/visible-token", publish_at=1700000000,
        ))
        conn.commit()
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)
    meta, data = call_wsgi(app, f"/sources/{source.id}")
    text = data.decode("utf-8")
    assert meta["status"].startswith("200")
    assert "https://mp.weixin.qq.com/s/visible-token" in text
    assert "补全缺失原文链接" in text


def test_article_resolve_url_post_route(tmp_path: Path, monkeypatch):
    from urllib.parse import urlencode
    from wechat_mp_fetcher import Article, save_article

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_654", name="补全链接", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article("r-missing", source.book_id, "缺链接文章", publish_at=1700000000))
        conn.commit()

    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    expected = "https://mp.weixin.qq.com/s/resolved-token"

    def fake_resolve(review_id: str) -> str:
        assert review_id == "r-missing"
        db.set_article_url(review_id, expected)
        return expected

    monkeypatch.setattr(service, "resolve_article_url", fake_resolve)
    body = urlencode({"csrf": app.csrf_token}).encode("utf-8")
    meta, _ = call_wsgi(app, "/articles/r-missing/resolve-url", method="POST", body=body)
    assert meta["status"].startswith("303")
    assert meta["headers"]["Location"].startswith("/articles/r-missing?message=")
    assert db.article_by_review("r-missing").url == expected


def test_article_resolve_url_post_json(tmp_path: Path, monkeypatch):
    from urllib.parse import urlencode
    import json
    from wechat_mp_fetcher import Article, save_article

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_655", name="补全链接JSON", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article("r-json", source.book_id, "JSON测试文章", publish_at=1700000000))
        conn.commit()

    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    expected = "https://mp.weixin.qq.com/s/json-token"

    def fake_resolve(review_id: str) -> str:
        assert review_id == "r-json"
        db.set_article_url(review_id, expected)
        return expected

    monkeypatch.setattr(service, "resolve_article_url", fake_resolve)
    body = urlencode({"csrf": app.csrf_token}).encode("utf-8")
    meta, data = call_wsgi(
        app, "/articles/r-json/resolve-url", method="POST", body=body,
        environ_overrides={"HTTP_ACCEPT": "application/json"},
    )
    assert meta["status"].startswith("200")
    assert meta["headers"]["Content-Type"].startswith("application/json")
    payload = json.loads(data.decode("utf-8"))
    assert payload["success"] is True
    assert payload["url"] == expected
    assert payload["review_id"] == "r-json"
    assert db.article_by_review("r-json").url == expected


def test_source_resolve_urls_post_json(tmp_path: Path, monkeypatch):
    from urllib.parse import urlencode
    import json
    from wechat_mp_fetcher import Article, save_article

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_656", name="批量补全", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article("r-batch-1", source.book_id, "文章1", publish_at=1700000000))
        save_article(conn, Article("r-batch-2", source.book_id, "文章2", publish_at=1700000001))
        conn.commit()

    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = web_app.WebApp(db, creds, service, scheduler)

    def fake_backfill(source_id: int, limit: int = 5) -> tuple[int, int]:
        assert source_id == source.id
        return 2, 0

    monkeypatch.setattr(service, "backfill_source_urls", fake_backfill)
    body = urlencode({"csrf": app.csrf_token}).encode("utf-8")
    meta, data = call_wsgi(
        app, f"/sources/{source.id}/resolve-urls", method="POST", body=body,
        environ_overrides={"HTTP_ACCEPT": "application/json"},
    )
    assert meta["status"].startswith("200")
    assert meta["headers"]["Content-Type"].startswith("application/json")
    payload = json.loads(data.decode("utf-8"))
    assert payload["success"] is True
    assert payload["resolved"] == 2
    assert payload["failed"] == 0


def test_dotenv_is_loaded_on_import(tmp_path: Path, monkeypatch):
    import os
    env_file = tmp_path / ".env"
    env_file.write_text("WERSS_TEST_DOTENV_VAR=loaded_from_dotenv\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("WERSS_TEST_DOTENV_VAR", raising=False)
    from dotenv import load_dotenv
    load_dotenv(env_file)
    assert os.getenv("WERSS_TEST_DOTENV_VAR") == "loaded_from_dotenv"
