from __future__ import annotations

from collections import defaultdict, deque
from typing import Any


class WorkflowDAGExecutor:
    """Execute workflow nodes in topological order with condition support."""

    @staticmethod
    def execute(nodes: list[dict], *, executor_fn) -> dict[str, Any]:
        """Run nodes in dependency order.

        *executor_fn(node, previous_results)* is called for each node.
        """
        node_map = {n["id"]: n for n in nodes}
        in_degree: dict[str, int] = {n["id"]: 0 for n in nodes}
        adj: dict[str, list[str]] = defaultdict(list)

        for n in nodes:
            deps = n.get("depends_on")
            if deps is None or not isinstance(deps, list):
                deps = []
            for dep in deps:
                if dep not in node_map:
                    raise ValueError(f"Node '{n['id']}' depends on unknown node '{dep}'")
                adj[dep].append(n["id"])
                in_degree[n["id"]] += 1

        queue = deque(nid for nid, d in in_degree.items() if d == 0)
        results: dict[str, Any] = {}

        while queue:
            nid = queue.popleft()
            node = node_map[nid]

            if not WorkflowDAGExecutor._check_condition(node, results):
                results[nid] = {"skipped": True, "reason": "condition not met"}
                for neighbor in adj.get(nid, []):
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)
                continue

            try:
                result = executor_fn(node, results)
                results[nid] = result
            except Exception as exc:
                results[nid] = {"error": str(exc)}

            for neighbor in adj.get(nid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        for nid, d in in_degree.items():
            if d > 0 and nid not in results:
                results[nid] = {"skipped": True, "reason": "unmet dependencies or cycle"}

        return results

    @staticmethod
    def _check_condition(node: dict, results: dict) -> bool:
        cond = node.get("condition")
        if cond is None:
            return True
        field = cond.get("field", "")
        op = cond.get("op", "exists")
        value = cond.get("value")

        # Look up field value from previous node results
        actual = None
        for nid, r in results.items():
            if isinstance(r, dict) and field in r:
                actual = r[field]
                break
        if actual is None:
            actual = results.get(field)

        if op == "exists":
            return actual is not None
        if op == "equals":
            return str(actual) == str(value)
        if op == "contains":
            return str(value) in str(actual) if actual is not None else False
        return True
