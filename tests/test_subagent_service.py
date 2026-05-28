from __future__ import annotations

from unittest import mock

import pytest

from app.services.workflow_dag_executor import WorkflowDAGExecutor


class TestWorkflowDAGExecutor:
    def test_linear_dag(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": ["a"]},
            {"id": "c", "type": "final", "depends_on": ["b"]},
        ]
        order = []

        def executor(node, results):
            order.append(node["id"])
            return {"node_id": node["id"]}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert order == ["a", "b", "c"]
        assert results["c"]["node_id"] == "c"

    def test_parallel_nodes(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": []},
            {"id": "c", "type": "final", "depends_on": ["a", "b"]},
        ]
        order = []

        def executor(node, results):
            order.append(node["id"])
            return {"node_id": node["id"]}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert order[:2] == ["a", "b"] or order[:2] == ["b", "a"]
        assert order[2] == "c"

    def test_condition_equals(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": ["a"], "condition": {"field": "status", "op": "equals", "value": "ok"}},
        ]

        def executor(node, results):
            return {"status": "ok"}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert results["a"]["status"] == "ok"
        assert results["b"]["status"] == "ok"  # condition met

    def test_condition_skips(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": ["a"], "condition": {"field": "status", "op": "equals", "value": "nope"}},
        ]

        def executor(node, results):
            return {"status": "not_nope"}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert results["b"]["skipped"] is True

    def test_unknown_dependency_raises(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": ["nonexistent"]},
        ]
        with pytest.raises(ValueError, match="nonexistent"):
            WorkflowDAGExecutor.execute(nodes, executor_fn=lambda n, r: {})

    def test_condition_exists(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": ["a"], "condition": {"field": "data", "op": "exists"}},
        ]
        calls = []

        def executor(node, results):
            calls.append(node["id"])
            return {"data": "present"}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert "b" in calls

    def test_condition_contains(self):
        nodes = [
            {"id": "a", "type": "tool_call", "depends_on": []},
            {"id": "b", "type": "tool_call", "depends_on": ["a"], "condition": {"field": "output", "op": "contains", "value": "error"}},
        ]
        calls = []

        def executor(node, results):
            calls.append(node["id"])
            return {"output": "found an error here"}

        results = WorkflowDAGExecutor.execute(nodes, executor_fn=executor)
        assert "b" in calls


class TestSubAgentProfileTools:
    def test_research_profile_has_safe_tools(self):
        from app.services.subagent_service import _PROFILE_TOOLS
        research = _PROFILE_TOOLS["research"]
        assert "web_search" in research
        assert "shell_exec" not in research
        assert "write_file" not in research

    def test_ops_profile_has_safe_tools(self):
        from app.services.subagent_service import _PROFILE_TOOLS
        ops = _PROFILE_TOOLS["ops"]
        assert "search_knowledge" in ops
        assert "shell_exec" not in ops
        assert "write_file" not in ops
