"""Tests for enhanced health check functionality."""
import time
from pathlib import Path

import pytest

from service import AppDB, CredentialStore, SyncService, Source
from wechat_mp_fetcher import Article, save_article


class TestDatabaseIntegrity:
    def test_check_integrity_healthy_database(self, tmp_path):
        """Integrity check passes for healthy database."""
        db = AppDB(tmp_path / "db.sqlite")
        source = db.add_source(source_value="MP_WXS_100", name="测试号")
        
        result = db.check_integrity()
        
        assert result["ok"] is True
        assert result["errors"] == []

    def test_check_integrity_empty_database(self, tmp_path):
        """Integrity check passes for empty database."""
        db = AppDB(tmp_path / "db.sqlite")
        
        result = db.check_integrity()
        
        assert result["ok"] is True
        assert result["errors"] == []


class TestDatabaseStats:
    def test_get_stats_empty_database(self, tmp_path):
        """Stats return zeros for empty database."""
        db = AppDB(tmp_path / "db.sqlite")
        
        stats = db.get_stats()
        
        assert stats["sources"] == 0
        assert stats["articles"] == 0
        assert stats["sync_runs"] == 0
        assert stats["db_size_bytes"] > 0  # Database file exists

    def test_get_stats_with_data(self, tmp_path):
        """Stats reflect actual data counts."""
        db = AppDB(tmp_path / "db.sqlite")
        
        # Add sources
        s1 = db.add_source(source_value="MP_WXS_100", name="号1")
        s2 = db.add_source(source_value="MP_WXS_200", name="号2")
        
        # Add articles
        with db.connect() as conn:
            save_article(conn, Article("r1", s1.book_id, "文章1", publish_at=1700000000))
            save_article(conn, Article("r2", s1.book_id, "文章2", publish_at=1700000001))
            save_article(conn, Article("r3", s2.book_id, "文章3", publish_at=1700000002))
            conn.commit()
        
        stats = db.get_stats()
        
        assert stats["sources"] == 2
        assert stats["articles"] == 3
        assert stats["sync_runs"] == 0
        assert stats["db_size_bytes"] > 0


class TestRecentSyncStats:
    def test_get_recent_sync_stats_no_runs(self, tmp_path):
        """Sync stats return zeros when no sync runs exist."""
        db = AppDB(tmp_path / "db.sqlite")
        
        stats = db.get_recent_sync_stats(hours=24)
        
        assert stats["hours"] == 24
        assert stats["total"] == 0
        assert stats["success"] == 0
        assert stats["failed"] == 0
        assert stats["success_rate"] == 100.0

    def test_get_recent_sync_stats_with_runs(self, tmp_path):
        """Sync stats calculate correctly from sync runs."""
        db = AppDB(tmp_path / "db.sqlite")
        source = db.add_source(source_value="MP_WXS_100", name="测试号")
        
        now = int(time.time())
        
        # Add sync runs with different statuses
        with db.connect() as conn:
            # Recent successful runs
            conn.execute(
                "INSERT INTO sync_runs(source_id, started_at, finished_at, status, received, new_count) VALUES(?, ?, ?, ?, ?, ?)",
                (source.id, now - 3600, now - 3500, "ok", 10, 2)
            )
            conn.execute(
                "INSERT INTO sync_runs(source_id, started_at, finished_at, status, received, new_count) VALUES(?, ?, ?, ?, ?, ?)",
                (source.id, now - 1800, now - 1700, "ok", 8, 1)
            )
            # Recent failed run
            conn.execute(
                "INSERT INTO sync_runs(source_id, started_at, finished_at, status, received, new_count) VALUES(?, ?, ?, ?, ?, ?)",
                (source.id, now - 900, now - 800, "error", 0, 0)
            )
            # Old run (outside 24h window)
            conn.execute(
                "INSERT INTO sync_runs(source_id, started_at, finished_at, status, received, new_count) VALUES(?, ?, ?, ?, ?, ?)",
                (source.id, now - 86400 - 3600, now - 86400 - 3500, "ok", 5, 1)
            )
            conn.commit()
        
        stats = db.get_recent_sync_stats(hours=24)
        
        assert stats["hours"] == 24
        assert stats["total"] == 3  # Only recent runs
        assert stats["success"] == 2
        assert stats["failed"] == 1
        assert stats["success_rate"] == pytest.approx(66.67, rel=0.01)

    def test_get_recent_sync_stats_custom_hours(self, tmp_path):
        """Sync stats respect custom hours parameter."""
        db = AppDB(tmp_path / "db.sqlite")
        source = db.add_source(source_value="MP_WXS_100", name="测试号")
        
        now = int(time.time())
        
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO sync_runs(source_id, started_at, finished_at, status, received, new_count) VALUES(?, ?, ?, ?, ?, ?)",
                (source.id, now - 3600, now - 3500, "ok", 10, 2)
            )
            conn.commit()
        
        # Check with 1 hour window
        stats_1h = db.get_recent_sync_stats(hours=1)
        assert stats_1h["hours"] == 1
        assert stats_1h["total"] == 1
        
        # Check with 2 hour window
        stats_2h = db.get_recent_sync_stats(hours=2)
        assert stats_2h["hours"] == 2
        assert stats_2h["total"] == 1


