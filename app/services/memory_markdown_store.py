from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from app.core.config import get_settings

# Idempotency marker: <!-- caibao:source_ref:{hash}:kind:{kind} -->
_SOURCE_REF_RE = re.compile(r"<!--\s*caibao:source_ref:([a-f0-9]+):kind:(\w+)\s*-->")


def _hash_ref(source_ref: str) -> str:
    return hashlib.sha256(source_ref.encode()).hexdigest()[:16]


def _has_source_ref(block: str, ref_hash: str, kind: str) -> bool:
    for m in _SOURCE_REF_RE.finditer(block):
        if m.group(1) == ref_hash and m.group(2) == kind:
            return True
    return False


def _source_ref_marker(ref_hash: str, kind: str) -> str:
    return f"<!-- caibao:source_ref:{ref_hash}:kind:{kind} -->"


class MemoryMarkdownStore:
    """File-system backed markdown memory layer.

    Directory layout::

        {root}/{team_id}/{user_id}/{space_id or "global"}/
            SELF.md
            MEMORY.md
            HISTORY.md
            RECENT_CONTEXT.md
            PENDING.md
            journal/YYYY-MM-DD.md
    """

    DEFAULT_SELF_MD = """# CaiBao 的自我认知

## 人格与形象
- 我是 CaiBao，一个温和、可靠、主动参与思考的长期协作伙伴。
- 我优先给出清晰结论，再补充必要细节；不把自己伪装成没有立场的工具。
- 我可以轻松一点、聪明一点，但不喧宾夺主；颜文字只在合适时少量使用。

## 我对当前用户的理解
- 我会从长期记忆和近期上下文中逐步形成对当前用户的理解。
- 没有证据时不编造画像；用户当前这轮明确表达永远优先于旧记忆。

## 我们关系的定义
- 我与当前用户的关系以透明、尊重边界和持续协作为基础。
- 我会尽量帮用户把事情推进，而不是只做礼貌的问答机器。
"""

    def __init__(self, root_dir: str | None = None) -> None:
        self.root = Path(root_dir or get_settings().memory_root_dir).resolve()

    # ------------------------------------------------------------------
    # Workspace
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_segment(raw: str) -> str:
        """Map an arbitrary user/team/space ID to a filesystem-safe directory name.

        Uses SHA-256 so the mapping is deterministic across restarts and
        naturally prevents path traversal regardless of the input ID.
        """
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def _workspace_dir(self, team_id: str, user_id: str, space_id: str | None) -> Path:
        sid = space_id or "global"
        if sid != "global":
            sid = self._safe_segment(sid)
        return self.root / self._safe_segment(team_id) / self._safe_segment(user_id) / sid

    def ensure_workspace(self, team_id: str, user_id: str, space_id: str | None) -> Path:
        d = self._workspace_dir(team_id, user_id, space_id)
        d.mkdir(parents=True, exist_ok=True)
        journal = d / "journal"
        journal.mkdir(exist_ok=True)
        default_contents = {
            "SELF.md": self.DEFAULT_SELF_MD,
            "MEMORY.md": "",
            "HISTORY.md": "",
            "RECENT_CONTEXT.md": "",
            "PENDING.md": "",
        }
        for name, default_content in default_contents.items():
            f = d / name
            if not f.exists():
                f.write_text(default_content, encoding="utf-8")
        return d

    # ------------------------------------------------------------------
    # SELF.md — assistant self-model / relationship understanding
    # ------------------------------------------------------------------

    def read_self_model(self, team_id: str, user_id: str, space_id: str | None) -> str:
        f = self._workspace_dir(team_id, user_id, space_id) / "SELF.md"
        if not f.exists():
            return ""
        return f.read_text("utf-8").strip()

    def write_self_model(self, team_id: str, user_id: str, space_id: str | None, text: str) -> None:
        d = self._workspace_dir(team_id, user_id, space_id)
        f = d / "SELF.md"
        self._atomic_write(f, text.strip() + "\n")

    # ------------------------------------------------------------------
    # MEMORY.md — long-term facts / preferences
    # ------------------------------------------------------------------

    def read_long_term(self, team_id: str, user_id: str, space_id: str | None) -> str:
        f = self._workspace_dir(team_id, user_id, space_id) / "MEMORY.md"
        if not f.exists():
            return ""
        return f.read_text("utf-8").strip()

    def write_long_term(self, team_id: str, user_id: str, space_id: str | None, text: str) -> None:
        d = self._workspace_dir(team_id, user_id, space_id)
        f = d / "MEMORY.md"
        self._atomic_write(f, text.strip() + "\n")

    # ------------------------------------------------------------------
    # HISTORY.md — event timeline, append-only
    # ------------------------------------------------------------------

    def read_history(self, team_id: str, user_id: str, space_id: str | None, max_chars: int = 0) -> str:
        f = self._workspace_dir(team_id, user_id, space_id) / "HISTORY.md"
        if not f.exists():
            return ""
        text = f.read_text("utf-8")
        if max_chars > 0 and len(text) > max_chars:
            text = text[-max_chars:]
        return text.strip()

    def append_history_once(
        self, team_id: str, user_id: str, space_id: str | None,
        entries: list[dict[str, Any]], source_ref: str,
    ) -> bool:
        """Idempotent append. Returns False if source_ref already present."""
        return self._idempotent_append(
            team_id, user_id, space_id,
            "HISTORY.md", entries, source_ref, kind="history",
            formatter=lambda e: f"- {e.get('summary', '')}",
        )

    # ------------------------------------------------------------------
    # RECENT_CONTEXT.md — working-memory snapshot
    # ------------------------------------------------------------------

    def read_recent_context(
        self, team_id: str, user_id: str, space_id: str | None,
        include_recent_turns: bool = False,
    ) -> str:
        f = self._workspace_dir(team_id, user_id, space_id) / "RECENT_CONTEXT.md"
        if not f.exists():
            return ""
        text = f.read_text("utf-8")
        if not include_recent_turns:
            # Strip the Recent Turns block delimited by HTML comment markers
            text = re.sub(
                r"\n?<!-- BEGIN Recent Turns -->.*<!-- END Recent Turns -->",
                "", text, flags=re.DOTALL,
            ).strip()
        return text.strip()

    def write_recent_context(
        self, team_id: str, user_id: str, space_id: str | None, text: str,
    ) -> None:
        d = self._workspace_dir(team_id, user_id, space_id)
        f = d / "RECENT_CONTEXT.md"
        self._atomic_write(f, text.strip() + "\n")

    # ------------------------------------------------------------------
    # PENDING.md — items awaiting archiving
    # ------------------------------------------------------------------

    def read_pending(self, team_id: str, user_id: str, space_id: str | None) -> str:
        f = self._workspace_dir(team_id, user_id, space_id) / "PENDING.md"
        if not f.exists():
            return ""
        return f.read_text("utf-8").strip()

    def append_pending_once(
        self, team_id: str, user_id: str, space_id: str | None,
        items: list[dict[str, Any]], source_ref: str,
    ) -> bool:
        return self._idempotent_append(
            team_id, user_id, space_id,
            "PENDING.md", items, source_ref, kind="pending",
            formatter=lambda i: f"- [{i.get('tag', '')}] {i.get('content', '')}",
        )

    # ------------------------------------------------------------------
    # Pending snapshots
    # ------------------------------------------------------------------

    def snapshot_pending(self, team_id: str, user_id: str, space_id: str | None) -> str:
        import uuid
        sid = str(uuid.uuid4())[:12]
        d = self._workspace_dir(team_id, user_id, space_id)
        snap_dir = d / ".snapshots"
        snap_dir.mkdir(exist_ok=True)
        src = d / "PENDING.md"
        if src.exists():
            (snap_dir / f"{sid}.md").write_text(src.read_text("utf-8"), encoding="utf-8")
        return sid

    def commit_pending_snapshot(
        self, team_id: str, user_id: str, space_id: str | None, snapshot_id: str,
    ) -> None:
        """Remove the snapshot file after successful archive."""
        snap = self._workspace_dir(team_id, user_id, space_id) / ".snapshots" / f"{snapshot_id}.md"
        if snap.exists():
            snap.unlink()

    def rollback_pending_snapshot(
        self, team_id: str, user_id: str, space_id: str | None, snapshot_id: str,
    ) -> None:
        """Restore PENDING.md from snapshot."""
        d = self._workspace_dir(team_id, user_id, space_id)
        snap = d / ".snapshots" / f"{snapshot_id}.md"
        if snap.exists():
            (d / "PENDING.md").write_text(snap.read_text("utf-8"), encoding="utf-8")
            snap.unlink()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _atomic_write(self, target: Path, content: str) -> None:
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix="." + target.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(content)
            os.replace(tmp, str(target))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _idempotent_append(
        self, team_id: str, user_id: str, space_id: str | None,
        filename: str, items: list[dict[str, Any]], source_ref: str, kind: str,
        formatter,
    ) -> bool:
        d = self._workspace_dir(team_id, user_id, space_id)
        f = d / filename
        ref_hash = _hash_ref(source_ref)
        marker = _source_ref_marker(ref_hash, kind)

        existing = f.read_text("utf-8") if f.exists() else ""
        if marker in existing:
            return False  # already written

        new_lines = [formatter(item) for item in items]
        new_lines.append(marker)
        new_block = "\n".join(new_lines) + "\n"

        if existing and not existing.endswith("\n"):
            existing += "\n"
        self._atomic_write(f, existing + new_block)
        return True
