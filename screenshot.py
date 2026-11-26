"""Capture app screenshots for README (main EN/RU, settings EN/RU, compact)."""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

import customtkinter as ctk  # noqa: E402
from PIL import Image  # noqa: E402

import i18n  # noqa: E402
import asr_ui  # noqa: E402

OUT = Path(__file__).parent / "img"
OUT.mkdir(exist_ok=True)


def capture_widget(widget, path: Path, pad: int = 2) -> None:
    widget.update_idletasks()
    time.sleep(0.15)
    x = widget.winfo_rootx() - pad
    y = widget.winfo_rooty() - pad
    w = widget.winfo_width() + pad * 2
    h = widget.winfo_height() + pad * 2
    try:
        import mss

        with mss.mss() as sct:
            img = sct.grab({"left": x, "top": y, "width": w, "height": h})
            Image.frombytes("RGB", img.size, img.bgra, "raw", "BGRX").save(path)
    except Exception:
        # fallback PrintWindow-less: full screen crop
        from PIL import ImageGrab

        ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(path)
    print("saved", path.name, w, "x", h)


def run() -> None:
    i18n.set_lang("en")
    asr_ui.i18n.set_lang("en")
    app = asr_ui.App()
    # keep window on top during capture
    app.attributes("-topmost", True)
    app.update()

    def shot_main() -> None:
        app.update_idletasks()
        capture_widget(app, OUT / "01-main-en.png")

    def open_settings() -> None:
        app.open_settings()
        app.settings.update_idletasks()
        time.sleep(0.2)

    def shot_settings() -> None:
        capture_widget(app.settings, OUT / "02-settings-en.png")

    def close_settings() -> None:
        app.settings._close()
        app.update()
        time.sleep(0.1)

    def switch_ru() -> None:
        i18n.set_lang("ru")
        asr_ui.i18n.set_lang("ru")
        app.refresh_i18n()
        app.update_idletasks()

    def shot_main_ru() -> None:
        capture_widget(app, OUT / "03-main-ru.png")

    def open_settings_ru() -> None:
        app.open_settings()
        app.settings.update_idletasks()
        time.sleep(0.2)

    def shot_settings_ru() -> None:
        capture_widget(app.settings, OUT / "04-settings-ru.png")

    def shot_compact() -> None:
        # build compact widget
        app.settings._close()
        app.update()
        if app.compact is None or not app.compact.winfo_exists():
            app.compact = asr_ui.CompactWindow(app)
        app.compact.deiconify()
        app.compact.lift()
        app.compact.update_idletasks()
        time.sleep(0.15)
        capture_widget(app.compact.win, OUT / "05-compact.png")

    seq = [
        shot_main,
        open_settings,
        shot_settings,
        close_settings,
        switch_ru,
        shot_main_ru,
        open_settings_ru,
        shot_settings_ru,
        shot_compact,
    ]

    def step(i: int = 0) -> None:
        if i >= len(seq):
            app.on_close()
            return
        seq[i]()
        app.after(250, lambda: step(i + 1))

    app.after(400, step)
    app.mainloop()
    print("done ->", OUT)


if __name__ == "__main__":
    run()
