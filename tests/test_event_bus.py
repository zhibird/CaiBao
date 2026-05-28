from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import pytest

from app.events.event_bus import EventBus


@dataclass
class TestEvent:
    value: str
    count: int = 0


@dataclass
class OtherEvent:
    data: str


class TestEventBusEmit:
    def test_emit_runs_handler(self):
        bus = EventBus()
        seen = []

        def handler(event):
            seen.append(event.value)
            return event

        bus.on("TestEvent", handler)
        bus.emit(TestEvent("hello"))
        assert seen == ["hello"]

    def test_emit_can_replace_event(self):
        bus = EventBus()
        first_called = []
        second_called = []

        def first(event):
            first_called.append(event.value)
            return TestEvent("replaced", 99)

        def second(event):
            second_called.append((event.value, event.count))
            return event

        bus.on("TestEvent", first)
        bus.on("TestEvent", second)
        bus.emit(TestEvent("original", 0))
        assert first_called == ["original"]
        assert second_called == [("replaced", 99)]

    def test_emit_can_swallow_event(self):
        bus = EventBus()
        second_called = []

        def first(event):
            return None  # swallow

        def second(event):
            second_called.append(1)
            return event

        bus.on("TestEvent", first)
        bus.on("TestEvent", second)
        result = bus.emit(TestEvent("swallowed"))
        assert result is None
        assert second_called == []

    def test_emit_handlers_called_in_order(self):
        bus = EventBus()
        order = []

        def h1(e):
            order.append(1)
            return e

        def h2(e):
            order.append(2)
            return e

        bus.on("TestEvent", h1)
        bus.on("TestEvent", h2)
        bus.emit(TestEvent("x"))
        assert order == [1, 2]


class TestEventBusObserve:
    def test_observe_fires_in_order(self):
        bus = EventBus()
        order = []

        def o1(e):
            order.append("a")

        def o2(e):
            order.append("b")

        bus.observe("TestEvent", o1)
        bus.observe("TestEvent", o2)
        bus.observe_event(TestEvent("x"))
        assert order == ["a", "b"]

    def test_observe_failure_does_not_prevent_others(self):
        bus = EventBus()
        second_called = []

        def failing(e):
            raise RuntimeError("boom")

        def ok(e):
            second_called.append(1)

        bus.observe("TestEvent", failing)
        bus.observe("TestEvent", ok)
        bus.observe_event(TestEvent("x"))
        assert second_called == [1]

    def test_observe_does_not_modify_event_for_other_observers(self):
        bus = EventBus()
        seen = []

        def o1(e):
            e.value = "modified by o1"

        def o2(e):
            seen.append(e.value)

        bus.observe("TestEvent", o1)
        bus.observe("TestEvent", o2)
        # Observers share the same event object — mutation is visible
        bus.observe_event(TestEvent("original"))
        # o2 sees the mutation from o1
        assert "modified" in seen[0]


class TestEventBusFanout:
    def test_fanout_runs_all_observers(self):
        bus = EventBus()
        results = []

        def o1(e):
            time.sleep(0.05)
            results.append("o1")

        def o2(e):
            results.append("o2")

        bus.observe("TestEvent", o1)
        bus.observe("TestEvent", o2)
        futures = bus.fanout(TestEvent("x"))
        # Wait for all futures
        for f in futures:
            f.result(timeout=2)
        assert sorted(results) == ["o1", "o2"]


class TestEventBusEnqueue:
    def test_enqueue_without_worker_falls_back_to_fanout(self):
        bus = EventBus()
        results = []

        def o1(e):
            results.append(e.value)

        bus.observe("TestEvent", o1)
        bus.enqueue(TestEvent("hello"))
        assert results == ["hello"]

    def test_enqueue_with_worker_drains(self):
        bus = EventBus()
        results = []

        def o1(e):
            results.append(e.value)

        bus.observe("TestEvent", o1)
        bus.start_worker()
        bus.enqueue(TestEvent("async"))
        # Give worker time to process
        time.sleep(0.3)
        bus.stop_worker()
        assert results == ["async"]

    def test_stop_worker_drains_remaining(self):
        bus = EventBus()
        results = []

        def o1(e):
            results.append(e.value)

        bus.observe("TestEvent", o1)
        bus.start_worker()
        bus.enqueue(TestEvent("a"))
        bus.enqueue(TestEvent("b"))
        bus.stop_worker(timeout=2)
        assert results == ["a", "b"]


class TestEventBusNonMatching:
    def test_events_only_trigger_matching_handlers(self):
        bus = EventBus()
        test_called = []
        other_called = []

        bus.on("TestEvent", lambda e: test_called.append(1) or e)
        bus.on("OtherEvent", lambda e: other_called.append(1) or e)
        bus.emit(TestEvent("x"))
        assert test_called == [1]
        assert other_called == []
