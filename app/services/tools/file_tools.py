from __future__ import annotations

import os
from pathlib import Path

from app.core.config import get_settings
from app.core.exceptions import DomainValidationError
from app.services.tool_registry import ToolDefinition


def _build_file_root(team_id: str) -> Path:
    settings = get_settings()
    root = Path(settings.file_tools_root_dir).resolve()
    if not root.is_absolute():
        root = Path.cwd() / root
    return (root / team_id).resolve()


def _resolve_safe_path(team_id: str, user_path: str) -> Path:
    """Resolve user_path within the team's sandbox. Raises on escape attempt."""
    root = _build_file_root(team_id)
    root.mkdir(parents=True, exist_ok=True)
    normalized = os.path.normpath(user_path)
    if os.path.isabs(normalized):
        normalized = normalized.lstrip(os.sep).lstrip("/")
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise DomainValidationError(f"Path escapes the allowed directory: {user_path}")
    if candidate.is_symlink():
        raise DomainValidationError(f"Symlinks are not allowed: {user_path}")
    return candidate


_LIST_DIR = ToolDefinition(
    name="list_dir",
    display_name="列出目录",
    description="List files and directories within the agent's workspace.",
    dangerous=False,
    handler_key="generic.list_dir",
    permission_scope="team",
    source="generic",
    provider="file_tools",
    input_schema={
        "type": "object",
        "properties": {
            "path": {"type": "string", "default": "."},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "entries": {"type": "array"},
        },
    },
)

_READ_FILE = ToolDefinition(
    name="read_file",
    display_name="读取文件",
    description="Read a text file from the agent's workspace, with optional offset/limit for paging.",
    dangerous=False,
    handler_key="generic.read_file",
    permission_scope="team",
    source="generic",
    provider="file_tools",
    input_schema={
        "type": "object",
        "required": ["path"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "offset": {"type": "integer", "minimum": 0, "default": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "total_lines": {"type": "integer"},
            "truncated": {"type": "boolean"},
        },
    },
)

_WRITE_FILE = ToolDefinition(
    name="write_file",
    display_name="写入文件",
    description="Write (overwrite) a file in the agent's workspace. Requires confirmation.",
    dangerous=True,
    handler_key="generic.write_file",
    permission_scope="team",
    source="generic",
    provider="file_tools",
    input_schema={
        "type": "object",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "content": {"type": "string", "minLength": 1, "maxLength": 200000},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "written_bytes": {"type": "integer"},
        },
    },
)

_EDIT_FILE = ToolDefinition(
    name="edit_file",
    display_name="编辑文件",
    description="Make exact string replacements in a file. Requires confirmation.",
    dangerous=True,
    handler_key="generic.edit_file",
    permission_scope="team",
    source="generic",
    provider="file_tools",
    input_schema={
        "type": "object",
        "required": ["path", "old_text", "new_text"],
        "properties": {
            "path": {"type": "string", "minLength": 1},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
            "replace_all": {"type": "boolean", "default": False},
        },
    },
    output_schema={
        "type": "object",
        "properties": {
            "replacements": {"type": "integer"},
        },
    },
)


def create_file_tools() -> list[ToolDefinition]:
    return [_LIST_DIR, _READ_FILE, _WRITE_FILE, _EDIT_FILE]


# ------------------------------------------------------------------
# Handlers
# ------------------------------------------------------------------

def list_dir_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    path = str(arguments.get("path", "."))
    target = _resolve_safe_path(team_id, path)
    if not target.exists():
        return {"entries": [], "path": path}
    if target.is_file():
        target = target.parent

    entries = []
    try:
        for item in sorted(target.iterdir()):
            try:
                is_sym = item.is_symlink()
            except OSError:
                is_sym = True
            entries.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if not item.is_dir() else 0,
                "symlink": is_sym,
            })
    except PermissionError as exc:
        raise DomainValidationError(f"Permission denied: {exc}") from exc

    return {"entries": entries, "path": path}


def read_file_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    path = str(arguments["path"])
    target = _resolve_safe_path(team_id, path)
    if not target.exists():
        raise DomainValidationError(f"File not found: {path}")
    if target.is_dir():
        raise DomainValidationError(f"Path is a directory, not a file: {path}")

    offset = int(arguments.get("offset", 0))
    limit = min(int(arguments.get("limit", 200)), 500)
    max_bytes = 100_000

    file_bytes = target.read_bytes()
    if len(file_bytes) > max_bytes:
        raise DomainValidationError(f"File too large ({len(file_bytes)} bytes). Max: {max_bytes} bytes.")

    text = file_bytes.decode("utf-8", errors="replace")
    lines = text.splitlines()
    total = len(lines)
    page = lines[offset : offset + limit]
    return {
        "content": "\n".join(page),
        "total_lines": total,
        "offset": offset,
        "truncated": offset + limit < total,
    }


def write_file_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    path = str(arguments["path"])
    content = str(arguments["content"])
    target = _resolve_safe_path(team_id, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = content.encode("utf-8")
    target.write_bytes(encoded)
    return {"written_bytes": len(encoded), "path": path}


def edit_file_handler(
    *,
    team_id: str,
    user_id: str,
    arguments: dict[str, object],
) -> dict[str, object]:
    path = str(arguments["path"])
    old_text = str(arguments["old_text"])
    new_text = str(arguments["new_text"])
    replace_all = bool(arguments.get("replace_all", False))

    target = _resolve_safe_path(team_id, path)
    if not target.exists():
        raise DomainValidationError(f"File not found: {path}")
    if target.is_dir():
        raise DomainValidationError(f"Cannot edit a directory: {path}")

    original = target.read_text("utf-8", errors="replace")
    if replace_all:
        count = original.count(old_text)
        modified = original.replace(old_text, new_text)
    else:
        count = 1 if old_text in original else 0
        modified = original.replace(old_text, new_text, 1)

    if count == 0:
        raise DomainValidationError("old_text not found in file.")

    target.write_text(modified, "utf-8")
    return {"replacements": count, "path": path}
