"""Tests for auto-backfill feature (issue #7)."""
import json
import time

import pytest

from service import AppDB, CredentialStore, Scheduler, SyncService, _PartialBackfillError
from wechat_mp_fetcher import Article, RiskControlError, save_article
from weread_auth import WeReadCredentials


def _setup_source_with_missing_urls(tmp_path, book_suffix="777", name="补全测试"):
    """Helper: create a source with 3 articles missing URLs."""
    db = AppDB(tmp_path / "db.sqlite")
    source = db.add_source(source_value=f"MP_WXS_{book_suffix}", name=name, fetch_content=False)
    with db.connect() as conn:
        for i in range(3):
            save_article(conn, Article(f"r-bf-{i}", source.book_id, f"文章{i}", publish_at=1700000000 + i))
        conn.commit()
    return db, source


def _setup_creds(tmp_path, vid="777"):
    creds = CredentialStore(tmp_path / "credentials.json")
    creds.save_record(WeReadCredentials(vid=vid, accessToken="token", refreshToken="refresh", deviceId="dev"))
    return creds


class TestSourcesWithMissingUrls:
    def test_returns_enabled_sources_with_missing_urls(self, tmp_path):
        db, source = _setup_source_with_missing_urls(tmp_path)
        sources = db.sources_with_missing_urls()
        assert len(sources) == 1
        assert sources[0].id == source.id

    def test_excludes_sources_with_all_urls_filled(self, tmp_path):
        db, source = _setup_source_with_missing_urls(tmp_path)
        for i in range(3):
            db.set_article_url(f"r-bf-{i}", f"https://example.com/{i}")
        sources = db.sources_with_missing_urls()
        assert len(sources) == 0

    def test_excludes_disabled_sources(self, tmp_path):
        db, source = _setup_source_with_missing_urls(tmp_path)
        db.set_enabled(source.id, False)
        sources = db.sources_with_missing_urls()
        assert len(sources) == 0

    def test_mixed_sources_only_returns_ones_with_missing(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        s1 = db.add_source(source_value="MP_WXS_111", name="有缺失", fetch_content=False)
        s2 = db.add_source(source_value="MP_WXS_222", name="无缺失", fetch_content=False)
        with db.connect() as conn:
            save_article(conn, Article("r-mix1", s1.book_id, "缺链接", publish_at=1700000000))
            save_article(conn, Article("r-mix2", s2.book_id, "有链接", url="https://example.com", publish_at=1700000000))
            conn.commit()
        sources = db.sources_with_missing_urls()
        assert len(sources) == 1
        assert sources[0].id == s1.id


class TestBackfillSourceUrlsLock:
    def test_backfill_acquires_lock(self, tmp_path, monkeypatch):
        """backfill_source_urls should acquire the sync lock."""
        import service as service_module
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        resolved, failed = sync.backfill_source_urls(source.id, limit=5)
        assert resolved == 3
        assert failed == 0

    def test_backfill_fails_when_lock_held(self, tmp_path, monkeypatch):
        """backfill_source_urls should fail if lock is already held."""
        import service as service_module
        from wechat_mp_fetcher import FetcherError
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        sync._lock.acquire()
        try:
            with pytest.raises(FetcherError, match="已有同步任务正在运行"):
                sync.backfill_source_urls(source.id, limit=5)
        finally:
            sync._lock.release()


class TestPartialBackfillError:
    def test_partial_progress_preserved_on_risk_control(self, tmp_path, monkeypatch):
        """When risk control triggers mid-backfill, partial progress is preserved via exception."""
        import service as service_module
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        call_count = {"n": 0}

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise RiskControlError("-2041")
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        with pytest.raises(_PartialBackfillError) as exc_info:
            sync.backfill_source_urls(source.id, limit=3)
        assert exc_info.value.resolved == 1
        assert exc_info.value.failed == 0


class TestAutoBackfillOnce:
    def test_returns_empty_when_no_missing_urls(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        result = sync.auto_backfill_once()
        assert result["status"] == "empty"

    def test_picks_random_source_and_backfills(self, tmp_path, monkeypatch):
        import service as service_module
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        result = sync.auto_backfill_once()
        assert result["status"] == "ok"
        assert result["resolved"] == 3
        assert result["source_id"] == source.id

    def test_returns_interrupted_on_partial(self, tmp_path, monkeypatch):
        import service as service_module
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        call_count = {"n": 0}

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                call_count["n"] += 1
                if call_count["n"] >= 2:
                    raise RiskControlError("-2041")
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        result = sync.auto_backfill_once()
        assert result["status"] == "interrupted"
        assert result["resolved"] == 1


class TestSchedulerBackfill:
    def test_backfill_disabled_by_default(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        scheduler = Scheduler(db, sync)
        assert scheduler.backfill_enabled is False

    def test_set_backfill_enabled_persists(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        scheduler = Scheduler(db, sync)
        scheduler.set_backfill_enabled(True)
        assert scheduler.backfill_enabled is True
        scheduler2 = Scheduler(db, sync)
        assert scheduler2.backfill_enabled is True

    def test_set_backfill_interval_clamps(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        scheduler = Scheduler(db, sync)
        scheduler.set_backfill_interval(30)
        assert scheduler.backfill_interval == 60
        scheduler.set_backfill_interval(20000)
        assert scheduler.backfill_interval == 10080

    def test_maybe_backfill_returns_none_when_disabled(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        scheduler = Scheduler(db, sync)
        assert scheduler.maybe_backfill() is None

    def test_maybe_backfill_returns_none_when_interval_not_elapsed(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = _setup_creds(tmp_path)
        sync = SyncService(db, creds)
        scheduler = Scheduler(db, sync)
        scheduler.set_backfill_enabled(True)
        scheduler.set_backfill_interval(60)
        db.set_setting("backfill_last_run_at", str(int(time.time())))
        scheduler._backfill_last_run = int(time.time())
        assert scheduler.maybe_backfill() is None

    def test_maybe_backfill_runs_when_interval_elapsed(self, tmp_path, monkeypatch):
        import service as service_module
        db, source = _setup_source_with_missing_urls(tmp_path)
        creds = _setup_creds(tmp_path)

        class FakeMobileClient:
            def __init__(self, *args, **kwargs):
                pass
            def resolve_article_url(self, review_id):
                return f"https://mp.weixin.qq.com/s/{review_id}"

        class FakeAuth:
            def version_headers(self):
                return {}

        monkeypatch.setattr(service_module, "WeReadMobileClient", FakeMobileClient)
        sync = SyncService(db, creds, auth_client=FakeAuth())
        scheduler = Scheduler(db, sync)
        scheduler.set_backfill_enabled(True)
        scheduler.set_backfill_interval(60)
        scheduler._backfill_last_run = 0
        result = scheduler.maybe_backfill()
        assert result is not None
        assert result["status"] == "ok"
        assert result["resolved"] == 3
        last_result = json.loads(scheduler.backfill_last_result)
        assert last_result["status"] == "ok"
