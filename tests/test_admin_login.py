import base64
import re
from urllib.parse import urlencode

from service import AppDB, CredentialStore, Scheduler, SyncService
from test_web import call_wsgi
from web_app import WebApp


def test_admin_form_login_without_authorization_header(tmp_path, monkeypatch):
    password = "mock-private-密码"
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    db = AppDB(tmp_path / "app.db")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds, account_pool=True)
    app = WebApp(db, creds, service, Scheduler(db, service))
    browser = {"HTTP_ACCEPT": "text/html", "HTTP_X_FORWARDED_PROTO": "https"}
    entry, _ = call_wsgi(app, "/admin", environ_overrides=browser)
    assert entry["status"].startswith("303")
    assert entry["headers"]["Location"] == "/admin/login"
    meta, form = call_wsgi(app, "/admin/login", environ_overrides=browser)
    assert meta["status"].startswith("200")
    assert meta["headers"]["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in meta["headers"]["Content-Security-Policy"]
    assert meta["headers"]["Cache-Control"] == "no-store"
    assert password.encode() not in form
    nonce = meta["headers"]["Set-Cookie"]
    assert nonce.startswith("__Host-werss_admin_login=")
    csrf = re.search(rb'name="csrf" value="([a-f0-9]+)"', form)[1].decode()

    def submit(value, headers):
        request = urlencode({"username": "admin", "password": value, "csrf": csrf}).encode()
        return call_wsgi(app, "/admin/login", "POST", request, headers)

    assert submit(password, browser)[0]["status"].startswith("403")
    sign_in = {**browser, "HTTP_COOKIE": nonce.split(";", 1)[0]}
    refused, _ = submit("incorrect", sign_in)
    assert refused["status"].startswith("401")
    assert "Set-Cookie" not in refused["headers"]
    logged_in, _ = submit(password, sign_in)
    assert logged_in["status"].startswith("303")
    assert logged_in["headers"]["Location"] == "/admin"
    cookie = logged_in["headers"]["Set-Cookie"]
    assert cookie.startswith("__Host-werss_admin=")
    assert all(flag in cookie for flag in ("Secure", "HttpOnly", "SameSite=Lax"))
    authenticated = {**browser, "HTTP_COOKIE": cookie.split(";", 1)[0]}
    meta, dashboard = call_wsgi(app, "/admin", environ_overrides=authenticated)
    assert meta["status"].startswith("200")
    assert b"/admin/logout" in dashboard
    assert call_wsgi(app, "/settings", environ_overrides=authenticated)[0]["status"].startswith("200")

    payload = urlencode({"csrf": app.csrf_token}).encode()
    assert call_wsgi(app, "/admin/logout", "POST", payload, authenticated)[0]["status"].startswith("303")
    assert call_wsgi(app, "/admin", environ_overrides=authenticated)[0]["status"].startswith("303")

    basic = base64.b64encode(f"admin:{password}".encode()).decode()
    meta, _ = call_wsgi(app, "/api/health", environ_overrides={"HTTP_AUTHORIZATION": "Basic " + basic})
    assert meta["status"].startswith("200")
