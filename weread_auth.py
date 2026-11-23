from __future__ import annotations

import base64
import hashlib
import io
import json
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable
from urllib.parse import quote

import qrcode
import requests

WEREAD_BASE = "https://i.weread.qq.com"
WX_APPID = "wxab9b71ad2b90ff34"
WX_SCOPE = "snsapi_userinfo,snsapi_timeline,snsapi_friend"

# Current, publicly reproducible WeRead e-ink mobile profile.  The article API
# only depends on the normal mobile auth headers (vid + accessToken), while this
# profile gives us a stable QR-login and refresh-token lifecycle.
PROFILE_NAME = "eink-2.1.2"
DEVICE_NAME = "BOOX"
DEVICE_TYPE = 3
VERSION_HEADERS = {
    "baseapi": "30",
    "appver": "2.1.2.10245900",
    "basever": "2.1.2.10245900",
    "osver": "11",
    "channelId": "900",
    "User-Agent": "WeRead/2.1.2 WRBrand/Onyx wr_eink Dalvik/2.1.0 (Linux; U; Android 11; BOOX Build/onyx)",
}


class WeReadAuthError(RuntimeError):
    pass


class RefreshExpiredError(WeReadAuthError):
    pass


class RefreshRiskError(WeReadAuthError):
    pass


class QRExpiredError(WeReadAuthError):
    pass


class QRDeclinedError(WeReadAuthError):
    pass


@dataclass
class WeReadCredentials:
    vid: str
    accessToken: str
    refreshToken: str = ""
    deviceId: str = ""
    deviceName: str = DEVICE_NAME
    profile: str = PROFILE_NAME
    skey: str = ""
    wxAccessToken: str = ""
    guestToken: str = ""
    syncKey: int = 0
    name: str = ""
    updatedAt: int = 0

    def as_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["updatedAt"] = int(self.updatedAt or time.time())
        return data

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "WeReadCredentials":
        return cls(
            vid=str(data.get("vid") or data.get("v_id") or "").strip(),
            accessToken=str(data.get("accessToken") or data.get("access_token") or "").strip(),
            refreshToken=str(data.get("refreshToken") or data.get("refresh_token") or "").strip(),
            deviceId=str(data.get("deviceId") or data.get("device_id") or "").strip(),
            deviceName=str(data.get("deviceName") or data.get("device_name") or DEVICE_NAME).strip() or DEVICE_NAME,
            profile=str(data.get("profile") or PROFILE_NAME).strip() or PROFILE_NAME,
            skey=str(data.get("skey") or "").strip(),
            wxAccessToken=str(data.get("wxAccessToken") or data.get("wx_access_token") or "").strip(),
            guestToken=str(data.get("guestToken") or data.get("guest_token") or "").strip(),
            syncKey=int(data.get("syncKey") or data.get("sync_key") or 0),
            name=str(data.get("name") or "").strip(),
            updatedAt=int(data.get("updatedAt") or data.get("updated_at") or 0),
        )

    def validate_basic(self) -> None:
        if not self.vid.isdigit():
            raise WeReadAuthError("登录结果中的 VID 无效")
        if not self.accessToken:
            raise WeReadAuthError("登录结果缺少 accessToken")

    @property
    def can_refresh(self) -> bool:
        return bool(self.refreshToken and self.deviceId)


@dataclass
class QRRequest:
    uuid: str
    confirm_url: str
    qr_data_uri: str


@dataclass
class PollResult:
    status: str
    errcode: int
    wx_code: str = ""


@dataclass
class SessionInitResult:
    guest_token: str = ""
    sync_key: int = 0
    warnings: list[str] = field(default_factory=list)


def _digits(length: int) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))


def new_device_id() -> str:
    # Mirrors the stable e-ink profile: fixed prefix + unsigned 63-bit decimal,
    # padded to 19 digits.
    n = secrets.randbits(63)
    return "eink334691225" + str(n).zfill(19)


def new_install_id() -> str:
    return "eink31" + _digits(26)


def signature(timestamp_ms: int, device_id: str, random_value: int) -> str:
    raw = f"{timestamp_ms}{device_id}{random_value}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def qr_data_uri(text: str) -> str:
    qr = qrcode.QRCode(version=None, box_size=8, border=3)
    qr.add_data(text)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


