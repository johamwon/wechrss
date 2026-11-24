from __future__ import annotations

import base64
import html as html_lib
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

BROWSER_UA = (
    "Mozilla/5.0 (Linux; Android 12; BRA-AL00 Build/W528JS) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Mobile Safari/537.36 "
    "MicroMessenger/8.0.50"
)

_BLOCK_MARKERS = (
    "当前环境异常，完成验证后即可继续访问",
    "访问过于频繁",
    "操作频繁",
)


class ArticleIdentityError(RuntimeError):
    pass


@dataclass
class ArticleIdentity:
    article_url: str
    resolved_url: str
    biz: str
    bid: str
    book_id: str
    title: str = ""
    account_name: str = ""
    method: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _pad_b64(value: str) -> str:
    value = value.strip()
    return value + "=" * ((4 - len(value) % 4) % 4)


def decode_biz(value: str) -> str:
    value = unquote(value).strip()
    try:
        decoded = base64.b64decode(_pad_b64(value), validate=False).decode("utf-8").strip()
    except Exception as exc:
        raise ArticleIdentityError(f"biz 不是有效 Base64: {exc}") from exc
    if not decoded.isdigit():
        raise ArticleIdentityError(f"biz 解码结果不是数字公众号 ID: {decoded!r}")
    return decoded


