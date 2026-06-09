"""Web tools: web_fetch (抓取网页) and web_search (网络搜索).

web_fetch — Inspired by akashic-agent's WebFetchTool:
  - lxml DOM-based HTML → text (properly removes script/style/noscript)
  - html2text for HTML → Markdown
  - User-Agent + Accept header negotiation
  - Binary content detection & rejection
  - SSRF protection with DNS-resolved IP filtering

web_search — Inspired by akashic-agent's WebSearchTool:
  - Exa MCP public endpoint (free, no API key)
  - Brave / Tavily with API key
  - MCP JSON-RPC 2.0 over SSE
"""

from __future__ import annotations

import html as _html
import hashlib
import ipaddress
import json as _json
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode
from xml.etree import ElementTree
from urllib.parse import urljoin, urlparse

import html2text as _html2text
import httpx
from lxml import html as lxml_html
from lxml.etree import ParserError

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError
from app.services.tool_registry import ToolDefinition

# ── constants ──────────────────────────────────────────────────────────────

_MAX_BYTES = 5 * 1024 * 1024          # 5 MB, same as akashic-agent / OpenCode
_MAX_TEXT_CHARS = 50_000               # ~12K tokens
_DEFAULT_TIMEOUT = 30
_MAX_TIMEOUT = 120
_USER_AGENT = "caibao/1.0"
_BILIBILI_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_DEFAULT_NUM_RESULTS = 8
_MCP_URL = "https://mcp.exa.ai/mcp"
_BILIBILI_API = "https://api.bilibili.com"
_BILIBILI_APP_API = "https://app.bilibili.com"
_BILIBILI_RSSHUB_URLS = (
    "https://rsshub.app/bilibili/user/video/{mid}",
    "https://rsshub.rssforever.com/bilibili/user/video/{mid}",
    "https://rsshub.rss.tips/bilibili/user/video/{mid}",
)
_BILIBILI_CACHE_TTL_SECONDS = 600
_BILIBILI_VIDEO_CACHE: dict[tuple[str, int], tuple[float, dict[str, object]]] = {}
_BILIBILI_WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_ACCEPT = {
    "markdown": "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, text/html;q=0.7, */*;q=0.1",
    "text":     "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1",
    "html":     "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, */*;q=0.1",
}

_BINARY_CONTENT_TYPES = frozenset({
    "application/pdf", "application/octet-stream",
    "image/", "video/", "audio/",
})

# ── tool definitions ───────────────────────────────────────────────────────

_WEB_FETCH_DEFINITION = ToolDefinition(
    name="web_fetch",
    display_name="抓取网页",
    description=(
        "Fetch a web page by URL and return its text content. "
        "Supports text / markdown / html output formats. "
        "HTTP/HTTPS only, 5 MB limit."
    ),
    dangerous=False,
    handler_key="generic.web_fetch",
    permission_scope="team",
    source="generic",
    provider="web_tools",
    input_schema={
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "minLength": 1, "maxLength": 2048},
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "description": "Return format: text / markdown / html. Default markdown.",
            },
            "timeout": {
                "type": "integer",
                "minimum": 1, "maximum": 120, "default": 30,
                "description": "Timeout in seconds.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "final_url": {"type": "string"},
            "status": {"type": "integer"},
            "content_type": {"type": "string"},
            "format": {"type": "string"},
            "length": {"type": "integer"},
            "text": {"type": "string"},
            "truncated": {"type": "boolean"},
        },
    },
)

_WEB_SEARCH_DEFINITION = ToolDefinition(
    name="web_search",
    display_name="搜索网页",
    description=(
        "Search the web using keywords. Returns title, snippet, and URL of each result. "
        "Best for current events, news, pricing, and time-sensitive queries. "
        "Use web_fetch afterwards to read full page content."
    ),
    dangerous=False,
    handler_key="generic.web_search",
    permission_scope="team",
    source="generic",
    provider="web_tools",
    input_schema={
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "num_results": {
                "type": "integer",
                "minimum": 1, "maximum": 20, "default": 8,
                "description": "Number of results to return, default 8, max 20.",
            },
            "livecrawl": {
                "type": "string",
                "enum": ["fallback", "preferred"],
                "description": "Crawl mode: fallback (cached-first) or preferred (live-first). Default fallback.",
            },
            "type": {
                "type": "string",
                "enum": ["auto", "fast", "deep"],
                "description": "Search depth: auto / fast / deep. Default auto.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "results": {"type": "array"},
            "provider": {"type": "string"},
            "query": {"type": "string"},
        },
    },
)

