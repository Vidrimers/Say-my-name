"""Runs detached after user clicks Update: waits for main app to exit, extracts zip, relaunches."""

from __future__ import annotations

import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path


def main() -> None:
    if len(sys.argv) < 3:
        return
    zip_url = sys.argv[1]
    root = Path(sys.argv[2])
    marker = root / "app" / ".updating"
    marker.write_text("1", encoding="utf-8")

    # Wait until the GUI process is gone (max ~20s)
    time.sleep(1.5)
    for _ in range(20):
        # crude: if pythonw/asr_ui still holds, keep waiting
        time.sleep(1.0)

    try:
        req = urllib.request.Request(zip_url, headers={"User-Agent": "SAY-MY-NAME"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            blob = resp.read()

        import io

        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
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
                if not rel or ".." in rel.split("/") or rel.startswith("/"):
                    continue
                out = root / rel
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(out, "wb") as dst:
                    dst.write(src.read())
    except Exception:
        pass
    finally:
        try:
            marker.unlink(missing_ok=True)
        except Exception:
            pass

    # Relaunch
    launcher = root / "start.vbs"
    if launcher.exists():
        os.startfile(str(launcher))  # noqa: S606
    else:
        os.startfile(str(root / "app" / "asr_ui.py"))  # noqa: S606


if __name__ == "__main__":
    main()
