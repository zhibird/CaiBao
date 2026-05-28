from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PhaseFrame:
    """Data bag that flows through the Phase pipeline."""
    run_id: str
    team_id: str
    user_id: str
    space_id: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)


@dataclass
class PluginContext:
    """Read-only context available to every PhaseModule."""
    team_id: str
    user_id: str
    settings: Any  # Settings instance


class PhaseModule:
    """A single phase hook with slot dependencies.

    Subclasses override name, phase, requires, produces as class attrs.
    """
    name: str = ""
    phase: str = ""
    requires: list[str] = []
    produces: list[str] = []

    def run(self, ctx: PluginContext, frame: PhaseFrame) -> PhaseFrame:
        raise NotImplementedError

    def rollback(self, ctx: PluginContext, frame: PhaseFrame) -> None:
        pass


class Plugin:
    """Top-level plugin container loaded from a plugin directory."""
    name: str = ""
    version: str = "0.1.0"
    modules: list[PhaseModule] = []
    tool_pre_hooks: list = []
    event_handlers: list[tuple[str, object]] = []

    def __init__(self) -> None:
        # Always copy mutable class attrs to prevent shared state across instances
        self.modules = list(self.__class__.modules)
        self.tool_pre_hooks = list(self.__class__.tool_pre_hooks)
        self.event_handlers = list(self.__class__.event_handlers)

    def on_load(self, ctx: PluginContext) -> None:
        pass

    def on_unload(self, ctx: PluginContext) -> None:
        pass
