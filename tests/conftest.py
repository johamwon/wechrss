import sys
import os
from pathlib import Path

import pytest

# Unit tests must never inherit a developer's real account from a local .env.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_runtime_environment(monkeypatch):
    for key in ("WEREAD_ACCESS_TOKEN", "WEREAD_VID", "WEREAD_REFRESH_TOKEN", "WEREAD_DEVICE_ID",
                "ADMIN_PASSWORD", "PUBLIC_BASE_URL"):
        monkeypatch.delenv(key, raising=False)
