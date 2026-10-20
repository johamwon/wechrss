import os
from pathlib import Path

from service import AppDB, CredentialStore, Scheduler, SyncService
from wechat_mp_fetcher import Article, save_article


def test_source_crud_and_feed(tmp_path: Path):
    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_123", name="测试号", interval_minutes=10)
    # Web 管理端强制最低 60 分钟。
    assert source.sync_interval_minutes == 60
    with db.connect() as conn:
        save_article(conn, Article("r1", source.book_id, "文章1", url="https://example.com", publish_at=1700000000))
        conn.commit()
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    xml = service.feed_bytes(source.id, "http://localhost:8080").decode("utf-8")
    assert "测试号" in xml
    assert "文章1" in xml
    assert db.get_source(source.id).article_count == 1


def test_credentials_file(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("WEREAD_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("WEREAD_VID", raising=False)
    store = CredentialStore(tmp_path / "credentials.json")
    store.save("abcdefghijk", "123456")
    assert store.get() == ("abcdefghijk", "123456")
    assert store.status()["token_hint"] == "abcd…hijk"
    if os.name != "nt":
        assert (store.path.stat().st_mode & 0o777) == 0o600


def test_scheduler_setting_persists(tmp_path: Path):
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    db.set_setting("scheduler_enabled", "0")
    scheduler = Scheduler(db, service)
    assert scheduler.enabled is False


def test_content_fetch_is_disabled_for_new_and_existing_sources(tmp_path: Path):
    path = tmp_path / "db.sqlite"
    db = AppDB(path)
    source = db.add_source(source_value="MP_WXS_999", name="正文开关", fetch_content=True)
    assert db.get_source(source.id).fetch_content is False

    with db.connect() as conn:
        conn.execute("UPDATE sources SET fetch_content=1 WHERE id=?", (source.id,))
        conn.commit()
    assert db.get_source(source.id).fetch_content is True

    reopened = AppDB(path)
    assert reopened.get_source(source.id).fetch_content is False


def test_sync_auto_refreshes_once_on_auth_expired(tmp_path: Path, monkeypatch):
    import service as service_module
    from wechat_mp_fetcher import AuthExpiredError
    from weread_auth import WeReadCredentials

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_123", name="测试号", fetch_content=False)
    creds = CredentialStore(tmp_path / "credentials.json")
    creds.save_record(WeReadCredentials(
        vid="123", accessToken="old-token", refreshToken="refresh", deviceId="device-1"
    ))

    calls = {"clients": 0, "refresh": 0}

    class FakeMobileClient:
        def __init__(self, token, vid, **kwargs):
            self.token = token
            calls["clients"] += 1

        def get_articles(self, book_id, **kwargs):
            if self.token == "old-token":
                raise AuthExpiredError("expired")
            return [Article("r-auto", book_id, "自动续期文章", publish_at=1700000010)]

    class FakeAuth:
        def version_headers(self):
            return {}

        def refresh(self, record):
            calls["refresh"] += 1
            record.accessToken = "new-token"
            record.refreshToken = "refresh-rotated"
            return record

    monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
    sync = SyncService(db, creds, auth_client=FakeAuth())
    result = sync.sync_source(source.id)
    assert result.status == "ok"
    assert result.new_count == 1
    assert calls["refresh"] == 1
    assert creds.get_record().accessToken == "new-token"
    assert creds.get_record().refreshToken == "refresh-rotated"


def test_risk_control_does_not_refresh(tmp_path: Path, monkeypatch):
    import service as service_module
    from wechat_mp_fetcher import RiskControlError
    from weread_auth import WeReadCredentials

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_456", name="风控测试", fetch_content=False)
    creds = CredentialStore(tmp_path / "credentials.json")
    creds.save_record(WeReadCredentials(
        vid="456", accessToken="token", refreshToken="refresh", deviceId="device-2"
    ))
    calls = {"refresh": 0}

    class FakeMobileClient:
        def __init__(self, *args, **kwargs):
            pass
        def get_articles(self, *args, **kwargs):
            raise RiskControlError("-2041")

    class FakeAuth:
        def version_headers(self):
            return {}
        def refresh(self, record):
            calls["refresh"] += 1
            return record

    monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
    sync = SyncService(db, creds, auth_client=FakeAuth())
    result = sync.sync_source(source.id)
    assert result.status == "risk_control"
    assert calls["refresh"] == 0


def test_backfill_missing_article_url(tmp_path: Path, monkeypatch):
    import service as service_module
    from weread_auth import WeReadCredentials

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_789", name="链接测试", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article("r-missing", source.book_id, "缺链接文章", publish_at=1700000000))
        conn.commit()
    creds = CredentialStore(tmp_path / "credentials.json")
    creds.save_record(WeReadCredentials(vid="789", accessToken="token", refreshToken="refresh", deviceId="dev"))

    class FakeMobileClient:
        def __init__(self, *args, **kwargs):
            pass
        def resolve_article_url(self, review_id):
            assert review_id == "r-missing"
            return "https://mp.weixin.qq.com/s/backfilled-token"

    class FakeAuth:
        def version_headers(self):
            return {}

    monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
    sync = SyncService(db, creds, auth_client=FakeAuth())
    resolved, failed = sync.backfill_source_urls(source.id, limit=5)
    assert (resolved, failed) == (1, 0)
    assert db.article_by_review("r-missing").url == "https://mp.weixin.qq.com/s/backfilled-token"


def test_sync_does_not_clear_backfilled_url(tmp_path: Path, monkeypatch):
    """端到端：手动/自动同步共同路径 sync_source() 不得用空 url 抹掉已补全的原文链接。"""
    import service as service_module
    from weread_auth import WeReadCredentials

    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value="MP_WXS_999", name="同步保留链接", fetch_content=False)
    with db.connect() as conn:
        save_article(conn, Article("r-keep", source.book_id, "已补全文章", url="", publish_at=1700000000))
        conn.commit()
    # 模拟用户点击「补全原文链接」写入真实链接
    db.set_article_url("r-keep", "https://mp.weixin.qq.com/s/REAL-TOKEN")
    assert db.article_by_review("r-keep").url == "https://mp.weixin.qq.com/s/REAL-TOKEN"

    creds = CredentialStore(tmp_path / "credentials.json")
    creds.save_record(WeReadCredentials(vid="999", accessToken="token", refreshToken="refresh", deviceId="dev"))

    class FakeMobileClient:
        def __init__(self, *args, **kwargs):
            pass
        def get_articles(self, book_id, **kwargs):
            # 上游 /mp/chapters 这次未返回 doc_url，url 为空
            return [Article("r-keep", book_id, "已补全文章", url="", publish_at=1700000000)]

    class FakeAuth:
        def version_headers(self):
            return {}

    monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
    sync = SyncService(db, creds, auth_client=FakeAuth())
    result = sync.sync_source(source.id)
    assert result.status == "ok"
    # 修复后：已补全的真实链接不被空值清空
    assert db.article_by_review("r-keep").url == "https://mp.weixin.qq.com/s/REAL-TOKEN"
