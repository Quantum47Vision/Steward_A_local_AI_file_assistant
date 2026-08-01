"""
Orkestrator server.

Binds to loopback only. No outbound calls anywhere in the app.
Every blocking operation — model load, generation, disk walks — runs in a
worker thread, so the UI keeps streaming activity while the model thinks.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
os.chdir(HERE)
sys.path.insert(0, str(HERE))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect          # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse             # noqa: E402

from core import audit, config, engine, registry, sandbox, selfcheck, settings  # noqa: E402
from core.agent import Agent                                          # noqa: E402
from core.sandbox import Denied                                       # noqa: E402
from core.tools.filesystem import describe                            # noqa: E402

selfcheck.enforce()
registry.discover()

clients: List[WebSocket] = []
agent = Agent()
loop: asyncio.AbstractEventLoop | None = None
_load_lock = threading.Lock()


# ── broadcasting ────────────────────────────────────────────
async def broadcast(message: Dict) -> None:
    dead = []
    for ws in list(clients):
        try:
            await ws.send_json(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in clients:
            clients.remove(ws)


def broadcast_threadsafe(message: Dict) -> None:
    """Callable from worker threads."""
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast(message), loop)


def on_audit(event: Dict) -> None:
    broadcast_threadsafe({"type": "activity", **event})


audit.set_listener(on_audit)
agent.emit = broadcast_threadsafe


# ── app ─────────────────────────────────────────────────────
app = FastAPI(title=settings.APP_NAME)


@app.on_event("startup")
async def startup() -> None:
    global loop
    loop = asyncio.get_running_loop()
    audit.record("BOOT", settings.APP_NAME, "start",
                 f"{len(registry.all_tools())} tools")
    print(f"\n  {settings.APP_NAME} listening on "
          f"http://{settings.HOST}:{settings.PORT}")
    print(f"  Drive lock: {settings.ALLOWED_DRIVE}   "
          f"Delete: impossible   Network: off\n")

    cfg = config.load()
    if cfg.get("auto_load_model") and cfg.get("model_path") and not engine.is_loaded():
        threading.Thread(target=_load_model_worker,
                         args=(cfg["model_path"],), daemon=True).start()


@app.on_event("shutdown")
async def shutdown() -> None:
    audit.record("BOOT", settings.APP_NAME, "stop")


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((HERE / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "model_loaded": engine.is_loaded(),
        "tools": len(registry.all_tools()),
        "sandbox": sandbox.status(),
    })


# ── state ───────────────────────────────────────────────────
def snapshot() -> Dict:
    cfg = config.load()
    return {
        "type": "state",
        "config": cfg,
        "sandbox": sandbox.status(),
        "model": {"loaded": engine.is_loaded(), **engine.info()},
        "tools": registry.catalogue(),
        "app": {"name": settings.APP_NAME, "tagline": settings.APP_TAGLINE},
    }


# ── workers (run in threads) ────────────────────────────────
def _load_model_worker(path: str) -> None:
    if not _load_lock.acquire(blocking=False):
        broadcast_threadsafe({"type": "load", "step": "error",
                              "message": "A model is already loading"})
        return
    try:
        def progress(step: str, message: str) -> None:
            broadcast_threadsafe({"type": "load", "step": step, "message": message})

        broadcast_threadsafe({"type": "load", "step": "start",
                              "message": "Checking model folder"})
        info = engine.load(path, progress)
        config.update(model_path=path, model_label=info.get("label", ""),
                      first_run=False)
        audit.record("MODEL", path, "loaded",
                     f"{info.get('backend')} on {info.get('device')}")
        broadcast_threadsafe({"type": "load", "step": "done",
                              "message": f"{info.get('label')} ready on "
                                         f"{str(info.get('device','')).upper()}"})
        broadcast_threadsafe(snapshot())
    except Exception as exc:
        audit.record("MODEL", path, "failed", str(exc)[:200])
        broadcast_threadsafe({"type": "load", "step": "error", "message": str(exc)})
        broadcast_threadsafe(snapshot())
    finally:
        _load_lock.release()


def _browse(path: str, for_model: bool) -> Dict:
    """
    Folder picker backing store.

    Browsing is drive-locked but not scope-locked — you need to see D: to
    choose what goes into scope in the first place. It returns names only,
    never contents.
    """
    drive_locked = (settings.MODEL_PATH_DRIVE_LOCKED if for_model else True)
    root = sandbox.allowed_root()

    if not path or path in (".", "root"):
        current = root
    else:
        current = Path(path)
        try:
            current = current.resolve()
        except OSError:
            current = root

    if drive_locked and current.drive and \
            current.drive.upper() != settings.ALLOWED_DRIVE.upper():
        current = root

    if not current.exists() or not current.is_dir():
        current = root

    folders = []
    signals = {"safetensors": 0, "gguf": 0, "config": False}
    try:
        for entry in sorted(current.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir():
                if entry.name.lower() in settings.FORBIDDEN_NAMES:
                    continue
                folders.append({"name": entry.name, "path": str(entry)})
            else:
                suffix = entry.suffix.lower()
                if suffix == ".safetensors":
                    signals["safetensors"] += 1
                elif suffix == ".gguf":
                    signals["gguf"] += 1
                elif entry.name == "config.json":
                    signals["config"] = True
    except OSError as exc:
        return {"error": str(exc), "path": str(current), "folders": []}

    parent = None
    if current != root and current.parent != current:
        parent = str(current.parent)

    return {
        "path": str(current),
        "parent": parent,
        "root": str(root),
        "folders": folders[:600],
        "signals": signals,
        "is_model_folder": bool(signals["safetensors"] or signals["gguf"]),
        "in_scope": any(sandbox.is_within(current, f)
                        for f in sandbox.scope_folders()),
    }


# ── websocket ───────────────────────────────────────────────
@app.websocket("/ws")
async def socket(ws: WebSocket) -> None:
    await ws.accept()
    clients.append(ws)
    await ws.send_json(snapshot())
    for line in audit.tail(40):
        await ws.send_json({"type": "log", "line": line})

    try:
        while True:
            msg = await ws.receive_json()
            await route(ws, msg)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        print(f"[ws] {exc}")
    finally:
        if ws in clients:
            clients.remove(ws)


async def route(ws: WebSocket, msg: Dict[str, Any]) -> None:
    cmd = msg.get("cmd", "")
    run = asyncio.get_running_loop().run_in_executor

    try:
        if cmd == "state":
            await ws.send_json(snapshot())

        elif cmd == "browse":
            result = await run(None, _browse, msg.get("path", ""),
                               bool(msg.get("for_model")))
            await ws.send_json({"type": "browse", **result})

        elif cmd == "load_model":
            path = (msg.get("path") or "").strip()
            if not path:
                await ws.send_json({"type": "load", "step": "error",
                                    "message": "Pick a model folder first"})
                return
            threading.Thread(target=_load_model_worker, args=(path,),
                             daemon=True).start()

        elif cmd == "unload_model":
            await run(None, engine.unload)
            await broadcast(snapshot())

        elif cmd == "add_scope":
            folder = (msg.get("path") or "").strip()
            resolved = Path(folder).resolve()
            if resolved.drive.upper() != settings.ALLOWED_DRIVE.upper():
                await ws.send_json({"type": "toast", "tone": "bad",
                                    "message": f"Only {settings.ALLOWED_DRIVE} "
                                               f"folders can be added"})
                return
            if not resolved.is_dir():
                await ws.send_json({"type": "toast", "tone": "bad",
                                    "message": "That is not a folder"})
                return
            config.add_scope(str(resolved))
            audit.record("SCOPE", str(resolved), "added")
            await broadcast(snapshot())
            await ws.send_json({"type": "toast", "tone": "good",
                                "message": f"{resolved.name} is now in scope"})

        elif cmd == "remove_scope":
            folder = (msg.get("path") or "").strip()
            config.remove_scope(folder)
            audit.record("SCOPE", folder, "removed")
            await broadcast(snapshot())

        elif cmd == "chat":
            text = (msg.get("text") or "").strip()
            if not text:
                return
            await broadcast({"type": "thinking", "on": True})
            result = await run(None, agent.handle, text)
            await broadcast({"type": "thinking", "on": False})
            await broadcast({"type": "assistant", **result})

        elif cmd == "confirm":
            result = await run(None, agent.confirm)
            await broadcast({"type": "assistant", **result})

        elif cmd == "cancel":
            result = await run(None, agent.cancel)
            await broadcast({"type": "assistant", **result})

        elif cmd == "reset_chat":
            agent.reset()
            await broadcast({"type": "chat_cleared"})

        elif cmd == "open_folder":
            from core.tools.filesystem import list_folder
            data = await run(None, list_folder, msg.get("path", "."))
            await ws.send_json({"type": "data", "tool": "list_folder", "data": data})

        elif cmd == "peek_file":
            from core.tools.filesystem import read_file
            data = await run(None, read_file, msg.get("path", ""))
            await ws.send_json({"type": "data", "tool": "read_file", "data": data})

        else:
            await ws.send_json({"type": "toast", "tone": "bad",
                                "message": f"Unknown command: {cmd}"})

    except Denied as exc:
        await ws.send_json({"type": "blocked", "gate": exc.gate,
                            "message": str(exc)})
    except Exception as exc:
        await ws.send_json({"type": "toast", "tone": "bad", "message": str(exc)})


# ── entry ───────────────────────────────────────────────────
def main() -> None:
    import uvicorn

    url = f"http://{settings.HOST}:{settings.PORT}"
    if settings.OPEN_BROWSER:
        threading.Timer(1.6, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=settings.HOST, port=settings.PORT, log_level="warning")


if __name__ == "__main__":
    main()
