#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from socketserver import ThreadingMixIn
from wsgiref.simple_server import make_server, WSGIRequestHandler, WSGIServer

from jinja2 import Environment, FileSystemLoader, select_autoescape

from experience import diagnose_feed, normalize_public_url, source_health
from service import AppDB, CredentialStore, Scheduler, SyncService
from weread_auth import LoginManager, WeReadAuthClient
from wechat_mp_fetcher import FetcherError

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("WECHAT_MP_DATA", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.getenv("WECHAT_MP_DB", str(DATA_DIR / "wechat_mp.db"))).resolve()
CREDENTIALS_PATH = Path(os.getenv("WECHAT_MP_CREDENTIALS", str(DATA_DIR / "credentials.json"))).resolve()
APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()

STATUS_LABELS = {
    "never": "待同步",
    "running": "同步中",
    "queued": "已排队",
    "ok": "正常",
    "paused": "已暂停",
    "risk_control": "需要验证",
    "auth_expired": "登录过期",
    "content_blocked": "同步受限",
    "error": "失败",
    "idle": "空闲",
    "requesting": "正在生成",
    "waiting": "等待扫码",
    "scanned": "等待确认",
    "exchanging": "正在登录",
    "initializing": "正在初始化",
    "success": "已登录",
    "expired": "已过期",
    "declined": "已拒绝",
    "cancelled": "已取消",
}


