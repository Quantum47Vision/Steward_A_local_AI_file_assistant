"""
The agent loop.

One turn looks like:

    user message
      -> model emits {"tool": "...", ...}
      -> tool runs, result goes back to the model
      -> model emits another tool call, or {"reply": "..."}
      -> reply reaches the user

Tools listed in settings.REQUIRES_CONFIRMATION stop the loop and hand a
proposal to the UI instead of running. Nothing touches disk until you click.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from . import audit, config, engine, registry, sandbox, settings
from .sandbox import Denied
from .tools import organize

Emit = Optional[Callable[[dict], None]]


@dataclass
class Proposal:
    action: str
    from_path: str
    to_path: str
    from_label: str
    to_label: str
    reason: str
    message: str
    created: str = field(default_factory=lambda: datetime.now().isoformat())

    def as_dict(self) -> Dict:
        return {
            "action": self.action,
            "from": self.from_path,
            "to": self.to_path,
            "from_label": self.from_label,
            "to_label": self.to_label,
            "reason": self.reason,
            "message": self.message,
        }


SYSTEM_TEMPLATE = """You are Steward, a local AI file assistant running locally on the user's Windows PC.

WHAT YOU ARE
You look after the user's files: you tell them what they have, read code to \
understand what it does, and suggest better names and locations. You are \
careful, concrete and brief.

WHAT YOU CANNOT DO
- You cannot delete anything. No delete tool exists. Never offer to delete, \
never suggest deleting, never say a file "should be removed". If the user asks \
you to delete something, say plainly that you have no delete ability and offer \
to move it somewhere out of the way instead.
- You cannot reach the internet or publish anything.
- You can only see {drive} and only inside these folders:
{scope}
  Anything outside is refused before you ever see it.
- Rename and move need the user's click. You propose, they confirm.

HOW TO ANSWER
Reply with exactly ONE JSON object and nothing else. No prose outside it, no \
markdown fences, no explanation before or after.

To use a tool:
{tools}

To talk to the user:
{{"reply": "your answer here"}}

