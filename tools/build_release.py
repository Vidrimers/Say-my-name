"""Build update zip: app/ + launchers only (no img/, README, tests)."""

from __future__ import annotations

import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# What users get in the update package
INCLUDE = [
    "app",
    "start.vbs",
    "start.bat",
    "run_ui.bat",
    "run_ui_debug.bat",
    "run.bat",
    "make_lnk.ps1",
    "SAY MY NAME.lnk",
]

EXCLUDE_DIRS = {
    "__pycache__",
    ".git",
    "img",
    "tasks",
    "venv",
    "assets",
}
EXCLUDE_EXT = {".pyc", ".pyo", ".log", ".wav"}
EXCLUDE_NAMES = {
    "config.json",
    "update_state.json",
    "hold_debug.log",
    "hold_diag.log",
}


def build(out_dir: Path | None = None) -> Path:
    out_dir = out_dir or ROOT / "dist"
    out_dir.mkdir(exist_ok=True)

    import re

    ver = "0.0.0"
    vp = ROOT / "app" / "version.py"
    if vp.exists():
        m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', vp.read_text(encoding="utf-8"))
        if m:
            ver = m.group(1)

    out = out_dir / f"say-my-name-update-{ver}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for item in INCLUDE:
            src = ROOT / item
            if not src.exists():
                continue
            if src.is_file():
                zf.write(src, item)
                continue
            for path in src.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(ROOT)
                if any(part in EXCLUDE_DIRS for part in rel.parts):
                    continue
                if path.suffix.lower() in EXCLUDE_EXT:
                    continue
                if path.name in EXCLUDE_NAMES:
                    continue
                zf.write(path, rel.as_posix())
    print("built", out, out.stat().st_size, "bytes")
    return out


if __name__ == "__main__":
    build()
