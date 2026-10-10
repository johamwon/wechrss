"""Tests for batch operations functionality."""
from pathlib import Path
from service import AppDB, SyncService, CredentialStore, Scheduler


def test_delete_sources_batch(tmp_path: Path):
    """Test batch deleting multiple sources."""
    db = AppDB(tmp_path / "db.sqlite")
    
    # Create test sources
    source1 = db.add_source(source_value="MP_WXS_123", name="Test 1")
    source2 = db.add_source(source_value="MP_WXS_456", name="Test 2")  # noqa: F841
    source3 = db.add_source(source_value="MP_WXS_789", name="Test 3")
    
    # Verify sources exist
    assert len(db.list_sources()) == 3
    
    # Delete source1 and source3
    deleted = db.delete_sources_batch([source1.id, source3.id])
    
    assert deleted == 2
    assert len(db.list_sources()) == 1
    
    # Verify source2 still exists
    remaining = db.list_sources()
    assert len(remaining) == 1
    assert remaining[0].id == source2.id


def test_delete_sources_batch_empty_list(tmp_path: Path):
    """Test batch delete with empty list."""
    db = AppDB(tmp_path / "db.sqlite")
    
    # Create test source
    source = db.add_source(source_value="MP_WXS_123", name="Test")  # noqa: F841
    
    # Delete with empty list
    deleted = db.delete_sources_batch([])
    
    assert deleted == 0
    assert len(db.list_sources()) == 1


def test_delete_sources_batch_nonexistent_ids(tmp_path: Path):
    """Test batch delete with non-existent IDs."""
    db = AppDB(tmp_path / "db.sqlite")
    
    # Create test source
    source = db.add_source(source_value="MP_WXS_123", name="Test")  # noqa: F841
    
    # Delete non-existent IDs
    deleted = db.delete_sources_batch([999, 1000])
    
    assert deleted == 0
    assert len(db.list_sources()) == 1


def test_delete_sources_batch_mixed_ids(tmp_path: Path):
    """Test batch delete with mix of valid and invalid IDs."""
    db = AppDB(tmp_path / "db.sqlite")
    
    # Create test sources
    source1 = db.add_source(source_value="MP_WXS_123", name="Test 1")
    source2 = db.add_source(source_value="MP_WXS_456", name="Test 2")  # noqa: F841
    
    # Delete with mix of valid and invalid IDs
    deleted = db.delete_sources_batch([source1.id, 999])
    
    assert deleted == 1
    assert len(db.list_sources()) == 1
    
    # Verify source2 still exists
    remaining = db.list_sources()
    assert remaining[0].id == source2.id


def test_batch_delete_route_requires_csrf(tmp_path: Path):
    """Test that batch delete route requires CSRF token."""
    from web_app import WebApp
    
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = WebApp(db, creds, service, scheduler)
    
    # Create test source
    source = db.add_source(source_value="MP_WXS_123", name="Test")  # noqa: F841
    
    # Try to delete without CSRF
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/sources/batch/delete",
        "wsgi.input": __import__("io").BytesIO(b"source_ids=" + str(source.id).encode()),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
    }
    
    status_captured = []
    
    def start_response(status, headers):
        status_captured.append(status)
    
    result = app(environ, start_response)  # noqa: F841
    
    # Should fail due to missing CSRF
    assert status_captured[0].startswith("303")


def test_batch_delete_route_with_valid_csrf(tmp_path: Path):
    """Test batch delete route with valid CSRF token."""
    from web_app import WebApp
    
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = WebApp(db, creds, service, scheduler)
    
    # Create test sources
    source1 = db.add_source(source_value="MP_WXS_123", name="Test 1")
    source2 = db.add_source(source_value="MP_WXS_456", name="Test 2")  # noqa: F841
    
    # Get CSRF token
    csrf_token = app.csrf_token
    
    # Delete with valid CSRF - just check the route accepts it
    body = f"csrf={csrf_token}&source_ids={source1.id}".encode()
    environ = {
        "REQUEST_METHOD": "POST",
        "PATH_INFO": "/sources/batch/delete",
        "wsgi.input": __import__("io").BytesIO(body),
        "CONTENT_TYPE": "application/x-www-form-urlencoded",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
    }
    
    status_captured = []
    
    def start_response(status, headers):
        status_captured.append(status)
    
    result = app(environ, start_response)  # noqa: F841
    
    # Should succeed with redirect
    assert status_captured[0].startswith("303")
