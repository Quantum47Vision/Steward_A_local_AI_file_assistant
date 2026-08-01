"""
Path sandbox.

Two gates, in order:
  1. DRIVE GATE  — the resolved path must sit on settings.ALLOWED_DRIVE.
  2. SCOPE GATE  — the resolved path must sit inside one of the folders
                   you added in the UI (skipped if RESTRICT_TO_SCOPE is False).

Resolution happens BEFORE either gate, so "..", symlinks, junctions and
8.3 short names cannot be used to climb out. A symlink on D: pointing at
C: resolves to a C: path and is refused by gate 1.

No Windows backslash string literals appear in this module. Everything
is built with pathlib so the escaping cannot rot.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from . import audit, config, settings


class Denied(PermissionError):
    """Raised when a path fails a gate. Carries a user-facing reason."""

    def __init__(self, message: str, gate: str = "sandbox"):
        super().__init__(message)
        self.gate = gate


# ── helpers ─────────────────────────────────────────────────
def allowed_root() -> Path:
    """The root of the permitted drive, e.g. D:\\ on Windows."""
    return Path(settings.ALLOWED_DRIVE + os.sep)


def _norm(path: Path) -> str:
    return os.path.normcase(str(path))


def _drive_of(path: Path) -> str:
    # Path("D:/x").drive -> "D:"   Path("/x").drive -> ""
    return path.drive.upper()


def is_within(child: Path, parent: Path) -> bool:
    """True if child is parent or lives under it. Case-insensitive."""
    c, p = _norm(child), _norm(parent)
    if c == p:
        return True
    return c.startswith(p.rstrip(os.sep) + os.sep)


def _anchor_relative(raw: str, folders: List[Path]) -> Path:
    """
    Work out what a relative path means.

    The model refers to folders by their short name — it says "Projects",
    not "D:\\Dev\\Projects". Four strategies, in order of confidence:

      1. First segment is the name of a scope folder  -> anchor there.
         "Projects/src/app.py" with scope D:\\Dev\\Projects
         becomes D:\\Dev\\Projects\\src\\app.py
      2. The whole path exists inside some scope folder -> use it.
         "src/app.py" resolves against each folder until one hits.
      3. Only one folder in scope -> anchor there regardless.
      4. Give up and anchor at the drive root, where the gates will
         judge it on its merits.
    """
    parts = [seg for seg in Path(raw).parts if seg not in (".", os.sep)]
    if not parts:
        return folders[0] if folders else allowed_root()

    head, tail = parts[0].lower(), parts[1:]

    for folder in folders:                                    # 1
        if folder.name.lower() == head:
            return folder.joinpath(*tail) if tail else folder

    for folder in folders:                                    # 2
        trial = folder.joinpath(*parts)
        if trial.exists():
            return trial

    if len(folders) == 1:                                     # 3
        return folders[0].joinpath(*parts)

    return allowed_root().joinpath(*parts)                    # 4


def scope_folders() -> List[Path]:
    out: List[Path] = []
    for entry in config.get_scope():
        try:
            out.append(Path(entry).resolve())
        except OSError:
            continue
    return out


# ── the gates ───────────────────────────────────────────────
def resolve(
    user_path: str,
    *,
    must_exist: bool = False,
    enforce_scope: Optional[bool] = None,
) -> Path:
    """
    Turn whatever the model typed into a real, checked absolute Path.

    Accepts:  "Projects"            -> D:\\Projects
              "Projects/utils.py"   -> D:\\Projects\\utils.py
              "D:\\Projects"        -> D:\\Projects
              "."                   -> first scope folder, or drive root
    Raises Denied on anything outside the permitted area.
    """
    if enforce_scope is None:
        enforce_scope = settings.RESTRICT_TO_SCOPE

    raw = (user_path or "").strip().strip('"').strip("'")
    folders = scope_folders()

    if raw in ("", ".", "./", ".\\"):
        candidate = folders[0] if folders else allowed_root()
    else:
        # pathlib understands both separators on Windows; on Linux we
        # normalise manually so the same code is testable everywhere.
        raw = raw.replace("\\", os.sep).replace("/", os.sep)
        p = Path(raw)
        if p.is_absolute() or p.drive:
            candidate = p
        else:
            candidate = _anchor_relative(raw, folders)

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError) as exc:
        raise Denied(f"Path could not be resolved: {user_path} ({exc})", "resolve")

    # ── GATE 1: drive ───────────────────────────────────────
    want = settings.ALLOWED_DRIVE.upper()
    have = _drive_of(resolved)
    if have and have != want:
        audit.record("BLOCK", str(resolved), "denied", f"drive {have} not permitted")
        raise Denied(
            f"{resolved} is on {have} — Steward only works on {want}", "drive"
        )
    if not have and os.name == "nt":
        audit.record("BLOCK", str(resolved), "denied", "no drive letter")
        raise Denied(f"{resolved} has no drive letter", "drive")

    # ── forbidden names ─────────────────────────────────────
    if resolved.name.lower() in settings.FORBIDDEN_NAMES:
        audit.record("BLOCK", str(resolved), "denied", "protected system name")
        raise Denied(f"{resolved.name} is a protected system object", "protected")

    # ── GATE 2: scope ───────────────────────────────────────
    if enforce_scope:
        if not folders:
            raise Denied(
                "No folders are in scope yet. Add at least one folder in the "
                "sidebar before asking about files.",
                "scope",
            )
        if not any(is_within(resolved, folder) for folder in folders):
            audit.record("BLOCK", str(resolved), "denied", "outside scope")
            raise Denied(
                f"{resolved} is outside the folders you gave me. "
                f"In scope: {', '.join(str(f) for f in folders)}",
                "scope",
            )

    if must_exist and not resolved.exists():
        raise Denied(f"{resolved} does not exist", "missing")

    return resolved


def check_write_target(dest: Path) -> None:
    """
    Guard for rename/move destinations.

    Refusing to overwrite is not politeness — an overwrite IS a delete.
    This is the difference between 'cannot delete' and 'cannot delete
    unless you phrase it as a move'.
    """
    if dest.exists():
        audit.record("BLOCK", str(dest), "denied", "destination exists")
        raise Denied(
            f"{dest.name} already exists there. Overwriting would destroy the "
            f"existing file, and Steward never destroys anything. "
            f"Pick a different name.",
            "overwrite",
        )
    parent = dest.parent
    if not parent.exists():
        raise Denied(f"Destination folder does not exist: {parent}", "missing")


def relative_label(path: Path) -> str:
    """Shortest readable label for a path, relative to its scope folder."""
    for folder in scope_folders():
        if is_within(path, folder):
            try:
                rel = path.relative_to(folder)
                return str(Path(folder.name) / rel) if str(rel) != "." else folder.name
            except ValueError:
                pass
    return str(path)


def should_skip_dir(name: str) -> bool:
    return name.lower() in settings.SKIP_DIRS


def status() -> dict:
    """Live sandbox state for the UI permission bar."""
    folders = scope_folders()
    return {
        "drive": settings.ALLOWED_DRIVE,
        "scope_locked": settings.RESTRICT_TO_SCOPE,
        "scope_count": len(folders),
        "scope": [str(f) for f in folders],
        "delete_possible": False,
        "network": False,
    }
