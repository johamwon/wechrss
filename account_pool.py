"""Server-side WeRead sessions. Application database backups exclude this file."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import tempfile
import threading
import time

from wechat_mp_fetcher import FetcherError
from weread_auth import WeReadAuthError, WeReadCredentials


@dataclass
class PoolAccount:
    record: WeReadCredentials
    state: str = "pending"
    checked_at: int = 0
    public_id: str = ""
    owner: str = ""


class AccountPool:
    def __init__(self, path: Path, legacy):
        self.path = path
        self._lock = threading.RLock()
        if not path.exists():
            try:
                first = legacy.get_record()
            except FetcherError:
                first = None
            self._write([PoolAccount(first, public_id=secrets.token_hex(8))] if first else [])
        else:
            accounts = self._read()
            if any(not item.public_id for item in accounts):
                for item in accounts:
                    if not item.public_id:
                        item.public_id = secrets.token_hex(8)
                self._write(accounts)

    def _read(self) -> list[PoolAccount]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [
            PoolAccount(WeReadCredentials.from_mapping(item["session"]), item["state"], item["checked_at"],
                        item.get("public_id", ""), item.get("owner", ""))
            for item in data
        ]

    def _write(self, accounts: list[PoolAccount]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name + ".", suffix=".tmp", dir=self.path.parent)
        temp = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump([
                    {"session": item.record.as_json(), "state": item.state, "checked_at": item.checked_at,
                     "public_id": item.public_id, "owner": item.owner}
                    for item in accounts
                ], stream, ensure_ascii=False)
            temp.replace(self.path)
        finally:
            temp.unlink(missing_ok=True)

    def save(self, record: WeReadCredentials, *, owner: str = "") -> None:
        try:
            record.validate_basic()
        except WeReadAuthError as exc:
            raise FetcherError(str(exc)) from exc
        with self._lock:
            accounts = self._read()
            previous = next((item for item in accounts if item.record.vid == record.vid), None)
            record.updatedAt = int(time.time())
            replacement = PoolAccount(
                record, state="valid" if owner else "pending",
                checked_at=record.updatedAt if owner else 0,
                public_id=previous.public_id if previous else secrets.token_hex(8),
                owner=owner or (previous.owner if previous else ""),
            )
            if previous:
                accounts[accounts.index(previous)] = replacement
            else:
                accounts.append(replacement)
            self._write(accounts)

    def mark(self, vid: str, state: str) -> None:
        with self._lock:
            accounts = self._read()
            for item in accounts:
                if item.record.vid == vid:
                    now = int(time.time())
                    # Avoid a disk write on every request when the status is unchanged.
                    if item.state != state or now - item.checked_at >= 60:
                        item.state, item.checked_at = state, now
                        self._write(accounts)
                    return

    def remove(self, vid: str) -> None:
        with self._lock:
            self._write([item for item in self._read() if item.record.vid != vid])

    def revoke(self, public_id: str, owner: str) -> bool:
        if not owner:
            return False
        with self._lock:
            accounts = self._read()
            remaining = [item for item in accounts if (item.public_id, item.owner) != (public_id, owner)]
            if len(remaining) == len(accounts):
                return False
            self._write(remaining)
            return True

    def public_status(self, owner: str, page: int) -> dict:
        with self._lock:
            accounts = self._read()
        page_size = 50
        pages = max(1, (len(accounts) + page_size - 1) // page_size)
        page = min(max(1, page), pages)
        accounts.sort(key=lambda item: (bool(owner and item.owner == owner), item.public_id), reverse=True)
        counts = dict.fromkeys(("valid", "pending", "expired"), 0)
        for item in accounts:
            counts[item.state] += 1
        start = (page - 1) * page_size
        return {
            "accounts": [{"id": item.public_id, "state": item.state, "checked_at": item.checked_at,
                          "owned": bool(owner and item.owner == owner)}
                         for item in accounts[start:start + page_size]],
            "counts": counts, "page": page, "pages": pages,
        }

    def contributor(self, owner: str) -> str:
        if not owner:
            return ""
        with self._lock:
            return next((item.public_id for item in self._read()
                         if item.owner == owner and item.state != "expired"), "")

    def candidates(self, *, after: str = "") -> list[WeReadCredentials]:
        with self._lock:
            accounts = [item for item in self._read() if item.state in {"valid", "pending"}]
        accounts.sort(key=lambda item: (item.state == "valid", item.checked_at), reverse=True)
        if after:
            valid = [item for item in accounts if item.state == "valid"]
            valid.sort(key=lambda item: (item.record.vid <= after, item.record.vid))
            accounts = valid + [item for item in accounts if item.state != "valid"]
        return [item.record for item in accounts]

    def status(self) -> dict:
        with self._lock:
            accounts = self._read()
        return {
            "configured": bool(accounts),
            "name": f"账号池 · {len(accounts)} 个账号" if accounts else "",
            "refreshable": any(
                item.state in {"valid", "pending"} and item.record.can_refresh
                and item.record.profile.startswith("eink") for item in accounts
            ),
            "accounts": [
                {"vid": item.record.vid, "name": item.record.name,
                 "state": item.state, "checked_at": item.checked_at}
                for item in accounts
            ],
        }