class WeReadAuthClient:
    def __init__(self, *, timeout: float = 25.0, session: requests.Session | None = None):
        self.timeout = float(timeout)
        self.session = session or requests.Session()

    @staticmethod
    def version_headers() -> dict[str, str]:
        return dict(VERSION_HEADERS)

    @staticmethod
    def auth_headers(credentials: WeReadCredentials) -> dict[str, str]:
        headers = dict(VERSION_HEADERS)
        headers.update({"vid": credentials.vid, "accessToken": credentials.accessToken})
        return headers

    @staticmethod
    def _payload_error(payload: dict[str, Any]) -> tuple[int, str]:
        code = payload.get("errcode", payload.get("errCode", 0))
        try:
            code = int(code or 0)
        except (TypeError, ValueError):
            code = 0
        msg = str(payload.get("errmsg") or payload.get("errMsg") or "")
        return code, msg

    def _json_response(self, response: requests.Response, label: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise WeReadAuthError(f"{label} 返回非 JSON 数据") from exc
        if not isinstance(payload, dict):
            raise WeReadAuthError(f"{label} 返回格式异常")
        return payload

    def request_qr(self) -> QRRequest:
        ticket_response = self.session.get(
            f"{WEREAD_BASE}/wxticket",
            params={"nonceStr": "weread"},
            headers=VERSION_HEADERS,
            timeout=self.timeout,
        )
        ticket_response.raise_for_status()
        ticket = self._json_response(ticket_response, "微信读书二维码 ticket")
        sig = ticket.get("signature")
        timestamp = ticket.get("timeStamp")
        if not isinstance(sig, str) or not sig or timestamp is None:
            code, msg = self._payload_error(ticket)
            raise WeReadAuthError(f"微信读书二维码 ticket 获取失败: {code or ''} {msg}".strip())

        qr_response = self.session.get(
            "https://open.weixin.qq.com/connect/sdk/qrconnect",
            params={
                "appid": WX_APPID,
                "noncestr": "weread",
                "timestamp": str(timestamp),
                "scope": WX_SCOPE,
                "signature": sig,
            },
            headers={"User-Agent": VERSION_HEADERS["User-Agent"]},
            timeout=self.timeout,
        )
        qr_response.raise_for_status()
        payload = self._json_response(qr_response, "微信二维码")
        try:
            errcode = int(payload.get("errcode", -1))
        except (TypeError, ValueError):
            errcode = -1
        uuid = payload.get("uuid")
        if errcode != 0 or not isinstance(uuid, str) or not uuid:
            raise WeReadAuthError(f"微信二维码生成失败: {errcode}")
        confirm_url = f"https://open.weixin.qq.com/connect/confirm?uuid={quote(uuid, safe='')}"
        return QRRequest(uuid=uuid, confirm_url=confirm_url, qr_data_uri=qr_data_uri(confirm_url))

    def poll_qr_once(self, uuid: str, *, last: int | None = None, timeout: float | None = None) -> PollResult:
        params: dict[str, Any] = {"f": "json", "uuid": uuid}
        if last is not None:
            params["last"] = str(last)
        response = self.session.get(
            "https://long.open.weixin.qq.com/connect/l/qrconnect",
            params=params,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=timeout or min(self.timeout, 25.0),
        )
        response.raise_for_status()
        payload = self._json_response(response, "微信扫码状态")
        try:
            code = int(payload.get("wx_errcode", 0))
        except (TypeError, ValueError):
            code = 0
        wx_code = str(payload.get("wx_code") or "")
        if code == 405:
            if not wx_code:
                raise WeReadAuthError("扫码已确认，但微信没有返回登录 code")
            return PollResult("confirmed", code, wx_code)
        if code == 404:
            return PollResult("scanned", code)
        if code == 408:
            return PollResult("waiting", code)
        if code == 402:
            raise QRExpiredError("二维码已过期，请重新生成")
        if code == 403:
            raise QRDeclinedError("你在微信中取消了登录，请重新生成二维码")
        raise WeReadAuthError(f"未知扫码状态: {code}")

    def exchange_qr(self, wx_code: str, *, device_id: str | None = None) -> WeReadCredentials:
        device_id = device_id or new_device_id()
        timestamp = int(time.time() * 1000)
        random_value = secrets.randbelow(1000)
        body = {
            "appFirstInstall": 1,
            "code": wx_code,
            "deviceId": device_id,
            "deviceName": DEVICE_NAME,
            "installId": new_install_id(),
            "isAutoLogout": 0,
            "isFromQrcode": 1,
            "random": random_value,
            "signature": signature(timestamp, device_id, random_value),
            "timestamp": timestamp,
            "trackId": "",
            "deviceType": DEVICE_TYPE,
        }
        headers = dict(VERSION_HEADERS)
        headers["Content-Type"] = "application/json; charset=UTF-8"
        response = self.session.post(
            f"{WEREAD_BASE}/login",
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        payload = self._json_response(response, "微信读书扫码登录")
        code, msg = self._payload_error(payload)
        access_token = str(payload.get("accessToken") or "")
        refresh_token = str(payload.get("refreshToken") or "")
        if not response.ok or not access_token or not refresh_token:
            raise WeReadAuthError(f"微信读书登录失败: {code or response.status_code} {msg}".strip())
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        credentials = WeReadCredentials(
            vid=str(payload.get("vid") or ""),
            accessToken=access_token,
            refreshToken=refresh_token,
            deviceId=device_id,
            deviceName=DEVICE_NAME,
            profile=PROFILE_NAME,
            skey=str(payload.get("skey") or ""),
            wxAccessToken=str(payload.get("wxAccessToken") or ""),
            name=str(user.get("name") or ""),
            updatedAt=int(time.time()),
        )
        credentials.validate_basic()
        return credentials

    def refresh(self, credentials: WeReadCredentials) -> WeReadCredentials:
        if not credentials.can_refresh:
            raise WeReadAuthError("当前凭证没有 refreshToken/deviceId，无法自动续期，请重新扫码")
        if not credentials.profile.startswith("eink"):
            raise WeReadAuthError("当前会话设备画像不支持此续期算法，请重新使用 Web 扫码登录")
        timestamp = int(time.time() * 1000)
        random_value = secrets.randbelow(1000) + 1
        body = {
            "deviceId": credentials.deviceId,
            "deviceName": credentials.deviceName or DEVICE_NAME,
            "inBackground": 0,
            "kickType": 1,
            "random": random_value,
            "refCgi": "",
            "refreshToken": credentials.refreshToken,
            "signature": signature(timestamp, credentials.deviceId, random_value),
            "timestamp": timestamp,
            "trackId": "",
            "deviceType": DEVICE_TYPE,
        }
        headers = dict(VERSION_HEADERS)
        headers["Content-Type"] = "application/json; charset=UTF-8"
        response = self.session.post(
            f"{WEREAD_BASE}/login",
            headers=headers,
            data=json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
            timeout=self.timeout,
        )
        if response.status_code in {403, 429}:
            raise RefreshRiskError("微信读书续期请求受限")
        if response.status_code == 401:
            raise RefreshExpiredError("微信读书续期凭证已失效")
        payload = self._json_response(response, "微信读书凭证续期")
        code, msg = self._payload_error(payload)
        if code in {-2041, -2010}:
            raise RefreshRiskError("微信读书要求人工处理后再续期")
        if code == -2012:
            raise RefreshExpiredError("微信读书续期凭证已失效")
        access_token = str(payload.get("accessToken") or "")
        if not response.ok or not access_token:
            raise WeReadAuthError(f"凭证续期失败: {code or response.status_code} {msg}".strip())
        new_vid = str(payload.get("vid") or credentials.vid)
        if new_vid != credentials.vid:
            raise WeReadAuthError("续期返回了不同账号的 VID，已拒绝覆盖本地凭证")
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        return WeReadCredentials(
            vid=credentials.vid,
            accessToken=access_token,
            refreshToken=str(payload.get("refreshToken") or credentials.refreshToken),
            deviceId=credentials.deviceId,
            deviceName=credentials.deviceName or DEVICE_NAME,
            profile=credentials.profile or PROFILE_NAME,
            skey=str(payload.get("skey") or credentials.skey),
            wxAccessToken=str(payload.get("wxAccessToken") or credentials.wxAccessToken),
            guestToken=credentials.guestToken,
            syncKey=credentials.syncKey,
            name=str(user.get("name") or credentials.name),
            updatedAt=int(time.time()),
        )

    def initialize_feature(self, credentials: WeReadCredentials) -> SessionInitResult:
        """Best-effort account metadata initialization; no risk-control bypasses."""
        result = SessionInitResult()
        try:
            response = self.session.get(
                f"{WEREAD_BASE}/feature",
                params={"synckey": max(int(credentials.syncKey or 0), 0)},
                headers=self.auth_headers(credentials),
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = self._json_response(response, "微信读书 feature")
            code, msg = self._payload_error(payload)
            if code:
                result.warnings.append(f"feature 初始化返回 {code}: {msg}")
                return result
            feature = payload.get("feature") if isinstance(payload.get("feature"), dict) else {}
            result.guest_token = str(feature.get("guest_token") or "")
            try:
                result.sync_key = int(payload.get("synckey") or 0)
            except (TypeError, ValueError):
                result.sync_key = 0
        except Exception as exc:
            result.warnings.append(f"feature 初始化失败: {exc}")
        return result


class LoginManager:
    """In-memory QR-login state machine. It never exposes tokens through the Web API."""

    ACTIVE = {"requesting", "waiting", "scanned", "exchanging", "initializing"}

    def __init__(
        self,
        auth_client: WeReadAuthClient,
        persist: Callable[[WeReadCredentials], None],
        *,
        deadline_seconds: int = 300,
        initialize_session: bool = True,
    ):
        self.auth_client = auth_client
        self.persist = persist
        self.deadline_seconds = max(60, int(deadline_seconds))
        self.initialize_session = initialize_session
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {
            "status": "idle",
            "message": "",
            "qr": "",
            "confirmUrl": "",
            "startedAt": 0,
            "expiresAt": 0,
            "account": "",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _update(self, **values: Any) -> None:
        with self._lock:
            self._state.update(values)

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("status") in self.ACTIVE:
                return dict(self._state)
            self._cancel = threading.Event()
            self._state = {
                "status": "requesting",
                "message": "正在生成微信登录二维码…",
                "qr": "",
                "confirmUrl": "",
                "startedAt": int(time.time()),
                "expiresAt": int(time.time()) + self.deadline_seconds,
                "account": "",
            }
        self._thread = threading.Thread(target=self._run, name="weread-qr-login", daemon=True)
        self._thread.start()
        return self.status()

    def cancel(self) -> None:
        self._cancel.set()
        if self.status().get("status") in self.ACTIVE:
            self._update(status="cancelled", message="扫码登录已取消", qr="", confirmUrl="")

    def _run(self) -> None:
        try:
            qr = self.auth_client.request_qr()
            if self._cancel.is_set():
                return
            deadline = time.time() + self.deadline_seconds
            self._update(
                status="waiting",
                message="请使用微信扫码并在手机上确认登录",
                qr=qr.qr_data_uri,
                confirmUrl=qr.confirm_url,
                expiresAt=int(deadline),
            )
            last: int | None = None
            while not self._cancel.is_set():
                if time.time() >= deadline:
                    raise QRExpiredError("二维码登录超时，请重新生成")
                try:
                    poll = self.auth_client.poll_qr_once(qr.uuid, last=last, timeout=20)
                except requests.Timeout:
                    continue
                if self._cancel.is_set():
                    return
                last = poll.errcode
                if poll.status == "waiting":
                    continue
                if poll.status == "scanned":
                    self._update(status="scanned", message="已扫码，请在微信中确认登录")
                    continue
                if poll.status == "confirmed":
                    self._update(status="exchanging", message="已确认，正在建立微信读书会话…")
                    credentials = self.auth_client.exchange_qr(poll.wx_code)
                    init = SessionInitResult()
                    if self.initialize_session and not self._cancel.is_set():
                        self._update(status="initializing", message="登录成功，正在初始化账号状态…")
                        init = self.auth_client.initialize_feature(credentials)
                        if init.guest_token:
                            credentials.guestToken = init.guest_token
                        if init.sync_key:
                            credentials.syncKey = init.sync_key
                    if self._cancel.is_set():
                        return
                    self.persist(credentials)
                    suffix = ""
                    if init.warnings:
                        suffix = "；" + "；".join(init.warnings[:2])
                    self._update(
                        status="success",
                        message=f"登录成功{suffix}",
                        qr="",
                        confirmUrl="",
                        account=credentials.name or credentials.vid,
                    )
                    return
            if self.status().get("status") in self.ACTIVE:
                self._update(status="cancelled", message="扫码登录已取消", qr="", confirmUrl="")
        except QRExpiredError as exc:
            self._update(status="expired", message=str(exc), qr="", confirmUrl="")
        except QRDeclinedError as exc:
            self._update(status="declined", message=str(exc), qr="", confirmUrl="")
        except Exception as exc:
            self._update(status="error", message=str(exc), qr="", confirmUrl="")
