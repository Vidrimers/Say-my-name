"""Whisper model inventory: path, downloaded check, background download."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Callable

# faster-whisper pulls Systran/faster-whisper-* from Hugging Face
MODEL_SIZES = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo"]

_REPO = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "Systran/faster-whisper-large-v3-turbo",
    # aliases
    "large": "Systran/faster-whisper-large-v3",
    "turbo": "Systran/faster-whisper-large-v3-turbo",
}


def hf_home() -> Path:
    env = os.environ.get("HF_HOME")
    if env:
        return Path(env)
    return Path.home() / ".cache" / "huggingface"


def models_root() -> Path:
    return hf_home() / "hub"


def repo_id(size: str) -> str:
    return _REPO.get(size, f"Systran/faster-whisper-{size}")


def model_dir(size: str) -> Path:
    # models--Systran--faster-whisper-small
    name = repo_id(size).replace("/", "--")
    return models_root() / f"models--{name}"


def is_downloaded(size: str) -> bool:
    d = model_dir(size)
    if not d.is_dir():
        return False
    # snapshots/<rev>/ should contain model.bin or config.json
    snap = d / "snapshots"
    if not snap.is_dir():
        return False
    for rev in snap.iterdir():
        if (rev / "model.bin").exists() or (rev / "config.json").exists():
            return True
    return False


def downloaded_sizes() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for s in MODEL_SIZES:
        out[s] = is_downloaded(s)
    return out


_download_lock = threading.Lock()
_active: dict[str, bool] = {}


def download_model(
    size: str,
    on_progress: Callable[[int], None] | None = None,
    on_done: Callable[[bool, str | None], None] | None = None,
) -> bool:
    """Download in a daemon thread. Returns False if already in progress."""
    if size not in _REPO and size not in MODEL_SIZES:
        if on_done:
            on_done(False, f"unknown model: {size}")
        return False
    with _download_lock:
        if _active.get(size):
            return False
        _active[size] = True

    def _worker() -> None:
        try:
            from huggingface_hub import snapshot_download

            rid = repo_id(size)

            def _hf_progress(bar) -> None:
                # bar: tqdm-like with total/n
                if on_progress and bar.total:
                    pct = int(bar.n * 100 / bar.total)
                    on_progress(pct)

            # progress via tqdm hooks is awkward; use simple end callback + intermediate
            snapshot_download(
                repo_id=rid,
                # local_dir not used — default HF cache
            )
            # after success, poll-free: report 100
            if on_progress:
                on_progress(100)
            if on_done:
                on_done(True, None)
        except Exception as e:  # noqa: BLE001
            if on_done:
                on_done(False, str(e))
        finally:
            with _download_lock:
                _active[size] = False

    # Better progress: use tqdm from huggingface
    def _worker_progress() -> None:
        try:
            from huggingface_hub import snapshot_download
            import tqdm as tqdm_mod

            rid = repo_id(size)
            orig_init = tqdm_mod.tqdm.__init__

            def patched_init(self, *args, **kwargs):
                orig_init(self, *args, **kwargs)
                if on_progress and getattr(self, "total", None):
                    # hook update
                    orig_update = self.update

                    def upd(n=1):
                        orig_update(n)
                        if self.total:
                            on_progress(int(self.n * 100 / self.total))

                    self.update = upd  # type: ignore[method-assign]

            tqdm_mod.tqdm.__init__ = patched_init  # type: ignore[method-assign]
            try:
                snapshot_download(repo_id=rid)
            finally:
                tqdm_mod.tqdm.__init__ = orig_init  # type: ignore[method-assign]
            if on_progress:
                on_progress(100)
            if on_done:
                on_done(True, None)
        except Exception as e:  # noqa: BLE001
            if on_done:
                on_done(False, str(e))
        finally:
            with _download_lock:
                _active[size] = False

    threading.Thread(target=_worker_progress, daemon=True).start()
    return True
