"""Tools declare their area and behaviour once; the server publishes only enabled areas.

The behaviour flags are the source of the MCP annotations, and contract tests check that the code
agrees: a tool that notifies others calls the guard, a read-only tool does not write.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

AREAS: dict[str, bool] = {
    # area -> on by default
    "account": True,
    "chats": True,
    "messages": True,
    "people": True,
    "files": True,
    "calls": True,
    "bots": True,
    "chat_admin": False,
}

ALWAYS_ON = frozenset({"account"})

ALLOWED_VERBS = ("list_", "get_", "save_", "send_", "update_", "find_", "download_", "respond_to_")


@dataclass(frozen=True)
class ToolSpec:
    fn: Callable[..., Any]
    name: str
    title: str
    area: str
    read_only: bool
    notifies_others: bool
    irreversible: bool
    idempotent: bool
    returns_list: bool
    description: str = ""
    gates: dict[str, str] = field(default_factory=dict)  # parameter -> area it needs


_TOOLS: dict[str, ToolSpec] = {}


def tool(
    *,
    area: str,
    title: str,
    read_only: bool = False,
    notifies_others: bool = False,
    irreversible: bool = False,
    idempotent: bool = False,
    returns_list: bool = False,
    gates: dict[str, str] | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    if area not in AREAS:
        raise ValueError(f"unknown area {area!r}")
    for gate in (gates or {}).values():
        if gate not in AREAS:
            raise ValueError(f"unknown gate area {gate!r}")

    def register(fn: Callable[..., Any]) -> Callable[..., Any]:
        name = fn.__name__
        if name in _TOOLS:
            raise ValueError(f"tool {name!r} declared twice")
        _TOOLS[name] = ToolSpec(
            fn=fn,
            name=name,
            title=title,
            area=area,
            read_only=read_only,
            notifies_others=notifies_others,
            irreversible=irreversible,
            idempotent=idempotent,
            returns_list=returns_list,
            description=" ".join((fn.__doc__ or "").split()),
            gates=dict(gates or {}),
        )
        return fn

    return register


def all_tools() -> dict[str, ToolSpec]:
    from vkteams_mcp import tools  # noqa: F401  importing the area modules registers their tools

    return dict(_TOOLS)


def enabled(areas: frozenset[str]) -> list[ToolSpec]:
    on = set(areas) | ALWAYS_ON
    return [s for s in all_tools().values() if s.area in on]


def parse_areas(value: str | None) -> frozenset[str]:
    if not value:
        return frozenset(a for a, on in AREAS.items() if on)
    names = {a.strip() for a in value.split(",") if a.strip()}
    unknown = names - set(AREAS)
    if unknown:
        raise ValueError(f"unknown area(s): {', '.join(sorted(unknown))}")
    return frozenset(names) | ALWAYS_ON
