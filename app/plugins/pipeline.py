from __future__ import annotations

from collections import defaultdict, deque
from typing import Any

from app.plugins.base import PhaseFrame, PhaseModule
from app.plugins.context import PluginContext


class PipelineError(Exception):
    """Raised when the phase pipeline cannot resolve dependencies."""


def _topological_sort(modules: list[PhaseModule]) -> list[PhaseModule]:
    """Sort modules by requires/produces dependency graph (Kahn's algorithm).

    A requirement can match either a module name or a produced slot name.
    """
    name_to_idx: dict[str, int] = {}
    slot_to_producers: dict[str, list[int]] = defaultdict(list)
    idx_to_mod: dict[int, PhaseModule] = {}
    for i, m in enumerate(modules):
        idx_to_mod[i] = m
        if m.name:
            name_to_idx[m.name] = i
        for slot in m.produces:
            slot_to_producers[slot].append(i)

    in_degree = [0] * len(modules)
    adj: dict[int, list[int]] = defaultdict(list)

    for i, m in enumerate(modules):
        for req in m.requires:
            # Try matching module name first, then produced slot name
            providers = []
            if req in name_to_idx:
                providers.append(name_to_idx[req])
            if req in slot_to_producers:
                providers.extend(slot_to_producers[req])
            for provider_idx in set(providers):
                if provider_idx != i:  # no self-loops
                    adj[provider_idx].append(i)
                    in_degree[i] += 1

    queue = deque(i for i, d in enumerate(in_degree) if d == 0)
    result: list[PhaseModule] = []

    while queue:
        node = queue.popleft()
        result.append(idx_to_mod[node])
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(modules):
        # Cycle or missing dependency
        missing = [m for i, m in enumerate(modules) if in_degree[i] > 0]
        names = [m.name for m in missing]
        raise PipelineError(f"Circular dependency or missing provider for: {names}")

    return result


def run_phase_modules(
    modules: list[PhaseModule],
    ctx: PluginContext,
    frame: PhaseFrame,
    *,
    fail_fast: bool = False,
) -> PhaseFrame:
    """Topologically sort and execute all modules for a given phase.

    If *fail_fast* is True and any module raises, previously-executed
    modules are rolled back in reverse order.
    """
    ordered = _topological_sort(modules)
    executed: list[PhaseModule] = []

    try:
        for mod in ordered:
            frame = mod.run(ctx, frame)
            executed.append(mod)
    except Exception:
        if fail_fast:
            for mod in reversed(executed):
                try:
                    mod.rollback(ctx, frame)
                except Exception:
                    pass
        raise

    return frame


def validate_slots(modules: list[PhaseModule]) -> list[str]:
    """Check that all required slots are produced by some module. Returns missing slots."""
    produced: set[str] = set()
    for m in modules:
        produced.update(m.produces)

    missing: list[str] = []
    for m in modules:
        for req in m.requires:
            if req not in produced:
                missing.append(f"{m.name}: requires '{req}' (not produced by any module)")
    return missing
