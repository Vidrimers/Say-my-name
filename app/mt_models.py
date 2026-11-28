"""Local machine-translation model catalog (Hugging Face)."""

from __future__ import annotations

from pathlib import Path

# id, repo, approx size GB, description EN/RU
MT_MODELS = [
    {
        "id": "opus-mt-en-ru",
        "repo": "Helsinki-NLP/opus-mt-en-ru",
        "gb": 0.3,
        "en": "Fast EN→RU pair model. Small and light — best for English speech.",
        "ru": "Быстрая пара EN→RU. Маленькая — лучший выбор для английской речи.",
    },
    {
        "id": "opus-mt-ru-en",
        "repo": "Helsinki-NLP/opus-mt-ru-en",
        "gb": 0.3,
        "en": "Fast RU→EN pair model. Small and light — best for Russian speech.",
        "ru": "Быстрая пара RU→EN. Маленькая — лучший выбор для русской речи.",
    },
    {
        "id": "opus-mt-mul-en",
        "repo": "Helsinki-NLP/opus-mt-mul-en",
        "gb": 0.35,
        "en": "Many languages → English. Covers a lot of source languages.",
        "ru": "Множество языков → английский. Широкий набор исходных языков.",
    },
    {
        "id": "nllb-200-distilled-600M",
        "repo": "facebook/nllb-200-distilled-600M",
        "gb": 1.2,
        "en": "200 languages both ways (incl. EN↔RU). Balanced quality/speed.",
        "ru": "200 языков в обе стороны (EN↔RU). Баланс качества и скорости.",
    },
    {
        "id": "nllb-200-1.3B",
        "repo": "facebook/nllb-200-1.3B",
        "gb": 2.6,
        "en": "Larger NLLB — better quality, slower, more VRAM/RAM.",
        "ru": "Больший NLLB — лучше качество, медленнее, больше памяти.",
    },
    {
        "id": "m2m100_418M",
        "repo": "facebook/m2m100_418M",
        "gb": 1.0,
        "en": "100 languages both ways. Older but reliable multilingual MT.",
        "ru": "100 языков в обе стороны. Старше, но надёжный многоязычный MT.",
    },
]


def hf_cache_dir() -> Path:
    env = Path.home() / ".cache" / "huggingface" / "hub"
    return env


def is_downloaded(model_id: str) -> bool:
    m = next((x for x in MT_MODELS if x["id"] == model_id), None)
    if not m:
        return False
    name = m["repo"].replace("/", "--")
    d = hf_cache_dir() / f"models--{name}"
    if not d.is_dir():
        return False
    snap = d / "snapshots"
    if not snap.is_dir():
        return False
    return True


def get(model_id: str) -> dict | None:
    return next((x for x in MT_MODELS if x["id"] == model_id), None)


def delete_model(model_id: str) -> None:
    """Remove downloaded MT model from HF cache."""
    m = get(model_id)
    if not m:
        return
    name = m["repo"].replace("/", "--")
    d = hf_cache_dir() / f"models--{name}"
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


def model_size_label(model_id: str) -> str:
    m = get(model_id)
    if not m:
        return ""
    name = m["repo"].replace("/", "--")
    return human_size(folder_size_bytes(hf_cache_dir() / f"models--{name}"))
