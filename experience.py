"""Local-only onboarding, feed-address guidance and actionable sync health."""
from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

from service import Source
from wechat_mp_fetcher import FetcherError


def normalize_public_url(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in {"http", "https"} or not host or parts.username is not None
                or parts.password is not None or parts.query or parts.fragment or parts.path not in {"", "/"}
                or any(char.isspace() for char in value) or "\\" in value or "%" in parts.netloc):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            host = host.encode("idna").decode("ascii").lower()
            if len(host) > 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                          for label in host.rstrip(".").split(".")):
                raise ValueError
        else:
            if address.is_unspecified or address.is_multicast:
                raise ValueError
            host = f"[{address}]" if address.version == 6 else str(address)
        if port == 0:
            raise ValueError
        return f"{parts.scheme}://{host}" + (f":{port}" if port else "")
    except (ValueError, UnicodeError):
        raise FetcherError("请填写完整的 http(s) 地址（可带端口），不要包含密码、路径、查询参数或 0.0.0.0") from None


def address_scope(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return "local"
    try:
        address = ipaddress.ip_address(host)
        address = getattr(address, "ipv4_mapped", None) or address
        if address.is_loopback or address.is_unspecified:
            return "local"
        if not address.is_global:
            return "lan"
    except ValueError:
        if "." not in host or host.endswith((".local", ".lan", ".internal", ".home", ".test", ".invalid")):
            return "lan"
    return "public"


def diagnose_feed(base_url: str, target: str, *, password_required: bool, token_available: bool) -> dict:
    target = target if target in {"local", "lan", "cloud"} else "local"
    scope = address_scope(base_url)
    notes = []
    blocked = False
    if scope == "local" and target != "local":
        blocked = True
        notes.append("这是本机地址。手机或云端阅读器访问 127.0.0.1 / localhost 时，访问的是它们自己，无法连接这台电脑。")
    elif scope == "lan" and target == "cloud":
        blocked = True
        notes.append("这是内网地址，云端阅读器通常无法访问。请配置可访问的 HTTPS 域名，或改用同一网络内的阅读器。")
    elif target == "lan":
        notes.append("请让手机和服务位于同一网络，使用服务的局域网 IP；仅绑定本机时还需调整监听地址与管理密码。")
    elif target == "local":
        notes.append("请使用这台电脑上的阅读器。电脑关机、休眠或关闭服务后，阅读器将无法获取更新。")
    else:
        notes.append("域名格式适合云端阅读器；仍需在阅读器中实际添加，确认 DNS、端口、防火墙和反向代理均可访问。")
    if target != "local" and urlsplit(base_url).scheme != "https":
        notes.append("当前是 HTTP 地址。远程订阅建议配置 HTTPS，避免订阅令牌在传输中泄露。")
    if password_required and not token_available:
        blocked = True
        notes.append("管理端已启用密码。请先生成阅读器专用链接，避免把管理密码交给阅读器。")
    if not password_required and target != "local":
        notes.append("尚未启用管理密码。开放远程访问前，请设置 ADMIN_PASSWORD；订阅令牌不会保护管理页面。")
    if token_available:
        notes.append("专用链接仅允许读取这个 Feed；持有链接的人可以读取文章元数据，请勿公开分享。")
    return {"target": target, "scope": scope, "blocked": blocked,
            "label": "需要调整地址或认证" if blocked else "待阅读器验证", "notes": notes}


def source_health(source: Source, *, configured: bool, scheduler_enabled: bool,
                  account_issue: str = "", queued: bool = False, now: int | None = None) -> dict:
    now = int(time.time()) if now is None else now
    status = source.last_status
    if not configured:
        code, title, message, action, href = (
            "disconnected", "等待账号连接", "连接微信读书后才能获取新文章；已有文章仍会保留。", "连接账号", "/settings?next=onboarding#login")
    elif queued or (status == "running" and now - source.updated_at < 600):
        code, title, message, action, href = (
            "running", "同步中" if status == "running" else "已排队", "完成后本页会自动更新，请稍候。", "", "")
    elif account_issue == "risk_control":
        code, title, message, action, href = (
            "risk_control", "需要在官方应用完成验证", "自动同步已暂停。请打开微信或微信读书完成人工验证，等待后再手动检查一次。", "查看处理方法", "/settings#login")
    elif account_issue == "auth_expired":
        code, title, message, action, href = (
            "auth_expired", "微信读书登录已过期", "自动续期未能恢复连接，自动同步已暂停。重新扫码后请同步一次确认。", "重新扫码", "/settings?next=onboarding#login")
    elif status in {"auth_expired", "risk_control"}:
        code, title, message, action, href = (
            "retry", "等待重新同步确认", "账号信息已更新，请手动同步一次确认是否恢复。", "查看订阅", f"/sources/{source.id}")
    elif status in {"error", "running"}:
        code, title, message, action, href = (
            "error", "同步中断" if status == "running" else "最近同步失败",
            "已有文章仍可订阅。请查看错误详情，检查网络后手动同步。", "查看详情", f"/sources/{source.id}")
    elif not source.enabled or not scheduler_enabled:
        code, title, message, action, href = (
            "paused", "自动同步已暂停", "已有文章仍可订阅，也可以手动同步。", "查看设置", "/settings#scheduler")
    elif source.last_success_at and now - source.last_success_at > max(source.sync_interval_minutes * 120, 3600):
        code, title, message, action, href = (
            "stale", "较长时间未同步成功", "这不代表公众号没有更新，请检查服务是否持续运行，并手动同步确认。", "查看订阅", f"/sources/{source.id}")
    elif not source.last_success_at:
        code, title, message, action, href = (
            "never", "等待首次同步", "成功获取文章后，就可以在阅读器里验证订阅。", "开始首次同步", f"/sources/{source.id}")
    else:
        code, title, message, action, href = (
            "ok", "最近同步成功", "本次没有新文章也是正常结果；可查看下方上次成功与最近收录时间。", "", "")
    return {"code": code, "title": title, "message": message, "action": action, "href": href,
            "busy": queued or (status == "running" and now - source.updated_at < 600)}
