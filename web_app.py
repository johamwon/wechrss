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
from dotenv import load_dotenv

from service import AppDB, CredentialStore, Scheduler, SyncService
from weread_auth import LoginManager, WeReadAuthClient
from wechat_mp_fetcher import FetcherError

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
DATA_DIR = Path(os.getenv("WECHAT_MP_DATA", str(BASE_DIR / "data"))).resolve()
DB_PATH = Path(os.getenv("WECHAT_MP_DB", str(DATA_DIR / "wechat_mp.db"))).resolve()
CREDENTIALS_PATH = Path(os.getenv("WECHAT_MP_CREDENTIALS", str(DATA_DIR / "credentials.json"))).resolve()
APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()

STATUS_LABELS = {
    "never": "待同步",
    "running": "同步中",
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
            super().log_message(format, *args)


class WebApp:
    def __init__(
        self, db: AppDB, credentials: CredentialStore, service: SyncService,
        scheduler: Scheduler, login_manager: LoginManager | None = None,
    ):
        self.db = db
        self.credentials = credentials
        self.service = service
        self.scheduler = scheduler
        self.login_manager = login_manager or LoginManager(service.auth_client, credentials.save_record)
        self.admin_password = os.getenv("ADMIN_PASSWORD", "")
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
        return hmac.compare_digest(username, "admin") and hmac.compare_digest(password, self.admin_password)

    def _base_url(self, environ) -> str:
        scheme = (environ.get("HTTP_X_FORWARDED_PROTO") or environ.get("wsgi.url_scheme", "http")).split(",", 1)[0].strip()
        if scheme not in {"http", "https"}:
            scheme = "http"
        host = (environ.get("HTTP_X_FORWARDED_HOST") or environ.get("HTTP_HOST") or "127.0.0.1").split(",", 1)[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", host):
            host = "127.0.0.1"
        return f"{scheme}://{host}"

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
        ctx.setdefault("scheduler_enabled", self.scheduler.enabled)
        ctx.setdefault("active_page", {
            "dashboard.html": "dashboard",
            "source_form.html": "add",
            "source_detail.html": "dashboard",
            "article.html": "dashboard",
            "settings.html": "settings",
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

    @staticmethod
    def _accepts_json(environ) -> bool:
        accept = environ.get("HTTP_ACCEPT", "")
        return "application/json" in accept

    def _require_csrf(self, form: dict[str, str]):
        if not hmac.compare_digest(form.get("csrf", ""), self.csrf_token):
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
                if asset.suffix in {".css", ".js", ".svg"}:
                    content_type += "; charset=utf-8"
                start_response("200 OK", [
                    ("Content-Type", content_type),
                    ("Content-Length", str(len(data))),
                    ("Cache-Control", "public, max-age=3600"),
                ])
                return [data]

            if path == "/" and method == "GET":
                return self._render(
                    start_response, "dashboard.html",
                    sources=self.db.list_sources(), runs=self.db.recent_runs(limit=12),
                    message=query.get("message", ""), error=query.get("error", ""), base_url=self._base_url(environ),
                )

            if path == "/sources/new" and method == "GET":
                return self._render(start_response, "source_form.html", error=query.get("error", ""))

            if path == "/sources" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                src = self.db.add_source(
                    source_value=form.get("source", ""), name=form.get("name", ""),
                    interval_minutes=int(form.get("interval_minutes") or 360),
                    fetch_content=False, rss_limit=int(form.get("rss_limit") or 50),
                )
                return self._redirect(start_response, f"/sources/{src.id}?message={quote('公众号已添加')}")

            m = re.fullmatch(r"/sources/(\d+)", path)
            if m and method == "GET":
                source_id = int(m.group(1)); source = self.db.get_source(source_id)
                if not source:
                    return self._render(start_response, "error.html", status="404 Not Found", error="没有找到这个公众号订阅，它可能已经被删除。")
                articles = self.db.get_articles(source.book_id, limit=source.rss_limit)
                return self._render(
                    start_response, "source_detail.html", source=source, articles=articles,
                    runs=self.db.recent_runs(source_id, 20), message=query.get("message", ""), error=query.get("error", ""),
                    feed_url=self._base_url(environ) + f"/feeds/{source.id}.xml",
                )

            if path == "/search" and method == "GET":
                from urllib.parse import parse_qs
                params = parse_qs(environ.get("QUERY_STRING", ""))
                query = params.get("q", [""])[0]
                book_id = params.get("source", [None])[0]
                limit = int(params.get("limit", ["50"])[0])
                
                articles = []
                sources_list = self.db.list_sources()
                if query:
                    articles = self.db.search_articles(query, book_id=book_id, limit=limit)
                
                return self._render(
                    start_response,
                    "search.html",
                    query=query,
                    source_filter=book_id or "",
                    articles=articles,
                    sources=sources_list,
                    article_count=len(articles),
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
                result = self.service.sync_source(source_id)
                if result.status == "ok":
                    msg = f"同步完成：收到 {result.received}，新增 {result.new_count}"
                    return self._redirect(start_response, f"/sources/{source_id}?message={quote(msg)}")
                return self._redirect(start_response, f"/sources/{source_id}?error={quote(result.message or result.status)}")

            m = re.fullmatch(r"/sources/(\d+)/resolve-urls", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                resolved, failed = self.service.backfill_source_urls(source_id, limit=5)
                if self._accepts_json(environ):
                    return self._json(start_response, {"success": True, "resolved": resolved, "failed": failed})
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
                if self._accepts_json(environ):
                    return self._json(start_response, {"success": True, "url": url, "review_id": review_id})
                return self._redirect(
                    start_response,
                    f"/articles/{quote(review_id, safe='')}?message={quote('原文链接已补全：' + url)}",
                )

            m = re.fullmatch(r"/feeds/(\d+)\.xml", path)
            if m and method == "GET":
                payload = self.service.feed_bytes(int(m.group(1)), self._base_url(environ))
                start_response("200 OK", [("Content-Type", "application/rss+xml; charset=utf-8"),
                                          ("Content-Length", str(len(payload))), ("Cache-Control", "public, max-age=300")])
                return [payload]

            if path == "/settings" and method == "GET":
                import json as _json
                backfill_last_result_raw = self.scheduler.backfill_last_result
                backfill_last_result = None
                if backfill_last_result_raw:
                    try:
                        backfill_last_result = _json.loads(backfill_last_result_raw)
                    except Exception:
                        pass
                return self._render(
                    start_response, "settings.html",
                    message=query.get("message", ""), error=query.get("error", ""),
                    login_status=self.login_manager.status(),
                    backfill_enabled=self.scheduler.backfill_enabled,
                    backfill_interval=self.scheduler.backfill_interval,
                    backfill_last_result=backfill_last_result,
                )

            if path == "/settings/login/start" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.login_manager.start()
                return self._redirect(start_response, "/settings#login")

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
                return self._redirect(start_response, "/settings?message=" + quote("本地凭证已保存"))

            if path == "/settings/credentials/clear" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.credentials.clear_file()
                return self._redirect(start_response, "/settings?message=" + quote("本地凭证文件已删除"))

            if path == "/settings/scheduler" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                self.scheduler.enabled = form.get("enabled") == "1"
                self.db.set_setting("scheduler_enabled", "1" if self.scheduler.enabled else "0")
                return self._redirect(start_response, "/settings?message=" + quote("调度器状态已更新"))

            if path == "/settings/backfill" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                enabled = form.get("enabled") == "1"
                interval = int(form.get("interval_minutes") or 60)
                self.scheduler.set_backfill_enabled(enabled)
                self.scheduler.set_backfill_interval(interval)
                if self._accepts_json(environ):
                    return self._json(start_response, {
                        "success": True,
                        "backfill_enabled": self.scheduler.backfill_enabled,
                        "backfill_interval_minutes": self.scheduler.backfill_interval,
                    })
                return self._redirect(start_response, "/settings?message=" + quote("自动补全设置已更新") + "#backfill")

            if path == "/settings/backfill/run" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                try:
                    result = self.service.auto_backfill_once()
                    import json as _json
                    result["timestamp"] = int(time.time())
                    self.db.set_setting("backfill_last_result", _json.dumps(result, ensure_ascii=False))
                    self.db.set_setting("backfill_last_run_at", str(result["timestamp"]))
                    if result["status"] == "empty":
                        msg = "暂无缺失原文链接的文章"
                    elif result["status"] == "ok":
                        msg = f"补全完成：成功 {result['resolved']}，失败 {result['failed']}"
                    elif result["status"] == "interrupted":
                        msg = f"补全中断（风控）：已解决 {result['resolved']}，失败 {result['failed']}"
                    else:
                        msg = f"补全失败：{result.get('error', '未知错误')}"
                    if self._accepts_json(environ):
                        return self._json(start_response, {"success": True, "result": result, "message": msg})
                    return self._redirect(start_response, "/settings?message=" + quote(msg) + "#backfill")
                except Exception as exc:
                    if self._accepts_json(environ):
                        return self._json(start_response, {"success": False, "error": str(exc)}, status="400 Bad Request")
                    return self._redirect(start_response, "/settings?error=" + quote(str(exc)) + "#backfill")

            if path == "/api/login/status" and method == "GET":
                return self._json(start_response, self.login_manager.status())

            if path == "/api/health" and method == "GET":
                cs = self.credentials.status()
                return self._json(start_response, {
                    "ok": True, "time": int(time.time()), "scheduler": self.scheduler.enabled,
                    "credentials": cs["configured"], "refreshable": cs["refreshable"],
                    "login": self.login_manager.status().get("status", "idle"), "version": APP_VERSION,
                })

            if path == "/api/sources" and method == "GET":
                data = []
                for source in self.db.list_sources():
                    item = dict(source.__dict__)
                    item.pop("fetch_content", None)
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
        auth_client, credentials.save_record,
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