def _validate_wechat_url(url: str) -> str:
    url = str(url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ArticleIdentityError("只支持 http/https 微信文章链接")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "mp.weixin.qq.com":
        raise ArticleIdentityError("只支持 mp.weixin.qq.com 的公众号文章链接")
    if not parsed.path.startswith("/s"):
        raise ArticleIdentityError("当前只支持 mp.weixin.qq.com/s... 公众号文章链接")
    return url


def biz_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    return str((query.get("__biz") or query.get("biz") or [""])[0]).strip()


def resolve_community_article(url: str, *, session: requests.Session | None = None) -> ArticleIdentity:
    """Resolve a public submission without following off-site redirects or running its scripts."""
    def checked(value: str) -> str:
        if len(value) > 4096 or re.search(r"[\s\\]", value):
            raise ArticleIdentityError("请填写有效的微信文章链接")
        parsed = urlparse(value)
        if (parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != "mp.weixin.qq.com"
                or parsed.params or not re.fullmatch(r"/s(?:/[A-Za-z0-9_-]+)?/?", parsed.path)):
            raise ArticleIdentityError("请粘贴 mp.weixin.qq.com 的公众号文章链接")
        return urlunparse(parsed._replace(scheme="https", netloc="mp.weixin.qq.com", fragment=""))

    original = checked(url.strip())
    current = original
    try:
        for _ in range(3):
            biz = biz_from_url(current)
            if biz:
                bid = decode_biz(biz)
                if not re.fullmatch(r"[0-9]{1,24}", bid):
                    raise ArticleIdentityError("链接没有有效的公众号 ID")
                return ArticleIdentity(original, current, biz, bid, f"MP_WXS_{bid}", method="url-query")
            if not re.fullmatch(r"/s/[A-Za-z0-9_-]+/?", urlparse(current).path):
                raise ArticleIdentityError("链接缺少公众号标识，请从微信文章菜单重新复制链接")
            requester = session or requests
            with requester.get(current, headers={"User-Agent": BROWSER_UA}, timeout=(3, 5),
                               allow_redirects=False, stream=True) as response:
                if response.is_redirect:
                    current = checked(urljoin(current, response.headers["Location"]))
                    continue
                if response.status_code in {403, 429}:
                    raise ArticleIdentityError("微信暂时限制链接识别，请稍后重试")
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_content(chunk_size=16384):
                    body.extend(chunk)
                    if len(body) > 512 * 1024:
                        break
                html_text = body.decode("utf-8", errors="replace")
                if any(marker in html_text for marker in _BLOCK_MARKERS):
                    raise ArticleIdentityError("微信文章页要求人工验证，请稍后重试")
                identity = extract_identity_from_html(html_text, original, current)
                if identity and re.fullmatch(r"[0-9]{1,24}", identity.bid):
                    return identity
                break
    except (requests.RequestException, ValueError) as exc:
        raise ArticleIdentityError("无法识别这篇文章，请核对链接后重试") from exc
    raise ArticleIdentityError("未找到公众号，请换一篇公开文章的链接")


def _clean_js_string(value: str) -> str:
    value = html_lib.unescape(value or "")
    # Common encodings found in inline WeChat script variables.
    value = value.replace(r"\/", "/")
    value = value.replace(r"\x26", "&").replace(r"\u0026", "&")
    value = value.replace(r"\x3d", "=").replace(r"\u003d", "=")
    value = value.replace(r"\x3f", "?").replace(r"\u003f", "?")
    return value


def _first_meta(soup: BeautifulSoup, *, prop: str | None = None, name: str | None = None) -> str:
    attrs: dict[str, str] = {}
    if prop:
        attrs["property"] = prop
    if name:
        attrs["name"] = name
    tag = soup.find("meta", attrs=attrs)
    if tag and tag.get("content"):
        return str(tag.get("content") or "").strip()
    return ""


def extract_identity_from_html(html_text: str, source_url: str, resolved_url: str | None = None) -> ArticleIdentity | None:
    """Extract public-account identity from already fetched WeChat article HTML.

    New `/s/<token>` links do not expose __biz in the URL.  The rendered/source HTML still
    commonly contains either `window.biz`/`var biz`, or `msg_link`, whose value is the canonical
    long article URL with __biz.  This parser handles both without executing JavaScript.
    """
    resolved_url = resolved_url or source_url

    # A redirect may already have expanded the short URL.
    candidates: list[tuple[str, str]] = []
    direct = biz_from_url(resolved_url)
    if direct:
        candidates.append((direct, "redirect-url"))

    # Direct JS variable. Be deliberately narrow so random "biz" strings in article text do not match.
    biz_patterns = (
        r"(?:window\.)?biz\s*=\s*['\"]([^'\"]+)['\"]",
        r"var\s+biz\s*=\s*['\"]([^'\"]+)['\"]",
        r"__biz\s*=\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in biz_patterns:
        m = re.search(pattern, html_text, flags=re.I)
        if m:
            candidates.append((_clean_js_string(m.group(1)), "html-biz"))
            break

    # Canonical long link stored in msg_link (common for new permanent /s/<token> URLs).
    link_patterns = (
        r"(?:window\.)?msg_link\s*=\s*['\"](.+?)['\"]\s*;",
        r"var\s+msg_link\s*=\s*['\"](.+?)['\"]\s*;",
    )
    for pattern in link_patterns:
        m = re.search(pattern, html_text, flags=re.I | re.S)
        if not m:
            continue
        link = _clean_js_string(m.group(1))
        candidate = biz_from_url(link)
        if candidate:
            candidates.append((candidate, "html-msg-link"))
            break

    soup = BeautifulSoup(html_text, "html.parser")

    # Metadata/canonical URL fallbacks.
    url_candidates: list[str] = []
    for prop in ("og:url", "twitter:url"):
        val = _first_meta(soup, prop=prop)
        if val:
            url_candidates.append(_clean_js_string(val))
    canonical = soup.find("link", attrs={"rel": lambda x: x and "canonical" in x})
    if canonical and canonical.get("href"):
        url_candidates.append(_clean_js_string(str(canonical.get("href"))))
    for link in url_candidates:
        candidate = biz_from_url(link)
        if candidate:
            candidates.append((candidate, "html-canonical"))
            break

    # Last static fallback: any full WeChat article URL embedded in script/source.
    if not candidates:
        for m in re.finditer(r"https?://mp\.weixin\.qq\.com/s\?[^\"'<>\s]+", html_text, flags=re.I):
            link = _clean_js_string(m.group(0))
            candidate = biz_from_url(link)
            if candidate:
                candidates.append((candidate, "html-embedded-url"))
                break

    if not candidates:
        return None

    biz, method = candidates[0]
    try:
        bid = decode_biz(biz)
    except ArticleIdentityError:
        # If the first loose candidate is not valid, try the remaining candidates.
        for biz2, method2 in candidates[1:]:
            try:
                bid = decode_biz(biz2)
                biz, method = biz2, method2
                break
            except ArticleIdentityError:
                continue
        else:
            return None

    title = _first_meta(soup, prop="og:title") or _first_meta(soup, name="twitter:title")
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    account = _first_meta(soup, prop="og:article:author")
    if not account:
        node = soup.select_one("#js_name, #js_wx_follow_nickname")
        if node:
            account = node.get_text(" ", strip=True)
    if not account:
        m = re.search(r"(?:var\s+)?nickname\s*=\s*['\"](.+?)['\"]\s*;", html_text, flags=re.I | re.S)
        if m:
            account = _clean_js_string(m.group(1)).strip()

    return ArticleIdentity(
        article_url=source_url,
        resolved_url=resolved_url,
        biz=biz,
        bid=bid,
        book_id=f"MP_WXS_{bid}",
        title=html_lib.unescape(title or "").strip(),
        account_name=html_lib.unescape(account or "").strip(),
        method=method,
    )


def _resolve_with_browser(url: str, timeout: float) -> ArticleIdentity:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise ArticleIdentityError(
            "普通 HTTP 页面中没有解析到公众号 BID，且 Playwright 未安装；请使用 Docker 版或安装 playwright + chromium"
        ) from exc

    timeout_ms = max(int(timeout * 1000), 5000)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(user_agent=BROWSER_UA, locale="zh-CN")
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if response and response.status >= 400:
                    raise ArticleIdentityError(f"微信文章页面返回 HTTP {response.status}")
                page.wait_for_timeout(800)
                body_text = page.locator("body").inner_text(timeout=5000)
                if any(marker in body_text for marker in _BLOCK_MARKERS):
                    raise ArticleIdentityError("微信文章页面要求人工验证，未尝试绕过验证")

                values = page.evaluate(
                    """() => ({
                      biz: (window.biz || window.__biz || ''),
                      msgLink: (window.msg_link || ''),
                      title: document.querySelector('meta[property="og:title"]')?.content || document.title || '',
                      account: document.querySelector('meta[property="og:article:author"]')?.content ||
                               document.querySelector('#js_name')?.textContent ||
                               document.querySelector('#js_wx_follow_nickname')?.textContent || ''
                    })"""
                )
                final_url = page.url
                html_text = page.content()
            finally:
                browser.close()
    except ArticleIdentityError:
        raise
    except Exception as exc:
        raise ArticleIdentityError(f"浏览器解析微信文章失败: {exc}") from exc

    if isinstance(values, dict):
        raw_biz = str(values.get("biz") or "").strip()
        if not raw_biz:
            raw_biz = biz_from_url(_clean_js_string(str(values.get("msgLink") or "")))
        if raw_biz:
            bid = decode_biz(raw_biz)
            return ArticleIdentity(
                article_url=url,
                resolved_url=final_url,
                biz=raw_biz,
                bid=bid,
                book_id=f"MP_WXS_{bid}",
                title=str(values.get("title") or "").strip(),
                account_name=str(values.get("account") or "").strip(),
                method="browser-window-biz",
            )

    parsed = extract_identity_from_html(html_text, url, final_url)
    if parsed:
        parsed.method = "browser-" + parsed.method
        return parsed
    raise ArticleIdentityError("浏览器已打开文章，但页面中仍未找到 biz/msg_link")


def resolve_article_identity(
    url: str,
    *,
    timeout: float = 20.0,
    browser_fallback: bool = True,
    session: requests.Session | None = None,
) -> ArticleIdentity:
    url = _validate_wechat_url(url)

    # Old-style long URL: zero network requests.
    direct_biz = biz_from_url(url)
    if direct_biz:
        bid = decode_biz(direct_biz)
        return ArticleIdentity(url, url, direct_biz, bid, f"MP_WXS_{bid}", method="url-query")

    sess = session or requests.Session()
    try:
        response = sess.get(
            url,
            timeout=timeout,
            allow_redirects=True,
            headers={
                "User-Agent": BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
            },
        )
        if response.status_code == 429:
            raise ArticleIdentityError("微信文章页面返回 HTTP 429，请稍后人工重试")
        response.raise_for_status()
        text = response.text
        plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
        if any(marker in plain for marker in _BLOCK_MARKERS):
            raise ArticleIdentityError("微信文章页面要求人工验证，未尝试绕过验证")
        parsed = extract_identity_from_html(text, url, response.url)
        if parsed:
            return parsed
    except ArticleIdentityError:
        raise
    except requests.RequestException as exc:
        if not browser_fallback:
            raise ArticleIdentityError(f"获取微信文章页面失败: {exc}") from exc

    if browser_fallback:
        return _resolve_with_browser(url, timeout)
    raise ArticleIdentityError("文章 URL 不含 __biz，且 HTML 中没有找到 window.biz/msg_link")
