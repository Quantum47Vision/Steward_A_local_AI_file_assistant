"""
Boot-time proof that Steward cannot delete.

Scans its own source for any call that removes data. If one is ever added --
by a future feature, a copy-pasted snippet, or a model writing its own tools --
the app refuses to start and names the file and line.

This is what turns "it will not delete your files" from a promise into a
property you can verify in three seconds.
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

from . import settings

# This file and settings.py both contain the banned strings by necessity.
EXEMPT = {"selfcheck.py", "settings.py"}


def scan(root: Path = None) -> List[Tuple[str, int, str]]:
    root = root or Path(__file__).resolve().parent.parent
    hits: List[Tuple[str, int, str]] = []
    for source in root.rglob("*.py"):
        if source.name in EXEMPT:
            continue
        try:
            lines = source.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for banned in settings.BANNED_CALLS:
                if banned in line:
                    hits.append((str(source.relative_to(root)), number, stripped))
                    break
    return hits


def enforce() -> dict:
    if not settings.ENFORCE_NO_DELETE_SELFCHECK:
        return {"ran": False, "clean": None, "hits": []}

    hits = scan()
    if hits:
        report = "\n".join(f"  {f}:{n}  {code}" for f, n, code in hits)
        raise SystemExit(
            "\n"
            "=================================================================\n"
            " STEWARD REFUSED TO START\n"
            "=================================================================\n"
            " Delete-capable code was found in the source tree:\n\n"
            f"{report}\n\n"
            " Steward guarantees it cannot destroy your files. That\n"
            " guarantee is enforced here, at boot, by scanning its own code.\n"
            " Remove the lines above, or set ENFORCE_NO_DELETE_SELFCHECK\n"
            " to False in core/settings.py if you know what you are doing.\n"
            "=================================================================\n"
        )
    return {"ran": True, "clean": True, "hits": []}