RULES OF THUMB
- Never guess what is in a folder. Call list_folder and look.
- Before proposing any rename, read the file first so the new name matches \
what the code actually does.
- When you find files sharing a name, call find_duplicates. It tells you \
whether they are identical copies or different files that collide. Identical \
copies: propose moving one into an archive folder. Colliding names: propose a \
descriptive rename for each based on its contents.
- One tool per response. You will see the result and can then call another.
- When you have the answer, stop calling tools and reply.
- Paths in tool calls use forward slashes: "Projects/src/utils.py".
"""


class Agent:
    def __init__(self, emit: Emit = None):
        self.emit = emit
        self.history: List[Dict] = []
        self.pending: Optional[Proposal] = None
        self.last_payload: Optional[Dict] = None

    # ── plumbing ────────────────────────────────────────────
    def _say(self, kind: str, **data) -> None:
        if self.emit:
            self.emit({"type": kind, **data})

    def _system_prompt(self) -> str:
        folders = sandbox.scope_folders()
        scope = "\n".join(f"  - {f}" for f in folders) or "  (none yet)"
        return SYSTEM_TEMPLATE.format(
            drive=settings.ALLOWED_DRIVE,
            scope=scope,
            tools=registry.prompt_spec(),
        )

    def reset(self) -> None:
        self.history.clear()
        self.pending = None
        self.last_payload = None

    # ── main turn ───────────────────────────────────────────
    def handle(self, user_text: str) -> Dict:
        user_text = (user_text or "").strip()
        if not user_text:
            return {"reply": ""}

        if not engine.is_loaded():
            return {"reply": "No model is loaded yet. Pick a model folder in the "
                             "sidebar and load it first."}

        self.pending = None
        self.last_payload = None
        audit.record("CHAT", "user", "in", user_text[:160])

        messages = [{"role": "system", "content": self._system_prompt()}]
        messages += self.history[-(settings.MAX_HISTORY_TURNS * 2):]
        messages.append({"role": "user", "content": user_text})

        reply = None

        for step in range(settings.MAX_TOOL_STEPS):
            raw = engine.chat(messages)
            parsed = extract_json(raw)

            if parsed is None:
                reply = strip_noise(raw)
                break

            if "reply" in parsed and "tool" not in parsed:
                reply = str(parsed["reply"]).strip()
                break

            name = str(parsed.get("tool", "")).strip()
            spec = registry.get(name)

            if not spec:
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": f'TOOL ERROR: "{name}" is not a tool. '
                               f'Available: {", ".join(registry.all_tools())}. '
                               f'Try again with one JSON object.',
                })
                continue

            args = {k: v for k, v in parsed.items() if k != "tool"}
            self._say("tool", name=name, args=args, step=step + 1)

            # confirmation-gated tools stop here
            if spec.confirm:
                try:
                    outcome = spec.fn(**_fit(spec, args))
                except Denied as exc:
                    outcome = {"error": str(exc), "gate": exc.gate}
                    self._say("blocked", gate=exc.gate, message=str(exc))
                except TypeError as exc:
                    outcome = {"error": f"Wrong arguments: {exc}"}
                except Exception as exc:
                    outcome = {"error": str(exc)}

                if outcome.get("proposal"):
                    self.pending = Proposal(
                        action=outcome["action"],
                        from_path=outcome["from"],
                        to_path=outcome["to"],
                        from_label=outcome["from_label"],
                        to_label=outcome["to_label"],
                        reason=outcome.get("reason", ""),
                        message=outcome.get("message", ""),
                    )
                    audit.record(outcome["action"].upper(),
                                 f"{outcome['from']} -> {outcome['to']}",
                                 "proposed", outcome.get("reason", ""))
                    reply = outcome.get("reason") or outcome.get("message", "")
                    break

                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user",
                                 "content": "TOOL RESULT:\n" + shrink(outcome)})
                continue

            # ordinary tools run immediately
            try:
                outcome = spec.fn(**_fit(spec, args))
            except Denied as exc:
                outcome = {"error": str(exc), "gate": exc.gate}
                self._say("blocked", gate=exc.gate, message=str(exc))
            except TypeError as exc:
                outcome = {"error": f"Wrong arguments for {name}: {exc}"}
            except Exception as exc:
                outcome = {"error": str(exc)}

            if isinstance(outcome, dict) and not outcome.get("error"):
                self.last_payload = {"tool": name, "data": outcome}
                self._say("data", tool=name, data=outcome)

            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user",
                             "content": "TOOL RESULT:\n" + shrink(outcome)})
        else:
            reply = ("I went round in circles on that one. Try asking for one "
                     "thing at a time.")

        reply = reply or "Done."
        self.history.append({"role": "user", "content": user_text})
        self.history.append({"role": "assistant", "content": reply})
        audit.record("CHAT", "assistant", "out", reply[:160])

        return {
            "reply": reply,
            "proposal": self.pending.as_dict() if self.pending else None,
            "payload": self.last_payload,
        }

    # ── confirmation ────────────────────────────────────────
    def confirm(self) -> Dict:
        if not self.pending:
            return {"reply": "There is nothing waiting for a confirmation."}
        p, self.pending = self.pending, None
        try:
            if p.action == "rename":
                result = organize.commit_rename(p.from_path, p.to_path)
                text = f"Renamed. {p.from_label} is now {p.to_label}."
            elif p.action == "move":
                result = organize.commit_move(p.from_path, p.to_path)
                text = f"Moved. {p.from_label} now lives at {p.to_label}."
            else:
                return {"reply": f"I do not know how to do {p.action}."}
        except Denied as exc:
            self._say("blocked", gate=exc.gate, message=str(exc))
            return {"reply": f"Blocked: {exc}"}
        except OSError as exc:
            return {"reply": f"Windows refused that: {exc}"}

        self.history.append({"role": "assistant", "content": text})
        return {"reply": text, "committed": result}

    def cancel(self) -> Dict:
        if not self.pending:
            return {"reply": "Nothing to cancel."}
        p, self.pending = self.pending, None
        audit.record(p.action.upper(), f"{p.from_path} -> {p.to_path}", "cancelled")
        return {"reply": f"Cancelled. {p.from_label} is untouched."}


# ── parsing helpers ─────────────────────────────────────────
def extract_json(text: str) -> Optional[Dict]:
    """
    Pull the first complete JSON object out of model output.

    Brace-matching, string-aware, escape-aware — so nested objects and
    braces inside string values do not break it. This is the part that a
    naive regex gets wrong.
    """
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"```\s*$", "", text).strip()

    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    chunk = text[start:i + 1]
                    try:
                        obj = json.loads(chunk)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def strip_noise(text: str) -> str:
    """Clean a plain-prose answer that arrived without JSON."""
    text = re.sub(r"```[a-zA-Z]*\n?", "", text or "").replace("```", "")
    return text.strip() or "Done."


def shrink(payload: Any, limit: int = 3500) -> str:
    """Serialise a tool result small enough to feed back into a 3B context."""
    try:
        text = json.dumps(payload, ensure_ascii=False, indent=1, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... (trimmed, {len(text)} chars total)"


def _fit(spec: registry.Tool, args: Dict) -> Dict:
    """
    Map whatever the model called its arguments onto the real signature.

    Small models improvise key names constantly — folder/dir/directory for
    path, name/newname for new_name. Rather than failing the call, translate.
    """
    aliases = {
        "path": ("path", "folder", "dir", "directory", "file", "filepath",
                 "file_path", "target", "location", "root"),
        "new_name": ("new_name", "newname", "name", "rename_to", "new"),
        "destination": ("destination", "dest", "to", "target_folder", "into"),
        "query": ("query", "search", "term", "pattern", "text", "name"),
        "reason": ("reason", "why", "explanation", "justification"),
        "limit": ("limit", "count", "n", "top"),
        "depth": ("depth", "levels", "level"),
    }
    lowered = {str(k).lower(): v for k, v in args.items()}
    out: Dict[str, Any] = {}
    for wanted in spec.params:
        for candidate in aliases.get(wanted, (wanted,)):
            if candidate in lowered:
                out[wanted] = lowered[candidate]
                break
    return out
