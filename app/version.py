"""App version and update via GitHub Releases.

Version scheme: YYYY.MM.N (YYYY year, MM month, N update in month).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

__version__ = "2026.11.28"

REPO = "Vidrimers/Say-my-name"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_INTERVAL_SEC = 24 * 3600

# Files/folders that ship in the update zip (see tools/build_release.py)
# img/, README* stay in the repo only.


def parse_version(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v or "")
    return tuple(int(p) for p in parts) or (0,)


def is_newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def fetch_latest(timeout: float = 10.0) -> tuple[str, str | None]:
    """Return (version, zip_url) from GitHub Releases API."""
    import urllib.request

    req = urllib.request.Request(
        RELEASES_API,
        headers={"User-Agent": "SAY-MY-NAME", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    version = (data.get("tag_name") or "").lstrip("v")
    zip_url = None
    for asset in data.get("assets") or []:
        name = asset.get("name") or ""
        if name.endswith(".zip") and "update" in name.lower():
            zip_url = asset.get("browser_download_url")
            break
    if zip_url is None:
        for asset in data.get("assets") or []:
            name = asset.get("name") or ""
            if name.endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break
    return version, zip_url


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


def download_and_extract(url: str, dest_root: Path, timeout: float = 120.0) -> None:
    """Download update zip and extract over dest_root (project root)."""
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "SAY-MY-NAME"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read()

    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        # If zip has a single top folder, strip it
        names = zf.namelist()
        tops = {n.split("/")[0] for n in names if n}
        prefix = ""
        if len(tops) == 1:
            only = tops.pop()
            if any(n.startswith(only + "/") for n in names):
                prefix = only + "/"

        for info in zf.infolist():
            name = info.filename
            if not name or name.endswith("/"):
                continue
            rel = name[len(prefix):] if prefix and name.startswith(prefix) else name
            if not rel or rel.startswith("..") or rel.startswith("/"):
                continue
            out = dest_root / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(out, "wb") as dst:
                dst.write(src.read())


def run_updater(zip_url: str, root: Path) -> None:
    """Spawn a detached process that waits for us to exit, applies zip, relaunches."""
    python = sys.executable
    script = root / "app" / "_update_apply.py"
    subprocess.Popen(
        [python, str(script), zip_url, str(root)],
        cwd=str(root),
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
