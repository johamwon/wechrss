"""Short-lived administrator sessions for browser deployments."""
from collections import deque
import hashlib
import hmac
import secrets
import threading
import time


class AdminLogin:
    max_age = 8 * 60 * 60

    def __init__(self):
        self._key = secrets.token_bytes(32)
        self._sessions: dict[str, tuple[float, bytes]] = {}
        self._failures: deque[float] = deque()
        self._lock = threading.Lock()

    def _password_tag(self, password: str) -> bytes:
        return hmac.digest(self._key, password.encode(), "sha256")

    @staticmethod
    def _session_id(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def challenge(self, nonce: str) -> str:
        return hmac.new(self._key, nonce.encode(), "sha256").hexdigest()

    def sign_in(self, username: str, password: str, expected: str) -> tuple[str, str]:
        with self._lock:
            now = time.monotonic()
            while self._failures and now - self._failures[0] >= 60:
                self._failures.popleft()
            if len(self._failures) >= 12:
                return "", "limited"
            expected_tag = self._password_tag(expected)
            if not hmac.compare_digest(self._password_tag(password), expected_tag) or username != "admin":
                self._failures.append(now)
                return "", "invalid"
            self._failures.clear()
            self._sessions = {key: value for key, value in self._sessions.items() if value[0] > now}
            if len(self._sessions) >= 64:
                del self._sessions[next(iter(self._sessions))]
            token = secrets.token_urlsafe(32)
            self._sessions[self._session_id(token)] = (now + self.max_age, expected_tag)
            return token, ""

    def authenticated(self, token: str, password: str) -> bool:
        if not token or not password:
            return False
        with self._lock:
            session = self._sessions.get(self._session_id(token))
            return bool(session and session[0] > time.monotonic()
                        and hmac.compare_digest(session[1], self._password_tag(password)))

    def sign_out(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(self._session_id(token), None)
