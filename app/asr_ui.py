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
import mt_models
import mt_translate

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
    "mic": "",
    "silence_stop": False,
    "close_to_tray": False,
    "autostart_win": False,
    "journal_enable": True,
    "processes": [],
    "tr_enable": False,
    "tr_mode": "api",
    "tr_provider": "mimo",
    "tr_api_key": "",
    "tr_base_url": "",
    "tr_model": "",
    "tr_lang_to": "auto",
    "tr_keep_model": False,
    "tr_local_model": "opus-mt-en-ru",
    "tr_device": "auto",
    "win_main": None,
    "win_compact": None,
    "silence_sec": 2.0,
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
    "held_prekey": None,
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
        mic = CFG.get("mic") or None
        if isinstance(mic, str) and mic != "":
            try:
                mic = int(mic)
            except Exception:
                mic = None
        with sd.InputStream(
            samplerate=sample_rate,
            channels=1,
            dtype="int16",
            device=mic,
            callback=callback,
        ):
            start = time.monotonic()
            silence_sec = float(CFG.get("silence_sec", 2.0))
            check_silence = CFG.get("silence_stop") and CFG.get("hotkey_mode") == "toggle"
            silent_since = None
            while _state["recording"]:
                if time.monotonic() - start > max_seconds:
                    _emit("warn", "log_max_dur")
                    _state["stop_requested"] = True
                    break
                if check_silence and frames:
                    # RMS of last ~0.3s
                    import numpy as _np

                    n = max(1, int(0.3 * sample_rate))
                    tail = frames[-1]
                    if tail.shape[0] >= n:
                        chunk = tail[-n:].astype(_np.float32)
                    else:
                        chunk = _np.concatenate(frames)[-n:].astype(_np.float32)
                    rms = float(_np.sqrt(_np.mean(chunk**2))) if chunk.size else 0.0
                    if rms < 300:  # quiet threshold
                        if silent_since is None:
                            silent_since = time.monotonic()
                        elif time.monotonic() - silent_since >= silence_sec:
                            _emit("info", "log_silence_stop")
                            _state["stop_requested"] = True
                            break
                    else:
                        silent_since = None
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
    _send_prekey()
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
        if CFG.get("tr_enable"):
            try:
                _emit("info", "log_translating")
                text = mt_translate.translate(text, CFG)
                _emit("text", text)
            except Exception as e:
                err = str(e)
                if "NO_TORCH" in err:
                    _emit("error", "log_tr_no_torch")
                elif "NOT_DOWNLOADED" in err:
                    _emit("error", "log_tr_no_model")
                else:
                    _emit("error", "log_tr_fail", err=err)
        _emit("text", text)
        _paste_text(text)
        _journal_append(text)
        _release_prekey()
        _emit("ok", "log_pasted", dt=dt)
    except Exception as e:
        _emit("error", "log_err", err=str(e))
        ui = _state.get("ui")
        if ui is not None:
            tb = traceback.format_exc(limit=3)
            ui.after(0, ui.on_log, "error", tb)
    finally:
        _release_prekey()
        _state["busy"] = False
        if ui is not None:
            ui.after(0, ui.set_status, "rec" if _state["recording"] else "idle")


def on_hotkey() -> None:
    toggle_record()


def _exe_icon(exe_path: str):
    """CTkImage (24px) from exe icon on Windows."""
    try:
        from PIL import Image
        import sys as _sys

        if _sys.platform != "win32" or not exe_path or not Path(exe_path).exists():
            return None
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        SHGFI_ICON = 0x000000100
        SHGFI_SMALLICON = 0x000000001
        hinst = ctypes.windll.shell32

        class SHFILEINFOW(ctypes.Structure):
            _fields_ = [
                ("hIcon", wintypes.HICON),
                ("iIcon", ctypes.c_int),
                ("dwAttributes", wintypes.DWORD),
                ("szDisplayName", ctypes.c_wchar * 260),
                ("szTypeName", ctypes.c_wchar * 80),
            ]

        shinfo = SHFILEINFOW()
        ok = hinst.SHGetFileInfoW(
            str(exe_path),
            0,
            ctypes.byref(shinfo),
            ctypes.sizeof(shinfo),
            SHGFI_ICON | SHGFI_SMALLICON,
        )
        if not ok or not shinfo.hIcon:
            return None

        # draw icon into 24x24 RGBA via GetIconInfo + DrawIconEx on mem DC
        class ICONINFO(ctypes.Structure):
            _fields_ = [
                ("fIcon", wintypes.BOOL),
                ("xHotspot", wintypes.DWORD),
                ("yHotspot", wintypes.DWORD),
                ("hbmMask", wintypes.HBITMAP),
                ("hbmColor", wintypes.HBITMAP),
            ]

        ii = ICONINFO()
        user32.GetIconInfo(shinfo.hIcon, ctypes.byref(ii))

        hdc = user32.GetDC(0)
        mem = ctypes.windll.gdi32.CreateCompatibleDC(hdc)
        bmp = ctypes.windll.gdi32.CreateCompatibleBitmap(hdc, 24, 24)
        old = ctypes.windll.gdi32.SelectObject(mem, bmp)
        # fill background transparent-ish
        user32.DrawIconEx(mem, 0, 0, shinfo.hIcon, 24, 24, 0, 0, 0x0003)

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        bi = BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bi.biWidth = 24
        bi.biHeight = -24
        bi.biPlanes = 1
        bi.biBitCount = 32
        buf = ctypes.create_string_buffer(24 * 24 * 4)
        ctypes.windll.gdi32.GetDIBits(mem, bmp, 0, 24, buf, ctypes.byref(bi), 0)

        ctypes.windll.gdi32.SelectObject(mem, old)
        ctypes.windll.gdi32.DeleteObject(bmp)
        ctypes.windll.gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, hdc)
        user32.DestroyIcon(shinfo.hIcon)

        im = Image.frombuffer("RGBA", (24, 24), buf.raw, "raw", "BGRA", 0, 1)
        return __import__("customtkinter").CTkImage(
            light_image=im, dark_image=im, size=(24, 24)
        )
    except Exception:
        return None


def _exe_at(x: int, y: int) -> str:
    """exe path of window at screen coords."""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pt = wintypes.POINT(int(x), int(y))
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return ""
        GA_ROOT = 2
        root_hwnd = user32.GetAncestor(hwnd, GA_ROOT)
        hwnd = root_hwnd or hwnd
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            ok = ctypes.windll.kernel32.QueryFullProcessImageNameW(
                h, 0, buf, ctypes.byref(size)
            )
            return buf.value if ok else ""
        finally:
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return ""


def _exe_under_cursor() -> str:
    try:
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        return _exe_at(pt.x, pt.y)
    except Exception:
        return ""


def _foreground_process_name() -> str:
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value
        )
        try:
            buf = ctypes.create_unicode_buffer(260)
            size = wintypes.DWORD(260)
            ctypes.windll.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
            name = Path(buf.value).name
            return name or "unknown.exe"
        finally:
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
    except Exception:
        return "unknown.exe"


def _match_process(exe_name: str) -> dict | None:
    """Find enabled process config by exe file name."""
    if not exe_name:
        return None
    for proc in CFG.get("processes") or []:
        if not proc.get("enabled", True):
            continue
        name = proc.get("name") or Path(proc.get("exe", "")).name
        if name.lower() == exe_name.lower():
            return proc
    return None


def _send_prekey() -> None:
    """If focused window is a configured process, send its key first."""
    _state["held_prekey"] = None
    try:
        target = _foreground_process_name()
        proc = _match_process(target)
        if not proc:
            return
        key = (proc.get("key") or "").strip()
        if not key:
            return
        if proc.get("key_mode", "press") == "hold":
            keyboard.press(key)
            _state["held_prekey"] = key
        else:
            keyboard.send(key)
    except Exception:
        pass


def _release_prekey() -> None:
    key = _state.get("held_prekey")
    if not key:
        return
    try:
        keyboard.release(key)
    except Exception:
        pass
    _state["held_prekey"] = None


