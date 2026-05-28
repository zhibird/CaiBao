from __future__ import annotations

from typing import Any, Callable

# ------------------------------------------------------------------
# @on_event — register an EventBus observer handler
# ------------------------------------------------------------------

_on_event_registry: list[tuple[str, Callable]] = []


def on_event(event_type: str):
    """Decorator: register this function as an EventBus observer for *event_type*."""
    def wrapper(fn: Callable):
        _on_event_registry.append((event_type, fn))
        return fn
    return wrapper


def get_on_event_handlers() -> list[tuple[str, Callable]]:
    return list(_on_event_registry)


def clear_on_event_registry() -> None:
    _on_event_registry.clear()


# ------------------------------------------------------------------
# @on_tool_pre — register a pre-execution tool hook
# ------------------------------------------------------------------

ToolPreHook = Callable[[str, dict[str, Any], Any], dict[str, Any]]

_on_tool_pre_registry: list[ToolPreHook] = []


def on_tool_pre(fn: ToolPreHook) -> ToolPreHook:
    """Decorator: register a tool pre-execution hook.

    Signature: ``fn(tool_name: str, arguments: dict, definition) -> dict``.
    Return modified arguments, or raise to deny execution.
    """
    _on_tool_pre_registry.append(fn)
    return fn


def get_on_tool_pre_hooks() -> list[ToolPreHook]:
    return list(_on_tool_pre_registry)


def clear_on_tool_pre_registry() -> None:
    _on_tool_pre_registry.clear()
