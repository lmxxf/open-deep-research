"""Normalize text at terminal, JSON, network and file boundaries."""

import json
from pathlib import Path


def clean_surrogates(obj):
    """Drop invalid surrogate code points recursively; keep Chinese and emoji."""
    if isinstance(obj, str):
        return obj.encode("utf-8", "ignore").decode("utf-8")
    if isinstance(obj, Path):
        return Path(clean_surrogates(str(obj)))
    if isinstance(obj, dict):
        return {clean_surrogates(k): clean_surrogates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean_surrogates(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(clean_surrogates(v) for v in obj)
    return obj


def dumps(obj, **kwargs):
    return json.dumps(clean_surrogates(obj), **kwargs)


def write_text(path, text):
    return path.write_text(clean_surrogates(text), encoding="utf-8")