def _journal_append(text: str) -> None:
    """my-talks/<process>_<date>.txt — one file per process per day."""
    if not CFG.get("journal_enable", True):
        return
    try:
        proc = _foreground_process_name()
        # do not journal into ourselves if focus is on this app? still ok
        day = time.strftime("%Y-%m-%d")
        folder = APP_DIR.parent / "my-talks"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{proc}_{day}.txt"
        stamp = time.strftime("%H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    """Windows mutex — only one app instance. Returns False if already running."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        mutex = kernel32.CreateMutexW(None, False, "SAYMYNAME_SINGLE_INSTANCE")
        last = kernel32.GetLastError()
        ERROR_ALREADY_EXISTS = 183
        if mutex and last == ERROR_ALREADY_EXISTS:
            return False
        _state["mutex"] = mutex
        return True
    except Exception:
        return True


def _set_autostart(enabled: bool) -> None:
    """HKCU Run key -> wscript start.vbs"""
    if sys.platform != "win32":
        return
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
        name = "SAYMYNAME"
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                vbs = str(APP_DIR.parent / "start.vbs")
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, f'wscript.exe "{vbs}"')
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
    except Exception:
        pass


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

        _g = CFG.get("win_compact")
        if isinstance(_g, dict) and "x" in _g and "y" in _g:
            x, y = int(_g["x"]), int(_g["y"])
        else:
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
        """Right-click menu: main, settings, hotkey mode (synced), exit."""
        import tkinter as tk

        menu = tk.Menu(
            self.win,
            tearoff=0,
            bg=T["panel"],
            fg=T["text"],
            activebackground=T["line"],
            activeforeground=T["text"],
            borderwidth=1,
            relief="solid",
        )
        menu.add_command(label=t("ctx_open_main"), command=self.app.show_main)
        menu.add_command(label=t("ctx_settings"), command=self._menu_settings)
        menu.add_separator()

        mode = CFG.get("hotkey_mode", "toggle")
        hk = CFG.get("hotkey", "ctrl+space")

        def pick(m: str) -> None:
            if CFG.get("hotkey_mode", "toggle") == m:
                return
            CFG["hotkey_mode"] = m
            save_config(CFG)
            try:
                register_hotkey(CFG.get("hotkey", "ctrl+space"), m)
            except Exception:
                pass
            self.app.on_log("info", t("log_hotkey_mode", mode=t("mode_toggle") if m == "toggle" else t("mode_hold")))

        # checked item shows hotkey in parentheses
        toggle_txt = t("mode_toggle")
        hold_txt = t("mode_hold")
        if mode == "toggle":
            toggle_txt = f"{t('mode_toggle')} ({hk})"
        else:
            hold_txt = f"{t('mode_hold')} ({hk})"

        menu.add_command(label=("✓ " if mode == "toggle" else "    ") + toggle_txt,
                         command=lambda: pick("toggle"))
        menu.add_command(label=("✓ " if mode == "hold" else "    ") + hold_txt,
                         command=lambda: pick("hold"))

        # silence auto-stop — only with Toggle
        if mode == "toggle":
            sil = bool(CFG.get("silence_stop", False))
            sil_txt = t("silence_stop")
            def pick_silence() -> None:
                CFG["silence_stop"] = not bool(CFG.get("silence_stop", False))
                save_config(CFG)
                self.app.on_log("info", t("log_hotkey_mode", mode=silence_label()))
            def silence_label() -> str:
                return t("silence_stop") if CFG.get("silence_stop") else t("mode_toggle")
            menu.add_command(
                label=("✓ " if sil else "    ") + sil_txt,
                command=pick_silence,
            )

        # translate toggle (synced with settings) — separate section
        menu.add_separator()
        tr_on = bool(CFG.get("tr_enable", False))
        tr_txt = t("tr_enable")

        def pick_tr() -> None:
            CFG["tr_enable"] = not bool(CFG.get("tr_enable", False))
            save_config(CFG)
            self.app.on_log(
                "info",
                t("log_tr_toggle", on=t("on") if CFG["tr_enable"] else t("off")),
            )

        menu.add_command(
            label=("✓ " if tr_on else "    ") + tr_txt,
            command=pick_tr,
        )

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
            self._save_pos()
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
        self._save_pos()
        if self._blink_after is not None:
            try:
                self.win.after_cancel(self._blink_after)
            except Exception:
                pass
        try:
            self.win.destroy()
        except Exception:
            pass

    def _save_pos(self) -> None:
        try:
            CFG["win_compact"] = {
                "x": self.win.winfo_x(),
                "y": self.win.winfo_y(),
            }
            save_config(CFG)
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

        self._wheel_on = False

    def _on_wheel(self, event) -> None:
        """Scroll only if pointer is over this canvas and content needs it."""
        if not getattr(self, "_wheel_on", False):
            return None
        try:
            if not self.canvas.winfo_containing(event.x_root, event.y_root):
                # pointer not on our canvas — maybe on log (nested scroll)
                pass
        except Exception:
            pass
        units = int(-1 * (event.delta / 120))
        self.canvas.yview_scroll(units, "units")
        return "break"

    def fit_scrollbar(self) -> None:
        try:
            self.update_idletasks()
            need = self.inner.winfo_reqheight()
            have = self.canvas.winfo_height()
            if need > have:
                self.vsb.pack(side="right", fill="y")
                self._wheel_on = True
            else:
                self.vsb.pack_forget()
                self.canvas.yview_moveto(0)
                self._wheel_on = False
            self.update_idletasks()
            self.canvas.itemconfigure(self._win, width=max(1, self.canvas.winfo_width()))
        except Exception:
            pass

    def _on_wheel(self, event) -> None:
        if not getattr(self, "_wheel_on", False):
            return
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")


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
        try:
            _g = CFG.get("win_main")
            if isinstance(_g, dict) and _g.get("w") and _g.get("h"):
                self.geometry(
                    f"{_g['w']}x{_g['h']}+{_g.get('x', 100)}+{_g.get('y', 100)}"
                )
        except Exception:
            pass

        self._build_header()
        self._build_status()
        self._build_controls()
        self._build_monitor()
        self._build_log()
        self.after(150, self._fit_scrollbar)
        self.after(200, self._setup_wheel)

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
        model = CFG.get("model", "")
        if not CFG.get("tr_enable"):
            return t(
                "status_idle_hint",
                hotkey=CFG.get("hotkey", ""),
                lang=CFG.get("language", ""),
                model=model,
            )
        # translation active — show what translates
        mode = CFG.get("tr_mode", "api")
        if mode == "local":
            tr = CFG.get("tr_local_model", "")
        else:
            prov = CFG.get("tr_provider", "deepl")
            m = CFG.get("tr_model", "")
            tr = f"{prov}:{m}" if m else prov
        return t(
            "status_idle_hint_tr",
            hotkey=CFG.get("hotkey", ""),
            model=model,
            tr=tr,
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

        self.btn_proc = ctk.CTkButton(
            row,
            text=self._proc_label(),
            height=40,
            corner_radius=0,
            font=ctk.CTkFont(family=BODY, size=13),
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            command=self.open_processes,
        )
        self.btn_proc.pack(side="left", expand=True, fill="x", padx=(0, 6))

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
        self._mon_title = ctk.CTkLabel(
            head,
            text=t("mon_title"),
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["text"],
            anchor="w",
        )
        self._mon_title.pack(side="left", fill="x", expand=True, pady=8)

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
        self._mon_title_labels = {}
        for i, (key, title) in enumerate(rows):
            row = ctk.CTkFrame(grid, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=2)
            lbl = ctk.CTkLabel(
                row, text=title, anchor="w",
                font=ctk.CTkFont(family=BODY, size=11), text_color=T["muted"],
                width=140,
            )
            lbl.pack(side="left")
            self._mon_title_labels[key] = lbl
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
        dev = gpu_info.gpu_name() if use_gpu else gpu_info.cpu_name()
        if not dev:
            dev = "GPU" if use_gpu else "CPU"
        L["model"].configure(text=f"{CFG.get('model')} / {dev}")

        gpu_keys = ("gpu_mem", "gpu_util", "gpu_temp", "gpu_power")
        if use_gpu and gpu_info.has_nvidia():
            for k in gpu_keys:
                if hasattr(L[k], "master"):
                    try:
                        L[k].master.pack(fill="x", padx=12, pady=2)
                    except Exception:
                        pass
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
                for k in gpu_keys:
                    L[k].configure(text=t("mon_na"))
        else:
            for k in gpu_keys:
                try:
                    L[k].master.pack_forget()
                except Exception:
                    pass

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

    def _setup_wheel(self) -> None:
        try:
            self.bind_all("<MouseWheel>", self._on_global_wheel, add="+")
        except Exception:
            pass

    def _log_text_widget(self):
        def find(w):
            if w.__class__.__name__ == "Text":
                return w
            for c in w.winfo_children():
                r = find(c)
                if r is not None:
                    return r
            return None

        return find(self.log)

    def _pointer_over_log(self, x, y) -> bool:
        w = self.winfo_containing(x, y)
        while w is not None:
            if w is self.log:
                return True
            w = getattr(w, "master", None)
        return False

    def _scroll_log_chained(self, units: int):
        """Scroll log; return 'edge' when at top/bottom so page can scroll."""
        text = self._log_text_widget()
        if text is None:
            return None
        try:
            first, last = text.yview()
        except Exception:
            return None
        if units < 0 and last >= 0.999:
            return "edge"
        if units > 0 and first <= 0.001:
            return "edge"
        try:
            text.yview_scroll(units, "units")
            return "ok"
        except Exception:
            return None

    def _on_global_wheel(self, event):
        # ignore wheel over other toplevels (settings, compact)
        try:
            top = self.winfo_toplevel()
            w = self.winfo_containing(event.x_root, event.y_root)
            while w is not None:
                if w is not top and isinstance(w, ctk.CTkToplevel):
                    return None
                w = getattr(w, "master", None)
        except Exception:
            pass

        units = int(-1 * (event.delta / 120))

        if self._pointer_over_log(event.x_root, event.y_root):
            res = self._scroll_log_chained(units)
            if res == "ok":
                return "break"
            if getattr(self._content, "_wheel_on", False):
                self._content.canvas.yview_scroll(units, "units")
            return "break"

        if getattr(self._content, "_wheel_on", False):
            w = self.winfo_containing(event.x_root, event.y_root)
            inside = False
            while w is not None:
                if w is self._content:
                    inside = True
                    break
                w = getattr(w, "master", None)
            if inside:
                self._content.canvas.yview_scroll(units, "units")
                return "break"
        return None

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
        self.btn_feedback = ctk.CTkButton(
            row,
            text=t("footer_feedback"),
            height=24,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            text_color=T["muted"],
            border_width=0,
            font=ctk.CTkFont(family=BODY, size=11),
            command=self.open_feedback,
        )
        self.btn_feedback.pack(side="left", padx=(0, 6))

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

    def open_feedback(self) -> None:
        FeedbackWindow(self)

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
        self.btn_feedback.configure(text=t("footer_feedback"))
        # system monitor
        try:
            self._mon_title.configure(text=t("mon_title"))
            titles = {
                "model": t("mon_model"),
                "gpu_mem": t("mon_gpu_mem"),
                "gpu_util": t("mon_gpu_util"),
                "gpu_temp": t("mon_gpu_temp"),
                "gpu_power": t("mon_gpu_power"),
                "cpu": t("mon_cpu_load"),
                "ram": t("mon_ram"),
            }
            for key, label in getattr(self, "_mon_title_labels", {}).items():
                if key in titles:
                    label.configure(text=titles[key])
            self._refresh_monitor()
        except Exception:
            pass
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

    def _proc_label(self) -> str:
        n = len(CFG.get("processes") or [])
        if n <= 0:
            return t("btn_processes")
        return f"{t('btn_processes')} ({n})"

    def refresh_processes_count(self) -> None:
        try:
            self.btn_proc.configure(text=self._proc_label())
        except Exception:
            pass

    def open_processes(self) -> None:
        if getattr(self, "proc_win", None) is not None and self.proc_win.winfo_exists():
            self.proc_win.lift()
            return
        self.proc_win = ProcessesWindow(self)

    def open_settings(self, hide_main: bool = False) -> None:
        if self.settings is not None and self.settings.winfo_exists():
            self.settings.lift()
            return
        win = SettingsWindow(self, from_compact=hide_main)
        self.settings = win

    def _to_tray(self) -> None:
        try:
            self.withdraw()
            if self.compact is not None and self.compact.winfo_exists():
                self.compact.withdraw()
        except Exception:
            pass
        self._ensure_tray()

    def _ensure_tray(self) -> None:
        if getattr(self, "_tray", None) is not None:
            return
        try:
            import pystray
            from PIL import Image

            icon_path = ICON_PATH
            try:
                pil = Image.open(icon_path)
            except Exception:
                pil = Image.new("RGB", (64, 64), (230, 69, 83))

            # default=True → left click on icon runs this item
            menu = pystray.Menu(
                pystray.MenuItem(
                    t("tray_open"),
                    lambda: self._from_tray(),
                    default=True,
                ),
                pystray.MenuItem(t("tray_exit"), lambda: self._really_exit()),
            )
            self._tray = pystray.Icon(
                "say_my_name",
                pil,
                t("app_title"),
                menu,
                # left click on tray icon opens the window
                on_activate=lambda _icon, _item=None: self._from_tray(),
            )
            threading.Thread(target=self._tray.run, daemon=True).start()
        except Exception:
            # no tray — close anyway
            self._really_exit()

    def _from_tray(self) -> None:
        try:
            self.deiconify()
            self.lift()
            self.focus_force()
            self._really_quit = False
        except Exception:
            pass
        # hide tray icon while window is visible
        self._hide_tray()

    def _hide_tray(self) -> None:
        tray = getattr(self, "_tray", None)
        if tray is None:
            return
        try:
            tray.stop()
        except Exception:
            pass
        self._tray = None

    def _really_exit(self) -> None:
        self._really_quit = True
        try:
            if getattr(self, "_tray", None):
                self._tray.stop()
        except Exception:
            pass
        self.on_close()

    def _save_geometry(self) -> None:
        try:
            if str(self.state()) != "normal":
                return
            CFG["win_main"] = {
                "x": self.winfo_x(),
                "y": self.winfo_y(),
                "w": self.winfo_width(),
                "h": self.winfo_height(),
            }
            save_config(CFG)
        except Exception:
            pass

    def on_close(self) -> None:
        if CFG.get("close_to_tray") and not getattr(self, "_really_quit", False):
            self._to_tray()
            return
        self._save_geometry()
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


class HuntOverlay:
    """Fullscreen crosshair overlay: click = capture exe, Esc = cancel.

    Click is swallowed — the window underneath is not activated.
    """

    def __init__(self, on_done) -> None:
        import tkinter as tk

        self.on_done = on_done
        self.win = tk.Toplevel()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.35)
        except Exception:
            pass
        self.win.configure(bg="#000000")
        # physical virtual-screen metrics — covers all monitors at any DPI
        try:
            import ctypes

            user32 = ctypes.windll.user32
            SM_XVIRTUALSCREEN = 76
            SM_YVIRTUALSCREEN = 77
            SM_CXVIRTUALSCREEN = 78
            SM_CYVIRTUALSCREEN = 79
            x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
            y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
            w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
            h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
        except Exception:
            x, y = 0, 0
            w = self.win.winfo_screenwidth()
            h = self.win.winfo_screenheight()
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.config(cursor="crosshair")

        lbl = tk.Label(
            self.win,
            text="◎",
            font=("Segoe UI", 48, "bold"),
            bg="#000000",
            fg="#ff2e2e",
        )
        lbl.place(relx=0.5, rely=0.45, anchor="center")
        tip = tk.Label(
            self.win,
            text=t("hunt_tip"),
            font=("Segoe UI", 12),
            bg="#000000",
            fg="#e8f0f4",
        )
        tip.place(relx=0.5, rely=0.55, anchor="center")

        # bind click on window AND all labels (labels swallow clicks otherwise)
        for wdg in (self.win, lbl, tip):
            wdg.bind("<Button-1>", self._click)
        self.win.bind("<Escape>", lambda e: self._finish(None))

        # also catch Esc globally while overlay is up
        try:
            self._esc_hook = keyboard.on_press_key("esc", lambda e: self._finish(None))
        except Exception:
            self._esc_hook = None

        try:
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
        except Exception:
            pass

    def _click(self, event) -> None:
        # remember point, hide overlay, resolve exe under that point
        x, y = event.x_root, event.y_root
        try:
            self.win.withdraw()
            self.win.update_idletasks()
            time.sleep(0.12)
        except Exception:
            pass
        exe = _exe_at(x, y)
        self._finish(exe)

    def _finish(self, exe: str | None) -> None:
        try:
            if self._esc_hook:
                keyboard.unhook(self._esc_hook)
        except Exception:
            pass
        try:
            self.win.destroy()
        except Exception:
            pass
        try:
            self.on_done(exe)
        except Exception:
            pass


