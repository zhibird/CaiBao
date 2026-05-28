from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from queue import Empty, Queue
from typing import Any, Callable

_logger = logging.getLogger(__name__)

_STOP_SENTINEL = object()
HandlerType = Callable[[Any], Any | None]


class EventBus:
    """Synchronous-friendly typed event bus.

    * ``emit(event)`` — sequential interception: each handler receives the
      event and may return a replacement or ``None`` to swallow it.
    * ``observe(event)`` — sequential fire-and-forget; exceptions are
      logged but never propagated.
    * ``fanout(event)`` — concurrent observe via thread pool; each handler
      runs in its own future, failures are logged.
    * ``enqueue(event)`` — push to a background queue processed by
      ``fanout``, decoupling the caller from handler execution.
    """

    def __init__(self, *, max_workers: int = 4) -> None:
        self._lock = threading.Lock()
        self._handlers: dict[str, list[HandlerType]] = {}
        self._observers: dict[str, list[HandlerType]] = {}
        self._queue: Queue[Any] | None = None
        self._worker: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="evtbus")
        self._running = True

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on(self, event_type: str, handler: HandlerType) -> None:
        """Register an *intercepting* handler. Handlers run in registration order."""
        with self._lock:
            self._handlers.setdefault(event_type, []).append(handler)

    def observe(self, event_type: str, handler: HandlerType) -> None:
        """Register a *fire-and-forget* observer. Handlers run in registration order."""
        with self._lock:
            self._observers.setdefault(event_type, []).append(handler)

    # ------------------------------------------------------------------
    # Synchronous dispatch
    # ------------------------------------------------------------------

    def emit(self, event: Any) -> Any | None:
        """Sequential interception + notification.

        Each ``on`` handler receives the event and may return a modified
        event or ``None`` to swallow (stop the chain).  The result of the
        previous handler becomes the input of the next.

        After all interceptors have run, the final event is also delivered
        to registered ``observe`` handlers so that notification-style
        subscriptions (plugins, audit) receive lifecycle events.
        """
        event_type = type(event).__name__
        current = event
        for handler in list(self._handlers.get(event_type, [])):
            try:
                result = handler(current)
            except Exception:
                _logger.exception("EventBus emit handler failed for %s", event_type)
                continue
            if result is None:
                return None  # swallowed
            current = result
        if current is not event:
            # Deliver the (possibly replaced) event to observers
            self._notify_observers(current)
        else:
            self._notify_observers(event)
        return current

    def _notify_observers(self, event: Any) -> None:
        """Deliver event to all registered observers (not interceptors)."""
        event_type = type(event).__name__
        for handler in list(self._observers.get(event_type, [])):
            try:
                handler(event)
            except Exception:
                _logger.exception("EventBus observer failed for %s", event_type)

    def observe_event(self, event: Any) -> None:
        """Sequential fire-and-forget observation (without interceptors)."""
        self._notify_observers(event)

    # ------------------------------------------------------------------
    # Concurrent / async dispatch
    # ------------------------------------------------------------------

    def fanout(self, event: Any) -> list[Future]:
        """Concurrent fire-and-forget via thread pool."""
        event_type = type(event).__name__
        futures: list[Future] = []
        for handler in list(self._observers.get(event_type, [])):
            futures.append(self._executor.submit(self._safe_observe, handler, event))
        return futures

    def enqueue(self, event: Any) -> None:
        """Push event to background queue; process via ``fanout``."""
        if not self._running:
            return
        q = self._queue
        if q is None:
            self.fanout(event)
            return
        q.put(event)

    # ------------------------------------------------------------------
    # Background worker
    # ------------------------------------------------------------------

    def start_worker(self) -> None:
        """Start the background queue consumer thread."""
        if self._worker is not None:
            return
        self._queue = Queue()
        self._running = True
        self._worker = threading.Thread(target=self._drain_queue, daemon=True, name="evtbus-worker")
        self._worker.start()

    def stop_worker(self, *, timeout: float = 2.0) -> None:
        """Signal the worker to drain remaining items then stop."""
        self._running = False
        if self._queue is not None:
            self._queue.put(_STOP_SENTINEL)
        if self._worker is not None:
            self._worker.join(timeout=timeout)
            self._worker = None
        self._queue = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _safe_observe(self, handler: HandlerType, event: Any) -> None:
        try:
            handler(event)
        except Exception:
            _logger.exception("EventBus fanout handler failed for %s", type(event).__name__)

    def _drain_queue(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5) if self._queue else None
            except Empty:
                continue
            if item is _STOP_SENTINEL:
                break
            futures = self.fanout(item)
            for f in futures:
                try:
                    f.result(timeout=30)
                except Exception:
                    pass

    def shutdown(self) -> None:
        """Stop worker and executor pool."""
        self.stop_worker()
        self._executor.shutdown(wait=False)
