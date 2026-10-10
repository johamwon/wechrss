"""Tests for logging configuration."""
import logging


from logging_config import setup_logging, get_logger, JsonFormatter


class TestLoggingSetup:
    def test_setup_returns_logger(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        logger = setup_logging()
        assert isinstance(logger, logging.Logger)
        assert logger.name == "wechrss"

    def test_default_level_is_info(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        logger = setup_logging()
        assert logger.level == logging.INFO

    def test_custom_log_level(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        logger = setup_logging()
        assert logger.level == logging.DEBUG

    def test_file_handler_created_when_log_file_set(self, tmp_path, monkeypatch):
        log_file = tmp_path / "test.log"
        monkeypatch.setenv("LOG_FILE", str(log_file))
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        logger = setup_logging()
        
        # Write a log message
        logger.info("Test message")
        
        # Verify file was created and contains the message
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        assert "Test message" in content

    def test_get_logger_returns_child(self, monkeypatch):
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        setup_logging()
        
        child = get_logger("test")
        assert child.name == "wechrss.test"


class TestJsonFormatter:
    def test_json_format(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="Test message", args=(), exc_info=None
        )
        output = formatter.format(record)
        
        import json
        data = json.loads(output)
        assert data["level"] == "INFO"
        assert data["message"] == "Test message"
        assert "timestamp" in data

    def test_json_format_with_exception(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("Test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()
        
        record = logging.LogRecord(
            name="test", level=logging.ERROR, pathname="", lineno=0,
            msg="Error occurred", args=(), exc_info=exc_info
        )
        output = formatter.format(record)
        
        import json
        data = json.loads(output)
        assert "exception" in data
        assert "ValueError" in data["exception"]


class TestLoggingIntegration:
    def test_service_uses_logger(self, tmp_path, monkeypatch):
        """Verify service module uses the logging system."""
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        setup_logging()
        
        from service import logger as service_logger
        assert service_logger.name == "wechrss.service"

    def test_webapp_uses_logger(self, tmp_path, monkeypatch):
        """Verify web_app module uses the logging system."""
        monkeypatch.delenv("LOG_LEVEL", raising=False)
        monkeypatch.delenv("LOG_FILE", raising=False)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        setup_logging()
        
        # Import web_app to trigger logger initialization
        import web_app
        assert hasattr(web_app, 'logger')
        assert web_app.logger.name == "wechrss.web"