def fmt_ts(value: int) -> str:
    if not value:
        return "—"
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def fmt_interval(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def fmt_relative_ts(value: int) -> str:
    if not value:
        return "—"
    delta = int(value - time.time())
    future = delta > 0
    seconds = abs(delta)
    if seconds < 60:
        return "即将开始" if future else "刚刚"
    if seconds < 3600:
        amount, unit = seconds // 60, "分钟"
    elif seconds < 86400:
        amount, unit = seconds // 3600, "小时"
    elif seconds < 86400 * 30:
        amount, unit = seconds // 86400, "天"
    else:
        return fmt_ts(value)
    return f"{amount} {unit}后" if future else f"{amount} {unit}前"


def status_label(value: str) -> str:
    return STATUS_LABELS.get(value, value.replace("_", " ") if value else "未知")


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        if os.getenv("WECHAT_MP_QUIET", "") != "1":
            safe_args = tuple(re.sub(r"([?&]token=)[^&\s\"]+", r"\1[redacted]", arg)
                              if isinstance(arg, str) else arg for arg in args)
            super().log_message(format, *safe_args)


class WebApp:
    def __init__(
        self, db: AppDB, credentials: CredentialStore, service: SyncService,
        scheduler: Scheduler, login_manager: LoginManager | None = None,
    ):
        self.db = db
        self.credentials = credentials
        self.service = service
        self.scheduler = scheduler
        self.login_manager = login_manager or LoginManager(service.auth_client, service.save_credentials)
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
        self.public_base_url = normalize_public_url(os.getenv("PUBLIC_BASE_URL", ""))
        self.csrf_token = os.getenv("WECHAT_MP_CSRF_TOKEN", "") or secrets.token_urlsafe(32)
        self.env = Environment(
            loader=FileSystemLoader(str(BASE_DIR / "templates")),
            autoescape=select_autoescape(["html", "xml"]),
        )
        self.env.globals.update(
            fmt_ts=fmt_ts,
            fmt_relative_ts=fmt_relative_ts,
            fmt_interval=fmt_interval,
            status_label=status_label,
            app_version=APP_VERSION,
        )

    def _authorized(self, environ) -> bool:
        match = re.fullmatch(r"/feeds/(\d+)\.xml", environ.get("PATH_INFO", ""))
        token = self._query(environ).get("token", "")
        if match and token and environ.get("REQUEST_METHOD", "GET") in {"GET", "HEAD"}:
            source = self.db.get_source(int(match.group(1)))
            return bool(source and source.feed_token and hmac.compare_digest(token.encode(), source.feed_token.encode()))
        if not self.admin_password:
            return True
        header = environ.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Basic "):
            return False
        try:
            raw = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
            username, password = raw.split(":", 1)
        except Exception:
            return False
        return hmac.compare_digest(username.encode(), b"admin") and hmac.compare_digest(password.encode(), self.admin_password.encode())

    def _base_url(self, environ) -> str:
        scheme = (environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")).split(",", 1)[0].strip()
        if scheme not in {"http", "https"}:
            scheme = "http"
        host = (environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or "127.0.0.1").split(",", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            host = "127.0.0.1"
        try:
            return normalize_public_url(f"{scheme}://{host}")
        except FetcherError:
            return "http://127.0.0.1"

    def _feed_base_url(self, environ) -> str:
        return self.db.get_setting("public_base_url") or self.public_base_url or self._base_url(environ)

    def _feed_url(self, source, environ) -> str:
        url = self._feed_base_url(environ) + f"/feeds/{source.id}.xml"
        return url + ("?token=" + quote(source.feed_token, safe="") if source.feed_token else "")

    def _experience(self) -> dict:
        configured = self.credentials.status()["configured"]
        issue = self.service.account_issue()
        sources = self.db.list_sources()
        items = []
        for source in sources:
            health = source_health(source, configured=configured, scheduler_enabled=self.scheduler.enabled,
                                   account_issue=issue, queued=self.service.is_queued(source.id))
            items.append({"id": source.id, "name": source.name or source.book_id,
                          "status": source.last_status, "last_success_at": source.last_success_at,
                          "last_new_at": source.last_new_at, "article_count": source.article_count,
                          "health": health})
        alerts = []
        if configured and issue in {"auth_expired", "risk_control"}:
            text = ("登录已过期，请重新扫码；自动同步已暂停。" if issue == "auth_expired" else
                    "平台要求人工验证，自动同步已暂停。请在官方应用完成验证，等待后手动检查。")
            alerts.append({"key": issue, "title": "账号连接需要处理", "message": text,
                           "href": "/settings?next=onboarding#login", "action": "处理账号连接"})
        for item in items:
            health = item["health"]
            if health["code"] in {"error", "stale", "retry"}:
                alerts.append({"key": f'{item["id"]}:{health["code"]}',
                               "title": item["name"] + "：" + health["title"],
                               "message": health["message"], "href": health["href"], "action": health["action"]})
        connection_label = ("等待账号连接" if not configured else
                            "需要人工验证" if issue == "risk_control" else
                            "需要重新扫码" if issue == "auth_expired" else
                            "已暂停" if not self.scheduler.enabled else "已开启")
        return {"configured": configured, "account_issue": issue, "sync_label": connection_label,
                "sources": items, "alerts": alerts[:8],
                "onboarding_complete": any(source.feed_verified_at for source in sources)}

    def _onboarding(self) -> dict:
        sources = self.db.list_sources()
        selected = self.db.get_setting("onboarding_source_id")
        source = next((s for s in sources if str(s.id) == selected), sources[0] if sources else None)
        if not self.credentials.status()["configured"] or self.service.account_issue() in {"auth_expired", "risk_control"}:
            step = 1
        elif source is None:
            step = 2
        elif not source.last_success_at or (not source.feed_verified_at and (
                source.last_status != "ok" or self.service.is_queued(source.id))):
            step = 3
        elif not source.feed_verified_at:
            step = 4
        else:
            step = 5
        return {"step": step, "source": source}

    def _feed_context(self, source, environ, target="local") -> dict:
        return {"source": source, "feed_url": self._feed_url(source, environ),
                "diagnosis": diagnose_feed(self._feed_base_url(environ), target,
                                           password_required=bool(self.admin_password),
                                           token_available=bool(source.feed_token)),
                "health": source_health(source, configured=self.credentials.status()["configured"],
                                        scheduler_enabled=self.scheduler.enabled,
                                        account_issue=self.service.account_issue(), queued=self.service.is_queued(source.id))}

    def _safe_return_path(self, environ) -> str:
        referer = environ.get("HTTP_REFERER", "")
        if not referer:
            return "/"
        parsed = urlsplit(referer)
        expected_host = (environ.get("HTTP_HOST") or "").lower()
        if parsed.netloc and parsed.netloc.lower() != expected_host:
            return "/"
        path = parsed.path if parsed.path.startswith("/") and not parsed.path.startswith("//") else "/"
        return path + (f"?{parsed.query}" if parsed.query else "")

    def _form(self, environ) -> dict[str, str]:
        try:
            size = min(int(environ.get("CONTENT_LENGTH") or 0), 1024 * 1024)
        except ValueError:
            size = 0
        raw = environ["wsgi.input"].read(size) if size else b""
        parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def _query(self, environ) -> dict[str, str]:
        parsed = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        return {k: v[-1] if v else "" for k, v in parsed.items()}

    def _render(self, start_response, template: str, *, status="200 OK", **ctx):
        ctx.setdefault("csrf", self.csrf_token)
        ctx.setdefault("credential_status", self.credentials.status())
        ctx.setdefault("experience", self._experience())
        ctx.setdefault("source_health_by_id", {item["id"]: item["health"] for item in ctx["experience"]["sources"]})
        ctx.setdefault("scheduler_enabled", self.scheduler.enabled)
        ctx.setdefault("active_page", {
            "dashboard.html": "dashboard",
            "source_form.html": "add",
            "source_detail.html": "dashboard",
            "article.html": "dashboard",
            "settings.html": "settings",
            "onboarding.html": "dashboard",
        }.get(template, ""))
        body = self.env.get_template(template).render(**ctx).encode("utf-8")
        start_response(status, [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Length", str(len(body))),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    @staticmethod
    def _redirect(start_response, location: str):
        start_response("303 See Other", [("Location", location), ("Content-Length", "0")])
        return [b""]

    @staticmethod
    def _text(start_response, text: str, status="200 OK", content_type="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        start_response(status, [("Content-Type", content_type), ("Content-Length", str(len(body)))])
        return [body]

    @staticmethod
    def _json(start_response, payload, status="200 OK"):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        start_response(status, [("Content-Type", "application/json; charset=utf-8"), ("Content-Length", str(len(body)))])
        return [body]

    def _require_csrf(self, form: dict[str, str]):
        if not hmac.compare_digest(form.get("csrf", "").encode(), self.csrf_token.encode()):
            raise FetcherError("CSRF 校验失败，请刷新页面后重试")

    def __call__(self, environ, start_response):
        original_start_response = start_response

        def secure_start_response(status, headers, exc_info=None):
            names = {name.lower() for name, _ in headers}
            common = [
                ("X-Content-Type-Options", "nosniff"),
                ("X-Frame-Options", "DENY"),
                ("Referrer-Policy", "same-origin"),
                ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
                ("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"),
            ]
            headers = list(headers) + [(name, value) for name, value in common if name.lower() not in names]
            if exc_info is None:
                return original_start_response(status, headers)
            return original_start_response(status, headers, exc_info)

        start_response = secure_start_response
        if not self._authorized(environ):
            body = b"Authentication required"
            start_response("401 Unauthorized", [("WWW-Authenticate", 'Basic realm="wechat-mp-fetcher"'), ("Content-Length", str(len(body)))])
            return [body]

        path = environ.get("PATH_INFO", "/")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        query = self._query(environ)
        try:
            if path in {"/static/app.css", "/static/app.js", "/static/favicon.svg"} and method == "GET":
                asset = BASE_DIR / "static" / path.rsplit("/", 1)[-1]
                data = asset.read_bytes()
                content_type = mimetypes.guess_type(asset.name)[0] or "application/octet-stream"
                if asset.suffix == ".js":
                    content_type = "text/javascript"
                if asset.suffix in {".css", ".js", ".svg"}:
                    content_type += "; charset=utf-8"
                start_response("200 OK", [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(data))),
                    ("Cache-Control", "public, max-age=3600"),
                ])
                return [data]

            if path == "/" and method == "GET":
                sources = self.db.list_sources()
                return self._render(
                    start_response, "dashboard.html",
                    sources=sources, runs=self.db.recent_runs(limit=12),
                    feed_urls={s.id: self._feed_url(s, environ) for s in sources},
                    message=query.get("message", ""), error=query.get("error", ""), base_url=self._base_url(environ),
                )

            if path == "/onboarding" and method == "GET":
                onboarding = self._onboarding()
                source = onboarding["source"]
                ctx = self._feed_context(source, environ, query.get("reader", "local")) if source else {}
                return self._render(start_response, "onboarding.html", onboarding=onboarding,
                                    message=query.get("message", ""), error=query.get("error", ""), **ctx)

            if path == "/sources/new" and method == "GET":
                return self._render(start_response, "source_form.html", error=query.get("error", ""),
                                    source_value=query.get("source", ""), source_name=query.get("name", ""))

            if path == "/sources" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                try:
                    src = self.db.add_source(
                        source_value=form.get("source", ""), name=form.get("name", ""),
                        interval_minutes=int(form.get("interval_minutes") or 360),
                        fetch_content=False, rss_limit=int(form.get("rss_limit") or 50),
                    )
                except (FetcherError, ValueError) as exc:
                    return self._render(start_response, "source_form.html", status="400 Bad Request", error=str(exc),
                                        source_value=form.get("source", ""), source_name=form.get("name", ""),
                                        next_onboarding=form.get("next") == "onboarding")
                self.db.set_setting("onboarding_source_id", str(src.id))
                message = "公众号已添加，请先连接账号后开始同步"
                if self.credentials.status()["configured"] and not self.service.account_issue():
                    self.service.queue_sync(src.id)
                    message = "公众号已添加，正在安排首次同步"
                target = "/onboarding" if form.get("next") == "onboarding" else f"/sources/{src.id}"
                return self._redirect(start_response, target + "?message=" + quote(message))

            m = re.fullmatch(r"/sources/(\d+)", path)
            if m and method == "GET":
                source_id = int(m.group(1)); source = self.db.get_source(source_id)
                if not source:
                    return self._render(start_response, "error.html", status="404 Not Found", error="没有找到这个公众号订阅，它可能已经被删除。")
                articles = self.db.get_articles(source.book_id, limit=source.rss_limit)
                return self._render(
                    start_response, "source_detail.html", articles=articles,
                    runs=self.db.recent_runs(source_id, 20), message=query.get("message", ""), error=query.get("error", ""),
                    **self._feed_context(source, environ, query.get("reader", "local")),
                )

            m = re.fullmatch(r"/sources/(\d+)/edit", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                self.db.update_source(
                    source_id, name=form.get("name", ""), interval_minutes=int(form.get("interval_minutes") or 360),
                    fetch_content=False, rss_limit=int(form.get("rss_limit") or 50),
                )
                return self._redirect(start_response, f"/sources/{source_id}?message={quote('设置已保存')}")

            m = re.fullmatch(r"/sources/(\d+)/sync", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                self.service.queue_sync(source_id)
                target = "/onboarding" if form.get("next") == "onboarding" else f"/sources/{source_id}"
                return self._redirect(start_response, target)

            m = re.fullmatch(r"/sources/(\d+)/(verify|feed-token)", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                if not self.db.get_source(source_id):
                    raise FetcherError("公众号不存在")
                if m.group(2) == "verify":
                    if not self.credentials.status()["configured"] or self.service.is_queued(source_id) or self.service.account_issue():
                        raise FetcherError("请等待同步成功后，再确认阅读器中的文章")
                    self.db.verify_feed(source_id)
                    message = "订阅验证完成！以后将按设定频率自动同步。"
                else:
                    had_token = bool(self.db.get_source(source_id).feed_token)
                    self.db.rotate_feed_token(source_id)
                    message = ("阅读器专用链接已更换；请更新阅读器中的旧地址。" if had_token else
                               "阅读器专用链接已生成，请复制新地址到阅读器。")
                target = "/onboarding" if form.get("next") == "onboarding" else f"/sources/{source_id}"
                return self._redirect(start_response, target + "?message=" + quote(message))

            m = re.fullmatch(r"/sources/(\d+)/resolve-urls", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                resolved, failed = self.service.backfill_source_urls(source_id, limit=5)
                msg = f"原文链接补全：成功 {resolved}，失败 {failed}；每次最多处理 5 篇"
                return self._redirect(start_response, f"/sources/{source_id}?message={quote(msg)}")

            m = re.fullmatch(r"/sources/(\d+)/toggle", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1)); source = self.db.get_source(source_id)
                if source:
                    self.db.set_enabled(source_id, not source.enabled)
                return self._redirect(start_response, f"/sources/{source_id}")

            m = re.fullmatch(r"/sources/(\d+)/delete", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                self.db.delete_source(source_id, delete_articles=form.get("delete_articles") == "1")
                return self._redirect(start_response, "/?message=" + quote("公众号已删除"))

            m = re.fullmatch(r"/articles/([^/]+)", path)
            if m and method == "GET":
                article = self.db.article_by_review(m.group(1))
                if not article:
                    return self._render(start_response, "error.html", status="404 Not Found", error="没有找到这篇文章，它可能已经被删除。")
                return self._render(start_response, "article.html", article=article, message=query.get("message", ""), error=query.get("error", ""))

            m = re.fullmatch(r"/articles/([^/]+)/resolve-url", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                review_id = m.group(1)
                url = self.service.resolve_article_url(review_id)
                return self._redirect(
                    start_response,
                    f"/articles/{quote(review_id, safe='')}?message={quote('原文链接已补全：' + url)}",
                )

            m = re.fullmatch(r"/feeds/(\d+)\.xml", path)
            if m and method in {"GET", "HEAD"}:
                source_id = int(m.group(1))
                if not self.db.get_source(source_id):
                    return self._text(start_response, "Feed not found", status="404 Not Found")
                payload = self.service.feed_bytes(source_id, self._feed_base_url(environ))
                if method == "GET":
                    self.db.record_feed_request(source_id)
                start_response("200 OK", [("Content-Type", "application/rss+xml; charset=utf-8"),
                                          ("Content-Length", str(len(payload))), ("Cache-Control", "private, max-age=300")])
                return [payload] if method == "GET" else []

            if path == "/settings" and method == "GET":
                login_state = self.login_manager.status()
                return self._render(
                    start_response, "settings.html",
                    message=query.get("message", ""), error=query.get("error", ""),
                    login_status=login_state,
                    login_poll=query.get("login") == "1" or login_state["status"] in {
                        "requesting", "waiting", "scanned", "exchanging", "initializing"},
                    login_return="/onboarding" if query.get("next") == "onboarding" else "/settings?message=" + quote("扫码登录成功"),
                    next_onboarding=query.get("next") == "onboarding",
                    public_base_url=self.db.get_setting("public_base_url") or self.public_base_url,
                )

            if path == "/settings/feed-address" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.db.set_setting("public_base_url", normalize_public_url(form.get("public_base_url", "")))
                target = "/onboarding" if form.get("next") == "onboarding" else "/settings"
                return self._redirect(start_response, target + "?message=" + quote("Feed 地址已保存，请在阅读器中验证"))

            if path == "/settings/login/start" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.login_manager.start()
                target = "/settings?login=1" + ("&next=onboarding" if form.get("next") == "onboarding" else "")
                return self._redirect(start_response, target + "#login")

            if path == "/settings/login/cancel" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.login_manager.cancel()
                return self._redirect(start_response, "/settings#login")

            if path == "/settings/session/refresh" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                record = self.service.refresh_credentials()
                account = record.name or record.vid
                return self._redirect(start_response, "/settings?message=" + quote(f"凭证续期成功：{account}"))

            if path == "/settings/credentials" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.credentials.save(form.get("access_token", ""), form.get("vid", ""))
                self.service.clear_account_issue()
                return self._redirect(start_response, "/settings?message=" + quote("本地凭证已保存"))

            if path == "/settings/credentials/clear" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.credentials.clear_file()
                self.service.clear_account_issue()
                return self._redirect(start_response, "/settings?message=" + quote("本地凭证文件已删除"))

            if path == "/settings/scheduler" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.scheduler.enabled = form.get("enabled") == "1"
                self.db.set_setting("scheduler_enabled", "1" if self.scheduler.enabled else "0")
                return self._redirect(start_response, "/settings?message=" + quote("调度器状态已更新"))

            if path == "/api/login/status" and method == "GET":
                return self._json(start_response, self.login_manager.status())

            if path == "/api/experience" and method == "GET":
                return self._json(start_response, self._experience())

            m = re.fullmatch(r"/api/sources/(\d+)/status", path)
            if m and method == "GET":
                item = next((item for item in self._experience()["sources"] if item["id"] == int(m.group(1))), None)
                return self._json(start_response, item or {"error": "公众号不存在"},
                                  status="200 OK" if item else "404 Not Found")

            if path == "/api/health" and method == "GET":
                cs = self.credentials.status()
                return self._json(start_response, {
                    "ok": True, "time": int(time.time()), "scheduler": self.scheduler.enabled,
                    "credentials": cs["configured"], "refreshable": cs["refreshable"],
                    "login": self.login_manager.status().get("status", "idle"), "version": APP_VERSION,
                    "account_issue": self.service.account_issue(),
                })

            if path == "/api/sources" and method == "GET":
                data = []
                for source in self.db.list_sources():
                    item = dict(source.__dict__)
                    item.pop("fetch_content", None)
                    item.pop("feed_token", None)
                    data.append(item)
                return self._json(start_response, data)

            return self._render(start_response, "error.html", status="404 Not Found", error="这个页面不存在，或地址已经发生变化。")
        except (FetcherError, ValueError) as exc:
            if method == "POST":
                target = self._safe_return_path(environ)
                sep = "&" if "?" in target else "?"
                return self._redirect(start_response, target + sep + "error=" + quote(str(exc)))
            return self._render(start_response, "error.html", status="400 Bad Request", error=str(exc))
        except Exception as exc:
            print(f"[web-error] {exc}", file=sys.stderr)
            return self._render(start_response, "error.html", status="500 Internal Server Error", error=str(exc))


def build_runtime():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = AppDB(DB_PATH)
    credentials = CredentialStore(CREDENTIALS_PATH)
    timeout = float(os.getenv("HTTP_TIMEOUT", "20"))
    auth_client = WeReadAuthClient(timeout=timeout)
    service = SyncService(
        db, credentials,
        request_interval=float(os.getenv("WEREAD_MIN_INTERVAL", "2")),
        timeout=timeout,
        auth_client=auth_client,
    )
    scheduler = Scheduler(db, service, poll_seconds=int(os.getenv("SCHEDULER_POLL_SECONDS", "30")))
    login_manager = LoginManager(
        auth_client, service.save_credentials,
        deadline_seconds=int(os.getenv("WEREAD_QR_DEADLINE_SECONDS", "300")),
    )
    app = WebApp(db, credentials, service, scheduler, login_manager)
    return app, scheduler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="WeRSS Web 管理端")
    parser.add_argument("--host", default=os.getenv("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8080")))
    args = parser.parse_args(argv)
    app, scheduler = build_runtime()
    scheduler.start()
    print(f"WeRSS Web: http://{args.host}:{args.port}")
    if not os.getenv("ADMIN_PASSWORD") and args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("[warning] ADMIN_PASSWORD 未设置；不要把无认证管理端暴露到公网。", file=sys.stderr)
    try:
        with make_server(args.host, args.port, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler) as server:
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        scheduler.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
