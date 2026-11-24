import base64
import re
from urllib.parse import urlencode

import pytest

from service import AppDB, CredentialStore, Scheduler, SyncService
from test_web import call_wsgi
import web_app
from web_app import WebApp


@pytest.mark.parametrize("allowed_frames, frames", [
    ("", ""),
    ("* http://preview.test https://*.example.test", ""),
    ("https://www.modelscope.cn * http://preview.test https://modelscope.cn",
     "https://www.modelscope.cn https://modelscope.cn"),
])
def test_admin_form_login_without_authorization_header(tmp_path, monkeypatch, allowed_frames, frames):
    password = "mock-private-密码"
    monkeypatch.setenv("ADMIN_PASSWORD", password)
    monkeypatch.setattr(web_app, "ALLOWED_FRAME_ANCESTORS", allowed_frames)
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
    if frames:
        assert "X-Frame-Options" not in meta["headers"]
        assert f"frame-ancestors {frames};" in meta["headers"]["Content-Security-Policy"]
    else:
        assert meta["headers"]["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in meta["headers"]["Content-Security-Policy"]
    assert meta["headers"]["Cache-Control"] == "no-store"
    assert password.encode() not in form
    nonce = meta["headers"]["Set-Cookie"]
    cookie_prefix = "__Host-werss_admin_frame" if frames else "__Host-werss_admin"
    expected_attributes = "SameSite=None; Secure; Partitioned" if frames else "SameSite=Lax; Secure"
    assert nonce.startswith(cookie_prefix + "_login=")
    assert expected_attributes in nonce
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
    assert cookie.startswith(cookie_prefix + "=")
    assert "HttpOnly" in cookie and expected_attributes in cookie
    authenticated = {**browser, "HTTP_COOKIE": cookie.split(";", 1)[0]}
    meta, dashboard = call_wsgi(app, "/admin", environ_overrides=authenticated)
    assert meta["status"].startswith("200")
    assert b"/admin/logout" in dashboard
    if frames:
        assert f"frame-ancestors {frames};" in meta["headers"]["Content-Security-Policy"]
    assert call_wsgi(app, "/settings", environ_overrides=authenticated)[0]["status"].startswith("200")

    payload = urlencode({"csrf": app.csrf_token}).encode()
    logout, _ = call_wsgi(app, "/admin/logout", "POST", payload, authenticated)
    assert logout["status"].startswith("303")
    cleared = logout["headers"]["Set-Cookie"]
    assert cleared.startswith(cookie_prefix + "=;")
    assert "Max-Age=0" in cleared and expected_attributes in cleared
    assert call_wsgi(app, "/admin", environ_overrides=authenticated)[0]["status"].startswith("303")

    basic = base64.b64encode(f"admin:{password}".encode()).decode()
    meta, _ = call_wsgi(app, "/api/health", environ_overrides={"HTTP_AUTHORIZATION": "Basic " + basic})
    assert meta["status"].startswith("200")
