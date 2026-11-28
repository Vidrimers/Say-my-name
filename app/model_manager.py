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
    """Download in a daemon thread with real progress. Returns False if busy."""
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
            from tqdm import tqdm

            rid = repo_id(size)
            last = {"pct": -1}

            class Progress(tqdm):
                def update(self, n=1):
                    r = super().update(n)
                    if on_progress and getattr(self, "total", None):
                        pct = min(100, max(0, int(self.n * 100 / self.total)))
                        if pct != last["pct"]:
                            last["pct"] = pct
                            on_progress(pct)
                    return r

            snapshot_download(repo_id=rid, tqdm_class=Progress)
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

    threading.Thread(target=_worker, daemon=True).start()
    return True


def delete_model(size: str) -> None:
    """Remove downloaded faster-whisper model from HF cache."""
    d = model_dir(size)
    if d.exists():
        import shutil

        shutil.rmtree(d, ignore_errors=True)


def folder_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


def human_size(n: int) -> str:
    gb = n / (1024**3)
    if gb >= 1:
        return f"{gb:.2f} GB"
    mb = n / (1024**2)
    if mb >= 1:
        return f"{mb:.0f} MB"
    return f"{n} B"


def model_size_label(size: str) -> str:
    return human_size(folder_size_bytes(model_dir(size)))
