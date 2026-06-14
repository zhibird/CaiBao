"""Shared HTTP infrastructure: retry, request budget, and connection profiles.

Adapted from akashic-agent's ``core/net/http.py``, sync-ified for CaiBao.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass

import httpx


# ── Retry policy ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RetryPolicy:
    """Retry configuration for transient HTTP failures.

    Attributes:
        max_attempts: Maximum total attempts (including the first).
        retry_statuses: HTTP status codes eligible for retry.
        base_delay_s: Base backoff delay in seconds.
        max_delay_s: Ceiling on computed backoff delay.
        jitter_ratio: Fraction of delay to randomise (±).
    """

    max_attempts: int = 3
    retry_statuses: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})
    base_delay_s: float = 0.3
    max_delay_s: float = 1.5
    jitter_ratio: float = 0.2


@dataclass(frozen=True)
class RequestBudget:
    """Absolute deadline for a request including all retries."""

    total_timeout_s: float


# ── Pre-defined policies ─────────────────────────────────────────────────────

RETRY_STANDARD = RetryPolicy()
"""Standard retry: 3 attempts, exponential backoff with jitter."""

RETRY_FAST = RetryPolicy(max_attempts=2, base_delay_s=0.15, max_delay_s=0.3)
"""Fast retry: 2 attempts, shorter backoff. For quick-polling endpoints."""

RETRY_NONE = RetryPolicy(max_attempts=1)
"""No retry — single attempt only."""


# ── Retry loop ───────────────────────────────────────────────────────────────

def retry_request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    retry_policy: RetryPolicy = RETRY_STANDARD,
    budget: RequestBudget | None = None,
    default_timeout_s: float = 30.0,
    **request_kwargs,
) -> httpx.Response:
    """Perform an HTTP request with configurable retry logic.

    Retries on:
    - Response status codes in ``retry_policy.retry_statuses``.
    - ``httpx.TimeoutException`` and ``httpx.TransportError`` exceptions.

    Uses exponential backoff with jitter between attempts.  A
    ``RequestBudget`` enforces an absolute deadline across all retries.

    Args:
        client: An ``httpx.Client`` instance (can be a context-managed one).
        method: HTTP method (``GET``, ``POST``, etc.).
        url: Target URL.
        retry_policy: Retry configuration.
        budget: Absolute deadline.  Defaults to ``default_timeout_s * 2``.
        default_timeout_s: Per-attempt timeout in seconds.
        **request_kwargs: Passed through to ``client.request()``
            (e.g. ``headers``, ``params``, ``json``, ``content``).

    Returns:
        The ``httpx.Response`` on success.

    Raises:
        httpx.TimeoutException: When the budget is exhausted.
        httpx.HTTPError: Other terminal HTTP errors after retries exhausted.
    """
    attempts = max(1, retry_policy.max_attempts)
    if budget is None:
        budget = RequestBudget(total_timeout_s=default_timeout_s * 2.0)

    method_up = method.upper()
    deadline = time.monotonic() + budget.total_timeout_s

    last_error: Exception | None = None
    response: httpx.Response | None = None
    budget_exhausted = False

    for attempt in range(1, attempts + 1):
        remaining = max(0.0, deadline - time.monotonic())
        if remaining <= 0:
            budget_exhausted = True
            break

        try:
            response = client.request(
                method_up,
                url,
                timeout=min(default_timeout_s, remaining),
                **request_kwargs,
            )
            if not _should_retry_response(response, attempt, attempts, retry_policy):
                return response
            # Read and discard body so the connection can be reused.
            response.read()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            if not _should_retry_exception(exc, attempt, attempts):
                raise

        sleep_s = min(
            _backoff_seconds(retry_policy, attempt),
            max(0.0, deadline - time.monotonic()),
        )
        if sleep_s > 0:
            time.sleep(sleep_s)

    if last_error is not None:
        raise last_error
    if budget_exhausted or response is None:
        raise httpx.TimeoutException("request budget exhausted")
    return response


# ── Internal helpers ─────────────────────────────────────────────────────────

def _should_retry_response(
    response: httpx.Response,
    attempt: int,
    attempts: int,
    policy: RetryPolicy,
) -> bool:
    return attempt < attempts and response.status_code in policy.retry_statuses


def _should_retry_exception(
    exc: Exception,
    attempt: int,
    attempts: int,
) -> bool:
    return attempt < attempts and isinstance(
        exc,
        (httpx.TimeoutException, httpx.TransportError),
    )


def _backoff_seconds(policy: RetryPolicy, attempt: int) -> float:
    delay = min(
        policy.max_delay_s,
        policy.base_delay_s * (2 ** max(0, attempt - 1)),
    )
    jitter = delay * policy.jitter_ratio
    return max(0.0, delay + random.uniform(-jitter, jitter))