_BILIBILI_LATEST_VIDEOS_DEFINITION = ToolDefinition(
    name="bilibili_latest_videos",
    display_name="Bilibili latest videos",
    description=(
        "Find the latest public video posts for a Bilibili/B站 UP主. "
        "Use this before generic web_search when the user asks for an UP's "
        "latest/recent videos, 投稿, or video title/content. Accepts either "
        "up_name or mid/uid and returns structured videos plus source diagnostics."
    ),
    dangerous=False,
    handler_key="generic.bilibili_latest_videos",
    permission_scope="team",
    source="generic",
    provider="web_tools",
    input_schema={
        "type": "object",
        "properties": {
            "up_name": {
                "type": "string",
                "description": "Bilibili UP display name, for example 逍遥散人.",
            },
            "mid": {
                "type": "string",
                "description": "Bilibili UID/mid if known.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1, "maximum": 20, "default": 5,
                "description": "Number of videos to return.",
            },
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "up": {"type": "object"},
            "mid": {"type": "string"},
            "videos": {"type": "array"},
            "source": {"type": "string"},
            "errors": {"type": "array"},
        },
    },
)


def create_web_tools() -> list[ToolDefinition]:
    return [_WEB_FETCH_DEFINITION, _WEB_SEARCH_DEFINITION, _BILIBILI_LATEST_VIDEOS_DEFINITION]


# ═══════════════════════════════════════════════════════════════════════════
# SSRF protection
# ═══════════════════════════════════════════════════════════════════════════

def _host_is_dangerous(host: str) -> bool:
    """Block private, loopback, link-local, and reserved IPs.

    Uses DNS resolution so domain→IP attacks are caught, not just literal IPs.
    """
    if host.lower().strip().rstrip(".") in {
        "localhost", "0.0.0.0", "::1", "0:0:0:0:0:0:0:1",
        "[::1]", "[0:0:0:0:0:0:0:1]",
    }:
        return True
    try:
        addrs = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except (socket.gaierror, OSError):
        return True  # block unresolvable
    for family, _, _, _, sockaddr in addrs:
        ip_str = sockaddr[0]
        if "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return True
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return True
        if ip_str == "169.254.169.254":
            return True
    return False