class ProcessesWindow(ctk.CTkToplevel):
    """Per-process pre-keys for dictation."""

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self._from_compact = False
        self.title(t("processes_title"))
        self.geometry("520x520")
        self.minsize(520, 520)
        self.maxsize(520, 520)
        self.resizable(False, False)
        self.configure(fg_color=T["bg"])
        try:
            _mx, _my = app.winfo_x(), app.winfo_y()
            self.geometry(f"520x520+{_mx + 60}+{_my + 60}")
        except Exception:
            pass
        try:
            self.grab_set()
            self.transient(app)
        except Exception:
            pass

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build(self) -> None:
        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=20, pady=(18, 8))
        ctk.CTkLabel(
            head,
            text=t("processes_title"),
            font=ctk.CTkFont(family=DISPLAY, size=16, weight="bold"),
            text_color=T["text"],
        ).pack(side="left")

        self.btn_add = ctk.CTkButton(
            head,
            text=t("proc_add"),
            height=32,
            width=110,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            command=self._add_file,
        )
        self.btn_add.pack(side="right")

        self.btn_hunt = ctk.CTkButton(
            head,
            text=t("proc_hunt"),
            height=32,
            width=90,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            command=self._hunt,
        )
        self.btn_hunt.pack(side="right", padx=(0, 6))

        self.list_frame = ctk.CTkScrollableFrame(self, fg_color=T["bg"], corner_radius=0)
        self.list_frame.pack(fill="both", expand=True, padx=20, pady=(4, 8))

        ctk.CTkLabel(
            self,
            text=t("proc_hint"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
        ).pack(fill="x", padx=20, pady=(0, 4))

        btns = ctk.CTkFrame(self, fg_color=T["panel"], corner_radius=0)
        btns.pack(fill="x", side="bottom")
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
            command=self._close,
        ).pack(side="right", padx=(8, 20), pady=12)
        ctk.CTkButton(
            btns,
            text=t("set_save"),
            width=120,
            height=38,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            command=self._save,
        ).pack(side="right", padx=(0, 8), pady=12)

        self._rebuild_list()

    def _rebuild_list(self) -> None:
        self._proc_bodies = []
        self._row_widgets = {}
        for ch in self.list_frame.winfo_children():
            ch.destroy()
        procs = CFG.get("processes") or []
        if not procs:
            ctk.CTkLabel(
                self.list_frame,
                text=t("processes_empty"),
                text_color=T["faint"],
            ).pack(pady=30)
            return
        for i, proc in enumerate(procs):
            self._proc_row(i, proc)

    def _proc_row(self, i: int, proc: dict) -> None:
        """Accordion: closed = icon + name, open = full settings."""
        wrap = ctk.CTkFrame(self.list_frame, fg_color="transparent")
        wrap.pack(fill="x", pady=4)

        head = ctk.CTkFrame(
            wrap,
            fg_color=T["panel"],
            corner_radius=0,
            border_width=1,
            border_color=T["line"],
        )
        head.pack(fill="x")

        icon_lbl = ctk.CTkLabel(
            head, text="", width=28, height=28, fg_color="transparent"
        )
        icon_lbl.pack(side="left", padx=(10, 4), pady=8)
        try:
            icon_img = _exe_icon(proc.get("exe", ""))
            if icon_img is not None:
                icon_lbl.configure(image=icon_img)
                icon_lbl._icon = icon_img
        except Exception:
            pass

        name = proc.get("name") or Path(proc.get("exe", "")).name
        ctk.CTkLabel(
            head,
            text=name,
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["text"],
            anchor="w",
        ).pack(side="left", fill="x", expand=True, pady=8)

        mark = ctk.CTkLabel(
            head,
            text="+",
            width=18,
            font=ctk.CTkFont(family=DISPLAY, size=14, weight="bold"),
            text_color=T["muted"],
        )
        mark.pack(side="right", padx=(4, 12), pady=8)

        body = ctk.CTkFrame(wrap, fg_color=T["bg"], corner_radius=0)

        # --- body content ---
        key_row = ctk.CTkFrame(body, fg_color="transparent")
        key_row.pack(fill="x", padx=12, pady=(10, 4))

        btn_key = ctk.CTkButton(
            key_row,
            text=f"{t('proc_key')}: {proc.get('key') or '—'}",
            height=28,
            width=150,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=lambda idx=i: self._capture_key(idx),
        )
        btn_key.pack(side="left", padx=(0, 6))
        if not hasattr(self, "_row_widgets"):
            self._row_widgets = {}
        self._row_widgets[i] = {"btn_key": btn_key}

        mode_var = ctk.StringVar(value=proc.get("key_mode", "press"))
        r_press = ctk.CTkRadioButton(
            key_row,
            text=t("proc_key_press"),
            variable=mode_var,
            value="press",
            font=ctk.CTkFont(family=BODY, size=11),
            fg_color=T["red"],
            command=lambda idx=i, v=mode_var: self._set_key_mode(idx, v.get()),
        )
        r_press.pack(side="left", padx=(0, 4))
        r_hold = ctk.CTkRadioButton(
            key_row,
            text=t("proc_key_hold"),
            variable=mode_var,
            value="hold",
            font=ctk.CTkFont(family=BODY, size=11),
            fg_color=T["red"],
            command=lambda idx=i, v=mode_var: self._set_key_mode(idx, v.get()),
        )
        r_hold.pack(side="left")

        act_row = ctk.CTkFrame(body, fg_color="transparent")
        act_row.pack(fill="x", padx=12, pady=(0, 10))

        enabled = ctk.CTkCheckBox(
            act_row,
            text=t("proc_enabled"),
            corner_radius=0,
            fg_color=T["red"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=lambda idx=i: self._toggle_enabled(idx),
        )
        if proc.get("enabled", True):
            enabled.select()
        enabled.pack(side="left")

        ctk.CTkButton(
            act_row,
            text=t("proc_clear_key"),
            height=26,
            width=110,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=10),
            command=lambda idx=i: self._clear_key(idx),
        ).pack(side="right")

        ctk.CTkButton(
            act_row,
            text=t("proc_remove"),
            height=26,
            width=80,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
            command=lambda idx=i: self._remove(idx),
        ).pack(side="right", padx=6)

        # exclusive accordion (one open)
        def toggle() -> None:
            if body.winfo_ismapped():
                body.pack_forget()
                mark.configure(text="+")
                return
            # close others
            for other in getattr(self, "_proc_bodies", []):
                try:
                    other["body"].pack_forget()
                    other["mark"].configure(text="+")
                except Exception:
                    pass
            body.pack(fill="x", pady=(2, 4))
            mark.configure(text="−")

        # click anywhere on the header row opens the accordion
        def bind_all(w):
            try:
                w.bind("<Button-1>", lambda _e: toggle())
            except Exception:
                pass
            for child in w.winfo_children():
                bind_all(child)

        bind_all(head)

        if not hasattr(self, "_proc_bodies"):
            self._proc_bodies = []
        self._proc_bodies.append({"body": body, "mark": mark})

    def _set_key_mode(self, idx: int, mode: str) -> None:
        procs = CFG.get("processes") or []
        if 0 <= idx < len(procs):
            procs[idx]["key_mode"] = mode

    def _toggle_enabled(self, idx: int) -> None:
        procs = CFG.get("processes") or []
        if 0 <= idx < len(procs):
            procs[idx]["enabled"] = not bool(procs[idx].get("enabled", True))

    def _clear_key(self, idx: int) -> None:
        procs = CFG.get("processes") or []
        if 0 <= idx < len(procs):
            procs[idx]["key"] = ""
            self._patch_key_btn(idx)

    def _patch_key_btn(self, idx: int) -> None:
        btn = self._row_widgets.get(idx, {}).get("btn_key")
        if btn is None:
            return
        procs = CFG.get("processes") or []
        if 0 <= idx < len(procs):
            key = procs[idx].get("key") or "—"
            try:
                btn.configure(text=f"{t('proc_key')}: {key}")
            except Exception:
                pass

    def _capture_key(self, idx: int) -> None:
        """Listen once for key combo and store it."""
        if getattr(self, "_capturing", False):
            return
        self._capturing = True
        self.app.on_log("info", t("set_hotkey_capture"))

        import keyboard as kb

        pressed = set()

        def on_press(e):
            name = _norm_key(e.name)
            if name == "esc":
                self._capturing = False
                kb.unhook(hook)
                return
            pressed.add(name)
            # simple combo: modifiers + last key
            mods = {"ctrl", "alt", "shift", "windows"}
            if name in mods:
                return
            combo = "+".join(sorted(pressed)) if len(pressed) > 1 else name
            procs = CFG.get("processes") or []
            if 0 <= idx < len(procs):
                procs[idx]["key"] = combo
            self._capturing = False
            kb.unhook(hook)
            self._patch_key_btn(idx)

        hook = kb.on_press(on_press)

    def _add_file(self) -> None:
        from tkinter import filedialog

        path = filedialog.askopenfilename(
            title=t("proc_add"),
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        procs = CFG.get("processes") or []
        procs.append(
            {
                "exe": str(p),
                "name": p.name,
                "key": "",
                "key_mode": "press",
                "enabled": True,
            }
        )
        CFG["processes"] = procs
        self._rebuild_list()
        self.app.refresh_processes_count()

    def _hunt(self) -> None:
        """Fullscreen overlay: click window to capture exe."""
        if getattr(self, "_hunting", False):
            return
        self._hunting = True
        self.app.on_log("info", "hunt start")
        # modal grab would eat overlay clicks
        try:
            self.grab_release()
        except Exception:
            pass

        def done(exe):
            self._hunting = False
            try:
                self.grab_set()
            except Exception:
                pass
            if not exe:
                self.app.on_log("warn", t("processes_empty"))
                return
            pth = Path(exe)
            procs = CFG.get("processes") or []
            for pr in procs:
                if pr.get("exe", "").lower() == str(pth).lower():
                    self._rebuild_list()
                    return
            procs.append(
                {
                    "exe": str(pth),
                    "name": pth.name,
                    "key": "",
                    "key_mode": "press",
                    "enabled": True,
                }
            )
            CFG["processes"] = procs
            self._rebuild_list()
            self.app.refresh_processes_count()

        try:
            HuntOverlay(done)
        except Exception as e:
            self._hunting = False
            self.app.on_log("error", f"hunt: {e}")

    def _remove(self, idx: int) -> None:
        procs = CFG.get("processes") or []
        if 0 <= idx < len(procs):
            procs.pop(idx)
            CFG["processes"] = procs
            self._rebuild_list()
            self.app.refresh_processes_count()

    def _save(self) -> None:
        save_config(CFG)
        self.app.refresh_processes_count()
        self.app.on_log("ok", t("set_saved"))
        if CFG.get("tr_enable"):
            name = (
                CFG.get("tr_local_model")
                if CFG.get("tr_mode") == "local"
                else CFG.get("tr_provider")
            )
            self.app.on_log("info", t("log_tr_model", name=name or "-"))
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()
        self.app.proc_win = None


class SettingsWindow(ctk.CTkToplevel):
    def __init__(self, app: App, from_compact: bool = False) -> None:
        super().__init__(app)
        self.app = app
        self._from_compact = from_compact
        self.title(t("settings_title"))
        # 125% DPI: 440×640 logical → 550×800 physical (height = main window)
        self.geometry("480x680")
        self.minsize(480, 680)
        self.maxsize(480, 680)
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
        try:
            _mx, _my = app.winfo_x(), app.winfo_y()
            self.geometry(f"440x640+{_mx + 40}+{_my + 40}")
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
        self._bind_settings_wheel()
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

        # silence auto-stop — only meaningful with Toggle
        self.chk_silence = ctk.CTkCheckBox(
            row_t,
            text=t("silence_stop"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.chk_silence.pack(anchor="w", padx=(28, 0), pady=(2, 0))
        if CFG.get("silence_stop"):
            self.chk_silence.select()

        def _sync_silence() -> None:
            # disable for Hold
            if self._mode_var.get() == "hold":
                self.chk_silence.configure(state="disabled", text_color=T["faint"])
            else:
                self.chk_silence.configure(state="normal", text_color=T["text"])

        self._mode_var.trace_add("write", lambda *_: _sync_silence())
        _sync_silence()

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

        # 1) model select
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

        # 2) status + download / delete
        d_row = ctk.CTkFrame(mod, fg_color="transparent")
        d_row.pack(fill="x", padx=14, pady=(6, 2))
        self.d_row = d_row
        self.lbl_model_state = ctk.CTkLabel(
            d_row, text="", anchor="w", font=ctk.CTkFont(family=BODY, size=11)
        )
        self.lbl_model_state.pack(side="left")

        self.btn_dl = ctk.CTkButton(
            d_row,
            text=t("set_download"),
            width=100,
            height=28,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._start_download,
        )
        self.btn_dl.pack(side="right", padx=(6, 0))

        self.btn_del_model = ctk.CTkButton(
            d_row,
            text=t("set_delete"),
            width=80,
            height=28,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._delete_model,
        )
        self.btn_del_model.pack(side="right", padx=(0, 6))

        # 3) progress
        self.pbar = ctk.CTkProgressBar(
            mod, height=4, corner_radius=0, progress_color=T["red"]
        )
        self.pbar.pack(fill="x", padx=14, pady=(4, 2))
        self.pbar.set(0)
        self.lbl_dl = ctk.CTkLabel(
            mod, text="", anchor="w", font=ctk.CTkFont(family=BODY, size=11)
        )
        self.lbl_dl.pack(fill="x", padx=14, pady=(0, 4))

        # 4) vram estimate
        self.lbl_vram = ctk.CTkLabel(
            mod,
            text="",
            anchor="w",
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=11),
        )
        self.lbl_vram.pack(fill="x", padx=14, pady=(2, 2))

        # 5) device
        ctk.CTkLabel(
            mod,
            text=t("set_device"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(10, 4))

        self._device_var = ctk.StringVar(value=CFG.get("device_choice", "cpu"))
        nvidia = gpu_info.has_nvidia()
        gpu_label = f'{t("device_gpu")} · {gpu_info.gpu_name() or "NVIDIA"}'
        cpu_label = f'{t("device_cpu")} · {gpu_info.cpu_name()}'
        self.rad_gpu = ctk.CTkRadioButton(
            mod,
            text=gpu_label,
            variable=self._device_var,
            value="gpu",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_gpu.pack(anchor="w", padx=14, pady=2)
        self.rad_cpu = ctk.CTkRadioButton(
            mod,
            text=cpu_label,
            variable=self._device_var,
            value="cpu",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["text"],
            fg_color=T["red"],
        )
        self.rad_cpu.pack(anchor="w", padx=14, pady=2)

        def _on_device_change() -> None:
            self.lbl_vram.configure(text=_vram_text())

        self._device_var.trace_add("write", lambda *_: _on_device_change())

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
            if self._device_var.get() == "cpu":
                return t("set_model_vram_cpu")
            name = self.cmb_model.get()
            est = gpu_info.VRAM_ESTIMATE_GB.get(name, 1.0)
            return f"{t('set_model_vram')}: ~{est:g} {t('mon_gb')}"

        self.lbl_vram.configure(text=_vram_text())

        def _on_model_change2(choice: str) -> None:
            self._on_model_change(choice)
            self.lbl_vram.configure(text=_vram_text())

        self.cmb_model.configure(command=_on_model_change2)

        # 6) path + open folder
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

        # --- 1.5 Audio ---
        audio = accordion("audio", t("set_audio"))
        ctk.CTkLabel(
            audio,
            text=t("set_mic"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(12, 4))

        def _mic_devices() -> list:
            out = [(t("mic_default"), "")]
            try:
                import sounddevice as sd

                for i, d in enumerate(sd.query_devices()):
                    if d.get("max_input_channels", 0) > 0:
                        name = f"{i}: {d.get('name', f'Device {i}')}"
                        out.append((name, str(i)))
            except Exception:
                pass
            return out

        mic_pairs = _mic_devices()
        self._mic_names = [n for n, _ in mic_pairs]
        self._mic_values = {n: v for n, v in mic_pairs}
        cur_mic = CFG.get("mic", "")
        cur_label = t("mic_default")
        for name, val in mic_pairs:
            if val == cur_mic:
                cur_label = name
                break

        self.cmb_mic = ctk.CTkOptionMenu(
            audio,
            values=self._mic_names,
            width=320,
            corner_radius=0,
            fg_color=T["panel"],
            button_color=T["line"],
            button_hover_color="#35424E",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.cmb_mic.set(cur_label)
        self.cmb_mic.pack(fill="x", padx=14, pady=(0, 12))

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

        self.chk_tray = ctk.CTkCheckBox(
            rec,
            text=t("close_to_tray"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("close_to_tray"):
            self.chk_tray.select()
        self.chk_tray.pack(anchor="w", padx=14, pady=6)

        self.chk_autostart = ctk.CTkCheckBox(
            rec,
            text=t("autostart_win"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("autostart_win"):
            self.chk_autostart.select()
        self.chk_autostart.pack(anchor="w", padx=14, pady=6)
        ctk.CTkLabel(
            rec,
            text=t("autostart_note"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=28, pady=(0, 6))

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

        # --- 5. Journal ---
        journal = accordion("journal", t("set_journal"))
        self.chk_journal = ctk.CTkCheckBox(
            journal,
            text=t("journal_enable"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("journal_enable", True):
            self.chk_journal.select()
        self.chk_journal.pack(anchor="w", padx=14, pady=(12, 4))
        ctk.CTkLabel(
            journal,
            text=t("journal_note"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=14, pady=(0, 12))

        # --- 4.5 Translation ---
        tr = accordion("translation", t("set_translation"))

        self.chk_tr_enable = ctk.CTkCheckBox(
            tr,
            text=t("tr_enable"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("tr_enable"):
            self.chk_tr_enable.select()
        self.chk_tr_enable.pack(anchor="w", padx=14, pady=(12, 4))

        # show install hint only when torch is missing
        try:
            import torch  # noqa: F401

            torch_ok = True
        except Exception:
            torch_ok = False
        if not torch_ok:
            warn_row = ctk.CTkFrame(tr, fg_color="transparent")
            warn_row.pack(fill="x", padx=14, pady=(2, 6))
            ctk.CTkLabel(
                warn_row,
                text=t("tr_torch_warn"),
                text_color=T["amber"],
                font=ctk.CTkFont(family=BODY, size=10),
                wraplength=300,
                justify="left",
                anchor="w",
            ).pack(side="left", fill="x", expand=True)
            ctk.CTkButton(
                warn_row,
                text=t("tr_torch_install"),
                width=90,
                height=26,
                corner_radius=0,
                fg_color=T["red"],
                hover_color="#D92222",
                text_color="#FFFFFF",
                font=ctk.CTkFont(family=BODY, size=11),
                command=self._install_torch,
            ).pack(side="right", padx=(8, 0))

        ctk.CTkLabel(
            tr,
            text=t("tr_provider"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(10, 4))

        self._tr_mode = ctk.StringVar(value=CFG.get("tr_mode", "api"))
        self._tr_mode_rbs = []
        r_api = ctk.CTkRadioButton(
            tr,
            text=t("tr_mode_api"),
            variable=self._tr_mode,
            value="api",
            font=ctk.CTkFont(family=BODY, size=12),
            fg_color=T["red"],
            command=self._sync_tr_mode,
        )
        r_api.pack(anchor="w", padx=14, pady=2)
        self._tr_mode_rbs.append(r_api)
        r_local = ctk.CTkRadioButton(
            tr,
            text=t("tr_mode_local"),
            variable=self._tr_mode,
            value="local",
            font=ctk.CTkFont(family=BODY, size=12),
            fg_color=T["red"],
            command=self._sync_tr_mode,
        )
        r_local.pack(anchor="w", padx=14, pady=(2, 8))
        self._tr_mode_rbs.append(r_local)

        # placeholder sections — filled by T18 / T19
        self.tr_api_box = ctk.CTkFrame(tr, fg_color="transparent")
        self.tr_local_box = ctk.CTkFrame(tr, fg_color="transparent")
        self._build_tr_api(self.tr_api_box)
        self._build_tr_local(self.tr_local_box)
        self._sync_tr_mode()
        self._tr_boxes = [self.tr_api_box, self.tr_local_box]
        self.chk_tr_enable.configure(
            command=self._sync_tr_enable
        )
        self._sync_tr_enable()

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

    def _bind_settings_wheel(self) -> None:
        """Scroll settings with mouse wheel; do not scroll past content edges."""

        def _on_wheel(event):
            # only when pointer is inside this settings window
            w = self.winfo_containing(event.x_root, event.y_root)
            inside = False
            while w is not None:
                if w is self:
                    inside = True
                    break
                w = getattr(w, "master", None)
            if not inside:
                return None

            units = int(-1 * (event.delta / 120))
            # find scrollable canvas
            target = self.winfo_containing(event.x_root, event.y_root)
            canvas = None
            w = target
            while w is not None:
                if isinstance(w, ctk.CTkScrollableFrame):
                    try:
                        canvas = w._parent_canvas
                    except Exception:
                        canvas = None
                    break
                w = getattr(w, "master", None)
            if canvas is None:
                return None

            try:
                first, last = canvas.yview()
            except Exception:
                return None
            # clamp — no empty space above or below
            if units < 0 and last >= 0.999:
                return "break"
            if units > 0 and first <= 0.001:
                return "break"
            try:
                canvas.yview_scroll(units, "units")
            except Exception:
                pass
            return "break"

        self.bind_all("<MouseWheel>", _on_wheel, add="+")

    def _build_tr_api(self, box) -> None:
        ctk.CTkLabel(
            box,
            text=t("tr_provider"),
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(4, 2))

        provs = [
            ("deepl", t("tr_prov_deepl")),
            ("openai", t("tr_prov_openai")),
            ("claude", t("tr_prov_claude")),
            ("gemini", t("tr_prov_gemini")),
            ("openrouter", t("tr_prov_openrouter")),
            ("mimo", t("tr_prov_mimo")),
            ("custom", t("tr_prov_custom")),
        ]
        self._tr_prov_ids = [k for k, _ in provs]
        self._tr_prov_names = {k: n for k, n in provs}

        self.cmb_tr_prov = ctk.CTkOptionMenu(
            box,
            values=[n for _, n in provs],
            width=220,
            corner_radius=0,
            fg_color=T["panel"],
            button_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._on_tr_prov,
        )
        cur = CFG.get("tr_provider", "deepl")
        if cur not in self._tr_prov_ids:
            cur = "deepl"
        self.cmb_tr_prov.set(self._tr_prov_names[cur])
        self.cmb_tr_prov.pack(anchor="w", padx=14, pady=(0, 6))

        # Base URL (custom / openai-compatible)
        self.lbl_base = ctk.CTkLabel(
            box,
            text=t("tr_base_url"),
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        )
        self.ent_base = ctk.CTkEntry(
            box,
            placeholder_text=t("tr_base_url_ph"),
            corner_radius=0,
            fg_color=T["panel"],
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.ent_base.insert(0, CFG.get("tr_base_url", ""))
        self.ent_base.bind("<FocusOut>", lambda _e: self._tr_url_visible())

        self.lbl_model = ctk.CTkLabel(
            box,
            text=t("tr_model_name"),
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        )
        self.ent_tr_model = ctk.CTkEntry(
            box,
            placeholder_text=t("tr_model_name_ph"),
            corner_radius=0,
            fg_color=T["panel"],
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        self.ent_tr_model.insert(0, CFG.get("tr_model", ""))

        ctk.CTkLabel(
            box,
            text=t("tr_api_key"),
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(6, 2))
        self.ent_tr_key = ctk.CTkEntry(
            box,
            placeholder_text=t("tr_api_key_ph"),
            corner_radius=0,
            fg_color=T["panel"],
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            show="*",
        )
        self.ent_tr_key.insert(0, CFG.get("tr_api_key", ""))
        self.ent_tr_key.pack(fill="x", padx=14, pady=(0, 8))

        # test row
        t_row = ctk.CTkFrame(box, fg_color="transparent")
        t_row.pack(fill="x", padx=14, pady=(0, 6))
        self.btn_tr_test = ctk.CTkButton(
            t_row,
            text=t("tr_test"),
            width=90,
            height=28,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._test_tr_api,
        )
        self.btn_tr_test.pack(side="left")
        self.lbl_tr_test = ctk.CTkLabel(
            t_row, text="", anchor="w", font=ctk.CTkFont(family=BODY, size=11)
        )
        self.lbl_tr_test.pack(side="left", padx=10)

        ctk.CTkLabel(
            box,
            text=t("tr_lang_to"),
            anchor="w",
            font=ctk.CTkFont(family=BODY, size=12),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(6, 2))
        self.cmb_tr_lang = ctk.CTkOptionMenu(
            box,
            values=[t("tr_lang_auto"), "EN", "RU"],
            width=160,
            corner_radius=0,
            fg_color=T["panel"],
            button_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        lang = CFG.get("tr_lang_to", "auto")
        if lang == "en":
            self.cmb_tr_lang.set("EN")
        elif lang == "ru":
            self.cmb_tr_lang.set("RU")
        else:
            self.cmb_tr_lang.set(t("tr_lang_auto"))
        self.cmb_tr_lang.pack(anchor="w", padx=14, pady=(0, 8))


        self._tr_url_visible()

    def _current_tr_prov(self) -> str:
        name = self.cmb_tr_prov.get()
        for k, n in self._tr_prov_names.items():
            if n == name:
                return k
        return "deepl"

    def _tr_url_visible(self) -> None:
        prov = self._current_tr_prov()
        # base url for openai-style (openai/custom/openrouter/claude/gemini)
        show = prov in ("custom", "openai", "openrouter", "claude", "gemini")
        # model name for LLM providers
        show_model = prov in ("custom", "openai", "openrouter", "claude", "gemini", "mimo")
        if show:
            self.lbl_base.pack(fill="x", padx=14, pady=(6, 2))
            self.ent_base.pack(fill="x", padx=14, pady=(0, 6))
        else:
            self.lbl_base.pack_forget()
            self.ent_base.pack_forget()
        if show_model:
            self.lbl_model.pack(fill="x", padx=14, pady=(2, 2))
            self.ent_tr_model.pack(fill="x", padx=14, pady=(0, 6))
        else:
            self.lbl_model.pack_forget()
            self.ent_tr_model.pack_forget()

    def _on_tr_prov(self, _name: str) -> None:
        self._tr_url_visible()

    def _test_tr_api(self) -> None:
        self.lbl_tr_test.configure(text="…", text_color=T["muted"])
        cfg = self._tr_cfg_snapshot()

        def worker() -> None:
            try:
                import mt_translate

                mt_translate.translate_api("hello", cfg)
                msg, color = t("tr_test_ok"), T["muted"]
            except Exception as e:
                msg, color = t("tr_test_fail", err=str(e)), T["faint"]

            def done() -> None:
                self.lbl_tr_test.configure(text=msg, text_color=color)

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _tr_cfg_snapshot(self) -> dict:
        lang = self.cmb_tr_lang.get()
        if lang == "EN":
            lang_v = "en"
        elif lang == "RU":
            lang_v = "ru"
        else:
            lang_v = "auto"
        return {
            "tr_enable": True,
            "tr_mode": "api",
            "tr_provider": self._current_tr_prov(),
            "tr_api_key": self.ent_tr_key.get().strip(),
            "tr_base_url": self.ent_base.get().strip(),
            "tr_model": self.ent_tr_model.get().strip(),
            "tr_lang_to": lang_v,
        }

    def _build_tr_local(self, box) -> None:
        ctk.CTkLabel(
            box,
            text=t("tr_local_desc"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=11),
            wraplength=380,
            justify="left",
        ).pack(fill="x", padx=14, pady=(4, 6))

        ids = [m["id"] for m in mt_models.MT_MODELS]
        cur = CFG.get("tr_local_model", ids[0])
        if cur not in ids:
            cur = ids[0]

        self.cmb_tr_model = ctk.CTkOptionMenu(
            box,
            values=ids,
            width=280,
            corner_radius=0,
            fg_color=T["panel"],
            button_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._on_tr_model,
        )
        self.cmb_tr_model.set(cur)
        self.cmb_tr_model.pack(anchor="w", padx=14, pady=(0, 6))

        self.lbl_tr_desc = ctk.CTkLabel(
            box,
            text="",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            wraplength=300,
            justify="left",
            anchor="w",
        )
        self.lbl_tr_desc.pack(fill="x", padx=14, pady=(0, 4))

        st_row = ctk.CTkFrame(box, fg_color="transparent")
        st_row.pack(fill="x", padx=14, pady=(2, 4))
        self.lbl_tr_state = ctk.CTkLabel(
            st_row, text="", anchor="w", font=ctk.CTkFont(family=BODY, size=11)
        )
        self.lbl_tr_state.pack(side="left")
        self.btn_tr_dl = ctk.CTkButton(
            st_row,
            text=t("set_download"),
            width=100,
            height=28,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._download_tr_model,
        )
        self.btn_tr_dl.pack(side="right", padx=(6, 0))
        self.btn_tr_del = ctk.CTkButton(
            st_row,
            text=t("set_delete"),
            width=80,
            height=28,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["line"],
            border_width=1,
            border_color=T["line"],
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=self._delete_tr_model,
        )
        self.btn_tr_del.pack(side="right", padx=(0, 6))
        self.pbar_tr = ctk.CTkProgressBar(box, height=4, corner_radius=0, progress_color=T["red"])
        self.pbar_tr.pack(fill="x", padx=14, pady=(2, 6))
        self.pbar_tr.set(0)

        ctk.CTkLabel(
            box,
            text=t("set_open_folder"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(8, 4))
        p_row = ctk.CTkFrame(box, fg_color="transparent")
        p_row.pack(fill="x", padx=14, pady=(0, 6))
        ctk.CTkLabel(
            p_row,
            text=str(mt_models.hf_cache_dir()),
            anchor="w",
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=10),
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            p_row,
            text=t("set_open_folder"),
            width=110,
            height=28,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=11),
            command=lambda: self._open_tr_folder(),
        ).pack(side="right")

        # device
        ctk.CTkLabel(
            box,
            text=t("tr_device"),
            anchor="w",
            font=ctk.CTkFont(family=DISPLAY, size=12, weight="bold"),
            text_color=T["muted"],
        ).pack(fill="x", padx=14, pady=(8, 4))
        self._tr_dev = ctk.StringVar(value=CFG.get("tr_device", "cpu"))
        d_row = ctk.CTkFrame(box, fg_color="transparent")
        d_row.pack(fill="x", padx=14, pady=(0, 10))
        gpu_lbl = f'{t("device_gpu")} · {gpu_info.gpu_name() or "NVIDIA"}'
        cpu_lbl = f'{t("device_cpu")} · {gpu_info.cpu_name()}'
        for val, lbl in (("gpu", gpu_lbl), ("cpu", cpu_lbl)):
            r = ctk.CTkRadioButton(
                d_row,
                text=lbl,
                variable=self._tr_dev,
                value=val,
                font=ctk.CTkFont(family=BODY, size=12),
                fg_color=T["red"],
            )
            r.pack(anchor="w", pady=1)

        # keep-in-memory — local MT only
        self.chk_tr_keep = ctk.CTkCheckBox(
            box,
            text=t("tr_keep_model"),
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color=T["text"],
            font=ctk.CTkFont(family=BODY, size=12),
        )
        if CFG.get("tr_keep_model"):
            self.chk_tr_keep.select()
        self.chk_tr_keep.pack(anchor="w", padx=14, pady=(10, 2))
        ctk.CTkLabel(
            box,
            text=t("tr_keep_hint"),
            text_color=T["faint"],
            font=ctk.CTkFont(family=BODY, size=10),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=28, pady=(0, 8))

        self._refresh_tr_model(cur)

    def _on_tr_model(self, model_id: str) -> None:
        self._refresh_tr_model(model_id)

    def _refresh_tr_model(self, model_id: str) -> None:
        m = mt_models.get(model_id)
        if not m:
            return
        lang = i18n.get_lang()
        self.lbl_tr_desc.configure(text=m.get(lang) or m.get("en", ""))
        ok = mt_models.is_downloaded(model_id)
        if ok:
            try:
                sz = mt_models.model_size_label(model_id)
                state_txt = f"✓ {t('set_model_dl')} · {sz}"
            except Exception:
                state_txt = f"✓ {t('set_model_dl')}"
            self.lbl_tr_state.configure(text=state_txt, text_color=T["muted"])
            self.btn_tr_dl.pack_forget()
            if not self.btn_tr_del.winfo_ismapped():
                self.btn_tr_del.pack(side="right", padx=(0, 6))
        else:
            self.btn_tr_del.pack_forget()
            self.lbl_tr_state.configure(
                text=f"✗ {t('set_model_not_dl')} (~{m['gb']} GB)",
                text_color=T["amber"],
            )
            if not self.btn_tr_dl.winfo_ismapped():
                self.btn_tr_dl.pack(side="right", padx=(6, 0))

    def _delete_tr_model(self) -> None:
        mid = self.cmb_tr_model.get()
        try:
            mt_models.delete_model(mid)
            self._refresh_tr_model(mid)
            self.app.on_log("info", f"{t('set_delete')}: {mid}")
        except Exception as e:
            self.app.on_log("error", str(e))

    def _open_tr_folder(self) -> None:
        path = mt_models.hf_cache_dir()
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except Exception:
            pass

    def _download_tr_model(self) -> None:
        mid = self.cmb_tr_model.get()
        m = mt_models.get(mid)
        if not m:
            return
        self.btn_tr_dl.configure(state="disabled")
        self.lbl_tr_state.configure(text=t("set_downloading", pct=0), text_color=T["amber"])

        def worker() -> None:
            try:
                from huggingface_hub import snapshot_download

                snapshot_download(m["repo"])
                ok, err = True, None
            except Exception as e:
                ok, err = False, str(e)

            def done() -> None:
                self._refresh_tr_model(mid)
                if ok:
                    self.lbl_tr_state.configure(
                        text=t("set_download_done"), text_color=T["muted"]
                    )
                else:
                    self.lbl_tr_state.configure(
                        text=t("set_download_fail", err=err), text_color=T["faint"]
                    )

            try:
                self.after(0, done)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _sync_tr_mode(self) -> None:
        mode = self._tr_mode.get()
        for w in list(self.tr_api_box.winfo_children()):
            w.destroy()
        for w in list(self.tr_local_box.winfo_children()):
            w.destroy()
        if mode == "api":
            self.tr_local_box.pack_forget()
            self._build_tr_api(self.tr_api_box)
            self.tr_api_box.pack(fill="x", pady=(0, 8))
        else:
            self.tr_api_box.pack_forget()
            self._build_tr_local(self.tr_local_box)
            self.tr_local_box.pack(fill="x", pady=(0, 8))
        try:
            self._sync_tr_enable()
        except Exception:
            pass

    def _sync_tr_enable(self, *_a) -> None:
        """Gray out all translation controls when translate is off."""
        on = True
        try:
            on = bool(self.chk_tr_enable.get())
        except Exception:
            on = True

        state = "normal" if on else "disabled"
        dim = T["muted"] if on else T["faint"]

        # mode radios live outside the boxes — store them at build time
        for wdg in getattr(self, "_tr_mode_rbs", []):
            try:
                wdg.configure(state=state)
            except Exception:
                pass

        def walk(widget):
            for c in widget.winfo_children():
                try:
                    cls = type(c).__name__
                    if "RadioButton" in cls:
                        c.configure(state=state)
                    elif "Entry" in cls:
                        c.configure(state=state)
                    elif "OptionMenu" in cls:
                        c.configure(state=state)
                    elif "Button" in cls:
                        c.configure(state=state)
                    elif "CheckBox" in cls and c is not self.chk_tr_enable:
                        c.configure(state=state)
                    elif "Label" in cls:
                        cur = c.cget("text_color")
                        if cur in (T["muted"], T["faint"]):
                            c.configure(text_color=dim)
                except Exception:
                    pass
                walk(c)

        for box in getattr(self, "_tr_boxes", []):
            try:
                walk(box)
            except Exception:
                pass

    def _install_torch(self) -> None:
        """Launch install_torch.bat next to the app root."""
        try:
            bat = APP_DIR.parent / "install_torch.bat"
            if bat.exists():
                subprocess.Popen(
                    ["cmd", "/c", str(bat)],
                    cwd=str(APP_DIR.parent),
                    creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                )
            else:
                self.app.on_log("error", "install_torch.bat not found")
        except Exception as e:
            self.app.on_log("error", str(e))

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
        color = T["muted"] if ok else T["amber"]
        if ok:
            try:
                size_txt = mm.model_size_label(size)
                label = f"✓ {t('set_model_dl')} · {size_txt}"
            except Exception:
                label = f"✓ {t('set_model_dl')}"
        else:
            label = f"✗ {t('set_model_not_dl')}"
        self.lbl_model_state.configure(text=label, text_color=color)
        if ok:
            self.btn_dl.pack_forget()
            self.btn_del_model.pack(side="right", padx=(0, 6))
            # keep progress bar in place (under status row), just full
            try:
                self.pbar.pack(fill="x", padx=14, pady=(4, 2), after=self.d_row)
                self.pbar.set(1)
                self.lbl_dl.pack(fill="x", padx=14, pady=(0, 4), after=self.pbar)
                self.lbl_dl.configure(text="")
            except Exception:
                pass
        else:
            self.btn_del_model.pack_forget()
            self.btn_dl.pack(side="right", padx=(6, 0))
            try:
                self.pbar.pack(fill="x", padx=14, pady=(4, 2), after=self.d_row)
                self.pbar.set(0)
                self.lbl_dl.pack(fill="x", padx=14, pady=(0, 4), after=self.pbar)
            except Exception:
                pass
        # VRAM estimate always visible after download row
        try:
            self.lbl_vram.pack(fill="x", padx=14, pady=(2, 2), after=self.lbl_dl)
        except Exception:
            pass

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

    def _delete_model(self) -> None:
        size = self.cmb_model.get()
        try:
            mm.delete_model(size)
            self._refresh_model_status()
            self.app.on_log("info", f"{t('set_delete')}: {size}")
        except Exception as e:
            self.app.on_log("error", str(e))

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
        old_device = CFG.get("device_choice")
        CFG["device_choice"] = self._device_var.get()
        CFG["mic"] = self._mic_values.get(self.cmb_mic.get(), "")
        CFG["silence_stop"] = bool(self.chk_silence.get()) if self._mode_var.get() != "hold" else False
        CFG["close_to_tray"] = bool(self.chk_tray.get())
        CFG["autostart_win"] = bool(self.chk_autostart.get())
        CFG["journal_enable"] = bool(self.chk_journal.get())
        if hasattr(self, "chk_tr_enable"):
            CFG["tr_enable"] = bool(self.chk_tr_enable.get())
            CFG["tr_mode"] = self._tr_mode.get()
            if CFG["tr_mode"] == "api":
                snap = self._tr_cfg_snapshot()
                CFG["tr_api_key"] = snap["tr_api_key"]
                CFG["tr_base_url"] = snap.get("tr_base_url", "")
                CFG["tr_model"] = snap.get("tr_model", "")
                CFG["tr_lang_to"] = snap["tr_lang_to"]
                CFG["tr_provider"] = snap["tr_provider"]
            if hasattr(self, "cmb_tr_model"):
                CFG["tr_local_model"] = self.cmb_tr_model.get()
            if hasattr(self, "_tr_dev"):
                CFG["tr_device"] = self._tr_dev.get()
            if hasattr(self, "chk_tr_keep"):
                CFG["tr_keep_model"] = bool(self.chk_tr_keep.get())
        _set_autostart(CFG["autostart_win"])
        if old_device != CFG["device_choice"]:
            dev_name = (
                gpu_info.gpu_name() or "GPU"
                if CFG["device_choice"] == "gpu"
                else gpu_info.cpu_name()
            )
            self.app.on_log("info", t("log_device_changed", device=dev_name))
            try:
                # reload model on device change
                _state["model"] = None
                _state["model_name_loaded"] = None
                threading.Thread(target=_load_model, daemon=True).start()
            except Exception:
                pass
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


class FeedbackWindow(ctk.CTkToplevel):
    """Choose Bug / Feature — opens GitHub issue with template."""

    REPO = "https://github.com/Vidrimers/Say-my-name/issues/new"

    def __init__(self, app: "App") -> None:
        super().__init__(app)
        self.app = app
        self.title(t("feedback_title"))
        self.geometry("380x240")
        self.resizable(False, False)
        self.configure(fg_color=T["bg"])
        try:
            self.transient(app)
            self.grab_set()
        except Exception:
            pass

        ctk.CTkLabel(
            self,
            text=t("feedback_title"),
            font=ctk.CTkFont(family=DISPLAY, size=16, weight="bold"),
            text_color=T["text"],
        ).pack(padx=24, pady=(22, 8), anchor="w")

        ctk.CTkLabel(
            self,
            text=t("feedback_note"),
            font=ctk.CTkFont(family=BODY, size=11),
            text_color=T["muted"],
        ).pack(fill="x", padx=24, pady=(0, 16))

        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=24, pady=(0, 12))

        ctk.CTkButton(
            row,
            text=t("feedback_bug"),
            height=42,
            corner_radius=0,
            fg_color=T["red"],
            hover_color="#D92222",
            text_color="#FFFFFF",
            font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
            command=lambda: self._open("bug"),
        ).pack(side="left", expand=True, fill="x", padx=(0, 6))

        ctk.CTkButton(
            row,
            text=t("feedback_feature"),
            height=42,
            corner_radius=0,
            fg_color=T["panel"],
            hover_color="#243038",
            border_width=1,
            border_color=T["line"],
            text_color=T["text"],
            font=ctk.CTkFont(family=DISPLAY, size=13, weight="bold"),
            command=lambda: self._open("feature"),
        ).pack(side="left", expand=True, fill="x")

        ctk.CTkButton(
            self,
            text=t("support_close"),
            width=100,
            height=34,
            corner_radius=0,
            fg_color="transparent",
            hover_color=T["panel"],
            border_width=1,
            border_color=T["line"],
            text_color=T["muted"],
            font=ctk.CTkFont(family=BODY, size=12),
            command=self._close,
        ).pack(pady=(4, 16))

    def _open(self, kind: str) -> None:
        import platform
        import urllib.parse
        import webbrowser

        lang = i18n.get_lang()
        ver = app_version.__version__
        win = platform.platform()
        dev = (
            gpu_info.gpu_name()
            if CFG.get("device_choice", "cpu") == "gpu"
            else gpu_info.cpu_name()
        )
        model = CFG.get("model", "small")

        if lang == "ru":
            env = (
                f"Версия: {ver}\n"
                f"Windows: {win}\n"
                f"Экран: {gpu_info.screen_info()}\n"
                f"Устройство: {dev}\n"
                f"Модель: {model}\n"
            )
            if kind == "bug":
                title = "[Bug] "
                body = (
                    f"{env}\n"
                    "**В чём ошибка**\n"
                    "[опишите, что пошло не так]\n\n"
                    "**Что ожидали**\n"
                    "[как должно быть]\n\n"
                    "**Шаги воспроизведения**\n"
                    "1. \n2. \n3. \n"
                )
            else:
                title = "[Feature] "
                body = (
                    "**Идея**\n"
                    "[что добавить или изменить]\n\n"
                    "**Зачем**\n"
                    "[какую проблему решает]\n\n"
                    "**Как представляете**\n"
                    "[по желанию: как это должно выглядеть]\n"
                )
        else:
            env = (
                f"Version: {ver}\n"
                f"Windows: {win}\n"
                f"Screen: {gpu_info.screen_info()}\n"
                f"Device: {dev}\n"
                f"Model: {model}\n"
            )
            if kind == "bug":
                title = "[Bug] "
                body = (
                    f"{env}\n"
                    "**What went wrong**\n"
                    "[describe the problem]\n\n"
                    "**What I expected**\n"
                    "[what should have happened]\n\n"
                    "**Steps to reproduce**\n"
                    "1. \n2. \n3. \n"
                )
            else:
                title = "[Feature] "
                body = (
                    "**Idea**\n"
                    "[what to add or change]\n\n"
                    "**Why**\n"
                    "[what problem this solves]\n\n"
                    "**How you imagine it**\n"
                    "[optional: how it should look]\n"
                )

        url = (
            "https://github.com/Vidrimers/Say-my-name/issues/new"
            f"?title={urllib.parse.quote(title)}"
            f"&body={urllib.parse.quote(body)}"
        )
        webbrowser.open(url)
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


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

    if not _acquire_single_instance():
        try:
            import tkinter as tk
            import tkinter.messagebox as mb

            r = tk.Tk()
            r.withdraw()
            mb.showinfo(t("app_title"), t("already_running"))
            r.destroy()
        except Exception:
            pass
        os._exit(0)

    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
