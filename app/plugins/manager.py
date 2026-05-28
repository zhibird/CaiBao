from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.events.event_bus import EventBus
from app.plugins.base import PhaseFrame, Plugin, PluginContext
from app.plugins.context import PluginContext as Ctx
from app.plugins.decorators import _on_event_registry, _on_tool_pre_registry
from app.plugins.pipeline import PipelineError, run_phase_modules, validate_slots

_logger = logging.getLogger(__name__)


class PluginManager:
    """Scans plugin directories, loads plugins, and manages lifecycle."""

    def __init__(
        self,
        *,
        event_bus: EventBus | None = None,
    ) -> None:
        self._bus = event_bus
        self._plugins: dict[str, Plugin] = {}        # name → Plugin
        self._phase_modules: dict[str, list] = {}    # phase → [PhaseModule]
        self._tool_pre_hooks: list = []              # [(tool_name_or_None, fn)]
        self._loaded = False
        self.settings = get_settings()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def load_all(self) -> list[str]:
        """Scan plugin dirs and load all plugins. Returns list of loaded names."""
        if not self.settings.plugin_enabled:
            return []

        dirs = [d.strip() for d in self.settings.plugin_dirs.split(",") if d.strip()]
        loaded: list[str] = []

        for d in dirs:
            root = Path(d)
            if not root.is_dir():
                continue
            for item in sorted(root.iterdir()):
                if not item.is_dir():
                    continue
                plugin_file = item / "plugin.py"
                if not plugin_file.exists():
                    continue
                try:
                    plugin = self._load_plugin(item.name, plugin_file)
                    self._plugins[plugin.name] = plugin

                    ctx = Ctx(team_id="", user_id="", settings=self.settings)
                    plugin.on_load(ctx)

                    # Register phase modules
                    for mod in plugin.modules:
                        phase = mod.phase
                        self._phase_modules.setdefault(phase, []).append(mod)

                    # Register tool pre hooks
                    for hook in plugin.tool_pre_hooks:
                        self._tool_pre_hooks.append(hook)

                    # Register event handlers
                    if self._bus:
                        for evt_type, handler in plugin.event_handlers:
                            self._bus.observe(evt_type, handler)

                    loaded.append(plugin.name)

                except Exception as exc:
                    _logger.exception("Failed to load plugin '%s'", item.name)
                    if self.settings.plugin_fail_fast:
                        raise

        self._loaded = True
        return loaded

    def shutdown(self) -> None:
        ctx = Ctx(team_id="", user_id="", settings=self.settings)
        for plugin in self._plugins.values():
            try:
                plugin.on_unload(ctx)
            except Exception:
                _logger.exception("Plugin '%s' on_unload failed", plugin.name)
        self._plugins.clear()
        self._phase_modules.clear()
        self._tool_pre_hooks.clear()
        self._loaded = False

    # ------------------------------------------------------------------
    # Phase execution
    # ------------------------------------------------------------------

    def run_phase(self, phase: str, frame: PhaseFrame) -> PhaseFrame:
        """Run all registered modules for *phase* in dependency order."""
        modules = self._phase_modules.get(phase, [])
        if not modules:
            return frame

        ctx = PluginContext(
            team_id=frame.team_id,
            user_id=frame.user_id,
            settings=self.settings,
        )
        return run_phase_modules(modules, ctx, frame, fail_fast=self.settings.plugin_fail_fast)

    def validate_phase(self, phase: str) -> list[str]:
        """Return missing slot dependencies for *phase*."""
        modules = self._phase_modules.get(phase, [])
        return validate_slots(modules)

    # ------------------------------------------------------------------
    # Tool pre hooks
    # ------------------------------------------------------------------

    def check_tool_pre(self, tool_name: str, arguments: dict[str, Any], definition: Any) -> dict[str, Any]:
        """Run all registered @on_tool_pre hooks.

        If a hook raises, the tool call is **denied** by default.
        Set plugin_fail_fast=false only if you want hooks to be advisory.
        """
        result = dict(arguments)
        for hook in self._tool_pre_hooks:
            try:
                result = hook(tool_name, result, definition)
            except Exception:
                _logger.exception("Tool pre-hook denied '%s'", tool_name)
                raise  # deny by default — plugin interceptors are security boundaries
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load_plugin(self, name: str, path: Path) -> Plugin:
        spec = importlib.util.spec_from_file_location(f"caibao_plugin_{name}", str(path))
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load plugin module: {path}")
        # Clear per-plugin decorator registries BEFORE exec so this plugin's
        # @on_tool_pre / @on_event decorators populate a clean registry.
        from app.plugins.decorators import clear_on_event_registry, clear_on_tool_pre_registry
        clear_on_event_registry()
        clear_on_tool_pre_registry()

        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        factory = getattr(module, "create_plugin", None)
        if factory is None:
            raise AttributeError(f"Plugin '{name}' must export create_plugin()")

        plugin = factory()
        if not isinstance(plugin, Plugin):
            raise TypeError(f"create_plugin() in '{name}' must return a Plugin instance")

        # Collect tool pre hooks from decorators
        for fn in _on_tool_pre_registry:
            plugin.tool_pre_hooks.append(fn)

        # Collect event handlers from decorators
        for evt_type, handler in _on_event_registry:
            plugin.event_handlers.append((evt_type, handler))

        return plugin
