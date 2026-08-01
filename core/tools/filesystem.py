"""Read-only filesystem tools. Nothing here changes anything on disk."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Dict, List

from .. import audit, sandbox, settings
from ..registry import tool

LANGUAGES = {
    ".py": "python", ".pyw": "python", ".js": "javascript", ".mjs": "javascript","go": "go",
    ".ts": "typescript", ".tsx": "typescript", ".jsx": "javascript",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".java": "java", ".kt": "kotlin", ".cpp": "cpp", ".cc": "cpp",
    ".c": "c", ".h": "c", ".hpp": "cpp", ".cs": "csharp", ".go": "go",
    ".rs": "rust", ".php": "php", ".rb": "ruby", ".swift": "swift",
    ".sql": "sql", ".sh": "bash", ".ps1": "powershell", ".bat": "batch",
    ".xml": "xml", ".md": "markdown", ".txt": "text", ".ini": "ini",
    ".cfg": "ini", ".env": "dotenv", ".r": "r", ".lua": "lua",
}

BINARY_HINTS = {
    ".exe", ".dll", ".so", ".dylib", ".zip", ".rar", ".7z", ".gz",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".mp3",
    ".mp4", ".avi", ".mkv", ".wav", ".pdf", ".safetensors", ".gguf",
    ".bin", ".pt", ".pth", ".onnx", ".db", ".sqlite",
}


def language_of(name: str) -> str:
    return LANGUAGES.get(Path(name).suffix.lower(), "text")


def human_size(n: int | None) -> str:
    if n is None:
        return ""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < step:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= step
    return f"{n:.1f} PB"


def describe(entry: Path) -> Dict:
    try:
        st = entry.stat()
        is_dir = entry.is_dir()
        return {
            "name": entry.name,
            "path": str(entry),
            "label": sandbox.relative_label(entry),
            "kind": "folder" if is_dir else "file",
            "ext": entry.suffix.lower(),
            "language": None if is_dir else language_of(entry.name),
            "bytes": None if is_dir else st.st_size,
            "size": "" if is_dir else human_size(st.st_size),
            "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
        }
    except OSError:
        return {"name": entry.name, "path": str(entry), "kind": "unknown"}


# ── tools ───────────────────────────────────────────────────
@tool(
    name="list_folder",
    description="List what is inside a folder",
    params={"path": "folder to list, e.g. Projects or Projects/src"},
)
def list_folder(path: str = ".") -> Dict:
    target = sandbox.resolve(path, must_exist=True)
    if not target.is_dir():
        return {"error": f"{target.name} is a file, not a folder"}

    folders, files = [], []
    truncated = False
    try:
        for entry in sorted(target.iterdir(), key=lambda e: e.name.lower()):
            if len(folders) + len(files) >= settings.MAX_LIST_ITEMS:
                truncated = True
                break
            if entry.is_dir():
                if sandbox.should_skip_dir(entry.name):
                    continue
                folders.append(describe(entry))
            else:
                files.append(describe(entry))
    except OSError as exc:
        return {"error": str(exc)}

    audit.record("LIST", str(target), "ok", f"{len(folders)}f {len(files)} files")
    return {
        "path": str(target),
        "label": sandbox.relative_label(target),
        "folders": folders,
        "files": files,
        "counts": {"folders": len(folders), "files": len(files)},
        "truncated": truncated,
    }


@tool(
    name="read_file",
    description="Read the contents of a text or code file so you can see what it does",
    params={"path": "file to read"},
)
def read_file(path: str) -> Dict:
    target = sandbox.resolve(path, must_exist=True)
    if not target.is_file():
        return {"error": f"{target.name} is not a file"}
    if target.suffix.lower() in BINARY_HINTS:
        return {
            "path": str(target),
            "error": f"{target.suffix} is a binary format — nothing readable inside",
        }
    size = target.stat().st_size
    if size > settings.MAX_READ_BYTES:
        return {
            "path": str(target),
            "error": f"File is {human_size(size)}, over the "
                     f"{human_size(settings.MAX_READ_BYTES)} read limit",
        }
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"error": str(exc)}

    clipped = len(text) > settings.MAX_READ_CHARS
    audit.record("READ", str(target), "ok", f"{len(text)} chars")
    return {
        "path": str(target),
        "label": sandbox.relative_label(target),
        "language": language_of(target.name),
        "lines": text.count("\n") + 1,
        "size": human_size(size),
        "clipped": clipped,
        "content": text[: settings.MAX_READ_CHARS],
    }


@tool(
    name="search_files",
    description="Find files and folders whose name contains some text",
    params={"query": "text to look for in names", "path": "folder to search inside"},
)
def search_files(query: str, path: str = ".") -> Dict:
    root = sandbox.resolve(path, must_exist=True)
    needle = (query or "").lower()
    if not needle:
        return {"error": "Give me something to search for"}

    hits: List[Dict] = []
    for entry in _walk(root):
        if needle in entry.name.lower():
            hits.append(describe(entry))
            if len(hits) >= settings.MAX_SEARCH_RESULTS:
                break

    audit.record("SEARCH", f"{query} in {root}", "ok", f"{len(hits)} hits")
    return {"query": query, "root": str(root), "count": len(hits), "results": hits}


@tool(
    name="folder_tree",
    description="Show the folder structure under a path, as a tree",
    params={"path": "folder to map", "depth": "how many levels deep, max 4"},
)
def folder_tree(path: str = ".", depth: int = 2) -> Dict:
    root = sandbox.resolve(path, must_exist=True)
    try:
        depth = max(1, min(int(depth), settings.MAX_TREE_DEPTH))
    except (TypeError, ValueError):
        depth = 2

    lines: List[str] = [root.name or str(root)]

    def walk(folder: Path, prefix: str, level: int) -> None:
        if level > depth:
            return
        try:
            entries = sorted(
                [e for e in folder.iterdir()
                 if not (e.is_dir() and sandbox.should_skip_dir(e.name))],
                key=lambda e: (e.is_file(), e.name.lower()),
            )[:60]
        except OSError:
            return
        for i, entry in enumerate(entries):
            last = i == len(entries) - 1
            lines.append(f"{prefix}{'└─ ' if last else '├─ '}{entry.name}"
                         f"{'/' if entry.is_dir() else ''}")
            if entry.is_dir():
                walk(entry, prefix + ("   " if last else "│  "), level + 1)

    walk(root, "", 1)
    audit.record("TREE", str(root), "ok", f"depth {depth}")
    return {"root": str(root), "depth": depth, "tree": "\n".join(lines[:400])}


@tool(
    name="file_info",
    description="Get size, dates and type for one file or folder",
    params={"path": "file or folder"},
)
def file_info(path: str) -> Dict:
    target = sandbox.resolve(path, must_exist=True)
    info = describe(target)
    if target.is_dir():
        files = folders = 0
        total = 0
        for entry in _walk(target):
            if entry.is_dir():
                folders += 1
            else:
                files += 1
                try:
                    total += entry.stat().st_size
                except OSError:
                    pass
        info.update({"contains_files": files, "contains_folders": folders,
                     "total_size": human_size(total)})
    audit.record("INFO", str(target), "ok")
    return info


@tool(
    name="largest_files",
    description="List the biggest files under a folder",
    params={"path": "folder to scan", "limit": "how many to return"},
)
def largest_files(path: str = ".", limit: int = 15) -> Dict:
    root = sandbox.resolve(path, must_exist=True)
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 15

    found = []
    for entry in _walk(root):
        if entry.is_file():
            try:
                found.append((entry.stat().st_size, entry))
            except OSError:
                continue
    found.sort(key=lambda pair: pair[0], reverse=True)
    audit.record("SCAN", str(root), "ok", f"largest {limit}")
    return {
        "root": str(root),
        "results": [describe(entry) for _, entry in found[:limit]],
    }


# ── shared walker ───────────────────────────────────────────
def _walk(root: Path):
    """Depth-first walk that honours SKIP_DIRS and never leaves the sandbox."""
    stack = [root]
    seen = 0
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            seen += 1
            if seen > 60_000:
                return
            if entry.is_dir():
                if sandbox.should_skip_dir(entry.name):
                    continue
                stack.append(entry)
            yield entry
