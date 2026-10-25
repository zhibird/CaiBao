"""Describe the execution actually selected, without persisting credentials.

The public identity is an observation, not a model preference. It is frozen by
the run owner before dispatch; callers must not reconstruct old identities from
today's defaults. Fingerprints detect declared connection changes while keeping
credentials and private prompt/configuration values out of the saved snapshot.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.core.config import Settings
from app.services.cli_session_store import build_cli_session_binding


def safe_execution_endpoint(value: object) -> str | None:
    """Expose an HTTP endpoint without URL credentials, query, or fragment.

    Malformed/non-HTTP values are omitted rather than copied into history.
    Endpoint paths remain useful for distinguishing OpenAI-compatible routes;
    the complete raw URL only participates in the one-way binding hash.
    """
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        hostname = parsed.hostname
        if parsed.scheme.lower() not in {"http", "https"} or not hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, "", ""))


def build_api_execution_identity(
    routes: Mapping[str, object], requested_model: str | None = None,
    provider: str = "",
) -> dict[str, Any]:
    """Return the resolved API model address and a private-config fingerprint.

    ``routes`` must be the live in-memory routes, not their masked persistence
    projection. Every route's credential contributes to the binding, but neither
    the credential nor its individual digest is returned. A credential rotation
    therefore rejects a pending continuation instead of changing its brain.
    """
    public_routes: dict[str, dict[str, str | None]] = {}
    binding_routes: dict[str, dict[str, str]] = {}
    for role in sorted(routes):
        route = routes[role]
        model = _text(_route_value(route, "model_name"))
        endpoint = _text(_route_value(route, "base_url"))
        credential = _text(_route_value(route, "api_key"))
        public_routes[role] = {
            "model": model or None,
            "endpoint": safe_execution_endpoint(endpoint),
            "source": _text(_route_value(route, "source")) or None,
        }
        binding_routes[role] = {
            "model": model,
            "endpoint": endpoint,
            "credential_hash": hashlib.sha256(credential.encode("utf-8")).hexdigest(),
        }
    planner = public_routes.get("planner", {})
    model = planner.get("model")
    requested = _text(requested_model) or None
    actual_provider = _text(provider) or None
    return {
        "version": 1,
        "kind": "api",
        "backend": "api",
        "provider": actual_provider,
        "model": model,
        "model_source": "requested" if model and model == requested else "resolved",
        "requested_model": requested,
        "endpoint": planner.get("endpoint"),
        "routes": public_routes,
        "binding": _fingerprint({
            "version": 1, "kind": "api", "provider": actual_provider,
            "routes": binding_routes,
        }),
    }


def build_cli_execution_identity(
    settings: Settings, mode: str, system_prompt: str = "",
    requested_model: str | None = None, *, dry_run: bool = False,
) -> dict[str, Any]:
    """Describe the declared CLI route, sharing its exact session binding.

    A model omitted from CaiBao's CLI settings is unknown: the CLI may resolve
    its own default from external state. Neither the API default nor the ignored
    request model proves which model that CLI used. Login and CLI-owned external
    configuration changes cannot be observed by this snapshot.
    """
    backend = mode.strip().lower()
    binding = build_cli_session_binding(
        settings, backend, dry_run=dry_run, system_prompt=system_prompt,
    )
    prefix = {"claude_code": "claude", "codex": "codex", "pi": "pi"}[backend]
    model = _text(getattr(settings, f"cli_agent_{prefix}_model")) or None
    provider = (_text(settings.cli_agent_pi_provider) or None) if backend == "pi" else None
    return {
        "version": 1,
        "kind": "cli",
        "backend": backend,
        "provider": provider,
        "model": model,
        "model_source": "configured" if model else "runtime_default",
        "requested_model": _text(requested_model) or None,
        "endpoint": None,
        "binding": binding,
    }


def _route_value(route: object, key: str) -> object:
    if isinstance(route, Mapping):
        return route.get(key)
    return getattr(route, key, None)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _fingerprint(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "v1:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
