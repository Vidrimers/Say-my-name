"""App version and update check.

Version scheme: YYYY.MM.N
  YYYY — year
  MM   — month
  N    — update number within the month (1, 2, 3, …)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

__version__ = "2026.11.1"

# Plain-text file that contains only the latest version, e.g. "2026.11.2"
# Change this URL when the project is published (GitHub raw / release asset).
CHECK_URL = (
    "https://raw.githubusercontent.com/Vidrimers/Say-my-name/main/latest.txt"
)

CHECK_INTERVAL_SEC = 24 * 3600


def parse_version(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def fetch_latest(timeout: float = 8.0) -> str:
    import urllib.request

    req = urllib.request.Request(CHECK_URL, headers={"User-Agent": "SAY-MY-NAME"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace").strip()
    m = re.search(r"\d{4}\.\d{1,2}\.\d+", text)
    return m.group(0) if m else text.split()[0]


def should_check(state_file: Path) -> bool:
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
        last = float(data.get("last_update_check", 0))
    except Exception:
        return True
    return (time.time() - last) >= CHECK_INTERVAL_SEC


def mark_checked(state_file: Path) -> None:
    data = {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except Exception:
        pass
    data["last_update_check"] = time.time()
    state_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
