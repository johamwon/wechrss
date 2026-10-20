"""Container liveness probe that respects the configured management password."""
from __future__ import annotations

import base64
import json
import os
import urllib.request


def build_request() -> urllib.request.Request:
    port = int(os.getenv("PORT", "8080"))
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/health")
    password = os.getenv("ADMIN_PASSWORD", "")
    if password:
        token = base64.b64encode(("admin:" + password).encode()).decode("ascii")
        request.add_header("Authorization", "Basic " + token)
    return request


def main() -> int:
    try:
        with urllib.request.urlopen(build_request(), timeout=5) as response:
            payload = json.load(response)
            return 0 if response.status == 200 and payload.get("alive", payload.get("ok")) is True else 1
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