def _validate_url_target(url: str) -> str | None:
    """SSRF guard — reject private / loopback / reserved addresses."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip().lower()
    if not host:
        return "URL missing hostname"
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved:
            return f"Cannot access private/local address: {host}"
    except ValueError:
        if host in {"localhost", "0.0.0.0"} or host.endswith(".local") or host.endswith(".localhost"):
            return f"Cannot access local domain: {host}"
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Handlers
# ═══════════════════════════════════════════════════════════════════════════

def web_fetch_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    url = str(arguments["url"]).strip()
    fmt = str(arguments.get("format", "markdown")).strip()
    timeout = min(int(arguments.get("timeout", _DEFAULT_TIMEOUT)), _MAX_TIMEOUT)

    # ── scheme check ──
    if not url.startswith(("http://", "https://")):
        raise DomainValidationError("web_fetch only supports http and https URLs.")

    # ── SSRF guard ──
    err = _validate_url_target(url)
    if err:
        raise DomainValidationError(err)

    # ── fetch ──
    client_kwargs = {
        "follow_redirects": True,
        "timeout": timeout,
        "headers": {
            "User-Agent": _USER_AGENT,
            "Accept": _ACCEPT.get(fmt, "*/*"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    }

    try:
        with httpx.Client(**client_kwargs) as client:
            resp = client.get(url)
    except httpx.TimeoutException:
        raise DomainValidationError(f"web_fetch timed out after {timeout}s.")
    except httpx.ConnectError:
        raise DomainValidationError("web_fetch could not establish connection.")
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"web_fetch request failed: {exc}") from exc

    if resp.status_code != 200:
        raise DomainValidationError(f"web_fetch returned HTTP {resp.status_code}.")

    # ── size check ──
    cl = resp.headers.get("content-length")
    if cl and int(cl) > _MAX_BYTES:
        raise DomainValidationError("web_fetch response exceeds 5 MB limit.")

    body = resp.content
    if len(body) > _MAX_BYTES:
        raise DomainValidationError("web_fetch response exceeds 5 MB limit.")

    content_type = resp.headers.get("content-type", "")
    encoding = resp.encoding or "utf-8"

    # ── binary rejection ──
    for prefix in _BINARY_CONTENT_TYPES:
        if prefix in content_type:
            raise DomainValidationError(
                f"web_fetch cannot process binary content ({content_type}). "
                "Use a dedicated tool for this format."
            )

    is_html = "text/html" in content_type

    # ── decode & transform ──
    if fmt == "html":
        text = body.decode(encoding, errors="replace")
    elif fmt == "markdown" and is_html:
        text = _html_to_markdown(body.decode(encoding, errors="replace"))
    elif fmt == "text" and is_html:
        text = _html_to_text(body)
    else:
        # non-HTML (JSON, plain text, etc.) — return as-is
        text = body.decode(encoding, errors="replace")

    # ── truncate ──
    truncated = len(text) > _MAX_TEXT_CHARS
    if truncated:
        text = text[:_MAX_TEXT_CHARS]

    result: dict[str, object] = {
        "url": url,
        "final_url": str(resp.url),
        "status": resp.status_code,
        "content_type": content_type,
        "format": fmt,
        "length": len(text),
        "text": text,
    }
    if truncated:
        result["truncated"] = True
        result["note"] = (
            f"Content truncated to {_MAX_TEXT_CHARS} chars. "
            "Narrow your scope or use a different tool for more."
        )
    return result


def web_search_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    settings = get_settings()
    provider = settings.web_search_provider.strip().lower()
    api_key = (settings.web_search_api_key or "").strip()

    query = str(arguments["query"]).strip()
    num_results = int(arguments.get("num_results", arguments.get("limit", _DEFAULT_NUM_RESULTS)))
    livecrawl = str(arguments.get("livecrawl", "fallback"))
    search_type = str(arguments.get("type", "auto"))

    bilibili_diagnostic: dict[str, object] | None = None
    if _looks_like_bilibili_latest_query(query):
        up_name = _extract_bilibili_up_name_from_query(query)
        if up_name:
            try:
                bilibili_diagnostic = bilibili_latest_videos_handler(
                    team_id=team_id,
                    user_id=user_id,
                    arguments={"up_name": up_name, "limit": num_results},
                )
                videos = bilibili_diagnostic.get("videos")
                source = str(bilibili_diagnostic.get("source") or "")
                if isinstance(videos, list) and videos and source != "exa_video_fallback":
                    return {
                        "results": _bilibili_videos_to_search_results(videos),
                        "provider": "bilibili",
                        "query": query,
                        "bilibili_latest_videos": bilibili_diagnostic,
                    }
            except DomainValidationError as exc:
                bilibili_diagnostic = {
                    "error": str(exc),
                    "query": query,
                    "up_name": up_name,
                }
            if bilibili_diagnostic is not None:
                return {
                    "results": [],
                    "provider": "bilibili",
                    "query": query,
                    "bilibili_latest_videos": bilibili_diagnostic,
                    "message": (
                        "Bilibili-specific lookup could not verify latest videos. "
                        "Do not infer latest order from generic search snippets."
                    ),
                }

    if provider == "disabled":
        raise DomainValidationError(
            "Web search is not configured. "
            "Set web_search_provider in config (e.g. 'exa' for free Exa MCP search)."
        )
    if provider == "exa":
        result = _exa_search(
            query=query, num_results=num_results,
            livecrawl=livecrawl, search_type=search_type,
        )
        if bilibili_diagnostic is not None:
            result["bilibili_latest_videos"] = bilibili_diagnostic
        return result
    if provider == "brave":
        if not api_key:
            raise DomainValidationError("Brave search requires WEB_SEARCH_API_KEY.")
        result = _brave_search(query=query, limit=num_results, api_key=api_key)
        if bilibili_diagnostic is not None:
            result["bilibili_latest_videos"] = bilibili_diagnostic
        return result
    if provider == "tavily":
        if not api_key:
            raise DomainValidationError("Tavily search requires WEB_SEARCH_API_KEY.")
        result = _tavily_search(query=query, limit=num_results, api_key=api_key)
        if bilibili_diagnostic is not None:
            result["bilibili_latest_videos"] = bilibili_diagnostic
        return result

    raise DomainValidationError(f"Unsupported search provider: {provider}")


def bilibili_latest_videos_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    up_name = str(
        arguments.get("up_name")
        or arguments.get("name")
        or arguments.get("keyword")
        or ""
    ).strip()
    mid = str(arguments.get("mid") or arguments.get("uid") or "").strip()
    limit = max(1, min(int(arguments.get("limit", _DEFAULT_NUM_RESULTS)), 20))
    if not up_name and not mid:
        raise DomainValidationError("bilibili_latest_videos requires up_name or mid.")
    return _bilibili_latest_videos(up_name=up_name, mid=mid, limit=limit)


def _looks_like_bilibili_latest_query(query: str) -> bool:
    lowered = query.lower()
    has_bilibili = any(token in lowered for token in ("bilibili", "b站", "哔哩", "嗶哩"))
    has_video_intent = any(
        token in lowered
        for token in ("最新", "最近", "投稿", "视频", "影片", "一期", "latest", "recent", "video")
    )
    return has_bilibili and has_video_intent


def _extract_bilibili_up_name_from_query(query: str) -> str:
    cleaned = query
    cleaned = re.sub(r"https?://\S+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bsite:\S+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\b(?:bilibili|latest|recent|video|videos|uid|mid)\b", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:B站|哔哩哔哩|哔哩|嗶哩嗶哩|嗶哩|UP主|up主)", " ", cleaned)
    cleaned = re.sub(r"(?:最新|最近|投稿|视频|影片|一期|内容|标题|叫什么|是什么|查询|查一下|帮我|一下)", " ", cleaned)
    cleaned = re.sub(r"(?:\d{4}年)?\d{1,2}月\d{0,2}日?", " ", cleaned)
    cleaned = re.sub(r"[，。！？、,.!?;:：\"'“”‘’()\[\]{}<>《》|/\\]+", " ", cleaned)
    parts = [part.strip() for part in cleaned.split() if part.strip()]
    return max(parts, key=len) if parts else ""


def _bilibili_latest_videos(*, up_name: str, mid: str, limit: int) -> dict[str, object]:
    errors: list[str] = []
    source = ""
    resolved_mid = mid.strip()
    up: dict[str, object] = {}
    videos: list[dict[str, object]] = []
    fallback_candidates: list[dict[str, object]] = []

    with httpx.Client(
        headers=_bilibili_headers(),
        timeout=15.0,
        follow_redirects=True,
    ) as client:
        if not resolved_mid:
            resolved_mid, up, source = _resolve_bilibili_mid(client, up_name=up_name, errors=errors)

        if resolved_mid:
            cached = _get_cached_bilibili_videos(mid=resolved_mid, limit=limit)
            if cached is not None:
                if up:
                    cached["up"] = {**dict(cached.get("up") or {}), **up}
                return cached

            card = _fetch_bilibili_card(client, mid=resolved_mid, errors=errors)
            if card:
                up = {**up, **card}

            fetchers = (
                _fetch_bilibili_videos_wbi,
                _fetch_bilibili_videos_plain,
                _fetch_bilibili_videos_dynamic,
                _fetch_bilibili_videos_app,
                _fetch_bilibili_videos_rsshub,
            )
            for fetcher in fetchers:
                videos, source = fetcher(client, mid=resolved_mid, limit=limit, errors=errors)
                if videos:
                    break

    if not videos:
        fallback_candidates, fallback_source = _search_bilibili_videos_fallback(
            up_name=up_name or str(up.get("name", "")),
            mid=resolved_mid,
            limit=limit,
            errors=errors,
        )
        if fallback_candidates:
            errors.append(
                f"{fallback_source}: low-confidence candidates kept out of videos because latest order is unverified"
            )

    result = {
        "up": up,
        "mid": resolved_mid,
        "query_name": up_name,
        "videos": videos[:limit],
        "fallback_candidates": fallback_candidates[:limit],
        "source": source or "unresolved",
        "errors": errors[-8:],
        "note": (
            "Bilibili web APIs may rate-limit anonymous requests; source shows which fallback succeeded."
            if errors else ""
        ),
    }
    if resolved_mid and videos and source != "exa_video_fallback":
        _set_cached_bilibili_videos(mid=resolved_mid, limit=limit, result=result)
    return result


def _bilibili_headers(*, referer: str = "https://www.bilibili.com/") -> dict[str, str]:
    return {
        "User-Agent": _BILIBILI_USER_AGENT,
        "Referer": referer,
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


def _get_cached_bilibili_videos(*, mid: str, limit: int) -> dict[str, object] | None:
    now = time.time()
    candidates = [
        (cached_limit, cached)
        for (cached_mid, cached_limit), cached in _BILIBILI_VIDEO_CACHE.items()
        if cached_mid == mid and cached_limit >= limit
    ]
    if not candidates:
        return None
    cached_limit, (created_at, result) = max(candidates, key=lambda item: item[0])
    if now - created_at > _BILIBILI_CACHE_TTL_SECONDS:
        _BILIBILI_VIDEO_CACHE.pop((mid, cached_limit), None)
        return None
    cloned = _json.loads(_json.dumps(result, ensure_ascii=False, default=str))
    if isinstance(cloned, dict):
        videos = cloned.get("videos")
        if isinstance(videos, list):
            cloned["videos"] = videos[:limit]
        cloned["source"] = f"cache:{cloned.get('source') or 'bilibili'}"
        return cloned
    return None


def _set_cached_bilibili_videos(*, mid: str, limit: int, result: dict[str, object]) -> None:
    cloned = _json.loads(_json.dumps(result, ensure_ascii=False, default=str))
    if isinstance(cloned, dict):
        _BILIBILI_VIDEO_CACHE[(mid, limit)] = (time.time(), cloned)


def _request_bilibili_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, object] | None,
    source: str,
    errors: list[str],
    referer: str = "https://www.bilibili.com/",
    allowed_codes: tuple[object, ...] = (0, "0", None),
) -> dict[str, object] | None:
    try:
        resp = client.get(url, params=params, headers=_bilibili_headers(referer=referer))
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"{source}: {exc}")
        return None

    if not isinstance(data, dict):
        errors.append(f"{source}: invalid JSON response")
        return None
    code = data.get("code")
    if code not in allowed_codes:
        message = data.get("message") or data.get("msg") or "unknown error"
        errors.append(f"{source}: code={code} message={message}")
        return None
    return data


def _resolve_bilibili_mid(
    client: httpx.Client,
    *,
    up_name: str,
    errors: list[str],
) -> tuple[str, dict[str, object], str]:
    mid, up = _resolve_mid_from_bilibili_search(client, up_name=up_name, errors=errors)
    if mid:
        return mid, up, "bilibili_user_search"

    mid = _resolve_mid_from_exa(up_name=up_name, errors=errors)
    if mid:
        return mid, {}, "exa_uid_search"

    return "", {}, ""


def _resolve_mid_from_bilibili_search(
    client: httpx.Client,
    *,
    up_name: str,
    errors: list[str],
) -> tuple[str, dict[str, object]]:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/web-interface/search/type",
        params={"search_type": "bili_user", "keyword": up_name, "page": 1},
        source="bilibili_user_search",
        errors=errors,
        referer="https://search.bilibili.com/",
    )
    if not data:
        return "", {}

    payload = data.get("data")
    if not isinstance(payload, dict):
        return "", {}
    items = payload.get("result")
    if not isinstance(items, list):
        return "", {}

    normalized_query = _strip_html(up_name).lower()
    candidates: list[dict[str, object]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("mid") or "").strip()
        uname = _strip_html(str(item.get("uname") or "")).strip()
        if not mid:
            continue
        candidates.append({
            "mid": mid,
            "name": uname,
            "fans": item.get("fans"),
            "videos": item.get("videos"),
            "sign": _strip_html(str(item.get("usign") or "")),
        })

    if not candidates:
        return "", {}
    exact = next(
        (candidate for candidate in candidates if str(candidate.get("name", "")).lower() == normalized_query),
        None,
    )
    selected = exact or candidates[0]
    return str(selected["mid"]), selected


def _resolve_mid_from_exa(*, up_name: str, errors: list[str]) -> str:
    if not up_name:
        return ""
    queries = [
        f'{up_name} B站 UID space.bilibili.com',
        f'site:space.bilibili.com "{up_name}" bilibili',
    ]
    for query in queries:
        try:
            result = _exa_search(query=query, num_results=5, livecrawl="preferred", search_type="auto")
        except DomainValidationError as exc:
            errors.append(f"exa_uid_search: {exc}")
            continue

        for item in result.get("results", []):
            if not isinstance(item, dict):
                continue
            text = " ".join(
                str(item.get(key, ""))
                for key in ("title", "url", "snippet")
            )
            mid = _extract_bilibili_mid_from_text(text)
            if mid:
                return mid
    return ""


def _extract_bilibili_mid_from_text(text: str) -> str:
    patterns = (
        r"space\.bilibili\.com/(\d{3,})",
        r"\bUID[:：\s]*([1-9]\d{2,})\b",
        r"\bmid[:：\s]*([1-9]\d{2,})\b",
        r"\buid[:：\s]*([1-9]\d{2,})\b",
    )
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return ""


def _fetch_bilibili_card(
    client: httpx.Client,
    *,
    mid: str,
    errors: list[str],
) -> dict[str, object]:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/web-interface/card",
        params={"mid": mid},
        source="bilibili_card",
        errors=errors,
        referer=f"https://space.bilibili.com/{mid}/",
    )
    payload = data.get("data") if data else None
    card = payload.get("card") if isinstance(payload, dict) else None
    if not isinstance(card, dict):
        return {}
    return {
        "mid": str(card.get("mid") or mid),
        "name": card.get("name") or "",
        "fans": card.get("fans"),
        "sign": card.get("sign") or "",
        "official": (
            (card.get("Official") or {}).get("title")
            if isinstance(card.get("Official"), dict)
            else ""
        ),
    }


def _fetch_bilibili_videos_plain(
    client: httpx.Client,
    *,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/space/arc/search",
        params={"mid": mid, "pn": 1, "ps": limit, "order": "pubdate"},
        source="bilibili_space_arc",
        errors=errors,
        referer=f"https://space.bilibili.com/{mid}/video",
    )
    videos = _extract_arc_videos(data, source="bilibili_space_arc")
    return videos, "bilibili_space_arc" if videos else ""


def _fetch_bilibili_videos_wbi(
    client: httpx.Client,
    *,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    keys = _fetch_bilibili_wbi_keys(client, errors=errors)
    if not keys:
        return [], ""
    img_key, sub_key = keys
    params = _sign_bilibili_wbi(
        {
            "mid": mid,
            "pn": 1,
            "ps": limit,
            "order": "pubdate",
            "platform": "web",
            "web_location": 1550101,
        },
        img_key=img_key,
        sub_key=sub_key,
    )
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/space/wbi/arc/search",
        params=params,
        source="bilibili_space_wbi_arc",
        errors=errors,
        referer=f"https://space.bilibili.com/{mid}/video",
    )
    videos = _extract_arc_videos(data, source="bilibili_space_wbi_arc")
    return videos, "bilibili_space_wbi_arc" if videos else ""


def _fetch_bilibili_wbi_keys(
    client: httpx.Client,
    *,
    errors: list[str],
) -> tuple[str, str] | None:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/web-interface/nav",
        params={},
        source="bilibili_nav",
        errors=errors,
        allowed_codes=(0, "0", -101, "-101"),
    )
    payload = data.get("data") if data else None
    wbi = payload.get("wbi_img") if isinstance(payload, dict) else None
    if not isinstance(wbi, dict):
        errors.append("bilibili_nav: missing wbi_img")
        return None
    img_key = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
    sub_key = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".", 1)[0]
    if not img_key or not sub_key:
        errors.append("bilibili_nav: empty WBI keys")
        return None
    return img_key, sub_key


def _sign_bilibili_wbi(
    params: dict[str, object],
    *,
    img_key: str,
    sub_key: str,
) -> dict[str, object]:
    signed = dict(params)
    signed["wts"] = int(time.time())
    mixin_key = "".join((img_key + sub_key)[i] for i in _BILIBILI_WBI_MIXIN_KEY_ENC_TAB)[:32]
    filtered = {
        key: "".join(ch for ch in str(value) if ch not in "!'()*")
        for key, value in signed.items()
    }
    query = urlencode(sorted(filtered.items()))
    signed["w_rid"] = hashlib.md5((query + mixin_key).encode("utf-8")).hexdigest()
    return signed


def _fetch_bilibili_videos_dynamic(
    client: httpx.Client,
    *,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_API}/x/polymer/web-dynamic/v1/feed/space",
        params={
            "host_mid": mid,
            "timezone_offset": -480,
            "features": "itemOpusStyle,listOnlyfans,opusBigCover,onlyfansVote,decorationCard,forwardListHidden,ugcDelete,onlyfansQaCard",
        },
        source="bilibili_dynamic_space",
        errors=errors,
        referer=f"https://space.bilibili.com/{mid}/dynamic",
    )
    payload = data.get("data") if data else None
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return [], ""
    videos = []
    for item in items:
        if isinstance(item, dict):
            video = _normalize_dynamic_video(item, source="bilibili_dynamic_space")
            if video:
                videos.append(video)
        if len(videos) >= limit:
            break
    return videos, "bilibili_dynamic_space" if videos else ""


def _fetch_bilibili_videos_app(
    client: httpx.Client,
    *,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    data = _request_bilibili_json(
        client,
        f"{_BILIBILI_APP_API}/x/v2/space/archive/cursor",
        params={"vmid": mid, "ps": limit, "next": 0},
        source="bilibili_app_archive",
        errors=errors,
        referer=f"https://space.bilibili.com/{mid}/video",
    )
    payload = data.get("data") if data else None
    item = payload.get("item") if isinstance(payload, dict) else None
    archives = item.get("archives") if isinstance(item, dict) else None
    if not isinstance(archives, list):
        return [], ""
    videos = [
        _normalize_arc_video(raw, source="bilibili_app_archive")
        for raw in archives
        if isinstance(raw, dict)
    ]
    videos = [video for video in videos if video]
    return videos[:limit], "bilibili_app_archive" if videos else ""


def _fetch_bilibili_videos_rsshub(
    client: httpx.Client,
    *,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    for url_template in _BILIBILI_RSSHUB_URLS:
        url = url_template.format(mid=mid)
        try:
            resp = client.get(
                url,
                headers={
                    "User-Agent": _BILIBILI_USER_AGENT,
                    "Accept": "application/rss+xml, application/xml, text/xml, */*",
                },
                timeout=15.0,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            errors.append(f"rsshub:{url}: {exc}")
            continue
        text = resp.text
        if "Just a moment" in text or "Cloudflare" in text:
            errors.append(f"rsshub:{url}: blocked by challenge")
            continue
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            errors.append(f"rsshub:{url}: invalid XML {exc}")
            continue
        videos = _parse_rsshub_videos(root, source=url, limit=limit)
        if videos:
            return videos, "rsshub"
    return [], ""


def _search_bilibili_videos_fallback(
    *,
    up_name: str,
    mid: str,
    limit: int,
    errors: list[str],
) -> tuple[list[dict[str, object]], str]:
    if not up_name and not mid:
        return [], ""
    query_parts = ["site:bilibili.com/video"]
    if up_name:
        query_parts.append(f'"{up_name}"')
    if mid:
        query_parts.append(f'"space.bilibili.com/{mid}"')
    query_parts.append("最新 投稿")
    query = " ".join(query_parts)
    try:
        result = _exa_search(query=query, num_results=limit * 2, livecrawl="preferred", search_type="auto")
    except DomainValidationError as exc:
        errors.append(f"exa_video_fallback: {exc}")
        return [], ""

    videos: list[dict[str, object]] = []
    for item in result.get("results", []):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "")
        if "bilibili.com/video/" not in url:
            continue
        snippet = str(item.get("snippet") or "")
        author_match = re.search(r"Author:\s*([^ ]+)", snippet)
        if up_name and author_match and author_match.group(1).strip() != up_name:
            continue
        if up_name and not author_match and up_name not in str(item.get("title", "")):
            continue
        video = {
            "title": _strip_html(str(item.get("title") or "")),
            "url": url,
            "published": item.get("published") or "",
            "description": snippet[:500],
            "author": up_name,
            "source": "exa_video_fallback",
        }
        videos.append(video)
        if len(videos) >= limit:
            break
    return videos, "exa_video_fallback" if videos else ""


def _extract_arc_videos(data: dict[str, object] | None, *, source: str) -> list[dict[str, object]]:
    payload = data.get("data") if data else None
    if not isinstance(payload, dict):
        return []
    listing = payload.get("list")
    vlist = listing.get("vlist") if isinstance(listing, dict) else None
    if not isinstance(vlist, list):
        return []
    videos = [
        _normalize_arc_video(raw, source=source)
        for raw in vlist
        if isinstance(raw, dict)
    ]
    return [video for video in videos if video]


def _normalize_arc_video(raw: dict[str, object], *, source: str) -> dict[str, object]:
    title = _strip_html(str(raw.get("title") or raw.get("name") or "")).strip()
    bvid = str(raw.get("bvid") or "").strip()
    aid = str(raw.get("aid") or raw.get("param") or "").strip()
    url = f"https://www.bilibili.com/video/{bvid}/" if bvid else (
        f"https://www.bilibili.com/video/av{aid}/" if aid else ""
    )
    if not title and not url:
        return {}
    created = raw.get("created") or raw.get("pubdate") or raw.get("ctime")
    return {
        "title": title,
        "url": url,
        "bvid": bvid,
        "aid": aid,
        "published": _format_bilibili_timestamp(created),
        "duration": raw.get("length") or raw.get("duration") or "",
        "description": _strip_html(str(raw.get("description") or raw.get("desc") or ""))[:500],
        "play": raw.get("play") or raw.get("stat", {}).get("view") if isinstance(raw.get("stat"), dict) else raw.get("play"),
        "comment": raw.get("comment"),
        "cover": raw.get("pic") or raw.get("cover") or "",
        "source": source,
    }


def _normalize_dynamic_video(raw: dict[str, object], *, source: str) -> dict[str, object]:
    modules = raw.get("modules")
    if not isinstance(modules, dict):
        return {}
    dynamic = modules.get("module_dynamic")
    if not isinstance(dynamic, dict):
        return {}
    major = dynamic.get("major")
    if not isinstance(major, dict):
        return {}
    archive = major.get("archive")
    if not isinstance(archive, dict):
        return {}
    author = modules.get("module_author")
    author_dict = author if isinstance(author, dict) else {}
    bvid = str(archive.get("bvid") or "").strip()
    jump_url = str(archive.get("jump_url") or "").strip()
    if jump_url.startswith("//"):
        jump_url = f"https:{jump_url}"
    return {
        "title": _strip_html(str(archive.get("title") or "")).strip(),
        "url": jump_url or (f"https://www.bilibili.com/video/{bvid}/" if bvid else ""),
        "bvid": bvid,
        "aid": archive.get("aid") or "",
        "published": _format_bilibili_timestamp(author_dict.get("pub_ts")),
        "duration": archive.get("duration_text") or "",
        "description": _strip_html(str(archive.get("desc") or ""))[:500],
        "author": author_dict.get("name") or "",
        "cover": archive.get("cover") or "",
        "source": source,
    }


def _parse_rsshub_videos(
    root: ElementTree.Element,
    *,
    source: str,
    limit: int,
) -> list[dict[str, object]]:
    videos: list[dict[str, object]] = []
    channel = root.find("channel")
    items = channel.findall("item") if channel is not None else root.findall(".//item")
    for item in items:
        title = _element_text(item, "title")
        link = _element_text(item, "link")
        published_raw = _element_text(item, "pubDate")
        published = ""
        if published_raw:
            try:
                published = parsedate_to_datetime(published_raw).isoformat()
            except (TypeError, ValueError):
                published = published_raw
        videos.append({
            "title": title,
            "url": link,
            "published": published,
            "description": _strip_html(_element_text(item, "description"))[:500],
            "author": _element_text(item, "author"),
            "source": source,
        })
        if len(videos) >= limit:
            break
    return videos


def _element_text(item: ElementTree.Element, name: str) -> str:
    child = item.find(name)
    return (child.text or "").strip() if child is not None else ""


def _format_bilibili_timestamp(value: object) -> str:
    if value in (None, ""):
        return ""
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return str(value)
    if timestamp > 10_000_000_000:
        timestamp = timestamp // 1000
    tz = timezone(timedelta(hours=8))
    return datetime.fromtimestamp(timestamp, tz=tz).isoformat()


def _bilibili_videos_to_search_results(videos: list[object]) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for item in videos:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        published = str(item.get("published") or "")
        description = str(item.get("description") or "")
        source = str(item.get("source") or "")
        snippet_parts = []
        if published:
            snippet_parts.append(f"Published: {published}")
        if source:
            snippet_parts.append(f"Source: {source}")
        if description:
            snippet_parts.append(description)
        results.append({
            "title": title,
            "url": str(item.get("url") or ""),
            "snippet": " ".join(snippet_parts),
        })
    return results


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", _html.unescape(text or "")).strip()


# ═══════════════════════════════════════════════════════════════════════════
# Brave / Tavily
# ═══════════════════════════════════════════════════════════════════════════

def _brave_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 10)},
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "X-Subscription-Token": api_key,
            },
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Brave search failed: {exc}") from exc

    web_results = data.get("web", {}).get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
            for r in web_results[:limit]
        ],
        "provider": "brave",
        "query": query,
    }


def _tavily_search(*, query: str, limit: int, api_key: str) -> dict[str, object]:
    try:
        resp = httpx.post(
            "https://api.tavily.com/search",
            json={"query": query, "max_results": min(limit, 10), "api_key": api_key},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Tavily search failed: {exc}") from exc

    results = data.get("results", [])
    return {
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("content", "")}
            for r in results[:limit]
        ],
        "provider": "tavily",
        "query": query,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Exa MCP search (JSON-RPC 2.0 over SSE)
# ═══════════════════════════════════════════════════════════════════════════

def _exa_search(
    *,
    query: str,
    num_results: int,
    livecrawl: str = "fallback",
    search_type: str = "auto",
) -> dict[str, object]:
    """Exa MCP public endpoint — free, no API key required."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "web_search_exa",
            "arguments": {
                "query": query,
                "numResults": min(num_results, 20),
                "livecrawl": livecrawl,
                "type": search_type,
            },
        },
    }

    try:
        resp = httpx.post(
            _MCP_URL,
            json=payload,
            headers={
                "accept": "application/json, text/event-stream",
                "content-type": "application/json",
            },
            timeout=25.0,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise DomainValidationError(f"Exa search failed: {exc}") from exc

    # Parse SSE response — each "data:" line is a JSON-RPC response chunk
    raw_text = ""
    for line in resp.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("data: "):
            try:
                chunk = _json.loads(stripped[6:])
                content = chunk.get("result", {}).get("content", [])
                if content:
                    raw_text = content[0].get("text", "")
            except (_json.JSONDecodeError, KeyError, IndexError, TypeError):
                continue

    if not raw_text:
        return {"results": [], "provider": "exa", "query": query}

    results = _parse_exa_results(raw_text)
    return {
        "results": results[:num_results],
        "provider": "exa",
        "query": query,
    }


def _parse_exa_results(raw: str) -> list[dict[str, str]]:
    """Parse Exa's plain-text result format into structured records.

    Expected format (one blank line between results)::

        Title: Example Article
        URL: https://example.com/article
        Published Date: 2025-06-01
        Text: A snippet of the article content...

        Title: Another Result
        URL: https://example.org/another
        Text: Another snippet...

    The parser is section-based: a blank line separates results; Title
    always starts a new result block.  Extra / unrecognised lines are
    appended to the current result's snippet.
    """
    results: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            if current is not None:
                results.append(current)
                current = None
            continue

        # Key: value pairs
        for prefix, key in [
            ("Title: ", "title"),
            ("URL: ", "url"),
            ("Published Date: ", "published"),
            ("Text: ", "snippet"),
            ("Score: ", "score"),
        ]:
            if stripped.startswith(prefix):
                value = stripped[len(prefix):].strip()
                if key == "title":
                    # Title always opens a new block
                    if current is not None:
                        results.append(current)
                    current = {"title": value}
                elif current is not None:
                    current[key] = value
                break
        else:
            # Unrecognised line — append to current snippet
            if current is not None:
                existing = current.get("snippet", "")
                current["snippet"] = f"{existing} {stripped}".strip() if existing else stripped

    if current is not None:
        results.append(current)

    return results


# ═══════════════════════════════════════════════════════════════════════════
# HTML processing (lxml + html2text)
# ═══════════════════════════════════════════════════════════════════════════

def _html_to_text(content: bytes) -> str:
    """HTML → plain text via lxml DOM (akashic-agent style).

    Removes script / style / noscript / iframe / object / embed tags,
    then extracts text_content and normalises whitespace.
    """
    try:
        doc = lxml_html.fromstring(content)
    except ParserError:
        return content.decode("utf-8", errors="replace")

    for tag_name in ("script", "style", "noscript", "iframe", "object", "embed"):
        for el in doc.xpath(f"//{tag_name}"):
            parent = el.getparent()
            if parent is not None:
                parent.remove(el)

    return " ".join(doc.text_content().split())


def _html_to_markdown(raw_html: str) -> str:
    """HTML → Markdown via html2text (akashic-agent style)."""
    h = _html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = False
    h.body_width = 0          # disable line wrapping
    h.unicode_snob = True     # preserve Unicode
    h.protect_links = True    # don't escape links
    return h.handle(raw_html).strip()
