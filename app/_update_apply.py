"""Detached updater: shows its own progress window, applies zip, relaunches."""

from __future__ import annotations

import io
import os
import sys
import threading
import time
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import ttk


def apply_zip(url: str, root: Path, ui) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "SAY-MY-NAME"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        chunks = []
        got = 0
        while True:
            chunk = resp.read(1024 * 128)
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
            if total:
                ui.progress(got / total)
            else:
                ui.indeterminate(got)

    ui.status("Installing…")
    blob = b"".join(chunks)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n and not n.endswith("/")]
        tops = {n.split("/")[0] for n in names}
        prefix = ""
        if len(tops) == 1:
            only = tops.pop()
            if any(n.startswith(only + "/") for n in names):
                prefix = only + "/"
        total = max(1, len(names))
        for i, info in enumerate(zf.infolist(), 1):
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
            ui.progress(i / total)

    ui.status("Done. Starting…")


class UpdaterUI:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)  # no titlebar / close button
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#151b20")
        self.root.geometry("420x150+200+200")

        tk.Label(
            self.root,
            text="Updating…",
            bg="#151b20",
            fg="#e8f0f4",
            font=("Segoe UI", 14, "bold"),
        ).pack(anchor="w", padx=22, pady=(22, 4))

        self.lbl = tk.Label(
            self.root,
            text="Preparing…",
            bg="#151b20",
            fg="#7e919e",
            font=("Segoe UI", 10),
        )
        self.lbl.pack(anchor="w", padx=22)

        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Up.Horizontal.TProgressbar", troughcolor="#1c242b", background="#ff2e2e")

        self.bar = ttk.Progressbar(
            self.root,
            mode="determinate",
            maximum=100,
            length=360,
            style="Up.Horizontal.TProgressbar",
        )
        self.bar.pack(padx=22, pady=(18, 22))

        self.root.protocol("WM_DELETE_WINDOW", lambda: None)

    def status(self, text: str) -> None:
        self.lbl.configure(text=text)
        self.root.update_idletasks()

    def progress(self, frac: float) -> None:
        self.bar.configure(value=max(0, min(100, frac * 100)))
        self.root.update_idletasks()

    def indeterminate(self, got: int) -> None:
        self.lbl.configure(text=f"Downloading… {got // 1024} KB")
        self.bar.configure(value=(got // 1024) % 100)
        self.root.update_idletasks()

    def loop(self) -> None:
        self.root.mainloop()

    def close(self) -> None:
        try:
            self.root.destroy()
        except Exception:
            pass


def main() -> None:
    if len(sys.argv) < 3:
        return
    zip_url = sys.argv[1]
    root = Path(sys.argv[2])

    ui = UpdaterUI()
    ui.status("Waiting for the app to close…")
    time.sleep(1.2)
    for _ in range(15):
        time.sleep(1.0)

    def worker() -> None:
        try:
            apply_zip(zip_url, root, ui)
        except Exception as e:
            ui.status(f"Update failed: {e}")
            time.sleep(4)
            ui.close()
            return
        ui.status("Done. Starting…")
        time.sleep(0.6)
        # recreate shortcuts (folder + desktop) in case user deleted them
        try:
            import subprocess

            ps1 = root / "make_lnk.ps1"
            if ps1.exists():
                subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(ps1),
                    ],
                    timeout=30,
                    close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except Exception:
            pass
        ui.close()
        launcher = root / "start.vbs"
        if launcher.exists():
            os.startfile(str(launcher))  # noqa: S606
        else:
            os.startfile(str(root / "app" / "asr_ui.py"))  # noqa: S606

    threading.Thread(target=worker, daemon=True).start()
    ui.loop()


if __name__ == "__main__":
    main()
