"""Tests for full-text search functionality."""
import pytest
from pathlib import Path
from service import AppDB, SyncService, CredentialStore, Scheduler
from wechat_mp_fetcher import Article, open_db, save_article


def test_search_articles_by_title(tmp_path: Path):
    """Test searching articles by title."""
    db_path = tmp_path / "db.sqlite"
    db = AppDB(db_path)
    
    # Create test articles in the same database
    conn = open_db(db_path)
    
    article1 = Article(
        review_id="1", book_id="MP_WXS_123", title="Python 编程入门",
        summary="学习 Python 基础", url="https://example.com/1", publish_at=1000
    )
    article2 = Article(
        review_id="2", book_id="MP_WXS_123", title="JavaScript 高级教程",
        summary="深入学习 JS", url="https://example.com/2", publish_at=2000
    )
    article3 = Article(
        review_id="3", book_id="MP_WXS_456", title="Python 数据分析",
        summary="使用 Python 进行数据处理", url="https://example.com/3", publish_at=3000
    )
    
    save_article(conn, article1)
    save_article(conn, article2)
    save_article(conn, article3)
    conn.commit()
    conn.commit()
    conn.close()
    
    # Search for "Python"
    results = db.search_articles("Python")
    assert len(results) == 2
    assert all("Python" in a.title for a in results)
    
    # Search for "JavaScript"
    results = db.search_articles("JavaScript")
    assert len(results) == 1
    assert results[0].title == "JavaScript 高级教程"


def test_search_articles_by_summary(tmp_path: Path):
    """Test searching articles by summary."""
    db_path = tmp_path / "db.sqlite"
    db = AppDB(db_path)
    
    conn = open_db(db_path)
    
    article = Article(
        review_id="1", book_id="MP_WXS_123", title="测试文章",
        summary="这是一篇关于机器学习的文章", url="https://example.com/1", publish_at=1000
    )
    save_article(conn, article)
    conn.commit()
    conn.close()
    
    # Search by summary keyword
    results = db.search_articles("机器学习")
    assert len(results) == 1
    assert results[0].title == "测试文章"


def test_search_articles_with_book_id_filter(tmp_path: Path):
    """Test searching articles with book_id filter."""
    db_path = tmp_path / "db.sqlite"
    db = AppDB(db_path)
    
    conn = open_db(db_path)
    
    article1 = Article(
        review_id="1", book_id="MP_WXS_123", title="Python 入门",
        summary="教程", url="https://example.com/1", publish_at=1000
    )
    article2 = Article(
        review_id="2", book_id="MP_WXS_456", title="Python 进阶",
        summary="高级教程", url="https://example.com/2", publish_at=2000
    )
    
    save_article(conn, article1)
    save_article(conn, article2)
    conn.commit()
    conn.close()
    
    # Search without filter
    results = db.search_articles("Python")
    assert len(results) == 2
    
    # Search with book_id filter
    results = db.search_articles("Python", book_id="MP_WXS_123")
    assert len(results) == 1
    assert results[0].book_id == "MP_WXS_123"


def test_search_articles_empty_query(tmp_path: Path):
    """Test that empty query returns no results."""
    db = AppDB(tmp_path / "db.sqlite")
    
    results = db.search_articles("")
    assert len(results) == 0
    
    results = db.search_articles("   ")
    assert len(results) == 0


def test_search_articles_limit(tmp_path: Path):
    """Test that search respects limit parameter."""
    db_path = tmp_path / "db.sqlite"
    db = AppDB(db_path)
    
    conn = open_db(db_path)
    
    # Create 10 articles
    for i in range(10):
        article = Article(
            review_id=str(i), book_id="MP_WXS_123", title=f"Python 教程 {i}",
            summary="教程", url=f"https://example.com/{i}", publish_at=1000 + i
        )
        save_article(conn, article)
    conn.commit()
    conn.close()
    
    # Search with limit
    results = db.search_articles("Python", limit=5)
    assert len(results) == 5
    
    # Search with max limit (200)
    results = db.search_articles("Python", limit=500)
    assert len(results) == 10  # Only 10 articles exist


def test_search_articles_no_results(tmp_path: Path):
    """Test search with no matching results."""
    db_path = tmp_path / "db.sqlite"
    db = AppDB(db_path)
    
    conn = open_db(db_path)
    
    article = Article(
        review_id="1", book_id="MP_WXS_123", title="Python 教程",
        summary="基础教程", url="https://example.com/1", publish_at=1000
    )
    save_article(conn, article)
    conn.commit()
    conn.close()
    
    # Search for non-existent keyword
    results = db.search_articles("Rust")
    assert len(results) == 0


def test_search_route_returns_200(tmp_path: Path):
    """Test that search route returns 200."""
    from web_app import WebApp
    
    db = AppDB(tmp_path / "db.sqlite")
    creds = CredentialStore(tmp_path / "credentials.json")
    service = SyncService(db, creds)
    scheduler = Scheduler(db, service)
    app = WebApp(db, creds, service, scheduler)
    
    # Mock WSGI call
    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/search",
        "QUERY_STRING": "q=Python",
        "SERVER_NAME": "localhost",
        "SERVER_PORT": "8080",
    }
    
    status_captured = []
    headers_captured = []
    
    def start_response(status, headers):
        status_captured.append(status)
        headers_captured.extend(headers)
    
    result = app(environ, start_response)
    
    assert status_captured[0].startswith("200")
