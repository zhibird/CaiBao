from __future__ import annotations

from urllib.parse import urlparse

from app.core.exceptions import DomainValidationError


_OPENAI_COMPATIBLE_ENDPOINT_SUFFIXES = (
    "/responses/chat/completions",
    "/chat/completions",
    "/responses",
)


def normalize_openai_compatible_base_url(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DomainValidationError("base_url must be a valid http(s) URL.")

    lowered = value.lower()
    changed = True
    while changed:
        changed = False
        for suffix in _OPENAI_COMPATIBLE_ENDPOINT_SUFFIXES:
            if lowered.endswith(suffix):
                value = value[: -len(suffix)].rstrip("/")
                lowered = value.lower()
                changed = True
                break

    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise DomainValidationError("base_url must be a valid http(s) URL.")
    return value
