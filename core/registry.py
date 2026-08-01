"""
Tool registry — the extension point.

Adding a new ability to Steward is one decorated function:

    from core.registry import tool

    @tool(
        name="count_lines",
        description="Count lines of code in a file",
        params={"path": "File to count"},
    )
    def count_lines(path: str) -> dict:
        target = sandbox.resolve(path, must_exist=True)
        return {"lines": len(target.read_text().splitlines())}

Drop that in core/tools/anything.py and it is live on next start:
the registry finds it, the system prompt teaches the model about it,
the agent can call it, and the audit log records it. No other file
needs touching.

Put its name in settings.REQUIRES_CONFIRMATION and it becomes gated
behind a Confirm button too.
"""
from __future__ import annotations

import importlib
import pkgutil
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from . import settings


@dataclass
class Tool:
    name: str
    description: str
    params: Dict[str, str] = field(default_factory=dict)
    fn: Callable[..., Any] = None
    confirm: bool = False
    examples: List[str] = field(default_factory=list)

    def spec_line(self) -> str:
        args = "".join(f', "{k}": "..."' for k in self.params)
        return f'{{"tool": "{self.name}"{args}}}  — {self.description}'


_REGISTRY: Dict[str, Tool] = {}


def tool(name: str, description: str, params: Dict[str, str] = None,
         examples: List[str] = None):
    def wrap(fn):
        _REGISTRY[name] = Tool(
            name=name,
            description=description,
            params=params or {},
            fn=fn,
            confirm=name in settings.REQUIRES_CONFIRMATION,
            examples=examples or [],
        )
        return fn
    return wrap


def discover() -> int:
    """Import every module in core.tools so decorators run."""
    from . import tools as tools_pkg

    for mod in pkgutil.iter_modules(tools_pkg.__path__):
        importlib.import_module(f"{tools_pkg.__name__}.{mod.name}")
    return len(_REGISTRY)


def all_tools() -> Dict[str, Tool]:
    return dict(_REGISTRY)


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def prompt_spec() -> str:
    """The tool catalogue, rendered for the system prompt."""
    lines = []
    for t in _REGISTRY.values():
        lines.append(t.spec_line())
        for key, desc in t.params.items():
            lines.append(f'      "{key}": {desc}')
    return "\n".join(lines)


def catalogue() -> List[dict]:
    """Machine-readable list for the UI."""
    return [
        {
            "name": t.name,
            "description": t.description,
            "params": list(t.params),
            "confirm": t.confirm,
        }
        for t in _REGISTRY.values()
    ]

