"""Persistent user config. Written next to the app as JSON."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List

from . import settings

_LOCK = threading.Lock()
_PATH = Path(settings.CONFIG_FILE).resolve()

DEFAULTS: Dict[str, Any] = {
    "model_path": "",
    "model_label": "",
    "backend": settings.BACKEND,
    "scope": [],            # absolute folder paths the assistant may reach
    "first_run": True,
    "auto_load_model": True,
}


def load() -> Dict[str, Any]:
    with _LOCK:
        data = dict(DEFAULTS)
        if _PATH.exists():
            try:
                data.update(json.loads(_PATH.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        for key, value in DEFAULTS.items():
            data.setdefault(key, value)
        return data


def save(data: Dict[str, Any]) -> Dict[str, Any]:
    with _LOCK:
        _PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return data


def update(**kwargs: Any) -> Dict[str, Any]:
    data = load()
    data.update(kwargs)
    return save(data)


def get_scope() -> List[str]:
    return list(load().get("scope", []))


def add_scope(folder: str) -> Dict[str, Any]:
    data = load()
    folder = str(Path(folder))
    if folder not in data["scope"]:
        data["scope"].append(folder)
        data["scope"].sort()
    return save(data)


def remove_scope(folder: str) -> Dict[str, Any]:
    data = load()
    data["scope"] = [f for f in data["scope"] if f != folder]
    return save(data)
