#!/usr/bin/env python3
"""Voice input via local Whisper: hotkey toggle -> speak -> text pasted into active window."""

import io
import json
import sys
import threading
import time
import wave
from pathlib import Path

import keyboard
import numpy as np
import pyperclip
import sounddevice as sd

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CFG = load_config()

_state = {
    "recording": False,
    "frames": [],
    "thread": None,
    "busy": False,
    "model": None,
}


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_model():
    if _state["model"] is not None:
        return _state["model"]
    from faster_whisper import WhisperModel

    model_size = CFG.get("model", "small")
    device = CFG.get("device", "auto")
    compute = CFG.get("compute_type", "auto")
    if device == "auto":
        try:
            import ctranslate2

            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            device = "cpu"
    if compute == "auto":
        compute = "float16" if device == "cuda" else "int8"
    _log(f"[model] loading whisper '{model_size}' device={device} compute={compute} ...")
    t0 = time.monotonic()
    _state["model"] = WhisperModel(model_size, device=device, compute_type=compute)
    _log(f"[model] loaded in {time.monotonic() - t0:.1f}s")
    return _state["model"]


def _record_worker() -> None:
    sample_rate = int(CFG.get("sample_rate", 16000))
    max_seconds = float(CFG.get("max_record_seconds", 60))
    frames = _state["frames"]

    def callback(indata, _frames, _time_info, status) -> None:
        if status:
            _log(f"[audio] {status}")
        if _state["recording"]:
            frames.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=sample_rate, channels=1, dtype="int16", callback=callback
        ):
            start = time.monotonic()
            while _state["recording"]:
                if time.monotonic() - start > max_seconds:
                    _log("[rec] max duration reached")
                    _state["recording"] = False
                    break
                time.sleep(0.05)
    except Exception as e:
        _log(f"[audio-error] {e}")
        _state["recording"] = False


def _start_recording() -> None:
    if _state["busy"]:
        _log("[hotkey] busy, ignore start")
        return
    if _state["recording"]:
        _log("[hotkey] already recording")
        return
    _state["recording"] = True
    _state["frames"] = []
    t = threading.Thread(target=_record_worker, daemon=True)
    _state["thread"] = t
    t.start()
    _log("[rec] RECORDING — press hotkey again to stop")


def _wav_bytes(frames: list) -> bytes:
    if not frames:
        raise RuntimeError("No audio captured")
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
    # faster-whisper accepts a file-like object
    bio = io.BytesIO(audio)
    language = CFG.get("language") or None
    segments, info = model.transcribe(
        bio,
        language=language,
        beam_size=5,
        vad_filter=True,
    )
    parts = [seg.text.strip() for seg in segments if seg.text.strip()]
    _log(f"[whisper] lang={info.language} p={info.language_probability:.2f}")
    return " ".join(parts).strip()


def _paste_text(text: str) -> None:
    pyperclip.copy(text)
    time.sleep(0.05)
    keyboard.send("ctrl+v")


def _stop_and_process() -> None:
    if not _state["recording"]:
        _log("[hotkey] not recording")
        return
    _state["recording"] = False
    t = _state["thread"]
    if t:
        t.join(timeout=3)
    n = len(_state["frames"])
    _log(f"[rec] STOPPED frames={n}, transcribing...")
    _state["busy"] = True
    try:
        audio = _wav_bytes(_state["frames"])
        t0 = time.monotonic()
        text = _transcribe(audio)
        dt = time.monotonic() - t0
        if not text:
            _log("[asr] empty result")
            return
        _log(f"[asr] ({dt:.1f}s) {text}")
        _paste_text(text)
        _log("[asr] pasted to active window")
    except Exception as e:
        _log(f"[error] {e}")
    finally:
        _state["busy"] = False


def _on_hotkey() -> None:
    _log(f"[hotkey] fired recording={_state['recording']} busy={_state['busy']}")
    if _state["busy"]:
        return
    if _state["recording"]:
        _stop_and_process()
    else:
        _start_recording()


def main() -> None:
    hotkey = CFG.get("hotkey", "ctrl+alt+space")

    _log("Voice ASR ready (local Whisper, TOGGLE mode).")
    _log(f"  Press <{hotkey}> once  = START recording")
    _log(f"  Press <{hotkey}> again = STOP + transcribe")
    _log(f"  Model: {CFG.get('model')}  Language: {CFG.get('language')}")
    _log("  Ctrl+C to quit")

    # Preload model in background so first dictation is fast
    threading.Thread(target=_load_model, daemon=True).start()

    keyboard.add_hotkey(hotkey, _on_hotkey, suppress=False, trigger_on_release=False)
    _log(f"[hotkey] registered: {hotkey}")

    try:
        while True:
            time.sleep(0.2)
    except KeyboardInterrupt:
        _log("bye")


if __name__ == "__main__":
    main()
