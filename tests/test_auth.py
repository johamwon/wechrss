import time
from urllib.parse import urlparse
from weread_auth import (
    LoginManager,
    PollResult,
    QRRequest,
    WeReadAuthClient,
    WeReadCredentials,
    signature,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.ok = 200 <= status_code < 300

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self):
        self.gets = []
        self.posts = []
        self.poll_payloads = []
        self.login_payloads = []

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        parsed = urlparse(url)
        if url.endswith("/wxticket"):
            return FakeResponse({"signature": "ticket-sign", "timeStamp": 123456})
        if "sdk/qrconnect" in url:
            return FakeResponse({"errcode": 0, "uuid": "uuid-test"})
        if parsed.hostname == "long.open.weixin.qq.com":
            return FakeResponse(self.poll_payloads.pop(0))
        if url.endswith("/feature"):
            return FakeResponse({"feature": {"guest_token": "guest-x"}, "synckey": 42})
        raise AssertionError(url)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        assert url.endswith("/login")
        return FakeResponse(self.login_payloads.pop(0))


def test_qr_request_poll_exchange_and_refresh():
    session = FakeSession()
    session.poll_payloads = [
        {"wx_errcode": 408, "wx_code": ""},
        {"wx_errcode": 404, "wx_code": ""},
        {"wx_errcode": 405, "wx_code": "wx-code"},
    ]
    session.login_payloads = [
        {
            "vid": 123456,
            "accessToken": "access-a",
            "refreshToken": "refresh-a",
            "skey": "skey-a",
            "wxAccessToken": "wx-a",
            "user": {"name": "测试账号"},
        },
        {
            "vid": 123456,
            "accessToken": "access-b",
            "refreshToken": "refresh-b",
            "user": {"name": "测试账号"},
        },
    ]
    client = WeReadAuthClient(session=session)

    qr = client.request_qr()
    assert qr.uuid == "uuid-test"
    assert qr.confirm_url.endswith("uuid=uuid-test")
    assert qr.qr_data_uri.startswith("data:image/png;base64,")

    assert client.poll_qr_once(qr.uuid).status == "waiting"
    assert client.poll_qr_once(qr.uuid, last=408).status == "scanned"
    confirmed = client.poll_qr_once(qr.uuid, last=404)
    assert confirmed.status == "confirmed"
    assert confirmed.wx_code == "wx-code"

    creds = client.exchange_qr(confirmed.wx_code, device_id="eink3346912250000000000000000001")
    assert creds.vid == "123456"
    assert creds.refreshToken == "refresh-a"
    assert creds.name == "测试账号"

    init = client.initialize_feature(creds)
    assert init.guest_token == "guest-x"
    assert init.sync_key == 42

    refreshed = client.refresh(creds)
    assert refreshed.accessToken == "access-b"
    assert refreshed.refreshToken == "refresh-b"

    # Both QR exchange and refresh use POST /login and JSON content type.
    assert len(session.posts) == 2
    for _, kwargs in session.posts:
        assert kwargs["headers"]["Content-Type"].startswith("application/json")


def test_signature_is_deterministic():
    assert signature(123, "dev", 7) == signature(123, "dev", 7)
    assert signature(123, "dev", 7) != signature(124, "dev", 7)


class InstantAuth:
    def request_qr(self):
        return QRRequest("u", "https://example.test/confirm", "data:image/png;base64,AA==")

    def poll_qr_once(self, uuid, *, last=None, timeout=None):
        return PollResult("confirmed", 405, "wx-code")

    def exchange_qr(self, code):
        return WeReadCredentials(
            vid="100",
            accessToken="secret-access-token",
            refreshToken="secret-refresh-token",
            deviceId="device-1",
            name="Alice",
        )

    def initialize_feature(self, credentials):
        class Result:
            guest_token = "guest"
            sync_key = 9
            warnings = []
        return Result()


def test_login_manager_never_exposes_tokens():
    saved = []
    manager = LoginManager(InstantAuth(), saved.append, deadline_seconds=60)
    manager.start()
    deadline = time.time() + 2
    while time.time() < deadline and manager.status()["status"] not in {"success", "error"}:
        time.sleep(0.01)
    state = manager.status()
    assert state["status"] == "success"
    assert state["account"] == "Alice"
    serialized = repr(state)
    assert "secret-access-token" not in serialized
    assert "secret-refresh-token" not in serialized
    assert saved[0].guestToken == "guest"
    assert saved[0].syncKey == 9
