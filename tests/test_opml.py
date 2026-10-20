"""Tests for OPML import/export feature."""

import pytest

from service import AppDB, CredentialStore, SyncService


def _setup_sources(tmp_path, count=3):
    """Helper: create multiple sources."""
    db = AppDB(tmp_path / "db.sqlite")
    sources = []
    for i in range(count):
        source = db.add_source(
            source_value=f"MP_WXS_{100 + i}",
            name=f"测试号{i}",
            interval_minutes=120,
            rss_limit=30
        )
        sources.append(source)
    return db, sources


class TestOPMLExport:
    def test_export_generates_valid_opml(self, tmp_path):
        db, sources = _setup_sources(tmp_path, count=3)
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = sync.export_opml("http://localhost:8080")
        
        assert '<?xml version="1.0" encoding="UTF-8"?>' in opml
        assert '<opml version="2.0">' in opml
        assert '<title>WechRss Subscriptions</title>' in opml
        assert '测试号0' in opml
        assert '测试号1' in opml
        assert '测试号2' in opml
        assert '/feeds/1.xml' in opml
        assert '/feeds/2.xml' in opml
        assert '/feeds/3.xml' in opml

    def test_export_includes_book_id(self, tmp_path):
        db, sources = _setup_sources(tmp_path, count=1)
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = sync.export_opml()
        
        assert 'bookId="MP_WXS_100"' in opml

    def test_export_empty_sources(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = sync.export_opml()
        
        assert '<?xml version="1.0" encoding="UTF-8"?>' in opml
        assert '<body></body>' in opml or '<body />' in opml


class TestOPMLImport:
    def test_import_creates_sources(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="公众号A" bookId="MP_WXS_200" xmlUrl="/feeds/1.xml"/>
    <outline type="rss" text="公众号B" bookId="MP_WXS_201" xmlUrl="/feeds/2.xml"/>
  </body>
</opml>'''
        
        result = sync.import_opml(opml)
        
        assert result["imported"] == 2
        assert result["skipped"] == 0
        assert result["failed"] == 0
        
        sources = db.list_sources()
        assert len(sources) == 2
        assert sources[0].book_id == "MP_WXS_200"
        assert sources[1].book_id == "MP_WXS_201"

    def test_import_skips_duplicates(self, tmp_path):
        db, existing = _setup_sources(tmp_path, count=1)
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = f'''<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="已存在" bookId="{existing[0].book_id}" xmlUrl="/feeds/1.xml"/>
    <outline type="rss" text="新增" bookId="MP_WXS_999" xmlUrl="/feeds/2.xml"/>
  </body>
</opml>'''
        
        result = sync.import_opml(opml)
        
        assert result["imported"] == 1
        assert result["skipped"] == 1
        assert result["failed"] == 0
        
        sources = db.list_sources()
        assert len(sources) == 2

    def test_import_handles_numeric_book_id(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="数字ID" bookId="300" xmlUrl="/feeds/1.xml"/>
  </body>
</opml>'''
        
        result = sync.import_opml(opml)
        
        assert result["imported"] == 1
        sources = db.list_sources()
        assert sources[0].book_id == "MP_WXS_300"

    def test_import_invalid_xml(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        from wechat_mp_fetcher import FetcherError
        with pytest.raises(FetcherError, match="OPML 文件解析失败"):
            sync.import_opml("not valid xml")

    def test_import_not_opml(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        from wechat_mp_fetcher import FetcherError
        with pytest.raises(FetcherError, match="不是有效的 OPML 文件"):
            sync.import_opml('<?xml version="1.0"?><html><body></body></html>')

    def test_import_empty_body(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body></body>
</opml>'''
        
        result = sync.import_opml(opml)
        
        assert result["imported"] == 0
        assert result["skipped"] == 0
        assert result["failed"] == 0

    def test_import_skips_entries_without_book_id(self, tmp_path):
        db = AppDB(tmp_path / "db.sqlite")
        creds = CredentialStore(tmp_path / "credentials.json")
        sync = SyncService(db, creds)
        
        opml = '''<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test</title></head>
  <body>
    <outline type="rss" text="无ID" xmlUrl="/feeds/1.xml"/>
    <outline type="rss" text="有ID" bookId="MP_WXS_400" xmlUrl="/feeds/2.xml"/>
  </body>
</opml>'''
        
        result = sync.import_opml(opml)
        
        assert result["imported"] == 1
        assert result["skipped"] == 1


class TestOPMLRoundTrip:
    def test_export_then_import(self, tmp_path):
        """Export from one instance, import to another."""
        # Setup source instance
        db1, sources = _setup_sources(tmp_path / "src", count=3)
        creds1 = CredentialStore(tmp_path / "src" / "credentials.json")
        sync1 = SyncService(db1, creds1)
        
        # Export
        opml = sync1.export_opml()
        
        # Setup target instance
        db2 = AppDB(tmp_path / "dst" / "db.sqlite")
        creds2 = CredentialStore(tmp_path / "dst" / "credentials.json")
        sync2 = SyncService(db2, creds2)
        
        # Import
        result = sync2.import_opml(opml)
        
        assert result["imported"] == 3
        assert result["skipped"] == 0
        
        # Verify
        imported_sources = db2.list_sources()
        assert len(imported_sources) == 3
        # Display order depends on creation timestamps, not the OPML contract.
        assert {(source.book_id, source.name) for source in imported_sources} == {
            (source.book_id, source.name) for source in sources
        }
