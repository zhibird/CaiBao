from __future__ import annotations

import logging
import threading
import time

_logger = logging.getLogger(__name__)


class ProactiveScheduler:
    """Background thread that runs proactive ticks on an interval. Default OFF."""

    def __init__(
        self,
        tick_fn,
        *,
        interval_seconds: float = 60.0,
    ) -> None:
        self._tick_fn = tick_fn
        self._interval = interval_seconds
        self._thread: threading.Thread | None = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="proactive-scheduler")
        self._thread.start()
        _logger.info("ProactiveScheduler started (interval=%ss)", self._interval)

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def _loop(self) -> None:
        while self._running:
            try:
                self._tick_fn()
            except Exception:
                _logger.exception("Proactive tick failed")
            time.sleep(self._interval)
