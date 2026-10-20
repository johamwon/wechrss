"""Tests for dark mode functionality."""
from pathlib import Path


def test_css_has_dark_mode_variables():
    """Test that CSS file contains dark mode CSS variables."""
    css_path = Path(__file__).parent.parent / "static" / "app.css"
    content = css_path.read_text(encoding="utf-8")
    
    # Check for dark mode media query
    assert "@media (prefers-color-scheme: dark)" in content
    
    # Check for manual toggle selectors
    assert '[data-theme="dark"]' in content
    assert '[data-theme="light"]' in content
    
    # Check for key dark mode variables
    assert "--bg: #1a1f1d" in content
    assert "--text: #e8f0ec" in content
    assert "--panel: #242b28" in content


def test_css_uses_variables_not_hardcoded():
    """Test that CSS uses variables instead of hardcoded colors."""
    css_path = Path(__file__).parent.parent / "static" / "app.css"
    content = css_path.read_text(encoding="utf-8")
    
    # Count hardcoded colors (should be minimal or zero)
    hardcoded_count = content.count("background: #") + content.count("color: #")
    
    # Allow some hardcoded colors in specific contexts (like white for text on colored backgrounds)
    # but the majority should use variables
    assert hardcoded_count < 10, f"Found {hardcoded_count} hardcoded colors, expected < 10"


def test_javascript_has_theme_toggle():
    """Test that JavaScript includes theme switching logic."""
    js_path = Path(__file__).parent.parent / "static" / "app.js"
    content = js_path.read_text(encoding="utf-8")
    
    # Check for theme toggle functionality
    assert "data-theme-toggle" in content
    assert "localStorage" in content
    assert "prefers-color-scheme" in content
    assert "data-theme" in content


def test_settings_page_has_theme_toggle():
    """Test that settings page includes theme toggle UI."""
    template_path = Path(__file__).parent.parent / "templates" / "settings.html"
    content = template_path.read_text(encoding="utf-8")
    
    # Check for theme toggle button
    assert "data-theme-toggle" in content
    assert "外观" in content
    assert "深色模式" in content
    assert "#appearance" in content


def test_base_template_supports_data_theme():
    """Test that base template can support data-theme attribute."""
    template_path = Path(__file__).parent.parent / "templates" / "base.html"
    content = template_path.read_text(encoding="utf-8")
    
    # Base template should have html tag that can accept data-theme
    assert "<html" in content
    assert 'lang="zh-CN"' in content
