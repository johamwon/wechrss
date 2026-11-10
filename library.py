"""Local group feeds and personal article bookmarks; no upstream requests."""
from contextlib import closing
from dataclasses import replace
import re
import secrets
from urllib.parse import parse_qs, urlencode, urlsplit

from service import AppDB
from wechat_mp_fetcher import FetcherError, load_articles


def bookmark_url(value: str) -> str:
    value = value.strip()
    if len(value) > 4096 or re.search(r"[\s\\]", value):
        raise FetcherError("请输入有效的微信文章链接")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "mp.weixin.qq.com":
        raise FetcherError("请粘贴 mp.weixin.qq.com 的文章链接")
    if re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", parsed.path):
        return "https://mp.weixin.qq.com" + parsed.path.rstrip("/")
    if parsed.path == "/s":
        query = parse_qs(parsed.query)
        if all(len(query.get(key, [])) == 1 for key in ("__biz", "mid", "idx")):
            params = {key: query[key][0] for key in ("__biz", "mid", "idx")}
            if re.fullmatch(r"[A-Za-z0-9+/=_-]+", params["__biz"]) and all(
                re.fullmatch(r"[0-9]+", params[key]) for key in ("mid", "idx")
            ):
                if len(query.get("sn", [])) == 1 and re.fullmatch(r"[a-fA-F0-9]+", query["sn"][0]):
                    params["sn"] = query["sn"][0]
                return "https://mp.weixin.qq.com/s?" + urlencode(params)
    raise FetcherError("链接不是支持的微信文章格式")


class Library:
    def __init__(self, db: AppDB):
        self.db = db

    def groups(self):
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT id,name FROM feed_groups ORDER BY name,id").fetchall()

    def group(self, group_id: int):
        with closing(self.db.connect()) as conn:
            return conn.execute("SELECT * FROM feed_groups WHERE id=?", (group_id,)).fetchone()

    def save_group(self, group_id: int | None, name: str, members: list[int]) -> int:
        name = name.strip()
        if not 1 <= len(name) <= 60 or re.search(r"[\x00-\x1f\ufffe\uffff]", name) or not 1 <= len(members) <= 50:
            raise FetcherError("分组名称须为 1–60 字，且请选择 1–50 个公众号")
        with closing(self.db.connect()) as conn, conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT 1 FROM feed_groups WHERE name=? AND id<>?", (name, group_id or 0)).fetchone():
                raise FetcherError("已有同名分组")
            valid = {row[0] for row in conn.execute("SELECT id FROM sources")}
            if not set(members) <= valid:
                raise FetcherError("有公众号已不存在，请刷新后重试")
            if group_id is None:
                group_id = conn.execute("INSERT INTO feed_groups(name,token) VALUES (?,?)",
                                        (name, secrets.token_urlsafe(32))).lastrowid
            elif not conn.execute("UPDATE feed_groups SET name=? WHERE id=?", (name, group_id)).rowcount:
                raise FetcherError("分组不存在")
            conn.execute("DELETE FROM feed_group_sources WHERE group_id=?", (group_id,))
            conn.executemany("INSERT INTO feed_group_sources VALUES (?,?)", [(group_id, sid) for sid in members])
            return group_id

    def change_group(self, group_id: int, action: str):
        with closing(self.db.connect()) as conn, conn:
            if action == "rotate":
                conn.execute("UPDATE feed_groups SET token=? WHERE id=?", (secrets.token_urlsafe(32), group_id))
            else:
                conn.execute("DELETE FROM feed_groups WHERE id=?", (group_id,))

    def group_contents(self, group_id: int):
        with closing(self.db.connect()) as conn:
            members = conn.execute(
                "SELECT s.id,s.book_id,s.rss_limit FROM sources s "
                "JOIN feed_group_sources g ON s.id=g.source_id WHERE g.group_id=? ORDER BY s.id", (group_id,)
            ).fetchall()
            articles = []
            for member in members:
                rules = self.db.get_filter_rules(member["id"], conn=conn)
                articles.extend(
                    replace(article, content_html="")
                    for article in load_articles(conn, member["book_id"], member["rss_limit"])
                    if self.db.matches_filter(article.title, rules)
                )
        articles.sort(key=lambda article: (article.publish_at, article.review_id), reverse=True)
        return {member["id"] for member in members}, articles[:100]

    def bookmarks(self, query: str):
        with closing(self.db.connect()) as conn:
            return conn.execute(
                "SELECT * FROM inbox_articles WHERE instr(lower(title || ' ' || tags || ' ' || note || ' ' || url), "
                "lower(?)) > 0 OR ? = '' ORDER BY id DESC", (query, query)
            ).fetchall()

    def save_bookmark(self, url: str, title: str, tags: str, note: str) -> bool:
        url = bookmark_url(url)
        title, tags, note = title.strip(), tags.strip(), note.strip()
        if len(title) > 200 or len(tags) > 120 or len(note) > 1000:
            raise FetcherError("标题、标签、备注最多分别为 200、120、1000 字")
        with closing(self.db.connect()) as conn, conn:
            result = conn.execute(
                "INSERT INTO inbox_articles(url,url_key,title,tags,note) VALUES (?,?,?,?,?) ON CONFLICT(url_key) DO NOTHING",
                (url, url.split("&sn=", 1)[0], title, tags, note),
            )
            return bool(result.rowcount)

    def remove_bookmark(self, bookmark_id: int):
        with closing(self.db.connect()) as conn, conn:
            conn.execute("DELETE FROM inbox_articles WHERE id=?", (bookmark_id,))
