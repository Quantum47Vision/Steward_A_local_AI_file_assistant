"""System and scope reporting tools."""
from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Dict

from .. import audit, config, sandbox, settings
from ..registry import tool
from .filesystem import human_size


@tool(
    name="scope_report",
    description=(
        "Report which folders you are allowed to see and how big they are. "
        "Use this when the user asks what you can access or what is on their machine."
    ),
    params={},
)
def scope_report() -> Dict:
    folders = []
    for folder in sandbox.scope_folders():
        entry = {"path": str(folder), "name": folder.name, "exists": folder.exists()}
        if folder.exists():
            files = subfolders = 0
            total = 0
            for root, dirs, names in os.walk(folder):
                dirs[:] = [d for d in dirs if not sandbox.should_skip_dir(d)]
                subfolders += len(dirs)
                files += len(names)
                for n in names:
                    try:
                        total += (Path(root) / n).stat().st_size
                    except OSError:
                        pass
                if files > 40_000:
                    break
            entry.update({"files": files, "folders": subfolders,
                          "size": human_size(total)})
        folders.append(entry)

    audit.record("SCOPE", "report", "ok", f"{len(folders)} folders")
    return {
        "drive": settings.ALLOWED_DRIVE,
        "scope_locked": settings.RESTRICT_TO_SCOPE,
        "folders": folders,
        "can_delete": False,
        "can_reach_network": False,
    }


@tool(
    name="disk_report",
    description="Report free and used space on the permitted drive",
    params={},
)
def disk_report() -> Dict:
    root = sandbox.allowed_root()
    try:
        usage = shutil.disk_usage(str(root))
    except OSError as exc:
        return {"error": str(exc)}
    audit.record("DISK", str(root), "ok")
    return {
        "drive": settings.ALLOWED_DRIVE,
        "total": human_size(usage.total),
        "used": human_size(usage.used),
        "free": human_size(usage.free),
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
        "machine": platform.machine(),
        "os": f"{platform.system()} {platform.release()}",
    }
