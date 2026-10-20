#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

from article_identity import ArticleIdentityError, resolve_article_identity

MOBILE_BASE = "https://i.weread.qq.com"
MP_BASE = "https://mp.weixin.qq.com"
MOBILE_UA = "WeRead/9.2.3 WRBrand/huawei Dalvik/2.1.0 (Linux; U; Android 12; BRA-AL00 Build/W528JS)"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


class FetcherError(RuntimeError):
    pass


class AuthExpiredError(FetcherError):
    pass


class RiskControlError(FetcherError):
    pass


class ContentBlockedError(FetcherError):
    pass


@dataclass
class Article:
    review_id: str
    book_id: str
    title: str
    summary: str = ""
    cover_url: str = ""
    url: str = ""
    publish_at: int = 0
    original_id: str = ""
    read_num: int = 0
    like_num: int = 0
    content_html: str = ""
    author: str = ""
    raw: dict[str, Any] | None = None


def _pad_b64(value: str) -> str:
    value = value.strip()
    return value + "=" * ((4 - len(value) % 4) % 4)


def decode_biz(value: str) -> str:
    value = unquote(value).strip()
    try:
        decoded = base64.b64decode(_pad_b64(value), validate=False).decode("utf-8").strip()
    except Exception as exc:
        raise FetcherError(f"biz 不是有效 Base64: {exc}") from exc
    if not decoded.isdigit():
        raise FetcherError(f"biz 解码结果不是数字公众号 ID: {decoded!r}")
    return decoded


def book_id_from_article_url(url: str) -> str:
    """Resolve both legacy long URLs and current /s/<token> permanent links."""
    try:
        return resolve_article_identity(url).book_id
    except ArticleIdentityError as exc:
        raise FetcherError(str(exc)) from exc


def normalize_book_id(value: str) -> str:
    value = value.strip()
    if value.startswith("MP_WXS_"):
        suffix = value.removeprefix("MP_WXS_")
        if not suffix.isdigit():
            raise FetcherError("MP_WXS_ 后必须是数字公众号 ID")
        return value
    if value.isdigit():
        return f"MP_WXS_{value}"
    raise FetcherError("book id 应为 MP_WXS_<数字> 或纯数字 BID")


def synthesized_article_url(original_id: str) -> str:
    """把裸 originalId 当作微信短链 token 拼出来的占位地址。

    当 originalId 本身已是 http(s) 链接、/s 路径、? 查询串或含 __biz= 时，
    视为上游给出的可信来源，返回空串表示「这不是拼出来的占位猜测」。
    """
    original = str(original_id or "").strip()
    if not original or original.startswith(("http://", "https://", "/s")):
        return ""
    if "__biz=" in original or original.startswith("?"):
        return ""
    return f"{MP_BASE}/s/{quote(original, safe='._~-')}"


def is_synthesized_article_url(url: str, original_id: str) -> bool:
    """判断 url 是否只是从 originalId 拼出来的占位猜测，而非上游直接给出的链接。"""
    return bool(url) and url == synthesized_article_url(original_id or "")


def article_url_from_mpinfo(mp_info: dict[str, Any], review: dict[str, Any]) -> str:
    for candidate in (mp_info.get("doc_url"), mp_info.get("docUrl"), mp_info.get("url"), review.get("url")):
        if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
            return candidate
    original = str(mp_info.get("originalId") or "").strip()
    if not original:
        return ""
    if original.startswith(("http://", "https://")):
        return original
    if original.startswith("/s"):
        return MP_BASE + original
    if "__biz=" in original or original.startswith("?"):
        return f"{MP_BASE}/s?{original.lstrip('?')}"
    return synthesized_article_url(original)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _extract_err(payload: dict[str, Any]) -> tuple[int, str]:
    code = payload.get("errcode", payload.get("errCode", 0))
    code = _to_int(code, 0)
    msg = str(payload.get("errmsg") or payload.get("errMsg") or "")
    return code, msg


def check_weread_error(payload: dict[str, Any]) -> None:
    code, msg = _extract_err(payload)
    if not code:
        return
    if code == -2012:
        raise AuthExpiredError(f"微信读书登录状态失效: {msg or code}")
    if code in {-2041, -2010}:
        raise RiskControlError(f"微信读书返回风控/限频错误 {code}: {msg or code}")
    raise FetcherError(f"微信读书接口错误 {code}: {msg or code}")


