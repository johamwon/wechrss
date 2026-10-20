#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hmac
import json
import logging
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
# 允许以 iframe 嵌入本站的源（空格分隔）；留空则禁止嵌入（X-Frame-Options: DENY + frame-ancestors 'none'）。
# 部署到 ModelScope 创空间等 iframe 平台时设置为对应域名，如 https://www.modelscope.cn
ALLOWED_FRAME_ANCESTORS = os.getenv("ALLOWED_FRAME_ANCESTORS", "").strip()
APP_VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()

logger = logging.getLogger("wechrss.web")

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
            frame_ancestors = ALLOWED_FRAME_ANCESTORS or "'none'"
            common = [
                ("X-Content-Type-Options", "nosniff"),
                ("Referrer-Policy", "same-origin"),
                ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
                ("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors %s; base-uri 'self'; form-action 'self'" % frame_ancestors),
            ]
            if not ALLOWED_FRAME_ANCESTORS:
                # 未显式放行时保留 X-Frame-Options 兜底（旧浏览器不支持 frame-ancestors）
                common.append(("X-Frame-Options", "DENY"))
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
            if path in {"/static/app.css", "/static/app.js", "/static/favicon.svg", "/static/qrcode.png"} and method == "GET":
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
                    filter_rules=self.db.get_filter_rules(source_id),
                )

            if path == "/statistics" and method == "GET":
                stats = self.db.get_statistics()
                return self._render(start_response, "statistics.html", stats=stats)

            if path == "/search" and method == "GET":
                q = query.get("q", "").strip()
                book_id_filter = query.get("book_id", "").strip() or None
                articles = self.db.search_articles(q, book_id=book_id_filter) if q else []
                if self._accepts_json(environ):
                    return self._json(start_response, [{"title": a.title, "review_id": a.review_id} for a in articles])
                return self._render(
                    start_response, "search.html", query=q, articles=articles,
                    book_id=book_id_filter or "",
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

            if path == "/sources/batch/delete" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                raw_ids = form.get("source_ids", "")
                ids = [int(x) for x in raw_ids.replace(",", " ").split() if x.strip().isdigit()]
                if ids:
                    deleted = self.db.delete_sources_batch(ids)
                    return self._redirect(start_response, "/?message=" + quote(f"已删除 {deleted} 个订阅"))
                return self._redirect(start_response, "/?error=" + quote("没有选择任何订阅"))

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
                    webhooks=self.db.list_webhooks(),
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
                action = form.get("action", "toggle")
                if action == "toggle":
                    self.scheduler.set_backfill_enabled(not self.scheduler.backfill_enabled)
                    return self._redirect(start_response, "/settings?message=" + quote("自动补全状态已更新") + "#backfill")
                if action == "save":
                    interval = int(form.get("interval_minutes") or 60)
                    self.scheduler.set_backfill_interval(interval)
                    return self._redirect(start_response, "/settings?message=" + quote("补全设置已保存") + "#backfill")
                return self._redirect(start_response, "/settings#backfill")

            if path == "/settings/webhooks" and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                url = form.get("url", "").strip()
                event_type = form.get("event_type", "all").strip()
                if not url:
                    return self._redirect(start_response, "/settings?error=" + quote("Webhook URL 不能为空") + "#webhooks")
                try:
                    self.db.add_webhook(url, event_type)
                    logger.info("Webhook added: %s", url)
                    return self._redirect(start_response, "/settings?message=" + quote("Webhook 已添加") + "#webhooks")
                except Exception as exc:
                    logger.error("Failed to add webhook: %s", exc)
                    return self._redirect(start_response, "/settings?error=" + quote(f"添加失败：{exc}") + "#webhooks")

            m = re.fullmatch(r"/settings/webhooks/(\d+)/delete", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                webhook_id = int(m.group(1))
                try:
                    self.db.delete_webhook(webhook_id)
                    logger.info("Webhook deleted: %d", webhook_id)
                    return self._redirect(start_response, "/settings?message=" + quote("Webhook 已删除") + "#webhooks")
                except Exception as exc:
                    logger.error("Failed to delete webhook: %s", exc)
                    return self._redirect(start_response, "/settings?error=" + quote(f"删除失败：{exc}") + "#webhooks")

            m = re.fullmatch(r"/settings/webhooks/(\d+)/toggle", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                webhook_id = int(m.group(1))
                webhooks = self.db.list_webhooks()
                wh = next((w for w in webhooks if w["id"] == webhook_id), None)
                if wh:
                    self.db.update_webhook(webhook_id, enabled=not bool(wh.get("enabled")))
                return self._redirect(start_response, "/settings?message=" + quote("Webhook 状态已更新") + "#webhooks")

            m = re.fullmatch(r"/sources/(\d+)/filters", path)
            if m and method == "POST":
                form = self._form(environ); self._require_csrf(form)
                source_id = int(m.group(1))
                patterns_raw = form.get("filter_patterns", "").strip()
                rules = []
                for line in patterns_raw.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("-"):
                        rules.append({"type": "exclude", "pattern": line[1:].strip()})
                    elif line.startswith("+"):
                        rules.append({"type": "include", "pattern": line[1:].strip()})
                    else:
                        rules.append({"type": "include", "pattern": line})
                self.db.set_filter_rules(source_id, rules)
                return self._redirect(start_response, f"/sources/{source_id}?message={quote('过滤规则已保存')}")

            if path == "/settings/backup" and method == "GET":
                try:
                    backup_path = self.db.create_backup()
                    # Read the backup file
                    backup_data = backup_path.read_bytes()
                    # Clean up the temporary backup file after reading
                    backup_path.unlink()
                    
                    filename = f"wechrss-backup-{time.strftime('%Y%m%d-%H%M%S')}.db"
                    start_response("200 OK", [
                        ("Content-Type", "application/octet-stream"),
                        ("Content-Disposition", f'attachment; filename="{filename}"'),
                        ("Content-Length", str(len(backup_data))),
                    ])
                    logger.info("Database backup downloaded: %s", filename)
                    return [backup_data]
                except Exception as exc:
                    logger.error("Backup failed: %s", exc)
                    return self._redirect(start_response, "/settings?error=" + quote(f"备份失败：{exc}") + "#backup")

            if path == "/api/login/status" and method == "GET":
                return self._json(start_response, self.login_manager.status())

            if path == "/api/health" and method == "GET":
                import shutil
                cs = self.credentials.status()
                integrity = self.db.check_integrity()
                db_stats = self.db.get_stats()
                sync_stats = self.db.get_recent_sync_stats(hours=24)
                try:
                    disk = shutil.disk_usage(str(DATA_DIR))
                    disk_info = {"free_gb": round(disk.free / (1 << 30), 2), "total_gb": round(disk.total / (1 << 30), 2), "usage_percent": round(disk.used / disk.total * 100, 1)}
                except Exception:
                    disk_info = {"free_gb": 0, "total_gb": 0, "usage_percent": 0}
                overall_ok = integrity["ok"] and cs["configured"]
                return self._json(start_response, {
                    "ok": overall_ok, "time": int(time.time()), "version": APP_VERSION,
                    "scheduler": self.scheduler.enabled,
                    "credentials": {"configured": cs["configured"], "refreshable": cs["refreshable"], "login_status": self.login_manager.status().get("status", "idle")},
                    "database": {"integrity_ok": integrity["ok"], "integrity_errors": integrity["errors"], "size_bytes": db_stats["db_size_bytes"], "sources": db_stats["sources"], "articles": db_stats["articles"]},
                    "sync_24h": sync_stats,
                    "disk": disk_info,
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
