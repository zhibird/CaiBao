from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.plugins.base import PhaseFrame, PhaseModule, Plugin
from app.plugins.context import PluginContext
from app.plugins.decorators import on_event, on_tool_pre, _on_event_registry, _on_tool_pre_registry
from app.plugins.manager import PluginManager
from app.plugins.pipeline import PipelineError, run_phase_modules, validate_slots


class EchoModule(PhaseModule):
    name = "echo"
    phase = "BeforeTurn"

    def run(self, ctx, frame):
        frame.slots["echo_ran"] = True
        return frame


class RequireSlotModule(PhaseModule):
    name = "consumer"
    phase = "BeforeTurn"
    requires = ["data_from_producer"]

    def run(self, ctx, frame):
        frame.slots["consumed"] = frame.slots.get("data_from_producer")
        return frame


class ProduceSlotModule(PhaseModule):
    name = "producer"
    phase = "BeforeTurn"
    produces = ["data_from_producer"]

    def run(self, ctx, frame):
        frame.slots["data_from_producer"] = "hello"
        return frame


_rollback_log: list[str] = []


class FailingModule(PhaseModule):
    name = "failer"
    phase = "BeforeTurn"
    executed: list[str] = []

    def run(self, ctx, frame):
        self.executed.append(self.name)
        raise RuntimeError("intentional failure")

    def rollback(self, ctx, frame):
        _rollback_log.append(type(self).__name__)


class RollbackEchoModule(PhaseModule):
    name = "rb_echo"
    phase = "BeforeTurn"

    def run(self, ctx, frame):
        frame.slots["rb_ran"] = True
        return frame

    def rollback(self, ctx, frame):
        _rollback_log.append(type(self).__name__)


class TestPhaseModule:
    def test_single_module_executes(self):
        m = EchoModule()
        ctx = PluginContext(team_id="t1", user_id="u1", settings=mock.MagicMock())
        frame = PhaseFrame(run_id="r1", team_id="t1", user_id="u1")
        result = m.run(ctx, frame)
        assert result.slots["echo_ran"] is True

    def test_slot_dependency_chain(self):
        producer = ProduceSlotModule()
        consumer = RequireSlotModule()
        ctx = PluginContext(team_id="t1", user_id="u1", settings=mock.MagicMock())
        frame = PhaseFrame(run_id="r1", team_id="t1", user_id="u1")

        result = run_phase_modules([producer, consumer], ctx, frame)
        assert result.slots["consumed"] == "hello"


class TestPipeline:
    def test_topological_sort(self):
        producer = ProduceSlotModule()
        consumer = RequireSlotModule()
        ctx = PluginContext(team_id="t1", user_id="u1", settings=mock.MagicMock())
        frame = PhaseFrame(run_id="r1", team_id="t1", user_id="u1")

        result = run_phase_modules([consumer, producer], ctx, frame)
        assert result.slots["consumed"] == "hello"

    def test_missing_slot_detected(self):
        consumer = RequireSlotModule()
        missing = validate_slots([consumer])
        assert len(missing) == 1
        assert "data_from_producer" in missing[0]

    def test_valid_slots_pass(self):
        producer = ProduceSlotModule()
        consumer = RequireSlotModule()
        missing = validate_slots([producer, consumer])
        assert missing == []

    def test_fail_fast_rolls_back(self):
        _rollback_log.clear()
        FailingModule.executed.clear()
        echo = RollbackEchoModule()
        failer = FailingModule()
        ctx = PluginContext(team_id="t1", user_id="u1", settings=mock.MagicMock())
        frame = PhaseFrame(run_id="r1", team_id="t1", user_id="u1")

        with pytest.raises(RuntimeError, match="intentional failure"):
            run_phase_modules([echo, failer], ctx, frame, fail_fast=True)

        assert "failer" in FailingModule.executed
        assert "RollbackEchoModule" in _rollback_log


class TestPluginManager:
    def test_loads_plugin_from_directory(self, tmp_path):
        plugin_dir = tmp_path / "test_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.py").write_text("""
from app.plugins.base import PhaseModule, PhaseFrame, Plugin

class TestModule(PhaseModule):
    name = "test_mod"
    phase = "BeforeTurn"
    def run(self, ctx, frame):
        frame.slots["loaded"] = True
        return frame

class TestPlugin(Plugin):
    name = "test_plugin"
    version = "0.1.0"
    modules = [TestModule()]

def create_plugin():
    return TestPlugin()
""")

        with mock.patch("app.plugins.manager.get_settings") as m:
            m.return_value.plugin_enabled = True
            m.return_value.plugin_dirs = str(tmp_path)
            m.return_value.plugin_fail_fast = False

            mgr = PluginManager()
            loaded = mgr.load_all()
            assert "test_plugin" in loaded

            frame = PhaseFrame(run_id="r1", team_id="t1", user_id="u1")
            result = mgr.run_phase("BeforeTurn", frame)
            assert result.slots["loaded"] is True


class TestDecorators:
    def test_on_event_registers_handler(self):
        _on_event_registry.clear()
        @on_event("TurnStarted")
        def handler(event):
            pass
        handlers = [(t, h) for t, h in _on_event_registry if h is handler]
        assert len(handlers) == 1
        assert handlers[0][0] == "TurnStarted"

    def test_on_tool_pre_registers_hook(self):
        _on_tool_pre_registry.clear()
        @on_tool_pre
        def hook(tool_name, args, definition):
            return args
        assert hook in _on_tool_pre_registry
