"""
Tools that change things on disk.

There are exactly two of them, rename and move, and both are gated
behind a Confirm click (see settings.REQUIRES_CONFIRMATION).

There is no delete tool. Not a disabled one, not a commented-out one —
the capability does not exist in this codebase, and core/selfcheck.py
refuses to boot if anyone ever adds one.
"""
from __future__ import annotations

import hashlib
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from .. import audit, sandbox, settings
from ..registry import tool
from .filesystem import BINARY_HINTS, _walk, describe, human_size, language_of


@tool(
    name="rename_entry",
    description=(
        "Rename a file or folder in place. Needs the user to confirm. "
        "Always explain why the new name is better."
    ),
    params={
        "path": "the file or folder to rename",
        "new_name": "the new name only, not a full path",
        "reason": "why this name is better",
    },
)
def rename_entry(path: str, new_name: str, reason: str = "") -> Dict:
    source = sandbox.resolve(path, must_exist=True)
    clean = Path(str(new_name).strip().strip('"').strip("'")).name
    if not clean:
        return {"error": "New name is empty"}
    if clean == source.name:
        return {"error": f"{clean} is already the name"}

    dest = source.parent / clean
    sandbox.resolve(str(dest))       # dest must pass both gates too
    sandbox.check_write_target(dest)

    return {
        "proposal": True,
        "action": "rename",
        "from": str(source),
        "to": str(dest),
        "from_label": sandbox.relative_label(source),
        "to_label": sandbox.relative_label(dest),
        "reason": reason,
        "message": f"Rename {source.name} to {clean}?",
    }


@tool(
    name="move_entry",
    description=(
        "Move a file or folder into another folder. Needs the user to confirm. "
        "Always explain why it belongs there."
    ),
    params={
        "path": "the file or folder to move",
        "destination": "the folder it should end up in",
        "reason": "why it belongs there",
    },
)
def move_entry(path: str, destination: str, reason: str = "") -> Dict:
    source = sandbox.resolve(path, must_exist=True)
    target_dir = sandbox.resolve(destination)

    if target_dir.exists() and target_dir.is_dir():
        dest = target_dir / source.name
    else:
        dest = target_dir

    if sandbox.is_within(dest, source) and source.is_dir():
        return {"error": "Cannot move a folder inside itself"}

    sandbox.check_write_target(dest)

    return {
        "proposal": True,
        "action": "move",
        "from": str(source),
        "to": str(dest),
        "from_label": sandbox.relative_label(source),
        "to_label": sandbox.relative_label(dest),
        "reason": reason,
        "message": f"Move {source.name} into {dest.parent.name}?",
    }


# ── executors, called only after the user clicks Confirm ────
def commit_rename(from_path: str, to_path: str) -> Dict:
    source = sandbox.resolve(from_path, must_exist=True)
    dest = sandbox.resolve(to_path)
    sandbox.check_write_target(dest)
    source.rename(dest)
    audit.record("RENAME", f"{source} -> {dest}", "committed")
    return {"ok": True, "action": "rename", "from": str(source), "to": str(dest)}


def commit_move(from_path: str, to_path: str) -> Dict:
    source = sandbox.resolve(from_path, must_exist=True)
    dest = sandbox.resolve(to_path)
    sandbox.check_write_target(dest)
    shutil.move(str(source), str(dest))
    audit.record("MOVE", f"{source} -> {dest}", "committed")
    return {"ok": True, "action": "move", "from": str(source), "to": str(dest)}


# ── duplicates ──────────────────────────────────────────────
def _fingerprint(entry: Path) -> str | None:
    try:
        h = hashlib.sha256()
        with open(entry, "rb") as fh:
            while chunk := fh.read(65536):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


@tool(
    name="find_duplicates",
    description=(
        "Find files that share the same name in different folders, read each "
        "one, and report whether they are byte-identical copies or different "
        "files that happen to collide. Use this before proposing renames."
    ),
    params={"path": "folder to scan"},
)
def find_duplicates(path: str = ".") -> Dict:
    root = sandbox.resolve(path, must_exist=True)

    by_name: Dict[str, List[Path]] = defaultdict(list)
    for entry in _walk(root):
        if entry.is_file():
            by_name[entry.name.lower()].append(entry)

    groups = []
    for name, entries in by_name.items():
        if len(entries) < 2:
            continue
        entries = entries[: settings.MAX_FILES_PER_GROUP]

        members = []
        digests = []
        for entry in entries:
            record = describe(entry)
            digest = _fingerprint(entry)
            digests.append(digest)
            record["fingerprint"] = (digest or "")[:12]

            if entry.suffix.lower() not in BINARY_HINTS:
                try:
                    text = entry.read_text(encoding="utf-8", errors="replace")
                    record["preview"] = text[:1200]
                    record["lines"] = text.count("\n") + 1
                except OSError:
                    record["preview"] = ""
            members.append(record)

        identical = len(set(d for d in digests if d)) == 1 and all(digests)
        groups.append({
            "name": name,
            "count": len(entries),
            "identical": identical,
            "verdict": (
                "byte-identical copies" if identical
                else "same name, different contents"
            ),
            "files": members,
        })
        if len(groups) >= settings.MAX_DUPLICATE_GROUPS:
            break

    groups.sort(key=lambda g: (not g["identical"], -g["count"]))
    audit.record("DUPSCAN", str(root), "ok", f"{len(groups)} groups")

    return {
        "root": str(root),
        "groups_found": len(groups),
        "identical_groups": sum(1 for g in groups if g["identical"]),
        "colliding_groups": sum(1 for g in groups if not g["identical"]),
        "groups": groups,
        "note": (
            "For byte-identical copies, suggest which single location should "
            "keep the file and where the other should be moved. For colliding "
            "names, read the contents and suggest a descriptive rename for each "
            "based on what the code actually does. Never suggest deleting."
        ),
    }
