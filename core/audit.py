"""Append-only audit trail. Every filesystem touch lands here."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import settings

_LOCK = threading.Lock()
_PATH = Path(settings.AUDIT_FILE).resolve()

# The server registers a callback here so the UI activity panel
# mirrors the log file live.
_listener: Optional[Callable[[dict], None]] = None


def set_listener(fn: Optional[Callable[[dict], None]]) -> None:
    global _listener
    _listener = fn


def record(action: str, target: str, status: str, detail: str = "") -> dict:
    stamp = datetime.now()
    event = {
        "time": stamp.strftime("%H:%M:%S"),
        "action": action.upper(),
        "status": status.upper(),
        "target": target,
        "detail": detail,
    }

    if settings.AUDIT_ENABLED:
        line = (
            f"[{stamp.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"{action.upper():<14} {status.upper():<9} {target}"
        )
        if detail:
            line += f" | {detail}"
        try:
            with _LOCK:
                with open(_PATH, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
        except OSError:
            pass
        if settings.AUDIT_ECHO_TO_CONSOLE:
            print(line)

    if _listener:
        try:
            _listener(event)
        except Exception:
            pass

    return event


def tail(lines: int = 80) -> List[str]:
    if not _PATH.exists():
        return []
    try:
        with open(_PATH, "r", encoding="utf-8", errors="ignore") as fh:
            return [ln.rstrip("\n") for ln in fh.readlines()[-lines:]]
    except OSError:
        return []
