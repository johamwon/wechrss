from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from weread_auth import WeReadAuthClient, WeReadAuthError, WeReadCredentials
from article_identity import ArticleIdentityError, resolve_article_identity

from wechat_mp_fetcher import (
    Article,
    AuthExpiredError,
    FetcherError,
    RiskControlError,
    WeReadMobileClient,
    article_exists,
    load_articles,
    normalize_book_id,
    open_db,
    render_rss,
    save_article,
)

APP_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL DEFAULT '',
    article_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    fetch_content INTEGER NOT NULL DEFAULT 0,
    sync_interval_minutes INTEGER NOT NULL DEFAULT 360,
    rss_limit INTEGER NOT NULL DEFAULT 50,
    last_sync_at INTEGER NOT NULL DEFAULT 0,
    next_sync_at INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT 'never',
    last_error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    started_at INTEGER NOT NULL,
    finished_at INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'running',
    received INTEGER NOT NULL DEFAULT 0,
    new_count INTEGER NOT NULL DEFAULT 0,
    content_blocked INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY(source_id) REFERENCES sources(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_sources_next_sync ON sources(enabled, next_sync_at);
CREATE INDEX IF NOT EXISTS idx_sync_runs_source_started ON sync_runs(source_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_book_id ON articles(book_id);
CREATE INDEX IF NOT EXISTS idx_articles_publish_at ON articles(publish_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_url ON articles(url);
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass
class Source:
    id: int
    book_id: str
    name: str
    article_url: str
    enabled: bool
    fetch_content: bool
    sync_interval_minutes: int
    rss_limit: int
    last_sync_at: int
    next_sync_at: int
    last_status: str
    last_error: str
    created_at: int
    updated_at: int
    article_count: int = 0


@dataclass
class SyncResult:
    source_id: int
    received: int
    new_count: int
    content_blocked: int
    status: str
    message: str = ""


class CredentialStore:
    """Persistent WeRead account state with backward compatibility for v2.

    A QR login or successful refresh becomes the active runtime record immediately and is
    saved to the local JSON file with mode 0600. Before the first runtime update, legacy
    WEREAD_ACCESS_TOKEN/WEREAD_VID environment variables still take precedence.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._runtime_record: WeReadCredentials | None = None

    def _file_data(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _env_record(self) -> WeReadCredentials | None:
        token = os.getenv("WEREAD_ACCESS_TOKEN", "").strip()
        vid = os.getenv("WEREAD_VID", "").strip()
        if not token or not vid:
            return None
        return WeReadCredentials(
            vid=vid,
            accessToken=token,
            refreshToken=os.getenv("WEREAD_REFRESH_TOKEN", "").strip(),
            deviceId=os.getenv("WEREAD_DEVICE_ID", "").strip(),
            deviceName=os.getenv("WEREAD_DEVICE_NAME", "BOOX").strip() or "BOOX",
            profile=os.getenv("WEREAD_PROFILE", "environment").strip() or "environment",
            name=os.getenv("WEREAD_ACCOUNT_NAME", "").strip(),
        )

    def get_record(self) -> WeReadCredentials:
        with self._lock:
            if self._runtime_record is not None:
                record = WeReadCredentials.from_mapping(self._runtime_record.as_json())
                record.validate_basic()
                return record
            env_record = self._env_record()
            if env_record is not None:
                env_record.validate_basic()
                return env_record
            record = WeReadCredentials.from_mapping(self._file_data())
            if not record.accessToken or not record.vid:
                raise FetcherError("尚未登录微信读书，请在设置中扫码登录")
            try:
                record.validate_basic()
            except WeReadAuthError as exc:
                raise FetcherError(str(exc)) from exc
            return record

    def get(self) -> tuple[str, str]:
        record = self.get_record()
        return record.accessToken, record.vid

    def save_record(self, record: WeReadCredentials) -> None:
        try:
            record.validate_basic()
        except WeReadAuthError as exc:
            raise FetcherError(str(exc)) from exc
        record.updatedAt = int(time.time())
        payload = record.as_json()
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            tmp.replace(self.path)
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
            self._runtime_record = WeReadCredentials.from_mapping(payload)

    def save(self, token: str, vid: str) -> None:
        token = token.strip()
        vid = vid.strip()
        if not token:
            raise FetcherError("accessToken 不能为空")
        if not vid.isdigit():
            raise FetcherError("VID 必须是数字")
        # Keep any refresh-capable fields already stored for the same account.
        old = WeReadCredentials.from_mapping(self._file_data())
        if old.vid != vid:
            old = WeReadCredentials(vid=vid, accessToken=token)
        old.vid = vid
        old.accessToken = token
        self.save_record(old)

    def clear_file(self) -> None:
        with self._lock:
            self._runtime_record = None
            if self.path.exists():
                self.path.unlink()

    def status(self) -> dict[str, Any]:
        with self._lock:
            runtime = self._runtime_record
            env_record = self._env_record()
            if runtime is not None:
                record = runtime
                source = "runtime + credentials.json"
            elif env_record is not None:
                record = env_record
                source = "environment"
            else:
                record = WeReadCredentials.from_mapping(self._file_data())
                source = "credentials.json" if record.accessToken and record.vid else "none"
        return {
            "configured": bool(record.accessToken and record.vid),
            "source": source,
            "vid": record.vid,
            "token_hint": self._mask(record.accessToken),
            "refreshable": bool(record.can_refresh and record.profile.startswith("eink")),
            "refresh_hint": self._mask(record.refreshToken),
            "device_id_hint": self._mask(record.deviceId),
            "profile": record.profile,
            "name": record.name,
            "updated_at": record.updatedAt,
            "environment_configured": env_record is not None,
        }

    @staticmethod
    def _mask(value: str) -> str:
        if not value:
            return ""
        if len(value) <= 8:
            return "*" * len(value)
        return value[:4] + "…" + value[-4:]


class AppDB:
    def __init__(self, path: Path):
        self.path = path
        with self.connect() as conn:
            conn.executescript(APP_SCHEMA)
            # v4.1 removes public-article body fetching from the Web product.
            # Keep the legacy column for database compatibility, but force it off.
            conn.execute("UPDATE sources SET fetch_content=0 WHERE fetch_content<>0")
            conn.commit()

    def connect(self) -> sqlite3.Connection:
        conn = open_db(self.path)
        conn.row_factory = sqlite3.Row
        conn.executescript(APP_SCHEMA)
        return conn

    @staticmethod
    def _row_to_source(row: sqlite3.Row) -> Source:
        return Source(
            id=row["id"], book_id=row["book_id"], name=row["name"], article_url=row["article_url"],
            enabled=bool(row["enabled"]), fetch_content=bool(row["fetch_content"]),
            sync_interval_minutes=row["sync_interval_minutes"], rss_limit=row["rss_limit"],
            last_sync_at=row["last_sync_at"], next_sync_at=row["next_sync_at"], last_status=row["last_status"],
            last_error=row["last_error"], created_at=row["created_at"], updated_at=row["updated_at"],
            article_count=row["article_count"] if "article_count" in row.keys() else 0,
        )


    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            conn.commit()

    def list_sources(self) -> list[Source]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, (SELECT COUNT(*) FROM articles a WHERE a.book_id=s.book_id) AS article_count
                FROM sources s ORDER BY s.created_at DESC
                """
            ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def get_source(self, source_id: int) -> Source | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT s.*, (SELECT COUNT(*) FROM articles a WHERE a.book_id=s.book_id) AS article_count
                FROM sources s WHERE s.id=?
                """, (source_id,)
            ).fetchone()
        return self._row_to_source(row) if row else None

    def add_source(self, *, source_value: str, name: str = "", interval_minutes: int = 360,
                   fetch_content: bool = False, rss_limit: int = 50) -> Source:
        source_value = source_value.strip()
        if source_value.startswith("http://") or source_value.startswith("https://"):
            try:
                identity = resolve_article_identity(source_value)
            except ArticleIdentityError as exc:
                raise FetcherError(str(exc)) from exc
            book_id = identity.book_id
            article_url = source_value
            if not name.strip() and identity.account_name:
                name = identity.account_name
        else:
            book_id = normalize_book_id(source_value)
            article_url = ""
        interval_minutes = max(60, min(int(interval_minutes), 10080))
        rss_limit = max(10, min(int(rss_limit), 500))
        now = int(time.time())
        with self.connect() as conn:
            try:
                cur = conn.execute(
                    """
                    INSERT INTO sources(book_id,name,article_url,enabled,fetch_content,sync_interval_minutes,rss_limit,
                                        last_sync_at,next_sync_at,last_status,last_error,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (book_id, name.strip(), article_url, 1, 0, interval_minutes, rss_limit,
                     0, now, "never", "", now, now),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise FetcherError(f"公众号 {book_id} 已存在") from exc
            source_id = int(cur.lastrowid)
        result = self.get_source(source_id)
        assert result is not None
        return result

    def update_source(self, source_id: int, *, name: str, interval_minutes: int, fetch_content: bool,
                      rss_limit: int, enabled: bool | None = None) -> None:
        interval_minutes = max(60, min(int(interval_minutes), 10080))
        rss_limit = max(10, min(int(rss_limit), 500))
        fields = ["name=?", "sync_interval_minutes=?", "fetch_content=?", "rss_limit=?", "updated_at=?"]
        values: list[Any] = [name.strip(), interval_minutes, 0, rss_limit, int(time.time())]
        if enabled is not None:
            fields.append("enabled=?")
            values.append(int(enabled))
        values.append(source_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE sources SET {', '.join(fields)} WHERE id=?", values)
            conn.commit()

    def set_enabled(self, source_id: int, enabled: bool) -> None:
        now = int(time.time())
        with self.connect() as conn:
            conn.execute("UPDATE sources SET enabled=?, next_sync_at=?, updated_at=? WHERE id=?",
                         (int(enabled), now if enabled else 0, now, source_id))
            conn.commit()

    def delete_source(self, source_id: int, delete_articles: bool = False) -> None:
        source = self.get_source(source_id)
        if not source:
            return
        with self.connect() as conn:
            conn.execute("DELETE FROM sources WHERE id=?", (source_id,))
            if delete_articles:
                conn.execute("DELETE FROM articles WHERE book_id=?", (source.book_id,))
            conn.commit()

    def due_sources(self, now: int | None = None) -> list[Source]:
        now = int(now or time.time())
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, (SELECT COUNT(*) FROM articles a WHERE a.book_id=s.book_id) AS article_count
                FROM sources s WHERE s.enabled=1 AND s.next_sync_at<=? ORDER BY s.next_sync_at ASC
                """, (now,)
            ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def mark_sync_start(self, source_id: int) -> int:
        now = int(time.time())
        with self.connect() as conn:
            cur = conn.execute("INSERT INTO sync_runs(source_id,started_at,status) VALUES(?,?,?)", (source_id, now, "running"))
            conn.execute("UPDATE sources SET last_status='running', last_error='', updated_at=? WHERE id=?", (now, source_id))
            conn.commit()
            return int(cur.lastrowid)

    def mark_sync_finish(self, source: Source, run_id: int, result: SyncResult) -> None:
        now = int(time.time())
        next_at = now + source.sync_interval_minutes * 60 if source.enabled else 0
        with self.connect() as conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?,status=?,received=?,new_count=?,content_blocked=?,message=? WHERE id=?",
                (now, result.status, result.received, result.new_count, result.content_blocked, result.message[:2000], run_id),
            )
            conn.execute(
                """UPDATE sources SET last_sync_at=?,next_sync_at=?,last_status=?,last_error=?,updated_at=? WHERE id=?""",
                (now, next_at, result.status, result.message[:2000] if result.status != "ok" else "", now, source.id),
            )
            conn.commit()

    def recent_runs(self, source_id: int | None = None, limit: int = 30) -> list[dict[str, Any]]:
        with self.connect() as conn:
            if source_id is None:
                rows = conn.execute(
                    """SELECT r.*, s.name, s.book_id FROM sync_runs r JOIN sources s ON s.id=r.source_id
                       ORDER BY r.started_at DESC LIMIT ?""", (limit,)
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT r.*, s.name, s.book_id FROM sync_runs r JOIN sources s ON s.id=r.source_id
                       WHERE r.source_id=? ORDER BY r.started_at DESC LIMIT ?""", (source_id, limit)
                ).fetchall()
        return [dict(r) for r in rows]

    def get_articles(self, book_id: str, limit: int = 100) -> list[Article]:
        with self.connect() as conn:
            return load_articles(conn, book_id, limit=limit)

    def article_by_review(self, review_id: str) -> Article | None:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
                          read_num, like_num, content_html, author
                   FROM articles WHERE review_id=?""", (review_id,)
            ).fetchone()
        if not row:
            return None
        vals = tuple(row)
        return Article(*vals[:10], content_html=vals[10], author=vals[11])

    def set_article_url(self, review_id: str, url: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE articles SET url=?, fetched_at=? WHERE review_id=?",
                         (url.strip(), int(time.time()), review_id))
            conn.commit()

    def missing_url_articles(self, book_id: str, limit: int = 5) -> list[Article]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
                          read_num, like_num, content_html, author
                   FROM articles WHERE book_id=? AND (url='' OR url IS NULL)
                   ORDER BY publish_at DESC LIMIT ?""",
                (book_id, max(1, min(int(limit), 20))),
            ).fetchall()
        return [Article(*tuple(r)[:10], content_html=tuple(r)[10], author=tuple(r)[11]) for r in rows]
    def sources_with_missing_urls(self) -> list[Source]:
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.*, (SELECT COUNT(*) FROM articles a WHERE a.book_id=s.book_id) AS article_count
                   FROM sources s WHERE s.enabled=1
                     AND EXISTS (
                       SELECT 1 FROM articles a WHERE a.book_id=s.book_id AND (a.url='' OR a.url IS NULL)
                     )
                   ORDER BY s.updated_at DESC"""
            ).fetchall()
        return [self._row_to_source(r) for r in rows]

    def search_articles(self, query: str, book_id: str | None = None, limit: int = 50) -> list[Article]:
        """Search articles by title, summary, or content.
        
        Args:
            query: Search query string
            book_id: Optional filter by source book_id
            limit: Maximum number of results (default 50, max 200)
        
        Returns:
            List of matching articles ordered by relevance
        """
        if not query or not query.strip():
            return []
        
        limit = max(1, min(int(limit), 200))
        query = query.strip()
        
        # Build search pattern for LIKE queries
        # Escape special characters and wrap with %
        pattern = f"%{query.replace('%', '%%').replace('_', '__')}%"
        
        with self.connect() as conn:
            if book_id:
                # Search within specific source
                rows = conn.execute(
                    """SELECT review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
                              read_num, like_num, content_html, author
                       FROM articles 
                       WHERE book_id=? AND (title LIKE ? OR summary LIKE ? OR content_html LIKE ?)
                       ORDER BY publish_at DESC LIMIT ?""",
                    (book_id, pattern, pattern, pattern, limit),
                ).fetchall()
            else:
                # Search across all sources
                rows = conn.execute(
                    """SELECT review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
                              read_num, like_num, content_html, author
                       FROM articles 
                       WHERE title LIKE ? OR summary LIKE ? OR content_html LIKE ?
                       ORDER BY publish_at DESC LIMIT ?""",
                    (pattern, pattern, pattern, limit),
                ).fetchall()
        
        return [Article(*tuple(r)[:10], content_html=tuple(r)[10], author=tuple(r)[11]) for r in rows]




class _PartialBackfillError(Exception):
    """Raised when backfill completes partially before hitting risk control."""
    def __init__(self, resolved: int, failed: int):
        self.resolved = resolved
        self.failed = failed
        super().__init__(f"Backfill interrupted: {resolved} resolved, {failed} failed")



    def cleanup_old_articles(self, days: int = 90) -> int:
        """Delete articles older than specified days.
        
        Args:
            days: Delete articles older than this many days (default 90)
        
        Returns:
            Number of articles deleted
        """
        cutoff = int(time.time()) - days * 24 * 3600
        with self.connect() as conn:
            result = conn.execute(
                "DELETE FROM articles WHERE publish_at > 0 AND publish_at < ?",
                (cutoff,)
            )
            deleted = result.rowcount
            conn.commit()
        return deleted


class SyncService:

    def __init__(
        self,
        db: AppDB,
        credentials: CredentialStore,
        *,
        request_interval: float = 2.0,
        timeout: float = 20.0,
        auth_client: WeReadAuthClient | None = None,
    ):
        self.db = db
        self.credentials = credentials
        self.request_interval = max(float(request_interval), 2.0)
        self.timeout = timeout
        self.auth_client = auth_client or WeReadAuthClient(timeout=timeout)
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()

    def _client_for(self, record: WeReadCredentials) -> WeReadMobileClient:
        headers = self.auth_client.version_headers() if record.profile.startswith("eink") else None
        return WeReadMobileClient(
            record.accessToken,
            record.vid,
            timeout=self.timeout,
            min_interval=self.request_interval,
            version_headers=headers,
        )

    def refresh_credentials(self) -> WeReadCredentials:
        """Refresh the active account and persist rotated credentials.

        There is no retry loop: a failed refresh is surfaced to the caller so the UI can ask for
        a new QR login instead of hammering /login.
        """
        with self._refresh_lock:
            record = self.credentials.get_record()
            if not record.can_refresh:
                raise FetcherError("当前账号没有可用的 refreshToken/deviceId，请重新扫码登录")
            if not record.profile.startswith("eink"):
                raise FetcherError("当前会话不是 Web 扫码创建的可续期会话，请重新扫码登录后再使用自动续期")
            try:
                refreshed = self.auth_client.refresh(record)
            except WeReadAuthError as exc:
                raise AuthExpiredError(str(exc)) from exc
            self.credentials.save_record(refreshed)
            return refreshed

    def _get_articles_with_auto_refresh(self, source: Source) -> list[Article]:
        record = self.credentials.get_record()
        client = self._client_for(record)
        try:
            return client.get_articles(source.book_id, count=20, synckey=0)
        except AuthExpiredError:
            # Exactly one refresh + one retry.  Risk-control errors are never used as a trigger
            # for credential rotation and are never automatically retried.
            refreshed = self.refresh_credentials()
            client = self._client_for(refreshed)
            return client.get_articles(source.book_id, count=20, synckey=0)

    def sync_source(self, source_id: int) -> SyncResult:
        source = self.db.get_source(source_id)
        if not source:
            raise FetcherError("公众号不存在")
        if not self._lock.acquire(blocking=False):
            raise FetcherError("已有同步任务正在运行，请稍后再试")
        run_id = self.db.mark_sync_start(source.id)
        try:
            try:
                articles = self._get_articles_with_auto_refresh(source)
                new_count = 0
                with self.db.connect() as conn:
                    for article in reversed(articles):
                        existed = article_exists(conn, article.review_id)
                        if not existed:
                            new_count += 1
                        save_article(conn, article)
                    conn.commit()
                result = SyncResult(source.id, len(articles), new_count, 0, "ok", "")
            except RiskControlError as exc:
                result = SyncResult(source.id, 0, 0, 0, "risk_control", str(exc))
            except AuthExpiredError as exc:
                result = SyncResult(source.id, 0, 0, 0, "auth_expired", str(exc))
            except Exception as exc:
                result = SyncResult(source.id, 0, 0, 0, "error", str(exc))
            self.db.mark_sync_finish(source, run_id, result)
            return result
        finally:
            self._lock.release()

    def _resolve_url_with_auto_refresh(self, review_id: str) -> str:
        record = self.credentials.get_record()
        client = self._client_for(record)
        try:
            return client.resolve_article_url(review_id)
        except AuthExpiredError:
            refreshed = self.refresh_credentials()
            return self._client_for(refreshed).resolve_article_url(review_id)

    def resolve_article_url(self, review_id: str) -> str:
        article = self.db.article_by_review(review_id)
        if not article:
            raise FetcherError("文章不存在")
        if article.url:
            return article.url
        url = self._resolve_url_with_auto_refresh(review_id)
        if not url:
            raise FetcherError("微信读书文章详情未返回 mpInfo.doc_url")
        self.db.set_article_url(review_id, url)
        return url

    def backfill_source_urls(self, source_id: int, limit: int = 5) -> tuple[int, int]:
        source = self.db.get_source(source_id)
        if not source:
            raise FetcherError("公众号不存在")
        if not self._lock.acquire(blocking=False):
            raise FetcherError("已有同步任务正在运行，请稍后再试")
        try:
            missing = self.db.missing_url_articles(source.book_id, limit=limit)
            if not missing:
                return 0, 0
            resolved = 0
            failed = 0
            try:
                for article in missing:
                    try:
                        url = self._resolve_url_with_auto_refresh(article.review_id)
                        if url:
                            self.db.set_article_url(article.review_id, url)
                            resolved += 1
                        else:
                            failed += 1
                    except (RiskControlError, AuthExpiredError):
                        raise
                    except Exception:
                        failed += 1
            except (RiskControlError, AuthExpiredError):
                raise _PartialBackfillError(resolved, failed)
            return resolved, failed
        finally:
            self._lock.release()

    def auto_backfill_once(self) -> dict[str, Any]:
        """Run one round of auto-backfill: randomly pick one source and backfill up to 5 articles."""
        import random
        sources = self.db.sources_with_missing_urls()
        if not sources:
            return {"status": "empty", "resolved": 0, "failed": 0, "source_id": None}
        source = random.choice(sources)
        try:
            resolved, failed = self.backfill_source_urls(source.id, limit=5)
            return {"status": "ok", "resolved": resolved, "failed": failed, "source_id": source.id, "source_name": source.name}
        except _PartialBackfillError as exc:
            return {"status": "interrupted", "resolved": exc.resolved, "failed": exc.failed, "source_id": source.id, "source_name": source.name, "error": str(exc)}
        except (RiskControlError, AuthExpiredError) as exc:
            return {"status": "error", "resolved": 0, "failed": 0, "source_id": source.id, "source_name": source.name, "error": str(exc)}

    def feed_bytes(self, source_id: int, base_url: str = "") -> bytes:
        source = self.db.get_source(source_id)
        if not source:
            raise FetcherError("公众号不存在")
        articles = self.db.get_articles(source.book_id, limit=source.rss_limit)
        title = source.name or source.book_id
        return render_rss(articles, feed_title=title, feed_link=(base_url.rstrip("/") + f"/sources/{source.id}") if base_url else "")


class Scheduler:
    def __init__(self, db: AppDB, service: SyncService, *, poll_seconds: int = 30):
        self.db = db
        self.service = service
        self.poll_seconds = max(10, int(poll_seconds))
        self.enabled = self.db.get_setting("scheduler_enabled", "1") != "0"
        self.backfill_enabled = self.db.get_setting("backfill_enabled", "0") == "1"
        self._backfill_interval = int(self.db.get_setting("backfill_interval_minutes", "60"))
        self._backfill_last_run = int(self.db.get_setting("backfill_last_run_at", "0"))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="wechat-mp-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def set_backfill_enabled(self, enabled: bool) -> None:
        self.backfill_enabled = enabled
        self.db.set_setting("backfill_enabled", "1" if enabled else "0")

    def set_backfill_interval(self, minutes: int) -> None:
        self._backfill_interval = max(60, min(int(minutes), 10080))
        self.db.set_setting("backfill_interval_minutes", str(self._backfill_interval))

    @property
    def backfill_interval(self) -> int:
        return self._backfill_interval

    @property
    def backfill_last_result(self) -> str:
        return self.db.get_setting("backfill_last_result", "")

    def maybe_backfill(self) -> dict[str, Any] | None:
        """Run a backfill round if enabled and interval has elapsed. Returns result or None."""
        if not self.backfill_enabled:
            return None
        now = int(time.time())
        if now - self._backfill_last_run < self._backfill_interval * 60:
            return None
        return self._do_backfill(now)

    def _do_backfill(self, now: int) -> dict[str, Any]:
        self._backfill_last_run = now
        self.db.set_setting("backfill_last_run_at", str(now))
        try:
            result = self.service.auto_backfill_once()
            import json
            result["timestamp"] = now
            self.db.set_setting("backfill_last_result", json.dumps(result, ensure_ascii=False))
            return result
        except Exception as exc:
            import json
            err_result = {"status": "error", "error": str(exc), "timestamp": now}
            self.db.set_setting("backfill_last_result", json.dumps(err_result, ensure_ascii=False))
            return err_result

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.enabled and self.service.credentials.status()["configured"]:
                for source in self.db.due_sources():
                    if self._stop.is_set():
                        break
                    try:
                        self.service.sync_source(source.id)
                    except Exception:
                        pass
                try:
                    self.maybe_backfill()
                except Exception:
                    pass
            self._stop.wait(self.poll_seconds)
