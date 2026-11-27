#!/usr/bin/env python3
"""Modern GUI for local Whisper voice dictation (no terminal required)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import traceback
import wave
from pathlib import Path

import keyboard
import numpy as np
import pyperclip
import sounddevice as sd

APP_DIR = Path(__file__).parent
CONFIG_PATH = APP_DIR / "config.json"
ICON_PATH = APP_DIR / "app.ico"


def _set_process_appid(app_id: str = "SAYMYNAME") -> None:
    """Tell Windows we are not pythonw.exe for taskbar icon/grouping."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def _iter_process_hwnds():
    """All top-level HWNDs of this process (Tk winfo_id is not always enough)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    pid = ctypes.windll.kernel32.GetCurrentProcessId()
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(int(hwnd))
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


def set_window_icon(widget, icon_path: Path) -> None:
    """Apply .ico to window class — this is what the Windows taskbar shows."""
    if not icon_path.exists():
        return

    try:
        widget.iconbitmap(str(icon_path))
    except Exception:
        pass
    try:
        widget.wm_iconbitmap(default=str(icon_path))
    except Exception:
        pass

    if sys.platform != "win32":
        return

    try:
        import ctypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        WM_SETICON = 0x0080
        ICON_SMALL, ICON_BIG = 0, 1
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x0010
        GCLP_HICON, GCLP_HICONSM = -14, -34

        set_cls = getattr(kernel32, "SetClassLongPtrW", None) or user32.SetClassLongW

        # 125% DPI: small=16, big=32/48 — pick real sizes from the .ico
        hicon_big = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 32, 32, LR_LOADFROMFILE
        )
        hicon_sm = user32.LoadImageW(
            None, str(icon_path), IMAGE_ICON, 16, 16, LR_LOADFROMFILE
        )
        if not hicon_big:
            return

        hwnds = []
        try:
            hwnds.append(int(widget.winfo_id()))
        except Exception:
            pass
        try:
            hwnds.extend(_iter_process_hwnds())
        except Exception:
            pass

        seen = set()
        for hwnd in hwnds:
            if not hwnd or hwnd in seen:
                continue
            seen.add(hwnd)
            try:
                user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, hicon_big)
                if hicon_sm:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, hicon_sm)
                set_cls(hwnd, GCLP_HICON, hicon_big)
                set_cls(hwnd, GCLP_HICONSM, hicon_sm or hicon_big)
            except Exception:
                pass
    except Exception:
        pass


def schedule_window_icon(widget, icon_path: Path) -> None:
    """Set AppID + icons now and after the window is really mapped."""
    _set_process_appid("SAYMYNAME")

    def _apply() -> None:
        try:
            widget.update_idletasks()
        except Exception:
            pass
        set_window_icon(widget, icon_path)

    set_window_icon(widget, icon_path)
    try:
        widget.after_idle(_apply)
        for delay in (200, 600, 1500):
            widget.after(delay, _apply)
    except Exception:
        pass

import i18n
from i18n import t
import model_manager as mm
import version as app_version
import gpu_info

DEFAULT_CONFIG = {
    "hotkey": "ctrl+alt+space",
    "hotkey_mode": "toggle",  # toggle | hold (hold = global hotkey only)
    "model": "small",
    "language": "auto",
    "ui_language": "en",
    "sample_rate": 16000,
    "max_record_seconds": 60,
    "device": "auto",
    "compute_type": "auto",
    "monitor_open": False,
    "device_choice": "auto",
    "topmost": False,
    "compact_on_start": False,
}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {**DEFAULT_CONFIG, **cfg}


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


CFG = load_config()

def _default_device_choice() -> str:
    if not CFG.get("device_choice") or CFG.get("device_choice") == "auto":
        if gpu_info.has_nvidia() and gpu_info.nvidia_series_ok(gpu_info.gpu_name()):
            return "gpu"
        return "cpu"
    return CFG.get("device_choice")

CFG["device_choice"] = _default_device_choice()
i18n.set_lang(CFG.get("ui_language", "en"))

_state = {
    "recording": False,
    "frames": [],
    "thread": None,
    "busy": False,
    "model": None,
    "ui": None,
    "model_name_loaded": None,
    "stop_requested": False,
}


def _emit(kind: str, key: str, **kwargs) -> None:
    """Emit localized log line to UI."""
    ui = _state.get("ui")
    msg = t(key, **kwargs)
    if ui is not None:
        ui.after(0, ui.on_log, kind, msg)


def _load_model():
    if _state["model"] is not None and _state.get("model_name_loaded") == CFG.get("model"):
        return _state["model"]
    from faster_whisper import WhisperModel

    model_size = CFG.get("model", "small")
    choice = CFG.get("device_choice", "cpu")
    if choice == "gpu" and gpu_info.has_nvidia():
        device = "cuda"
    else:
        device = "cpu"
    compute = CFG.get("compute_type", "auto")
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    _emit("info", "log_model_loading", model=model_size, device=device, compute=compute)
    t0 = time.monotonic()
    _state["model"] = WhisperModel(model_size, device=device, compute_type=compute)
    _state["model_name_loaded"] = model_size
    _emit("ok", "log_model_loaded", dt=time.monotonic() - t0)
    return _state["model"]


def _record_worker() -> None:
    sample_rate = int(CFG.get("sample_rate", 16000))
    max_seconds = float(CFG.get("max_record_seconds", 60))
    frames = _state["frames"]

    def callback(indata, _frames, _time_info, status) -> None:
        if status:
            _emit("warn", "log_mic_err", err=str(status))
        if _state["recording"]:
            frames.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16", callback=callback
        ):
            start = time.monotonic()
            while _state["recording"]:
                if time.monotonic() - start > max_seconds:
                    _emit("warn", "log_max_dur")
                    _state["recording"] = False
                    break
                time.sleep(0.05)
    except Exception as e:
        _emit("error", "log_mic_err", err=str(e))
        _state["recording"] = False


def _start_recording() -> None:
    if _state["busy"]:
        _emit("warn", "log_busy")
        return
    if _state["recording"]:
        return
    _state["recording"] = True
    _state["frames"] = []
    t = threading.Thread(target=_record_worker, daemon=True)
    _state["thread"] = t
    t.start()
    _emit("rec", "log_rec_start")


def _wav_bytes(frames: list) -> bytes:
    if not frames:
        raise RuntimeError("No audio")
    data = np.concatenate(frames, axis=0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(CFG.get("sample_rate", 16000)))
        wf.writeframes(data.tobytes())
    return buf.getvalue()


def _transcribe(audio: bytes) -> str:
    model = _load_model()
    bio = io.BytesIO(audio)
    language = CFG.get("language") or None
    if language == "auto":
        language = None
    segments, info = model.transcribe(bio, language=language, beam_size=5, vad_filter=True)
    parts = [seg.text.strip() for seg in segments if seg.text.strip()]
    _emit("info", "log_lang", lang=info.language, pct=f"{info.language_probability:.0%}")
    return " ".join(parts).strip()


def _paste_text(text: str) -> None:
    pyperclip.copy(text)
    time.sleep(0.05)
    try:
        keyboard.send("ctrl+v")
    except Exception:
        _emit("warn", "log_paste_fail")


def toggle_record() -> None:
    if _state["busy"]:
        _emit("warn", "log_busy")
        return
    if _state["recording"]:
        _stop_and_process()
    else:
        _start_recording()


def _stop_and_process() -> None:
    if not _state["recording"]:
        return
    _state["recording"] = False
    t = _state["thread"]
    if t:
        t.join(timeout=3)
    n = len(_state["frames"])
    _emit("info", "log_stopped", n=n)
    _state["busy"] = True
    ui = _state.get("ui")
    if ui is not None:
        ui.after(0, ui.set_status, "busy")
    try:
        audio = _wav_bytes(_state["frames"])
        t0 = time.monotonic()
        text = _transcribe(audio)
        dt = time.monotonic() - t0
        if not text:
            _emit("warn", "log_empty")
            return
        _emit("text", text)
        _paste_text(text)
        _emit("ok", "log_pasted", dt=dt)
    except Exception as e:
        _emit("error", "log_err", err=str(e))
        ui = _state.get("ui")
        if ui is not None:
            tb = traceback.format_exc(limit=3)
            ui.after(0, ui.on_log, "error", tb)
    finally:
        _state["busy"] = False
        if ui is not None:
            ui.after(0, ui.set_status, "rec" if _state["recording"] else "idle")


def on_hotkey() -> None:
    toggle_record()


def register_hotkey(hotkey: str, mode: str | None = None) -> None:
    """Register global hotkey. mode: 'toggle' (default) or 'hold' (push-to-talk)."""
    mode = mode or CFG.get("hotkey_mode", "toggle")
    try:
        keyboard.unhook_all_hotkeys()
    except Exception:
        pass
    try:
        keyboard.unhook_all()
    except Exception:
        pass

    if mode == "hold":
        _register_hold(hotkey)
    else:
        keyboard.add_hotkey(hotkey, on_hotkey, suppress=False)


def _norm_key(name: str) -> str:
    """Normalize keyboard event names to hotkey part names."""
    n = (name or "").lower()
    if n in ("ctrl", "ctrl l", "ctrl r", "control", "control l", "control r"):
        return "ctrl"
    if n in ("alt", "alt l", "alt r", "option", "option l", "option r"):
        return "alt"
    if n in ("shift", "shift l", "shift r"):
        return "shift"
    if n in ("windows", "windows l", "windows r", "win", "cmd", "super"):
        return "windows"
    return n


def _register_hold(hotkey: str) -> None:
    """Push-to-talk via a single keyboard.hook (down/up in one place).

    Two add_hotkey() on one combo fight over shared state.
    Debug: app writes key events to hold_debug.log next to asr_ui.py.
    """
    parts = [p.strip().lower() for p in hotkey.split("+") if p.strip()]
    parts_set = set(parts)
    trigger = parts[-1] if parts else ""
    mods = set(parts[:-1])
    held: set[str] = set()
    debug_path = APP_DIR / "hold_debug.log"

    def dbg(msg: str) -> None:
        try:
            with debug_path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
        except Exception:
            pass

    dbg(f"register hold hotkey={hotkey!r} parts={parts}")

    def on_event(event) -> None:
        key = _norm_key(getattr(event, "name", "") or "")
        down = getattr(event, "event_type", "") == "down"
        if down:
            held.add(key)
        else:
            held.discard(key)
        dbg(f"{event.event_type} name={event.name!r} norm={key!r} held={sorted(held)}")

        if down:
            if key != trigger:
                return
            try:
                mods_ok = all(keyboard.is_pressed(m) for m in mods) if mods else True
            except Exception:
                mods_ok = mods.issubset(held)
            dbg(f"trigger press mods_ok={mods_ok} rec={_state['recording']}")
            if not mods_ok:
                return
            if _state["busy"]:
                _emit("warn", "log_busy")
                return
            if not _state["recording"]:
                dbg("START recording")
                _start_recording()
        else:
            # release of any combo key ends recording — once
            if key in parts_set and _state["recording"] and not _state.get("stop_requested"):
                dbg(f"STOP requested on release {key}")
                _state["stop_requested"] = True
                _emit("info", "log_hold_release")

    keyboard.hook(on_event)


def _poll_hold_stop() -> None:
    """Main-thread poller for hold-mode stop requests."""
    if _state.get("stop_requested"):
        _state["stop_requested"] = False
        if _state["recording"]:
            _emit("info", "log_hold_stop")
            _stop_and_process()
    ui = _state.get("ui")
    if ui is not None:
        try:
            ui.after(50, _poll_hold_stop)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

import customtkinter as ctk  # noqa: E402

# --- design tokens: studio console, not SaaS cards ---
T = {
    "bg": "#151B20",        # graphite
    "panel": "#1C242B",
    "line": "#2A3540",
    "text": "#E8F0F4",
    "muted": "#7E919E",
    "faint": "#4A5A66",
    "red": "#FF2E2E",
    "red_dim": "#3A1A1C",
    "amber": "#E0A34B",
}
DISPLAY = "Bahnschrift"
BODY = "Segoe UI"


class CompactWindow:
    """Camera-style REC widget (plain Tk Toplevel — no CTk chrome, no DWM shadow)."""

    W = 72
    H = 32

    def __init__(self, app: "App") -> None:
        import tkinter as tk

        self.app = app
        self.win = tk.Toplevel(app)
        self.win.overrideredirect(True)
        self.win.resizable(False, False)
        try:
            self.win.transient("")
        except Exception:
            pass

        # kill Windows drop-shadow (the dark band on right/bottom)
        self._disable_dwm_shadow()

        self.win.configure(bg="#1c1c28")

        try:
            sw = self.win.winfo_screenwidth()
            sh = self.win.winfo_screenheight()
            x = max(0, (sw - self.W) // 2)
            y = max(0, (sh - self.H) // 2)
        except Exception:
            x, y = 120, 120
        self.win.geometry(f"{self.W}x{self.H}+{x}+{y}")

        self._drag = {"x": 0, "y": 0, "moved": False, "press_x": 0, "press_y": 0}
        self._click_after = None
        self._blink_after = None
        self._blink_on = True

        self.canvas = tk.Canvas(
            self.win,
            width=self.W,
            height=self.H,
            highlightthickness=0,
            bd=0,
            bg="#1c1c28",
        )
        self.canvas.place(x=0, y=0, width=self.W, height=self.H)

        # Bind ONLY on canvas — binding toplevel too double-fires release
        # (one click → two releases → treated as double-click → show_main)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_context)
        self.canvas.bind("<Button-2>", self._on_context)

        self.win.attributes("-topmost", True)
        self.win.deiconify()
        try:
            self.win.lift()
        except Exception:
            pass
        self.win.update_idletasks()
        self._disable_dwm_shadow()
        self._paint()
        self._blink()

    def _disable_dwm_shadow(self) -> None:
        """Remove DWM drop shadow — that was the extra block right/below the widget."""
        try:
            import ctypes

            hwnd = int(self.win.winfo_id())
            DWMWA_NCRENDERING_POLICY = 2
            DWMNCRP_DISABLED = 1
            val = ctypes.c_int(DWMNCRP_DISABLED)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, DWMWA_NCRENDERING_POLICY, ctypes.byref(val), ctypes.sizeof(val)
            )
            # also drop thickframe / caption styles
            GWL_STYLE = -16
            GWL_EXSTYLE = -20
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~0x00040000 & ~0x00C00000)
        except Exception:
            pass

    def winfo_exists(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except Exception:
            return False

    def deiconify(self) -> None:
        self.win.deiconify()
        self._disable_dwm_shadow()

    def withdraw(self) -> None:
        self.win.withdraw()

    def lift(self) -> None:
        self.win.lift()

    def update_idletasks(self) -> None:
        self.win.update_idletasks()

    def attributes(self, *args, **kwargs):
        return self.win.attributes(*args, **kwargs)

    def geometry(self, *args, **kwargs):
        return self.win.geometry(*args, **kwargs)

    def transient(self, *args, **kwargs):
        return self.win.transient(*args, **kwargs)

    def winfo_x(self):
        return self.win.winfo_x()

    def winfo_y(self):
        return self.win.winfo_y()

    def after(self, *args, **kwargs):
        return self.win.after(*args, **kwargs)

    def after_cancel(self, *args, **kwargs):
        return self.win.after_cancel(*args, **kwargs)

    def _paint(self) -> None:
        c = self.canvas
        c.delete("all")
        w, h = self.W, self.H

        recording = _state["recording"]

        if recording:
            border = "#ff1e2d" if self._blink_on else "#7a1520"
        else:
            border = "#ff2433"

        c.create_rectangle(0, 0, w - 1, h - 1, fill="#1c1c28", outline="")
        # red border (2px)
        c.create_rectangle(1, 1, w - 2, h - 2, outline=border, width=2)

        if recording:
            dot = "#ff1e2d" if self._blink_on else "#5a1a22"
        else:
            dot = "#c23a48"

        r = 4
        cx, cy = 16, h // 2
        c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=dot, outline="")

        label = "#ff4d5e" if recording else "#c8ccd8"
        c.create_text(
            cx + r + 6,
            cy,
            text="REC",
            anchor="w",
            fill=label,
            font=("Segoe UI", 10, "bold"),
        )

    def _on_context(self, event) -> None:
        import tkinter as tk

        menu = tk.Menu(
            self.win, tearoff=0, bg=T["panel"], fg=T["text"],
            activebackground=T["line"], activeforeground=T["text"],
            borderwidth=1, relief="solid",
        )
        menu.add_command(label=t("ctx_open_main"), command=self.app.show_main)
        menu.add_command(label=t("ctx_settings"), command=self._menu_settings)
        menu.add_separator()
        menu.add_command(label=t("ctx_exit"), command=self.app.on_close)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except Exception:
                pass

    def _menu_settings(self) -> None:
        try:
            self.withdraw()
        except Exception:
            pass
        self.app.open_settings(hide_main=True)

    def _on_press(self, event) -> None:
        self._drag["press_x"] = event.x_root
        self._drag["press_y"] = event.y_root
        self._drag["x"] = event.x_root - self.win.winfo_x()
        self._drag["y"] = event.y_root - self.win.winfo_y()
        self._drag["moved"] = False

    def _on_motion(self, event) -> None:
        dx = abs(event.x_root - self._drag["press_x"])
        dy = abs(event.y_root - self._drag["press_y"])
        if dx > 4 or dy > 4:
            self._drag["moved"] = True
            if self._click_after is not None:
                self.win.after_cancel(self._click_after)
                self._click_after = None
        if self._drag["moved"]:
            nx = event.x_root - self._drag["x"]
            ny = event.y_root - self._drag["y"]
            self.win.geometry(f"+{nx}+{ny}")
            self._disable_dwm_shadow()

    def _on_release(self, event) -> None:
        if self._drag["moved"]:
            return
        if self._click_after is not None:
            self.win.after_cancel(self._click_after)
            self._click_after = None
            self.app.show_main()
            return
        self._click_after = self.win.after(280, self._single_click)

    def _single_click(self) -> None:
        self._click_after = None
        toggle_record()

    def _blink(self) -> None:
        self._blink_on = not self._blink_on
        self._paint()
        self._blink_after = self.win.after(
            400 if _state["recording"] else 250, self._blink
        )

    def destroy(self) -> None:
        if self._blink_after is not None:
            try:
                self.win.after_cancel(self._blink_after)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass


class ScrollFrame(ctk.CTkFrame):
    """Plain scroll area: scrollbar shown only when content does not fit."""

    def __init__(self, master) -> None:
        super().__init__(master, fg_color=T["bg"], corner_radius=0)
        import tkinter as tk

        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, bg=T["bg"])
        self.vsb = ctk.CTkScrollbar(self, orientation="vertical", command=self.canvas.yview)
        self.inner = ctk.CTkFrame(self.canvas, fg_color=T["bg"], corner_radius=0)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")

        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")),
        )
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)

        # mouse wheel
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def fit_scrollbar(self) -> None:
        try:
            self.update_idletasks()
            need = self.inner.winfo_reqheight()
            have = self.canvas.winfo_height()
            if need > have:
                self.vsb.pack(side="right", fill="y")
            else:
                self.vsb.pack_forget()
                self.canvas.yview_moveto(0)
            self.update_idletasks()
            # stretch inner width
            self.canvas.itemconfigure(self._win, width=max(1, self.canvas.winfo_width()))
        except Exception:
            pass


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        _state["ui"] = self

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.title(t("app_title"))
        # 125% DPI: 480×640 logical → 600×800 physical on screen
        self.geometry("480x720")
        self.minsize(480, 720)
        self.maxsize(480, 720)
        self.resizable(False, False)
        # hide maximize / disable zoom button on Windows
        try:
            self.attributes("-zoomed", False)
        except Exception:
            pass
        try:
            import ctypes

            hwnd = int(self.winfo_id())
            GWL_STYLE = -16
            WS_MAXIMIZEBOX = 0x00010000
            WS_THICKFRAME = 0x00040000
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(
                hwnd, GWL_STYLE, style & ~WS_MAXIMIZEBOX & ~WS_THICKFRAME
            )
        except Exception:
            pass
        self.configure(fg_color=T["bg"])
        try:
            if ICON_PATH.exists():
                schedule_window_icon(self, ICON_PATH)
        except Exception:
            pass

        self.settings = None
        self.compact: CompactWindow | None = None
        self._status_mode = "idle"
        self._pending_update = None

        # footer always on top of pack order with side=bottom -> stays visible
        self._build_footer()
        self._content = ScrollFrame(self)
        self._content.pack(fill="both", expand=True)
        self._build_header()
        self._build_status()
        self._build_controls()
        self._build_monitor()
        self._build_log()
        self.after(150, self._fit_scrollbar)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.attributes("-topmost", bool(CFG.get("topmost", False)))

        try:
            register_hotkey(CFG.get("hotkey", "ctrl+alt+space"), CFG.get("hotkey_mode", "toggle"))
        except Exception as e:
            self.on_log("error", t("log_hotkey_fail", err=str(e)))

        self.after(100, lambda: self.on_log("info", t("log_ready")))
        # poll hold-mode stop flags on the UI thread
        self.after(50, _poll_hold_stop)
        self.after(800, self._maybe_check_updates)
        if not mm.is_downloaded(CFG.get("model", "small")):
            threading.Thread(target=_load_model, daemon=True).start()
        else:
            # still preload in background
            threading.Thread(target=_load_model, daemon=True).start()

        if CFG.get("compact_on_start"):
            # Hide main immediately so it never flashes on screen
            self.withdraw()
            self.after(50, self.show_compact)

    # ---- build ----

    def _build_header(self) -> None:
        head = ctk.CTkFrame(self._content.inner, fg_color="transparent")
        head.pack(fill="x", padx=28, pady=(26, 4))

        # row 1: title left, version right
        top = ctk.CTkFrame(head, fg_color="transparent")
        top.pack(fill="x")
        self.lbl_title = ctk.CTkLabel(
            top,
            text=t("app_title"),
            font=ctk.CTkFont(family=DISPLAY, size=28, weight="bold"),
            anchor="w",
            text_color=T["text"],
        )
        self.lbl_title.pack(side="left")
        self.lbl_version = ctk.CTkLabel(
            top,
            text=app_version.__version__,
            font=ctk.CTkFont(family=BODY, size=11),
            anchor="e",
            text_color=T["faint"],
        )
        self.lbl_version.pack(side="right")
        self.lbl_version.bind("<Button-1>", self._on_version_click)

        # row 2: tagline under the title
        self.lbl_tag = ctk.CTkLabel(
            head,
            text=t("tagline"),
            font=ctk.CTkFont(family=BODY, size=12),
            anchor="w",
            text_color=T["muted"],
        )
        self.lbl_tag.pack(fill="x", pady=(2, 0))

    def _build_status(self) -> None:
        # status as a signal rail: 3px left bar + text (no card)
        self.status_card = ctk.CTkFrame(
            self._content.inner, fg_color=T["panel"], corner_radius=0,
            border_width=1, border_color=T["line"],
        )
        self.status_card.pack(fill="x", padx=28, pady=(18, 12))

        self.status_dot = ctk.CTkLabel(
            self.status_card, text="", width=8, fg_color=T["red"],
            corner_radius=0,
        )
        self.status_dot.pack(side="left", fill="y", padx=(0, 14), pady=0)

        col = ctk.CTkFrame(self.status_card, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, padx=16, pady=14)
        self.status_title = ctk.CTkLabel(
            col,
            text=t("status_ready"),
            font=ctk.CTkFont(family=DISPLAY, size=16, weight="bold"),
            text_color=T["text"],
            anchor="w",
        )
        self.status_title.pack(fill="x")
        self.status_sub = ctk.CTkLabel(
            col,
            text=self._idle_hint(),
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
            anchor="w",
        )
        self.status_sub.pack(fill="x", pady=(2, 0))

    def _idle_hint(self) -> str:
        return t(
            "status_idle_hint",
            hotkey=CFG.get("hotkey", ""),
            lang=CFG.get("language", ""),
            model=CFG.get("model", ""),
        )

    def _build_controls(self) -> None:
        # primary record: full-width console button
        self.btn_rec = ctk.CTkButton(
            self._content.inner,
            text=t("btn_record"),
            height=48,
            corner_radius=0,
            font=ctk.CTkFont(family=DISPLAY, size=15, weight="bold"),
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            command=toggle_record,
        )
        self.btn_rec.pack(fill="x", padx=28, pady=(0, 8))

        row = ctk.CTkFrame(self._content.inner, fg_color="transparent")
        row.pack(fill="x", padx=28, pady=(0, 4))

        self.btn_compact = ctk.CTkButton(
            row,
            text=t("btn_compact"),
            height=40,
            corner_radius=0,
            font=ctk.CTkFont(family=BODY, size=13),
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            command=self.show_compact,
        )
        self.btn_compact.pack(side="left", expand=True, fill="x", padx=(0, 6))

        self.btn_settings = ctk.CTkButton(
            row,
            text=t("btn_settings"),
            height=40,
            corner_radius=0,
            font=ctk.CTkFont(family=BODY, size=13),
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            command=self.open_settings,
        )
        self.btn_settings.pack(side="left", expand=True, fill="x")

    def _build_monitor(self) -> None:
        # collapsible system monitor (remembers open/closed in config)
        wrap = ctk.CTkFrame(self._content.inner, fg_color="transparent")
        wrap.pack(fill="x", padx=28, pady=(8, 0))

        head = ctk.CTkFrame(
            wrap, fg_color=T["panel"], corner_radius=0,
            border_width=1, border_color=T["line"],
        )
        head.pack(fill="x")
        self._mon_open = bool(CFG.get("monitor_open", False))
        self._mon_mark = ctk.CTkLabel(
            head,
            text="−" if self._mon_open else "+",
            width=18,
            font=ctk.CTkFont(family=DISPLAY, size=14, weight="bold"),
            text_color=T["muted"],
        )
        self._mon_mark.pack(side="left", padx=(12, 6), pady=8)
        ctk.CTkLabel(
            head,
            text=t("mon_title"),
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["text"],
            anchor="w",
        ).pack(side="left", fill="x", expand=True, pady=8)

        body = ctk.CTkFrame(wrap, fg_color="transparent")
        self._mon_body = body

        def toggle_mon() -> None:
            self._mon_open = not self._mon_open
            if self._mon_open:
                body.pack(fill="x", pady=(2, 4))
                self._mon_mark.configure(text="−")
            else:
                body.pack_forget()
                self._mon_mark.configure(text="+")
            CFG["monitor_open"] = self._mon_open
            try:
                save_config(CFG)
            except Exception:
                pass
            self.after(50, self._fit_scrollbar)

        for wdg in (head, self._mon_mark):
            wdg.bind("<Button-1>", lambda _e: toggle_mon())
        for child in head.winfo_children():
            if child is not self._mon_mark:
                child.bind("<Button-1>", lambda _e: toggle_mon())

        grid = ctk.CTkFrame(body, fg_color=T["panel"], corner_radius=0,
                            border_width=1, border_color=T["line"])
        grid.pack(fill="x", pady=(2, 0))
        self._mon_labels = {}
        rows = [
            ("model", t("mon_model")),
            ("gpu_mem", t("mon_gpu_mem")),
            ("gpu_util", t("mon_gpu_util")),
            ("gpu_temp", t("mon_gpu_temp")),
            ("gpu_power", t("mon_gpu_power")),
            ("cpu", t("mon_cpu_load")),
            ("ram", t("mon_ram")),
        ]
        for i, (key, title) in enumerate(rows):
            row = ctk.CTkFrame(grid, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(
                row, text=title, anchor="w",
                font=ctk.CTkFont(family=BODY, size=11), text_color=T["muted"],
                width=140,
            ).pack(side="left")
            val = ctk.CTkLabel(
                row, text="…", anchor="w",
                font=ctk.CTkFont(family=BODY, size=11), text_color=T["text"],
            )
            val.pack(side="left", fill="x", expand=True)
            self._mon_labels[key] = val

        if self._mon_open:
            body.pack(fill="x", pady=(2, 4))

        self.after(300, self._refresh_monitor)
        self.after(2000, self._monitor_loop)

    def _refresh_monitor(self) -> None:
        use_gpu = CFG.get("device_choice", "cpu") == "gpu"
        L = self._mon_labels
        L["model"].configure(text=f"{CFG.get('model')} / {'GPU' if use_gpu else 'CPU'}")

        if use_gpu and gpu_info.has_nvidia():
            try:
                m = gpu_info.gpu_metrics()
                used = m["used_mb"] / 1024
                total = m["total_mb"] / 1024
                L["gpu_mem"].configure(text=f"{used:.2f} / {total:.1f} {t('mon_gb')}")
                L["gpu_util"].configure(
                    text=f"{m['util']:.0f}%" if m["util"] is not None else t("mon_na")
                )
                L["gpu_temp"].configure(
                    text=f"{m['temp']:.0f}°C" if m["temp"] is not None else t("mon_na")
                )
                L["gpu_power"].configure(
                    text=f"{m['power_w']:.0f} W" if m["power_w"] is not None else t("mon_na")
                )
            except Exception:
                for k in ("gpu_mem", "gpu_util", "gpu_temp", "gpu_power"):
                    L[k].configure(text=t("mon_na"))
        else:
            for k in ("gpu_mem", "gpu_util", "gpu_temp", "gpu_power"):
                L[k].configure(text=t("mon_na"))

        pct, used_gb = gpu_info.cpu_ram()
        cl = gpu_info.cpu_load()
        L["cpu"].configure(
            text=f"{cl:.0f}%" if cl is not None else t("mon_na")
        )
        L["ram"].configure(
            text=f"{pct:.0f}% ({used_gb:.1f} {t('mon_gb')})"
            if pct is not None
            else t("mon_na")
        )

    def _monitor_loop(self) -> None:
        try:
            if self._mon_open:
                self._refresh_monitor()
        except Exception:
            pass
        try:
            self.after(2000, self._monitor_loop)
        except Exception:
            pass

    def _fit_scrollbar(self) -> None:
        try:
            self._content.fit_scrollbar()
        except Exception:
            pass

    def _build_log(self) -> None:
        wrap = ctk.CTkFrame(self._content.inner, fg_color="transparent")
        wrap.pack(fill="x", padx=28, pady=(8, 8))

        top = ctk.CTkFrame(wrap, fg_color="transparent")
        top.pack(fill="x", pady=(0, 8))
        self.lbl_log = ctk.CTkLabel(
            top,
            text=t("btn_log"),
            font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
            text_color=T["muted"],
            anchor="w",
        )
        self.lbl_log.pack(side="left")
        self.btn_clear = ctk.CTkButton(
            top,
            text=t("btn_clear"),
            width=72,
            height=26,
            corner_radius=0,
            font=ctk.CTkFont(family=BODY, size=11),
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["muted"],
            command=self.clear_log,
        )
        self.btn_clear.pack(side="right")

        self.log = ctk.CTkTextbox(
            wrap,
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["text"],
            fg_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            corner_radius=0,
            wrap="word",
        )
        self.log.pack(fill="x")
        self.log.configure(state="disabled")

    def _build_footer(self) -> None:
        foot_wrap = ctk.CTkFrame(self, fg_color=T["panel"], corner_radius=0)
        foot_wrap.pack(side="bottom", fill="x", padx=0, pady=0)

        self.foot = ctk.CTkLabel(
            foot_wrap,
            text=t("footer"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
        )
        self.foot.pack(pady=(8, 2))

        row = ctk.CTkFrame(foot_wrap, fg_color="transparent")
        row.pack(pady=(0, 8))
        self.btn_support = ctk.CTkButton(
            row,
            text=t("footer_support"),
            height=24,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            text_color=T["muted"],
            border_width=0,
            font=ctk.CTkFont(family=BODY, size=11),
            command=self.open_support,
        )
        self.btn_support.pack(side="left", padx=(0, 6))
        self.btn_license = ctk.CTkButton(
            row,
            text=t("footer_license"),
            height=24,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            text_color=T["muted"],
            border_width=0,
            font=ctk.CTkFont(family=BODY, size=11),
            command=self.open_license,
        )
        self.btn_license.pack(side="left")

    def _maybe_check_updates(self) -> None:
        """Once per day, check quietly if a newer version exists."""
        state_file = APP_DIR / "update_state.json"
        if not app_version.should_check(state_file):
            return

        def worker() -> None:
            try:
                remote, zip_url = app_version.fetch_latest()
                app_version.mark_checked(state_file)
                if remote and app_version.is_newer(remote) and self._update_allowed(remote):
                    _emit("warn", "update_available", remote=remote)
                    self.after(0, lambda: self._set_update_available(remote, zip_url))
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _update_allowed(self, remote: str) -> bool:
        return str(CFG.get("skip_version", "")) != str(remote)

    def _set_update_available(self, remote: str, zip_url: str | None) -> None:
        self._pending_update = (remote, zip_url)
        self._blink_version(True)
        try:
            self.lbl_version.configure(cursor="hand2")
        except Exception:
            pass

    def _blink_version(self, on: bool) -> None:
        if not getattr(self, "_pending_update", None):
            try:
                self.lbl_version.configure(text_color=T["faint"])
            except Exception:
                pass
            return
        color = T["amber"] if on else T["faint"]
        try:
            self.lbl_version.configure(text_color=color)
        except Exception:
            pass
        self.after(500, lambda: self._blink_version(not on))

    def _on_version_click(self, _event=None) -> None:
        pending = getattr(self, "_pending_update", None)
        if not pending:
            return
        UpdateDialog(self, pending[0], pending[1])

    def open_support(self) -> None:
        SupportWindow(self)

    def open_license(self) -> None:
        LicenseWindow(self)

    # ---- log / status ----

    def on_log(self, kind: str, message: str) -> None:
        prefix = {
            "info": "·",
            "ok": "✓",
            "rec": "●",
            "warn": "!",
            "error": "✕",
            "text": "»",
        }.get(kind, "·")
        self.log.configure(state="normal")
        self.log.insert("end", f"{prefix} {message}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

        if kind == "rec":
            self.set_status("rec")
        elif kind in ("ok", "text") and not _state["recording"] and not _state["busy"]:
            self.set_status("idle")
        elif kind == "error":
            self.set_status("idle")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def set_status(self, mode: str) -> None:
        self._status_mode = mode
        if mode == "rec":
            self.status_dot.configure(fg_color=T["red"])
            self.status_title.configure(text=t("status_rec"), text_color=T["red"])
            self.status_sub.configure(text=t("status_rec_hint"), text_color=T["muted"])
            self.btn_rec.configure(
                text=t("btn_stop"),
                fg_color="#B51E24",
                hover_color="#96181D",
                state="normal",
            )
        elif mode == "busy":
            self.status_dot.configure(fg_color=T["amber"])
            self.status_title.configure(text=t("status_busy"), text_color=T["amber"])
            self.status_sub.configure(text=t("status_busy_hint"), text_color=T["muted"])
            self.btn_rec.configure(
                text=t("btn_wait"), state="disabled", fg_color=T["faint"], hover_color=T["faint"]
            )
        else:
            self.status_dot.configure(fg_color=T["red_dim"])
            self.status_title.configure(text=t("status_ready"), text_color=T["text"])
            self.status_sub.configure(text=self._idle_hint(), text_color=T["muted"])
            self.btn_rec.configure(
                text=t("btn_record"),
                state="normal",
                fg_color=T["red"],
                hover_color="#D92222",
            )

    def refresh_i18n(self) -> None:
        """Re-apply all UI strings after language change."""
        self.title(t("app_title"))
        self.lbl_title.configure(text=t("app_title"))
        self.lbl_tag.configure(text=t("tagline"))
        self.lbl_log.configure(text=t("btn_log"))
        self.btn_clear.configure(text=t("btn_clear"))
        self.foot.configure(text=t("footer"))
        self.btn_support.configure(text=t("footer_support"))
        self.btn_license.configure(text=t("footer_license"))
        self.btn_settings.configure(text=t("btn_settings"))
        self.btn_compact.configure(text=t("btn_compact"))
        self.set_status(self._status_mode)

    # ---- compact ----

    def show_compact(self) -> None:
        # Build compact FIRST (while main still mapped) so Toplevel parent is valid
        if self.compact is None or not self.compact.winfo_exists():
            self.compact = CompactWindow(self)
            # Break transient AFTER create — parent withdraw would hide it
            try:
                self.compact.transient("")
            except Exception:
                pass
        try:
            self.compact.deiconify()
            self.compact.attributes("-topmost", True)
            self.compact.lift()
            self.compact.update_idletasks()
        except Exception as e:
            self.deiconify()
            self.on_log("error", t("log_err", err=f"compact: {e}"))
            return
        # Withdraw main only after compact is up
        self.withdraw()
        # re-assert visibility after parent withdraw
        try:
            self.compact.deiconify()
            self.compact.lift()
            self.compact.attributes("-topmost", True)
            self.compact.update_idletasks()
        except Exception:
            pass
        self.on_log("info", "compact shown")

    def show_main(self) -> None:
        if self.compact is not None and self.compact.winfo_exists():
            try:
                self.compact.withdraw()
            except Exception:
                pass
        self.deiconify()
        self.attributes("-topmost", bool(CFG.get("topmost", False)))
        self.lift()
        try:
            self.focus_force()
        except Exception:
            pass

    # ---- settings ----

    def open_settings(self, hide_main: bool = False) -> None:
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.lift()
            return
        win = SettingsWindow(self, from_compact=hide_main)
        self.settings = win

    def on_close(self) -> None:
        try:
            keyboard.unhook_all()
        except Exception:
            pass
        try:
            if self.compact is not None and self.compact.winfo_exists():
                self.compact.destroy()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
        sys.stdout.flush() if sys.stdout else None
        sys.stderr.flush() if sys.stderr else None
        os._exit(0)


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, app: App, from_compact: bool = False) -> None:
        super().__init__(app)
        self.app = app
        self._from_compact = from_compact
        self.title(t("settings_title"))
        # 125% DPI: 440×640 logical → 550×800 physical (height = main window)
        self.geometry("440x640")
        self.minsize(440, 640)
        self.maxsize(440, 640)
        self.resizable(False, False)
        try:
            self.attributes("-zoomed", False)
        except Exception:
            pass
        try:
            import ctypes

            hwnd = int(self.winfo_id())
            GWL_STYLE = -16
            WS_MAXIMIZEBOX = 0x00010000
            WS_THICKFRAME = 0x00040000
            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(hwnd, GWL_STYLE)
            user32.SetWindowLongW(
                hwnd, GWL_STYLE, style & ~WS_MAXIMIZEBOX & ~WS_THICKFRAME
            )
        except Exception:
            pass
        self.grab_set()
        # Above our app windows only (not global topmost):
        # - normal open → transient to main
        # - from compact → main is withdrawn, stay standalone
        if not from_compact:
            try:
                self.transient(app)
            except Exception:
                pass
        else:
            try:
                self.transient("")
            except Exception:
                pass

        self._capturing_hotkey = False
        self._hk_hook = None
        self._pending_hotkey = CFG.get("hotkey", "ctrl+alt+space")
        self._dl_size = None

        self._build()
        self._refresh_model_status()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        self.configure(fg_color=T["bg"])
        outer = ctk.CTkScrollableFrame(self, fg_color=T["bg"], corner_radius=0)
        outer.pack(fill="both", expand=True, padx=0, pady=(0, 0))

        self._acc_panels = {}
        self._acc_marks = {}

        def accordion(key: str, title: str, open_by_default: bool = False) -> ctk.CTkFrame:
            wrap = ctk.CTkFrame(outer, fg_color="transparent")
            wrap.pack(fill="x", padx=24, pady=(10, 0))

            head = ctk.CTkFrame(
                wrap, fg_color=T["panel"], corner_radius=0,
                border_width=1, border_color=T["line"],
            )
            head.pack(fill="x")

            mark = ctk.CTkLabel(
                head,
                text="−" if open_by_default else "+",
                width=18,
                font=ctk.CTkFont(family=DISPLAY, size=14, weight="bold"),
                text_color=T["muted"],
            )
            mark.pack(side="left", padx=(12, 6), pady=10)
            ctk.CTkLabel(
                head,
                text=title,
                font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
                text_color=T["text"],
                anchor="w",
            ).pack(side="left", fill="x", expand=True, pady=10)

            body = ctk.CTkFrame(wrap, fg_color="transparent")

            def toggle() -> None:
                # always keep exactly one panel open
                if body.winfo_ismapped():
                    return
                for other_key, other_body in self._acc_panels.items():
                    try:
                        other_body.pack_forget()
                    except Exception:
                        pass
                    mark_w = self._acc_marks.get(other_key)
                    if mark_w is not None:
                        mark_w.configure(text="+")
                body.pack(fill="x", pady=(2, 4))
                mark.configure(text="−")

            for w in (head, mark):
                w.bind("<Button-1>", lambda _e: toggle())
            for child in head.winfo_children():
                if child is not mark:
                    child.bind("<Button-1>", lambda _e: toggle())

            self._acc_panels[key] = body
            self._acc_marks[key] = mark
            if open_by_default:
                body.pack(fill="x", pady=(2, 4))
            return body

        # --- 1. Hotkey (open) ---
        hot = accordion("hotkey", t("set_hotkey"), True)

        hk_row = ctk.CTkFrame(hot, fg_color="transparent")
        hk_row.pack(fill="x", padx=14, pady=(12, 6))
        self.btn_hotkey = ctk.CTkButton(
            hk_row,
            text=self._hk_label(),
            height=36,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._toggle_hotkey_capture,
        )
        self.btn_hotkey.pack(fill="x")

        ctk.CTkLabel(
            hot,
            text=t("set_hotkey_mode"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(12, 4))

        cur_mode = CFG.get("hotkey_mode", "toggle")
        self._mode_var = ctk.StringVar(value="hold" if cur_mode == "hold" else "toggle")

        row_t = ctk.CTkFrame(hot, fg_color="transparent")
        row_t.pack(fill="x", padx=14, pady=(0, 2))
        self.rad_toggle = ctk.CTkRadioButton(
            row_t,
            text=t("mode_toggle"),
            variable=self._mode_var,
            value="toggle",
            font=ctk.CTkFont(family=BODY, size=13, weight="bold"),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_toggle.pack(anchor="w")
        ctk.CTkLabel(
            row_t,
            text=t("mode_toggle_hint"),
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
            anchor="w",
        ).pack(anchor="w", padx=(28, 0))

        row_h = ctk.CTkFrame(hot, fg_color="transparent")
        row_h.pack(fill="x", padx=14, pady=(8, 12))
        self.rad_hold = ctk.CTkRadioButton(
            row_h,
            text=t("mode_hold"),
            variable=self._mode_var,
            value="hold",
            font=ctk.CTkFont(family=BODY, size=13, weight="bold"),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_hold.pack(anchor="w")
        ctk.CTkLabel(
            row_h,
            text=t("mode_hold_hint"),
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
            anchor="w",
        ).pack(anchor="w", padx=(28, 0))

        # --- 2. Model ---
        mod = accordion("model", t("set_model"))
        m_row = ctk.CTkFrame(mod, fg_color="transparent")
        m_row.pack(fill="x", padx=14, pady=(12, 4))
        self.cmb_model = ctk.CTkOptionMenu(
            m_row,
            values=mm.MODEL_SIZES,
            width=200,
            command=self._on_model_change,
            corner_radius=0,
            fg_color=T["panel"],
            button_color=T["line"],
            button_hover_color="#35424E",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.cmb_model.set(CFG.get("model", "small"))
        self.cmb_model.pack(side="left")

        self.lbl_model_state = ctk.CTkLabel(
            m_row,
            text="",
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=11),
        )
        self.lbl_model_state.pack(side="left", padx=10)

        self.lbl_vram = ctk.CTkLabel(
            mod,
            text="",
            anchor="w",
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
        )
        self.lbl_vram.pack(fill="x", padx=14, pady=(2, 2))

        ctk.CTkLabel(
            mod,
            text=t("set_device"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(10, 4))

        self._device_var = ctk.StringVar(value=CFG.get("device_choice", "cpu"))
        nvidia = gpu_info.has_nvidia()
        self.rad_gpu = ctk.CTkRadioButton(
            mod,
            text=t("device_gpu"),
            variable=self._device_var,
            value="gpu",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_gpu.pack(anchor="w", padx=14, pady=2)
        self.rad_cpu = ctk.CTkRadioButton(
            mod,
            text=t("device_cpu"),
            variable=self._device_var,
            value="cpu",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_cpu.pack(anchor="w", padx=14, pady=2)

        if not nvidia:
            self.rad_gpu.configure(state="disabled", text_color=T["faint"])
            tip = ctk.CTkLabel(
                mod,
                text=t("device_gpu_tooltip"),
                text_color=T["faint"],
                font=ctk.CTkFont(family=BODY, size=10),
                wraplength=360,
                justify="left",
            )
            tip.pack(fill="x", padx=28, pady=(0, 6))

        def _vram_text() -> str:
            name = self.cmb_model.get()
            est = gpu_info.VRAM_ESTIMATE_GB.get(name, 1.0)
            return f"{t('set_model_vram')}: ~{est:g} {t('mon_gb')}"

        self.lbl_vram.configure(text=_vram_text())

        def _on_model_change2(choice: str) -> None:
            self._on_model_change(choice)
            self.lbl_vram.configure(text=_vram_text())

        self.cmb_model.configure(command=_on_model_change2)

        d_row = ctk.CTkFrame(mod, fg_color="transparent")
        d_row.pack(fill="x", padx=14, pady=(4, 2))
        self.btn_dl = ctk.CTkButton(
            d_row,
            text=t("set_download"),
            width=110,
            height=30,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._start_download,
        )
        self.btn_dl.pack(side="left")
        self.lbl_dl = ctk.CTkLabel(
            d_row, text="", anchor="w", font=ctk.CTkFont(family=BODY, size=11)
        )
        self.lbl_dl.pack(side="left", padx=10)

        self.pbar = ctk.CTkProgressBar(
            mod, height=4, corner_radius=0, progress_color=T["red"]
        )
        self.pbar.pack(fill="x", padx=14, pady=(4, 8))
        self.pbar.set(0)

        ctk.CTkLabel(
            mod,
            text=t("set_models_path"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(10, 4))

        p_row = ctk.CTkFrame(mod, fg_color="transparent")
        p_row.pack(fill="x", padx=14, pady=(0, 12))
        self.lbl_path = ctk.CTkLabel(
            p_row,
            text=str(mm.models_root()),
            anchor="w",
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
        )
        self.lbl_path.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            p_row,
            text=t("set_open_folder"),
            width=120,
            height=30,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._open_folder,
        ).pack(side="right")

        # --- 3. Language ---
        lang = accordion("lang", t("set_audio_lang"))
        ctk.CTkLabel(
            lang,
            text=t("set_audio_lang"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(12, 4))

        self._speech_lang = CFG.get("language", "ru")
        self._lang_btns = {}
        lang_row = ctk.CTkFrame(lang, fg_color="transparent")
        lang_row.pack(fill="x", padx=14, pady=(0, 10))
        for code, flag, label in (
            ("ru", "ru.png", t("lang_ru").replace("🇷🇺 ", "")),
            ("en", "gb.png", t("lang_en").replace("🇬🇧 ", "")),
            ("auto", "auto.png", t("lang_auto").replace("🌐 ", "")),
        ):
            btn = self._make_flag_btn(lang_row, flag, label, selected=(code == self._speech_lang))
            btn.configure(command=lambda c=code: self._pick_speech_lang(c))
            btn.pack(side="left", padx=(0, 6))
            self._lang_btns[code] = btn

        ctk.CTkLabel(
            lang,
            text=t("set_ui_lang"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(6, 4))

        self._ui_lang = CFG.get("ui_language", "en")
        self._ui_btns = {}
        ui_row = ctk.CTkFrame(lang, fg_color="transparent")
        ui_row.pack(fill="x", padx=14, pady=(0, 12))
        for code, flag, label in (("ru", "ru.png", t("ui_ru")), ("en", "gb.png", t("ui_en"))):
            btn = self._make_flag_btn(ui_row, flag, label, selected=(code == self._ui_lang))
            btn.configure(command=lambda c=code: self._pick_ui_lang(c))
            btn.pack(side="left", padx=(0, 6))
            self._ui_btns[code] = btn

        # --- 4. Record / window ---
        rec = accordion("record", t("set_behavior"))
        ctk.CTkLabel(
            rec, text=t("set_max_rec"), anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(12, 4))
        self.ent_max = ctk.CTkEntry(
            rec,
            width=120,
            corner_radius=0,
            fg_color=T["panel"],
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.ent_max.insert(0, str(CFG.get("max_record_seconds", 60)))
        self.ent_max.pack(anchor="w", padx=14, pady=(0, 8))

        self.chk_top = ctk.CTkCheckBox(
            rec,
            text=t("set_topmost"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("topmost"):
            self.chk_top.select()
        self.chk_top.pack(anchor="w", padx=14, pady=6)

        self.chk_compact = ctk.CTkCheckBox(
            rec,
            text=t("set_compact_start"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("compact_on_start"):
            self.chk_compact.select()
        self.chk_compact.pack(anchor="w", padx=14, pady=(6, 12))

        # --- 5. Updates (no accordion) ---
        ctk.CTkLabel(
            outer,
            text=t("set_updates"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=24, pady=(18, 4))
        ctk.CTkFrame(outer, height=1, fg_color=T["line"]).pack(fill="x", padx=24)

        upd_row = ctk.CTkFrame(outer, fg_color="transparent")
        upd_row.pack(fill="x", padx=24, pady=(10, 4))
        self.btn_upd = ctk.CTkButton(
            upd_row,
            text=t("set_check_updates"),
            width=160,
            height=32,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._check_updates,
        )
        self.btn_upd.pack(side="left")
        self.lbl_upd = ctk.CTkLabel(
            upd_row,
            text=app_version.__version__,
            anchor="w",
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
        )
        self.lbl_upd.pack(side="left", padx=12)

        # --- Save / Cancel (pinned below scroll area) ---
        btns = ctk.CTkFrame(self, fg_color=T["panel"], corner_radius=0)
        btns.pack(fill="x", side="bottom", padx=0, pady=0)
        ctk.CTkButton(
            btns,
            text=t("set_cancel"),
            width=100,
            height=38,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._close,
        ).pack(side="right", padx=(8, 24), pady=12)
        ctk.CTkButton(
            btns,
            text=t("set_save"),
            width=120,
            height=38,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            command=self._save,
        ).pack(side="right", padx=(0, 8), pady=12)

    def _check_updates(self) -> None:
        def worker() -> None:
            self.after(0, lambda: self.lbl_upd.configure(text=t("update_checking")))
            try:
                remote, zip_url = app_version.fetch_latest()
                app_version.mark_checked(APP_DIR / "update_state.json")
                if app_version.is_newer(remote) and zip_url:
                    msg = t("update_available", remote=remote)
                    color = T["amber"]

                    def to_update() -> None:
                        self.lbl_upd.configure(text=msg, text_color=color)
                        self.btn_upd.configure(
                            text=t("update_do"),
                            fg_color=T["red"],
                            hover_color="#D92222",
                            text_color="#FFFFFF",
                            command=lambda: self._do_update(zip_url),
                        )

                    try:
                        self.after(0, to_update)
                    except Exception:
                        pass
                    return
                if app_version.is_newer(remote):
                    msg = t("update_available", remote=remote)
                    color = T["amber"]
                else:
                    msg = t("update_current")
                    color = T["muted"]
            except Exception:
                msg = t("update_failed")
                color = T["faint"]

            def done() -> None:
                self.lbl_upd.configure(text=msg, text_color=color)

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _do_update(self, zip_url: str) -> None:
        self.lbl_upd.configure(text=t("update_done"), text_color=T["amber"])
        try:
            app_version.run_updater(zip_url, APP_DIR.parent)
        except Exception:
            self.lbl_upd.configure(text=t("update_failed"), text_color=T["faint"])
            return
        self.after(400, self.app.on_close)

    def _hk_label(self) -> str:
        if self._capturing_hotkey:
            return t("set_hotkey_capture")
        return f"{t('set_hotkey')}: {self._pending_hotkey}"

    def _load_flag(self, filename: str):
        """Load flag from SVG source (tiny repo files) → CTkImage."""
        name = filename.rsplit(".", 1)[0]
        try:
            import svg_flags

            pil = svg_flags.load_flag(name, size=(24, 16))
            return ctk.CTkImage(light_image=pil, dark_image=pil, size=(24, 16))
        except Exception:
            # fallback: cached PNG if present
            from PIL import Image

            path = APP_DIR / "assets" / "flags" / filename
            if not path.exists():
                return None
            try:
                pil = Image.open(path).convert("RGBA")
                return ctk.CTkImage(light_image=pil, dark_image=pil, size=(24, 16))
            except Exception:
                return None

    def _make_flag_btn(
        self, parent, flag_file: str, label: str, selected: bool
    ) -> ctk.CTkButton:
        img = self._load_flag(flag_file)
        btn = ctk.CTkButton(
            parent,
            text=f"  {label}" if img else label,
            image=img,
            compound="left",
            width=110,
            height=36,
            corner_radius=0,
            fg_color=T["red"] if selected else T["panel"],
            hover_color="#D92222" if selected else "#243038",
            border_width=1,
            border_color=T["red"] if selected else T["line"],
            text_color="#FFFFFF" if selected else T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        # keep a ref so PhotoImage is not garbage-collected
        if img is not None:
            btn._flag_img = img
        return btn

    def _pick_speech_lang(self, code: str) -> None:
        self._speech_lang = code
        for k, btn in self._lang_btns.items():
            sel = k == code
            btn.configure(
                fg_color=T["red"] if sel else T["panel"],
                hover_color="#D92222" if sel else "#243038",
                border_color=T["red"] if sel else T["line"],
                text_color="#FFFFFF" if sel else T["text"],
            )

    def _pick_ui_lang(self, code: str) -> None:
        self._ui_lang = code
        for k, btn in self._ui_btns.items():
            sel = k == code
            btn.configure(
                fg_color=T["red"] if sel else T["panel"],
                hover_color="#D92222" if sel else "#243038",
                border_color=T["red"] if sel else T["line"],
                text_color="#FFFFFF" if sel else T["text"],
            )

    def _toggle_hotkey_capture(self) -> None:
        if self._capturing_hotkey:
            self._stop_capture()
            return
        self._capturing_hotkey = True
        self.btn_hotkey.configure(text=t("set_hotkey_capture"), fg_color=T["red"])
        try:
            keyboard.unhook_all_hotkeys()  # don't trigger record while capturing
        except Exception:
            pass
        pressed: set[str] = set()

        def on_press(event) -> None:
            name = event.name
            if name in ("ctrl", "ctrl l", "ctrl r", "shift", "shift l", "shift r", "alt", "alt l", "alt r", "windows", "windows l", "windows r"):
                pressed.add(_norm_mod(name))
                self.btn_hotkey.configure(text="+".join(sorted(pressed)) + "+…")
                return
            if name == "esc":
                self._pending_hotkey = CFG.get("hotkey", "ctrl+alt+space")
                self._stop_capture()
                return
            parts = sorted(pressed) + [name]
            self._pending_hotkey = "+".join(parts)
            self._stop_capture()

        def on_release(_e) -> None:
            pass

        self._hk_hook_press = keyboard.on_press(on_press)
        self._hk_hook_rel = keyboard.on_release(on_release)

    def _stop_capture(self) -> None:
        self._capturing_hotkey = False
        try:
            keyboard.unhook(self._hk_hook_press)
        except Exception:
            pass
        try:
            keyboard.unhook(self._hk_hook_rel)
        except Exception:
            pass
        try:
            register_hotkey(CFG.get("hotkey", "ctrl+alt+space"), CFG.get("hotkey_mode", "toggle"))
        except Exception:
            pass
        self.btn_hotkey.configure(text=self._hk_label(), fg_color=T["panel"])

    def _on_model_change(self, choice: str) -> None:
        self._refresh_model_status(choice)

    def _refresh_model_status(self, size: str | None = None) -> None:
        size = size or self.cmb_model.get()
        ok = mm.is_downloaded(size)
        color = "#a6e3a1" if ok else "#f9e2af"
        label = f"✓ {t('set_model_dl')}" if ok else f"✗ {t('set_model_not_dl')}"
        self.lbl_model_state.configure(text=label, text_color=color)
        if ok:
            # no Download button for already-downloaded models
            self.btn_dl.pack_forget()
            self.pbar.pack_forget()
            self.lbl_dl.configure(text="")
            self.pbar.set(1)
        else:
            if not self.btn_dl.winfo_ismapped():
                self.btn_dl.pack(side="left")
            if not self.pbar.winfo_ismapped():
                self.pbar.pack(fill="x", pady=(4, 8))
            self.btn_dl.configure(state="normal")
            self.pbar.set(0)

    def _start_download(self) -> None:
        size = self.cmb_model.get()
        if self._dl_size:
            return
        self._dl_size = size
        self.btn_dl.configure(state="disabled")
        self.lbl_dl.configure(text=t("set_downloading", pct=0))

        def on_progress(pct: int) -> None:
            try:
                self.after(0, self.pbar.set, pct / 100.0)
                self.after(0, self.lbl_dl.configure, {"text": t("set_downloading", pct=pct)})
            except Exception:
                pass

        def on_done(ok: bool, err: str | None) -> None:
            def finish() -> None:
                self._dl_size = None
                if ok:
                    self.lbl_dl.configure(text=t("set_download_done"), text_color="#a6e3a1")
                    self.pbar.set(1)
                else:
                    self.lbl_dl.configure(text=t("set_download_fail", err=err or ""), text_color="#f38ba8")
                self._refresh_model_status()

            try:
                self.after(0, finish)
            except Exception:
                pass

        mm.download_model(size, on_progress=on_progress, on_done=on_done)

    def _open_folder(self) -> None:
        path = mm.models_root()
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(path))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as e:
            self.app.on_log("error", t("set_open_folder_fail", err=str(e)))

    def _on_ui_lang_preview(self, choice: str) -> None:
        # apply on Save only for simplicity — preview optional
        pass

    def _save(self) -> None:
        global CFG
        try:
            new_max = int(self.ent_max.get().strip() or DEFAULT_CONFIG["max_record_seconds"])
        except ValueError:
            self.app.on_log("error", t("set_bad_values"))
            return

        speech_lang = self._speech_lang
        ui_lang = self._ui_lang

        old_model = CFG.get("model")
        old_hotkey = CFG.get("hotkey")

        mode_text = self.seg_mode.get() if hasattr(self, "seg_mode") else self._mode_var.get()
        hotkey_mode = "hold" if mode_text in ("hold", t("mode_hold")) else "toggle"

        CFG["hotkey"] = self._pending_hotkey
        CFG["hotkey_mode"] = hotkey_mode
        CFG["model"] = self.cmb_model.get()
        CFG["language"] = speech_lang
        CFG["ui_language"] = ui_lang
        CFG["max_record_seconds"] = new_max
        CFG["device_choice"] = self._device_var.get()
        CFG["topmost"] = bool(self.chk_top.get())
        CFG["compact_on_start"] = bool(self.chk_compact.get())
        save_config(CFG)

        i18n.set_lang(ui_lang)

        try:
            register_hotkey(CFG["hotkey"], CFG["hotkey_mode"])
        except Exception as e:
            self.app.on_log("error", t("set_hotkey_fail", err=str(e)))

        if old_model != CFG["model"]:
            _state["model"] = None
            _state["model_name_loaded"] = None
            if mm.is_downloaded(CFG["model"]):
                threading.Thread(target=_load_model, daemon=True).start()

        self.app.attributes("-topmost", CFG.get("topmost", False))
        self.app.refresh_i18n()
        self.app.on_log("ok", t("set_saved"))
        self._close()

    def _close(self) -> None:
        if self._capturing_hotkey:
            self._stop_capture()
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.app.settings = None
        # Return to compact bubble if settings were opened from it
        if self._from_compact:
            try:
                self.app.withdraw()
                if self.app.compact is not None and self.app.compact.winfo_exists():
                    self.app.compact.deiconify()
                    self.app.compact.lift()
                    self.app.compact.attributes("-topmost", True)
                    self.app.compact.update_idletasks()
            except Exception:
                pass


class SupportWindow(ctk.CTkToplevel):
    """Support the author — wallet list with copy / open links (same as yaroPhotoSort)."""

    WALLETS = [
        {
            "name": "DonationAlerts",
            "address": "https://dalink.to/v1drimers",
            "link": "https://dalink.to/v1drimers",
            "color": "#e64553",
        },
        {
            "name": "Kaspa",
            "address": "kaspa:qzdkq9n6p0rgp7fg3cyhuq4uznfy6a4csh5jcqt4gs355zyf3r3t2eszhhc9c",
            "link": None,
            "color": "#70c7ba",
        },
        {
            "name": "TON",
            "address": "UQB6VnvZJXUfq3CW-xS6ku38t3fIK7RJ30a5TMTGJiJal8tr",
            "link": None,
            "color": "#0088cc",
        },
        {
            "name": "USDT (TRC-20)",
            "address": "TYYvAa7u8agTheFHoJK6sGqPV2E6UJd6Er",
            "link": None,
            "color": "#26a17b",
        },
    ]

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title(t("support_title"))
        self.geometry("560x640")
        self.resizable(False, False)
        self.configure(fg_color=T["bg"])
        try:
            self.transient(app)
            self.grab_set()
        except Exception:
            pass

        head = ctk.CTkLabel(
            self._content,
            text=t("support_title"),
            font=ctk.CTkFont(family=DISPLAY, size=18, weight="bold"),
            text_color=T["text"],
        )
        head.pack(padx=24, pady=(22, 12), anchor="w")

        body = ctk.CTkFrame(self._content, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=20, pady=(0, 12))

        for w in self.WALLETS:
            self._wallet_row(body, w)

        btns = ctk.CTkFrame(self._content, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(4, 16))
        ctk.CTkButton(
            btns,
            text=t("support_close"),
            width=110,
            height=36,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._close,
        ).pack(side="right")

    @staticmethod
    def _qr_image(data: str, px: int = 88):
        """Local QR (no external API) → CTkImage."""
        try:
            import qrcode
            from PIL import Image

            qr = qrcode.QRCode(
                version=None,
                error_correction=qrcode.constants.ERROR_CORRECT_M,
                box_size=4,
                border=2,
            )
            qr.add_data(data)
            qr.make(fit=True)
            pil = qr.make_image(fill_color="black", back_color="white").convert("RGB")
            pil = pil.resize((px, px), Image.Resampling.NEAREST)
            return ctk.CTkImage(light_image=pil, dark_image=pil, size=(px, px))
        except Exception:
            return None

    def _wallet_row(self, parent, w: dict) -> None:
        row = ctk.CTkFrame(
            parent, fg_color=T["panel"], corner_radius=0,
            border_width=1, border_color=T["line"],
        )
        row.pack(fill="x", pady=(0, 8))

        stripe = ctk.CTkLabel(
            row, text="", width=4, fg_color=w["color"], corner_radius=0
        )
        stripe.pack(side="left", fill="y", padx=(0, 10), pady=10)

        qr_img = self._qr_image(w["address"], 88)
        if qr_img is not None:
            qr_lbl = ctk.CTkLabel(
                row, text="", image=qr_img, width=92, height=92, fg_color="#FFFFFF",
            )
            qr_lbl.image = qr_img  # keep ref
            qr_lbl.pack(side="right", padx=(8, 10), pady=10)

        col = ctk.CTkFrame(row, fg_color="transparent")
        col.pack(side="left", fill="both", expand=True, padx=(0, 8), pady=10)

        ctk.CTkLabel(
            col,
            text=w["name"],
            font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
            text_color=T["text"],
            anchor="w",
        ).pack(fill="x")
        addr = w["address"]
        display = addr if len(addr) <= 36 else addr[:18] + "…" + addr[-14:]
        ctk.CTkLabel(
            col,
            text=display,
            font=ctk.CTkFont(family=BODY, size=11),
            text_color=T["muted"],
            anchor="w",
        ).pack(fill="x", pady=(2, 0))

        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.pack(side="right", padx=12)

        def copy_addr() -> None:
            try:
                import pyperclip

                pyperclip.copy(w["address"])
                self.app.on_log("ok", t("support_copied"))
            except Exception:
                pass

        ctk.CTkButton(
            actions,
            text=t("support_copy"),
            width=150,
            height=28,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=copy_addr,
        ).pack(side="left", padx=(0, 6))

        if w.get("link"):
            def open_link() -> None:
                import webbrowser

                webbrowser.open(w["link"])

            ctk.CTkButton(
                actions,
                text=t("support_open"),
                width=90,
                height=28,
                corner_radius=0,
                fg_color=w["color"],
                hover_color=T["faint"],
                text_color="#FFFFFF",
                font=ctk.CTkFont(family=BODY, size=11),
                command=open_link,
            ).pack(side="left")

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class LicenseWindow(ctk.CTkToplevel):
    """CC BY-NC-ND 4.0 summary like the design: ✓ allowed, ✗ not allowed."""

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title(t("license_title"))
        self.geometry("440x420")
        self.resizable(False, False)
        self.configure(fg_color=T["bg"])
        try:
            self.transient(app)
            self.grab_set()
        except Exception:
            pass

        head = ctk.CTkLabel(
            self._content,
            text=t("license_title"),
            font=ctk.CTkFont(family=DISPLAY, size=18, weight="bold"),
            text_color=T["text"],
        )
        head.pack(padx=24, pady=(22, 10), anchor="w")

        ctk.CTkLabel(
            self._content,
            text=t("license_name"),
            font=ctk.CTkFont(family=DISPLAY, size=15, weight="bold"),
            text_color=T["text"],
            anchor="w",
        ).pack(fill="x", padx=24)
        ctk.CTkLabel(
            self._content,
            text=t("license_full_name"),
            font=ctk.CTkFont(family=BODY, size=11),
            text_color=T["muted"],
            anchor="w",
            wraplength=390,
            justify="left",
        ).pack(fill="x", padx=24, pady=(4, 14))

        rows = [
            ("✓", "#2F9E44", t("license_ok_free"), True),
            ("✓", "#2F9E44", t("license_ok_share"), True),
            ("✗", "#E03131", t("license_no_commercial"), False),
            ("✗", "#E03131", t("license_no_derivatives"), False),
        ]
        for mark, color, label, _ok in rows:
            row = ctk.CTkFrame(self._content, fg_color="transparent")
            row.pack(fill="x", padx=24, pady=3)
            ctk.CTkLabel(
                row,
                text=mark,
                width=22,
                font=ctk.CTkFont(family=DISPLAY, size=16, weight="bold"),
                text_color=color,
            ).pack(side="left")
            ctk.CTkLabel(
                row,
                text=label,
                font=ctk.CTkFont(family=BODY, size=12),
                text_color=T["text"],
                anchor="w",
            ).pack(side="left", padx=(8, 0))

        ctk.CTkLabel(
            self._content,
            text=t("license_note"),
            font=ctk.CTkFont(family=BODY, size=11),
            text_color=T["faint"],
            anchor="w",
            wraplength=390,
            justify="left",
        ).pack(fill="x", padx=24, pady=(16, 0))

        btns = ctk.CTkFrame(self._content, fg_color="transparent")
        btns.pack(fill="x", padx=24, pady=(18, 16))

        def open_full() -> None:
            import webbrowser

            webbrowser.open("https://github.com/Vidrimers/Say-my-name/blob/main/LICENSE")

        ctk.CTkButton(
            btns,
            text=t("license_full_text"),
            width=120,
            height=36,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=open_full,
        ).pack(side="left")
        ctk.CTkButton(
            btns,
            text=t("license_ok_btn"),
            width=110,
            height=36,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            command=self._close,
        ).pack(side="right")

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class UpdateDialog(ctk.CTkToplevel):
    """New version available: Update / Later / Skip this version."""

    def __init__(self, app: "App", remote: str, zip_url: str | None) -> None:
        super().__init__(app)
        self.app = app
        self.remote = remote
        self.zip_url = zip_url
        self.title(t("update_dialog_title"))
        self.geometry("400x220")
        self.resizable(False, False)
        self.configure(fg_color=T["bg"])
        try:
            self.transient(app)
            self.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            self._content,
            text=t("update_dialog_title"),
            font=ctk.CTkFont(family=DISPLAY, size=16, weight="bold"),
            text_color=T["text"],
        ).pack(padx=24, pady=(22, 8), anchor="w")
        ctk.CTkLabel(
            self._content,
            text=t("update_dialog_text", remote=remote, local=app_version.__version__),
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
            wraplength=350,
            justify="left",
        ).pack(fill="x", padx=24, pady=(0, 18))

        row = ctk.CTkFrame(self._content, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 18))

        ctk.CTkButton(
            row,
            text=t("update_later"),
            height=34,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._close,
        ).pack(side="left")

        ctk.CTkButton(
            row,
            text=t("update_skip"),
            height=34,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._skip,
        ).pack(side="left", padx=(8, 0))

        ctk.CTkButton(
            row,
            text=t("update_do"),
            height=34,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            command=self._update,
        ).pack(side="right")

    def _skip(self) -> None:
        CFG["skip_version"] = self.remote
        save_config(CFG)
        self.app._pending_update = None
        try:
            self.app._blink_version(False)
            self.app.lbl_version.configure(text_color=T["faint"], cursor="")
        except Exception:
            pass
        self._close()

    def _update(self) -> None:
        if not self.zip_url:
            self._close()
            return
        try:
            app_version.run_updater(self.zip_url, APP_DIR.parent)
        except Exception:
            pass
        self.after(300, self.app.on_close)

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


def _norm_mod(name: str) -> str:
    n = name.lower()
    if n.startswith("ctrl"):
        return "ctrl"
    if n.startswith("shift"):
        return "shift"
    if n.startswith("alt"):
        return "alt"
    if n.startswith("windows") or n == "win":
        return "windows"
    return n


def main() -> None:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