class TestHealthEndpoint:
    def test_health_endpoint_returns_comprehensive_data(self, tmp_path):
        """Health endpoint returns all expected fields."""
        from web_app import WebApp
        from weread_auth import LoginManager, WeReadAuthClient
        
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        service = SyncService(db, creds)
        
        # Mock scheduler
        class MockScheduler:
            enabled = True
        
        scheduler = MockScheduler()
        auth_client = WeReadAuthClient()
        login_manager = LoginManager(auth_client, lambda x: None)
        
        app = WebApp(db, creds, service, scheduler, login_manager)
        
        # Simulate health check request
        class MockStartResponse:
            def __init__(self):
                self.status = None
                self.headers = None
            
            def __call__(self, status, headers):
                self.status = status
                self.headers = dict(headers)
        
        start_response = MockStartResponse()
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/health"}
        
        response = app(environ, start_response)
        
        assert start_response.status == "200 OK"
        assert "application/json" in start_response.headers["Content-Type"]
        
        import json
        data = json.loads(b"".join(response).decode("utf-8"))
        
        # Check all expected fields are present
        assert "ok" in data
        assert "time" in data
        assert "version" in data
        assert "scheduler" in data
        assert "credentials" in data
        assert "database" in data
        assert "sync_24h" in data
        assert "disk" in data
        
        # Check nested structures
        assert "configured" in data["credentials"]
        assert "refreshable" in data["credentials"]
        assert "login_status" in data["credentials"]
        
        assert "integrity_ok" in data["database"]
        assert "integrity_errors" in data["database"]
        assert "size_bytes" in data["database"]
        assert "sources" in data["database"]
        assert "articles" in data["database"]
        
        assert "hours" in data["sync_24h"]
        assert "total" in data["sync_24h"]
        assert "success" in data["sync_24h"]
        assert "failed" in data["sync_24h"]
        assert "success_rate" in data["sync_24h"]
        
        assert "free_gb" in data["disk"]
        assert "total_gb" in data["disk"]
        assert "usage_percent" in data["disk"]

    def test_health_endpoint_ok_reflects_actual_health(self, tmp_path):
        """Health endpoint ok field reflects actual system health."""
        from web_app import WebApp
        from weread_auth import LoginManager, WeReadAuthClient, WeReadCredentials
        
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        
        # Set up configured credentials
        creds.save_record(WeReadCredentials(
            vid="123", accessToken="token", refreshToken="refresh", deviceId="dev"
        ))
        
        service = SyncService(db, creds)
        
        class MockScheduler:
            enabled = True
        
        scheduler = MockScheduler()
        auth_client = WeReadAuthClient()
        login_manager = LoginManager(auth_client, lambda x: None)
        
        app = WebApp(db, creds, service, scheduler, login_manager)
        
        class MockStartResponse:
            def __init__(self):
                self.status = None
                self.headers = None
            
            def __call__(self, status, headers):
                self.status = status
                self.headers = dict(headers)
        
        start_response = MockStartResponse()
        environ = {"REQUEST_METHOD": "GET", "PATH_INFO": "/api/health"}
        
        response = app(environ, start_response)
        
        import json
        data = json.loads(b"".join(response).decode("utf-8"))
        
        # With configured credentials and healthy database, ok should be True
        assert data["ok"] is True
        assert data["credentials"]["configured"] is True
        assert data["database"]["integrity_ok"] is True
