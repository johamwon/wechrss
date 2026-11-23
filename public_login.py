"""Visitor QR sessions are isolated from the administrator and one another."""
from dataclasses import dataclass
import threading
import time

from weread_auth import LoginManager


@dataclass
class SignIn:
    manager: LoginManager
    started: float


class PublicLogin:
    def __init__(self, auth_factory, persist, deadline_seconds: int):
        self.auth_factory = auth_factory
        self.persist = persist
        self.deadline_seconds = deadline_seconds
        self._sessions: dict[str, SignIn] = {}
        self._lock = threading.Lock()
        self._last_start: float | None = None

    def status(self, owner: str) -> dict:
        with self._lock:
            sign_in = self._sessions.get(owner)
            state = sign_in.manager.status() if sign_in else {}
        # Do not return provider messages, account metadata or the upstream confirm URL.
        return {"status": state.get("status", "idle"), "qr": state.get("qr", "")}

    def start(self, owner: str) -> bool:
        with self._lock:
            now = time.monotonic()
            self._sessions = {
                key: value for key, value in self._sessions.items()
                if now - value.started < self.deadline_seconds + 120 or value.manager.running()
            }
            previous = self._sessions.get(owner)
            if previous and previous.manager.running():
                return True
            if self._last_start is not None and now - self._last_start < 2:
                return False
            if sum(entry.manager.running() for entry in self._sessions.values()) >= 8:
                return False
            manager = LoginManager(
                self.auth_factory(), lambda record: self.persist(record, owner=owner),
                deadline_seconds=self.deadline_seconds, initialize_session=False,
            )
            self._sessions[owner] = SignIn(manager, now)
            self._last_start = now
            manager.start()
            return True

    def cancel(self, owner: str) -> None:
        with self._lock:
            sign_in = self._sessions.get(owner)
            if sign_in:
                sign_in.manager.cancel()
