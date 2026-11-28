"""Translation: API (DeepL/MiMo/OpenAI) or local MT models. Lazy load."""

from __future__ import annotations

import gc
import json
import threading

_model_lock = threading.Lock()
_local_model = None  # (model, tokenizer, repo_id)


def _unload_local() -> None:
    global _local_model
    with _model_lock:
        _local_model = None
    try:
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        gc.collect()
    except Exception:
        pass




def _clean_translated(text: str) -> str:
    """Drop leading language tags some models/APIs emit (en. / ru: / eng_Latn …)."""
    import re

    s = (text or "").strip()
    s = re.sub(
        r"^(?:[a-z]{2,3}_[A-Za-z]{2,8}|[a-z]{2}|auto)(?:\s*[.:：]|\s)",
        "",
        s,
        count=1,
        flags=re.IGNORECASE,
    )
    return s.strip()


def translate_api(text: str, cfg: dict) -> str:
    provider = cfg.get("tr_provider", "mimo")
    key = (cfg.get("tr_api_key") or "").strip()
    lang = cfg.get("tr_lang_to", "auto")
    if not text.strip():
        return text
    if not key:
        raise RuntimeError("No API key")

    import urllib.request

    if provider == "deepl":
        import urllib.parse

        data = urllib.parse.urlencode(
            {
                "text": text,
                "target_lang": "EN-US" if lang == "en" else ("RU" if lang == "ru" else "EN-US"),
            }
        ).encode()
        req = urllib.request.Request(
            "https://api-free.deepl.com/v2/translate",
            data=data,
            headers={
                "Authorization": f"DeepL-Auth-Key {key}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            j = json.loads(r.read().decode())
        return j["translations"][0]["text"]

    # MiMo / OpenAI-compatible chat
    if provider == "mimo":
        url = "https://api.xiaomimimo.com/v1/chat/completions"
        model = "mimo-v2.6-flash"
    else:
        url = cfg.get("tr_base_url") or "https://api.openai.com/v1/chat/completions"
        model = cfg.get("tr_model") or "gpt-4o-mini"

    tgt = {"en": "English", "ru": "Russian", "auto": "the same language"}.get(lang, "the same language")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": f"Translate the user text to {tgt}. Reply with only the translation.",
            },
            {"role": "user", "content": text},
        ],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        j = json.loads(r.read().decode())
    return j["choices"][0]["message"]["content"].strip()


def _load_local(cfg: dict):
    global _local_model
    with _model_lock:
        if _local_model is not None:
            return _local_model
        mid = cfg.get("tr_local_model", "opus-mt-en-ru")
        m = next(
            (
                x
                for x in __import__("mt_models").MT_MODELS
                if x["id"] == mid
            ),
            None,
        )
        if not m:
            raise RuntimeError("Unknown MT model")
        repo = m["repo"]
        device = cfg.get("tr_device", "auto")
        try:
            import torch

            if device == "auto":
                device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device == "cuda" else torch.int8
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            tok = AutoTokenizer.from_pretrained(repo)
            model = AutoModelForSeq2SeqLM.from_pretrained(
                repo,
                device_map="auto" if device == "cuda" else None,
                torch_dtype=torch.float16 if device == "cuda" else None,
            )
            if device != "cuda":
                model.eval()
        except Exception as e:
            raise RuntimeError(f"Load failed: {e}")
        _local_model = (model, tok, repo, device)
        return _local_model


def translate_local(text: str, cfg: dict) -> str:
    if not text.strip():
        return text
    mid = cfg.get("tr_local_model", "opus-mt-en-ru")
    try:
        import mt_models

        if not mt_models.is_downloaded(mid):
            raise RuntimeError("NOT_DOWNLOADED")
    except RuntimeError:
        raise
    except Exception:
        pass
    try:
        import torch  # noqa: F401
    except ImportError:
        raise RuntimeError("NO_TORCH")
    model, tok, repo, device = _load_local(cfg)
    lang = cfg.get("tr_lang_to", "auto")
    import torch

    try:
        if "nllb" in repo:
            # NLLB: src/tgt language codes
            tgt = "rus_Latn" if lang == "ru" else ("eng_Latn" if lang == "en" else "eng_Latn")
            src = "rus_Cyrl" if lang == "ru" else "eng_Latn"
            # try to keep source same as tgt if explicit
            if lang == "ru":
                src = "eng_Latn"
            elif lang == "en":
                src = "rus_Cyrl"
            tok.src_lang = src if lang != "auto" else "eng_Latn"
            inputs = tok(text, return_tensors="pt", truncation=True)
            if device == "cuda":
                inputs = {k: v.to(model.device) for k, v in inputs.items()}
            out = model.generate(**inputs, max_new_tokens=400)
            return tok.decode(out[0], skip_special_tokens=True).strip()
        if "m2m100" in repo:
            src = "ru" if lang == "ru" else "en"
            tgt = "en" if lang == "en" else "ru"
            if lang == "ru":
                src, tgt = "en", "ru"
            elif lang == "en":
                src, tgt = "ru", "en"
            tok.src_lang = src
            inputs = tok(text, return_tensors="pt", truncation=True)
            out = model.generate(**inputs, forced_bos_token_id=tok.convert_tokens_to_ids(tgt), max_new_tokens=400)
            return tok.decode(out[0], skip_special_tokens=True).strip()
        # opus-mt pair models
        if "en-ru" in repo:
            src, tgt = "en", "ru"
        elif "ru-en" in repo:
            src, tgt = "ru", "en"
        elif "mul-en" in repo:
            src, tgt = "en", "en"  # model assumes source != en
            inputs = tok(text, return_tensors="pt", truncation=True)
            out = model.generate(**inputs, max_new_tokens=400)
            return tok.decode(out[0], skip_special_tokens=True).strip()
        else:
            src, tgt = "en", "ru" if lang == "ru" else "en"
        inputs = tok(text, return_tensors="pt", truncation=True)
        out = model.generate(
            **inputs,
            forced_bos_token_id=tok.convert_tokens_to_ids(tgt),
            max_new_tokens=400,
        )
        return tok.decode(out[0], skip_special_tokens=True).strip()
    except Exception as e:
        raise RuntimeError(f"Translate failed: {e}")


def translate(text: str, cfg: dict) -> str:
    """Translate if enabled. Unloads local model unless keep_model."""
    if not cfg.get("tr_enable"):
        return text
    mode = cfg.get("tr_mode", "api")
    if mode == "local":
        result = translate_local(text, cfg)
        if not cfg.get("tr_keep_model"):
            _unload_local()
        return _clean_translated(result)
    return _clean_translated(translate_api(text, cfg))