def _article_from_entry(entry: dict[str, Any], book_id: str, *, group_time: int = 0) -> Article | None:
    review = entry.get("review") if isinstance(entry.get("review"), dict) else entry
    review_id = str(review.get("reviewId") or entry.get("reviewId") or "").strip()
    mp_info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
    title = str(review.get("title") or mp_info.get("title") or "").strip()
    if not review_id or not title:
        return None
    publish_at = _to_int(mp_info.get("time") or review.get("createTime") or group_time, 0)
    return Article(
        review_id=review_id,
        book_id=book_id,
        title=title,
        summary=str(mp_info.get("content") or review.get("content") or ""),
        cover_url=str(mp_info.get("pic_url") or mp_info.get("picUrl") or ""),
        url=article_url_from_mpinfo(mp_info, review),
        publish_at=publish_at,
        original_id=str(mp_info.get("originalId") or ""),
        read_num=_to_int(mp_info.get("readNum"), 0),
        like_num=_to_int(mp_info.get("likeNum"), 0),
        author=str(mp_info.get("mp_name") or mp_info.get("mpName") or ""),
        raw=entry,
    )


def parse_articles_payload(payload: dict[str, Any], book_id: str) -> list[Article]:
    """Parse both the current /mp/chapters and legacy /book/articles envelopes."""
    check_weread_error(payload)
    result: list[Article] = []

    # Current mobile API (2026): {data: [{reviewId,title,createTime,mpInfo}, ...]}
    data = payload.get("data")
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            article = _article_from_entry(entry, book_id)
            if article is not None:
                result.append(article)
        result.sort(key=lambda x: x.publish_at, reverse=True)
        return result

    # Legacy Wechat2RSS v1.4.9 shape: reviews[].subReviews[].review
    reviews = payload.get("reviews") or []
    if not isinstance(reviews, list):
        return result
    for group in reviews:
        if not isinstance(group, dict):
            continue
        group_time = _to_int(group.get("createTime"), 0)
        sub_reviews = group.get("subReviews") or []
        if not isinstance(sub_reviews, list):
            continue
        for sub in sub_reviews:
            if not isinstance(sub, dict):
                continue
            article = _article_from_entry(sub, book_id, group_time=group_time)
            if article is not None:
                article.raw = {"group": group, "sub": sub}
                result.append(article)
    result.sort(key=lambda x: x.publish_at, reverse=True)
    return result


class RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self.min_interval = max(float(min_interval), 0.0)
        self.last = 0.0

    def wait(self) -> None:
        if not self.last:
            self.last = time.monotonic()
            return
        elapsed = time.monotonic() - self.last
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self.last = time.monotonic()


