"""Tests for database backup feature."""
import sqlite3
import time
from pathlib import Path

import pytest

from service import AppDB, CredentialStore, SyncService
from wechat_mp_fetcher import Article, save_article


class TestDatabaseBackup:
    def test_create_backup_generates_file(self, tmp_path):
        """Backup creates a valid SQLite file."""
        db = AppDB(tmp_path / "db.sqlite")
        # Add some data
        source = db.add_source(source_value="MP_WXS_100", name="测试号")
        with db.connect() as conn:
            save_article(conn, Article("r1", source.book_id, "文章1", publish_at=1700000000))
            conn.commit()
        
        backup_path = db.create_backup()
        
        assert backup_path.exists()
        assert backup_path.suffix == ".db"
        assert "backup" in backup_path.name
        
        # Verify it's a valid SQLite database
        conn = sqlite3.connect(str(backup_path))
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM sources")
            count = cursor.fetchone()[0]
            assert count == 1
        finally:
            conn.close()
        
        # Clean up
        backup_path.unlink()

    def test_create_backup_with_custom_path(self, tmp_path):
        """Backup can be created at a custom location."""
        db = AppDB(tmp_path / "db.sqlite")
        custom_path = tmp_path / "backups" / "my-backup.db"
        
        result = db.create_backup(custom_path)
        
        assert result == custom_path
        assert custom_path.exists()
        
        # Clean up
        custom_path.unlink()

    def test_backup_contains_all_data(self, tmp_path):
        """Backup includes sources, articles, and settings."""
        db = AppDB(tmp_path / "db.sqlite")
        
        # Add sources
        s1 = db.add_source(source_value="MP_WXS_100", name="号1")
        s2 = db.add_source(source_value="MP_WXS_200", name="号2")
        
        # Add articles
        with db.connect() as conn:
            save_article(conn, Article("r1", s1.book_id, "文章1", publish_at=1700000000))
            save_article(conn, Article("r2", s2.book_id, "文章2", publish_at=1700000001))
            conn.commit()
        
        # Add settings
        db.set_setting("test_key", "test_value")
        
        # Create backup
        backup_path = db.create_backup()
        
        # Verify all data is present
        conn = sqlite3.connect(str(backup_path))
        try:
            # Check sources
            cursor = conn.execute("SELECT COUNT(*) FROM sources")
            assert cursor.fetchone()[0] == 2
            
            # Check articles
            cursor = conn.execute("SELECT COUNT(*) FROM articles")
            assert cursor.fetchone()[0] == 2
            
            # Check settings
            cursor = conn.execute("SELECT value FROM app_settings WHERE key='test_key'")
            assert cursor.fetchone()[0] == "test_value"
        finally:
            conn.close()
        
        # Clean up
        backup_path.unlink()

    def test_backup_is_consistent_snapshot(self, tmp_path):
        """Backup represents a consistent point-in-time snapshot."""
        db = AppDB(tmp_path / "db.sqlite")
        
        # Add initial data
        s1 = db.add_source(source_value="MP_WXS_100", name="号1")
        
        # Create backup
        backup_path = db.create_backup()
        
        # Add more data after backup
        s2 = db.add_source(source_value="MP_WXS_200", name="号2")
        
        # Verify backup only contains initial data
        conn = sqlite3.connect(str(backup_path))
        try:
            cursor = conn.execute("SELECT COUNT(*) FROM sources")
            count = cursor.fetchone()[0]
            assert count == 1  # Only s1, not s2
        finally:
            conn.close()
        
        # Clean up
        backup_path.unlink()

    def test_backup_creates_parent_directories(self, tmp_path):
        """Backup creates parent directories if they don't exist."""
        db = AppDB(tmp_path / "db.sqlite")
        nested_path = tmp_path / "level1" / "level2" / "backup.db"
        
        result = db.create_backup(nested_path)
        
        assert result.exists()
        assert nested_path.parent.exists()
        
        # Clean up
        nested_path.unlink()

    def test_backup_filename_includes_timestamp(self, tmp_path):
        """Default backup filename includes timestamp."""
        db = AppDB(tmp_path / "db.sqlite")
        
        backup_path = db.create_backup()
        
        # Check filename format: wechat_mp.backup-YYYYMMDD-HHMMSS.db
        assert "backup-" in backup_path.name
        # Should have timestamp pattern
        import re
        assert re.search(r'backup-\d{8}-\d{6}', backup_path.name)
        
        # Clean up
        backup_path.unlink()

    def test_backup_can_be_restored(self, tmp_path):
        """Backup file can be used to restore the database."""
        # Create original database with data
        db1 = AppDB(tmp_path / "original.db")
        s1 = db1.add_source(source_value="MP_WXS_100", name="原始号")
        with db1.connect() as conn:
            save_article(conn, Article("r1", s1.book_id, "原始文章", publish_at=1700000000))
            conn.commit()
        
        # Create backup
        backup_path = db1.create_backup(tmp_path / "backup.db")
        
        # Restore to new location
        restored_path = tmp_path / "restored.db"
        backup_path.rename(restored_path)
        
        # Open restored database
        db2 = AppDB(restored_path)
        
        # Verify data is present
        sources = db2.list_sources()
        assert len(sources) == 1
        assert sources[0].name == "原始号"
        
        articles = db2.get_articles(sources[0].book_id)
        assert len(articles) == 1
        assert articles[0].title == "原始文章"
