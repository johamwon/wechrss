from urllib.parse import quote, urlencode
import base64
import xml.etree.ElementTree as ET

import pytest

from library import bookmark_url
from service import AppDB, CredentialStore, Scheduler, SyncService
from test_web import call_wsgi
from web_app import WebApp
from wechat_mp_fetcher import Article, FetcherError, save_article


@pytest.fixture
def app(tmp_path):
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    return WebApp(db, creds, service, Scheduler(db, service))


def post(app, path, fields, *, headers=None, csrf=True):
    payload = dict(fields)
    if csrf:
        payload["csrf"] = app.csrf_token
    return call_wsgi(app, path, "POST", urlencode(payload).encode(), headers)


def test_group_preview_feed_and_existing_feed(app):
    first, second, third = [app.db.add_source(source_value=f"MP_WXS_{idx}") for idx in (1, 2, 3)]
    with app.db.connect() as conn:
        for idx, source, title in [(1, first, "Alpha"), (2, second, "Beta"),
                                   (3, second, "Noise"), (4, third, "Gamma")]:
            save_article(conn, Article(str(idx), source.book_id, title, publish_at=idx, content_html="PRIVATE_BODY"))
        conn.commit()
    app.db.set_filter_rules(second.id, [{"pattern": "Noise", "type": "exclude"}])
    fields = {"name": "Research", f"source_{first.id}": 1, f"source_{second.id}": 1, f"source_{third.id}": 1}
    assert post(app, "/groups", fields, csrf=False)[0]["status"].startswith("303")
    assert not app.library.groups()
    response, _ = post(app, "/groups", fields)
    path = response["headers"]["Location"]
    assert path == "/groups/1"
    _, page = call_wsgi(app, path)
    assert b"Alpha" in page and b"Beta" in page and b"Gamma" in page and b"Noise" not in page
    _, xml = call_wsgi(app, "/group-feeds/1.xml")
    assert [item.text for item in ET.fromstring(xml).findall("./channel/item/title")] == ["Gamma", "Beta", "Alpha"]
    assert b"PRIVATE_BODY" not in xml
    _, original = call_wsgi(app, f"/feeds/{second.id}.xml")
    assert b"Noise" in original
    token = app.library.group(1)["token"]
    post(app, path, {"name": "Renamed", f"source_{first.id}": 1})
    assert app.library.group(1)["token"] == token
    app.db.delete_source(first.id)
    assert app.library.group_contents(1) == (set(), [])
    post(app, path, {"action": "delete"})
    assert call_wsgi(app, "/group-feeds/1.xml")[0]["status"].startswith("404")
    assert app.db.get_source(second.id)


def test_group_token_is_scoped_and_revocable(app):
    source = app.db.add_source(source_value="MP_WXS_11")
    one = app.library.save_group(None, "One", [source.id])
    two = app.library.save_group(None, "Two", [source.id])
    app.admin_password = "secret"
    token = app.library.group(one)["token"]
    query = {"QUERY_STRING": "token=" + token}
    feed = f"/group-feeds/{one}.xml"
    assert call_wsgi(app, feed, environ_overrides=query)[0]["status"].startswith("200")
    meta, body = call_wsgi(app, feed, "HEAD", environ_overrides=query)
    assert meta["status"].startswith("200") and not body
    for path in (f"/group-feeds/{two}.xml", f"/feeds/{source.id}.xml", "/groups", "/inbox"):
        assert call_wsgi(app, path, environ_overrides=query)[0]["status"].startswith("401")
    assert post(app, feed, {}, headers=query)[0]["status"].startswith("401")
    auth = {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"admin:secret").decode()}
    post(app, f"/groups/{one}", {"action": "rotate"}, headers=auth)
    assert call_wsgi(app, feed, environ_overrides=query)[0]["status"].startswith("401")
    assert call_wsgi(app, feed, environ_overrides=auth)[0]["status"].startswith("200")


def test_inbox_keeps_notes_and_finds_bookmark_without_subscription(app):
    fields = {"url": "https://mp.weixin.qq.com/s/Abc?scene=1", "title": "<b>title</b>", "tags": "reading", "note": "find me"}
    post(app, "/inbox", fields)
    post(app, "/inbox", {**fields, "url": "http://mp.weixin.qq.com/s/Abc#fragment", "note": "not overwritten"})
    assert not app.db.list_sources()
    assert len(app.library.bookmarks("")) == 1
    _, page = call_wsgi(app, "/inbox", environ_overrides={"QUERY_STRING": "q=" + quote("find me")})
    assert b"&lt;b&gt;title&lt;/b&gt;" in page and b"not overwritten" not in page
    assert app.library.bookmarks("missing") == []
    post(app, "/inbox", {"action": "remove", "id": 1}, csrf=False)
    assert app.library.bookmarks("")
    post(app, "/inbox", {"action": "remove", "id": 1})
    assert not app.library.bookmarks("")


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "https://mp.weixin.qq.com.evil.test/s/a",
    "https://mp.weixin.qq.com@evil.test/s/a", "https://mp.weixin.qq.com:443/s/a",
    "https://mp.weixin.qq.com/s?mid=1", "https://mp.weixin.qq.com/s/a%2Fb",
])
def test_bookmark_rejects_other_destinations(url):
    with pytest.raises(FetcherError):
        bookmark_url(url)


def test_long_link_keeps_signature_but_deduplicates_by_identity(app):
    base = "https://mp.weixin.qq.com/s?mid=1&idx=2&__biz=abc%3D"
    assert app.library.save_bookmark(base + "&sn=deadbeef&scene=1", "", "", "")
    assert not app.library.save_bookmark(base, "", "", "")
    assert app.library.bookmarks("")[0]["url"].endswith("&sn=deadbeef")