class WeReadMobileClient:
    def __init__(
        self,
        access_token: str,
        vid: str | int,
        *,
        timeout: float = 20.0,
        min_interval: float = 2.0,
        version_headers: dict[str, str] | None = None,
    ) -> None:
        if not access_token:
            raise FetcherError("缺少 accessToken")
        if not str(vid).isdigit():
            raise FetcherError("VID 必须是数字")
        self.session = requests.Session()
        self.timeout = timeout
        self.limiter = RateLimiter(min_interval)
        headers = {
            "accessToken": access_token,
            "vid": str(vid),
            "User-Agent": MOBILE_UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if version_headers:
            headers.update(version_headers)
            # Authentication values always belong to the active account.
            headers["accessToken"] = access_token
            headers["vid"] = str(vid)
        self.session.headers.update(headers)

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        self.limiter.wait()
        response = self.session.get(MOBILE_BASE + path, params=params, timeout=self.timeout)
        if response.status_code in {401, 403}:
            raise AuthExpiredError(f"HTTP {response.status_code}: 登录状态可能已失效")
        if response.status_code == 429:
            raise RiskControlError("HTTP 429: 请求过于频繁")

        payload: dict[str, Any] | None = None
        try:
            candidate = response.json()
            if isinstance(candidate, dict):
                payload = candidate
        except ValueError:
            payload = None

        # WeRead also reports business failures in JSON bodies, sometimes with HTTP 200.
        if payload is not None:
            check_weread_error(payload)

        if not response.ok:
            body_hint = response.text[:200].replace("\n", " ").strip()
            raise FetcherError(
                f"微信读书接口 HTTP {response.status_code} {path}: {body_hint!r}; "
                "请求形态或当前客户端画像可能不被该接口接受"
            )
        if payload is None:
            raise FetcherError(f"接口返回非 JSON: {response.text[:160]!r}")
        return payload

    def get_info(self, book_id: str) -> dict[str, Any]:
        return self._get_json("/book/info", {"bookId": normalize_book_id(book_id)})

    def get_articles(
        self,
        book_id: str,
        *,
        count: int = 20,
        offset: int | None = None,
        synckey: int = 0,
    ) -> list[Article]:
        """Fetch public-account articles through the current mobile endpoint.

        The first request uses synckey and MUST NOT include offset. Paging requests use
        offset and MUST NOT include synckey. Mixing both shapes is rejected by current
        WeRead clients/servers.
        """
        book_id = normalize_book_id(book_id)
        count = max(1, min(int(count), 50))
        if offset is None:
            params = {
                "bookId": book_id,
                "count": count,
                "synckey": max(int(synckey), 0),
            }
        else:
            params = {
                "bookId": book_id,
                "count": count,
                "offset": max(int(offset), 0),
            }
        payload = self._get_json("/mp/chapters", params)
        return parse_articles_payload(payload, book_id)

    def get_review_single(self, review_id: str) -> dict[str, Any]:
        review_id = str(review_id).strip()
        if not review_id:
            raise FetcherError("reviewId 不能为空")
        return self._get_json(
            "/review/single",
            {
                "reviewId": review_id,
                "commentsCount": 10,
                "commentsDirection": 0,
                "likesCount": 10,
                "likesDirection": 0,
                "synckey": 0,
            },
        )

    def resolve_article_url(self, review_id: str) -> str:
        payload = self.get_review_single(review_id)
        review = payload.get("review") if isinstance(payload.get("review"), dict) else payload
        mp_info = review.get("mpInfo") if isinstance(review.get("mpInfo"), dict) else {}
        return article_url_from_mpinfo(mp_info, review)

    def get_articles_legacy(
        self, book_id: str, *, count: int = 20, offset: int = 0, synckey: int = 0
    ) -> list[Article]:
        """Explicit legacy Wechat2RSS v1.4.9 route; not used by the Web scheduler."""
        book_id = normalize_book_id(book_id)
        count = max(1, min(int(count), 20))
        payload = self._get_json(
            "/book/articles",
            {"bookId": book_id, "offset": max(int(offset), 0), "count": count, "synckey": max(int(synckey), 0)},
        )
        return parse_articles_payload(payload, book_id)


CHALLENGE_MARKERS = ("当前环境异常", "完成验证后即可继续访问", "访问过于频繁", "操作频繁")


def fetch_public_article(url: str, *, timeout: float = 20.0) -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise FetcherError("文章 URL 无效")
    headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://mp.weixin.qq.com/",
    }
    response = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
    response.raise_for_status()
    text = response.text
    if any(marker in text for marker in CHALLENGE_MARKERS):
        raise ContentBlockedError("微信公众号页面要求验证/触发风控；工具不会尝试绕过，请稍后重试")
    soup = BeautifulSoup(text, "html.parser")
    content = soup.select_one("#js_content") or soup.select_one(".rich_media_content")
    if content is None:
        raise ContentBlockedError("页面中没有找到 #js_content，可能是特殊文章类型或环境限制")
    for node in content.select("script, style, iframe, object, embed, form"):
        node.decompose()
    # Web 管理端会直接展示已保存正文，因此去掉事件处理器和 javascript: URL。
    for node in content.find_all(True):
        for attr in list(node.attrs):
            if attr.lower().startswith("on"):
                del node.attrs[attr]
        for attr in ("href", "src"):
            value = node.get(attr)
            if isinstance(value, str) and value.strip().lower().startswith("javascript:"):
                del node.attrs[attr]
    for img in content.select("img"):
        data_src = img.get("data-src") or img.get("data-original")
        if data_src and not img.get("src"):
            img["src"] = data_src
    title_node = soup.select_one("#activity-name") or soup.select_one("h1.rich_media_title")
    author_node = soup.select_one("#js_name") or soup.select_one(".rich_media_meta_nickname")
    return {
        "url": response.url,
        "title": title_node.get_text(" ", strip=True) if title_node else "",
        "author": author_node.get_text(" ", strip=True) if author_node else "",
        "content_html": str(content),
    }


ARTICLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    review_id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    cover_url TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    publish_at INTEGER NOT NULL DEFAULT 0,
    original_id TEXT NOT NULL DEFAULT '',
    read_num INTEGER NOT NULL DEFAULT 0,
    like_num INTEGER NOT NULL DEFAULT 0,
    author TEXT NOT NULL DEFAULT '',
    content_html TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT '{}',
    fetched_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_book_publish ON articles(book_id, publish_at DESC);
"""


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(ARTICLE_SCHEMA)
    return conn


def article_exists(conn: sqlite3.Connection, review_id: str) -> bool:
    return conn.execute("SELECT 1 FROM articles WHERE review_id=?", (review_id,)).fetchone() is not None


def save_article(conn: sqlite3.Connection, article: Article) -> None:
    conn.execute(
        """
        INSERT INTO articles(
            review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
            read_num, like_num, author, content_html, raw_json, fetched_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(review_id) DO UPDATE SET
            title=excluded.title, summary=excluded.summary, cover_url=excluded.cover_url,
            url=CASE
                WHEN excluded.url='' THEN articles.url
                WHEN ? AND articles.url<>'' THEN articles.url
                ELSE excluded.url
            END,
            publish_at=excluded.publish_at,
            original_id=CASE WHEN excluded.original_id<>'' THEN excluded.original_id ELSE articles.original_id END,
            read_num=excluded.read_num, like_num=excluded.like_num,
            author=CASE WHEN excluded.author<>'' THEN excluded.author ELSE articles.author END,
            content_html=CASE WHEN excluded.content_html<>'' THEN excluded.content_html ELSE articles.content_html END,
            raw_json=excluded.raw_json, fetched_at=excluded.fetched_at
        """,
        (
            article.review_id, article.book_id, article.title, article.summary, article.cover_url, article.url,
            article.publish_at, article.original_id, article.read_num, article.like_num, article.author,
            article.content_html, json.dumps(article.raw or {}, ensure_ascii=False), int(time.time()),
            int(is_synthesized_article_url(article.url, article.original_id)),
        ),
    )


def load_articles(conn: sqlite3.Connection, book_id: str, limit: int = 50) -> list[Article]:
    rows = conn.execute(
        """
        SELECT review_id, book_id, title, summary, cover_url, url, publish_at, original_id,
               read_num, like_num, content_html, author
        FROM articles WHERE book_id=? ORDER BY publish_at DESC LIMIT ?
        """,
        (book_id, max(1, int(limit))),
    ).fetchall()
    return [Article(*row[:10], content_html=row[10], author=row[11]) for row in rows]


def render_rss(articles: Iterable[Article], *, feed_title: str, feed_link: str = "") -> bytes:
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = feed_title
    ET.SubElement(channel, "link").text = feed_link or "https://mp.weixin.qq.com/"
    ET.SubElement(channel, "description").text = f"Generated by wechat-mp-fetcher for {feed_title}"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    for article in articles:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = article.title
        ET.SubElement(item, "link").text = article.url
        ET.SubElement(item, "guid", {"isPermaLink": "false"}).text = article.review_id
        if article.publish_at:
            ET.SubElement(item, "pubDate").text = format_datetime(datetime.fromtimestamp(article.publish_at, tz=timezone.utc))
        ET.SubElement(item, "description").text = article.summary
        if article.content_html:
            ET.SubElement(item, "{http://purl.org/rss/1.0/modules/content/}encoded").text = article.content_html
    buf = io.BytesIO()
    ET.ElementTree(rss).write(buf, encoding="utf-8", xml_declaration=True)
    return buf.getvalue()


def generate_rss(articles: Iterable[Article], output: Path, *, feed_title: str, feed_link: str = "") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(render_rss(articles, feed_title=feed_title, feed_link=feed_link))


def load_credentials(path: str | None) -> tuple[str, str]:
    token = os.getenv("WEREAD_ACCESS_TOKEN", "").strip()
    vid = os.getenv("WEREAD_VID", "").strip()
    if path:
        payload = json.loads(Path(path).read_text("utf-8"))
        token = str(payload.get("accessToken") or payload.get("access_token") or token).strip()
        vid = str(payload.get("vid") or payload.get("v_id") or vid).strip()
    if not token or not vid:
        raise FetcherError("请通过环境变量 WEREAD_ACCESS_TOKEN/WEREAD_VID 或 --credentials 提供自己的有效凭证")
    return token, vid


def resolve_book_id(args: argparse.Namespace) -> str:
    if getattr(args, "book_id", None):
        return normalize_book_id(args.book_id)
    if getattr(args, "article_url", None):
        return book_id_from_article_url(args.article_url)
    raise FetcherError("需要 --book-id 或 --article-url")


def command_resolve(args: argparse.Namespace) -> int:
    try:
        identity = resolve_article_identity(args.url, timeout=args.timeout, browser_fallback=not args.no_browser)
    except ArticleIdentityError as exc:
        raise FetcherError(str(exc)) from exc
    print(json.dumps(identity.to_dict(), ensure_ascii=False, indent=2))
    return 0


def command_bookid(args: argparse.Namespace) -> int:
    print(book_id_from_article_url(args.url)); return 0


def command_list(args: argparse.Namespace) -> int:
    token, vid = load_credentials(args.credentials)
    book_id = resolve_book_id(args)
    articles = WeReadMobileClient(token, vid, min_interval=args.min_interval).get_articles(
        book_id, count=args.count, offset=args.offset, synckey=args.synckey
    )
    payload = [asdict(a) for a in articles]
    for item in payload:
        item.pop("raw", None); item.pop("content_html", None)
    print(json.dumps(payload, ensure_ascii=False, indent=2)); return 0


def command_sync(args: argparse.Namespace) -> int:
    token, vid = load_credentials(args.credentials)
    book_id = resolve_book_id(args)
    articles = WeReadMobileClient(token, vid, min_interval=args.min_interval).get_articles(book_id, count=args.count, synckey=args.synckey)
    db_path = Path(args.db); conn = open_db(db_path)
    new_count = 0
    try:
        for article in reversed(articles):
            existed = article_exists(conn, article.review_id)
            if not existed: new_count += 1
            save_article(conn, article); conn.commit()
        generate_rss(load_articles(conn, book_id, limit=args.rss_limit), Path(args.feed), feed_title=args.feed_title or book_id)
    finally:
        conn.close()
    print(json.dumps({"book_id": book_id, "received": len(articles), "new": new_count,
                      "db": str(db_path), "feed": str(Path(args.feed))},
                     ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wechat-mp-fetcher", description="读取公众号最近文章、保存元数据并生成 RSS。")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("bookid"); p.add_argument("--url", required=True); p.set_defaults(func=command_bookid)
    p = sub.add_parser("resolve"); p.add_argument("--url", required=True); p.add_argument("--timeout", type=float, default=20.0); p.add_argument("--no-browser", action="store_true"); p.set_defaults(func=command_resolve)
    def add_source(p: argparse.ArgumentParser) -> None:
        source = p.add_mutually_exclusive_group(required=True)
        source.add_argument("--book-id"); source.add_argument("--article-url")
    def add_auth(p: argparse.ArgumentParser) -> None:
        p.add_argument("--credentials"); p.add_argument("--min-interval", type=float, default=2.0)
    p = sub.add_parser("list"); add_source(p); add_auth(p); p.add_argument("--count", type=int, default=20); p.add_argument("--offset", type=int, default=None); p.add_argument("--synckey", type=int, default=0); p.set_defaults(func=command_list)
    p = sub.add_parser("sync"); add_source(p); add_auth(p); p.add_argument("--count", type=int, default=20); p.add_argument("--synckey", type=int, default=0); p.add_argument("--db", default="data/wechat_mp.db"); p.add_argument("--feed", default="data/feed.xml"); p.add_argument("--feed-title", default=""); p.add_argument("--rss-limit", type=int, default=50); p.set_defaults(func=command_sync)
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).resolve().parent / ".env")
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except RiskControlError as exc:
        print(f"[risk-control] {exc}\n程序已停止，不会自动重试或绕过验证。", file=sys.stderr); return 3
    except AuthExpiredError as exc:
        print(f"[auth] {exc}", file=sys.stderr); return 4
    except (FetcherError, requests.RequestException, json.JSONDecodeError) as exc:
        print(f"[error] {exc}", file=sys.stderr); return 2


if __name__ == "__main__":
    raise SystemExit(main())
