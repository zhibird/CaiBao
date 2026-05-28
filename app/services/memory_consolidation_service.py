from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any

from app.core.config import get_settings
from app.events.lifecycle import (
    ConsolidationCommitted,
    MemoryUpdated,
    TurnCommitted,
)
from app.events.event_bus import EventBus
from app.services.memory_markdown_store import MemoryMarkdownStore

_logger = logging.getLogger(__name__)


def _make_source_ref(run_id: str, conversation_id: str | None, *message_ids: str) -> str:
    raw = run_id + (conversation_id or "") + "".join(message_ids)
    return hashlib.sha256(raw.encode()).hexdigest()


class MemoryConsolidationService:
    """Listens to TurnCommitted events and maintains markdown memory files.

    - Every turn: refreshes the Recent Turns block in RECENT_CONTEXT.md.
    - Every ``min_turns`` turns: triggers a full consolidation pass (LLM
      extraction of pending items + history entries + context compression).
    """

    def __init__(
        self,
        event_bus: EventBus,
        store: MemoryMarkdownStore,
        memory_service_factory=None,  # callable() -> fresh MemoryService
        llm_service=None,     # optional: LLMService for extraction
        fast_model_name: str = "",
        fast_base_url: str = "",
        fast_api_key: str = "",
    ) -> None:
        self._bus = event_bus
        self._store = store
        self._memory_svc_factory = memory_service_factory
        self._llm = llm_service
        self._fast_model_name = fast_model_name
        self._fast_base_url = fast_base_url
        self._fast_api_key = fast_api_key
        self._turn_counts: dict[str, int] = {}
        self._turn_buffers: dict[str, list[dict[str, Any]]] = {}
        self._workspace_locks: dict[str, threading.Lock] = {}
        self._locks_lock = threading.Lock()

        if get_settings().memory_markdown_enabled:
            event_bus.observe("TurnCommitted", self._on_turn_committed)
            event_bus.observe("ConsolidationCommitted", self._on_consolidation_committed)

    # ------------------------------------------------------------------
    # Turn handling
    # ------------------------------------------------------------------

    def _on_turn_committed(self, event: TurnCommitted) -> None:
        settings = get_settings()
        store = self._store
        store.ensure_workspace(event.team_id, event.user_id, event.space_id)

        conv_key = event.conversation_id or event.run_id
        ws_key = f"{event.team_id}:{event.user_id}:{event.space_id or 'global'}"

        # Lock the entire read-modify-write block per workspace so
        # concurrent TurnCommitted events cannot clobber RECENT_CONTEXT.md.
        with self._get_workspace_lock(ws_key):
            # 1. Update RECENT_CONTEXT.md "Recent Turns" block
            self._update_recent_turns(event, conv_key)

            # 2. Buffer the current turn for the consolidation window
            buf = self._turn_buffers.setdefault(conv_key, [])
            buf.append({"input": event.input_message, "response": event.assistant_response[:500]})
            max_buf = max(settings.memory_consolidation_min_turns, 6)
            if len(buf) > max_buf:
                buf[:] = buf[-max_buf:]

            # 3. Track turn count
            count = self._turn_counts.get(conv_key, 0) + 1
            self._turn_counts[conv_key] = count

            # 4. Trigger consolidation at threshold
            if count >= settings.memory_consolidation_min_turns:
                self._run_consolidation(event, conv_key, turns=list(buf))
                self._turn_counts[conv_key] = 0
                buf.clear()

    # ------------------------------------------------------------------
    # Recent turns
    # ------------------------------------------------------------------

    def _update_recent_turns(self, event: TurnCommitted, conv_key: str) -> None:
        settings = get_settings()
        store = self._store
        recent = store.read_recent_context(
            event.team_id, event.user_id, event.space_id,
            include_recent_turns=True,
        )

        # Extract existing turns between markers
        turns_match = re.search(
            r"<!-- BEGIN Recent Turns -->\r?\n(.*)<!-- END Recent Turns -->",
            recent, re.DOTALL,
        )
        preamble = re.sub(
            r"\n?<!-- BEGIN Recent Turns -->.*<!-- END Recent Turns -->",
            "", recent, flags=re.DOTALL,
        ).strip()

        old_turns = turns_match.group(1).strip() if turns_match else ""

        task_preview = event.input_message[:120].replace("\n", " ")
        tools_str = ", ".join(event.tools_used) if event.tools_used else "none"
        new_line = f"- **{event.timestamp}** | `{event.run_id[:8]}` | {task_preview} | tools: {tools_str}"

        lines = [l for l in old_turns.split("\n") if l.strip().startswith("-")]
        lines.append(new_line)
        lines = lines[-settings.memory_recent_turns:]

        new_recent = preamble
        if new_recent:
            new_recent += "\n\n"
        new_recent += "<!-- BEGIN Recent Turns -->\n" + "\n".join(lines) + "\n<!-- END Recent Turns -->"
        store.write_recent_context(event.team_id, event.user_id, event.space_id, new_recent)

    # ------------------------------------------------------------------
    # Consolidation
    # ------------------------------------------------------------------

    def _get_workspace_lock(self, key: str) -> threading.Lock:
        with self._locks_lock:
            if key not in self._workspace_locks:
                self._workspace_locks[key] = threading.Lock()
            return self._workspace_locks[key]

    def _run_consolidation(self, event: TurnCommitted, conv_key: str, turns: list[dict]) -> None:
        source_ref = _make_source_ref(event.run_id, event.conversation_id)
        store = self._store

        entries, pending, context_update = self._extract_memory(
            event=event, turns=turns, source_ref=source_ref,
        )

        # Write HISTORY/PENDING (idempotent via source_ref)
        if entries:
            store.append_history_once(
                event.team_id, event.user_id, event.space_id,
                entries, source_ref,
            )
        if pending:
            store.append_pending_once(
                event.team_id, event.user_id, event.space_id,
                pending, source_ref,
            )
            # Promote identity/preference/key_info items to long-term MEMORY.md
            long_term_tags = {"identity", "preference", "key_info", "health_long_term"}
            lt_items = [p for p in pending if p.get("tag", "") in long_term_tags]
            if lt_items:
                existing_mem = store.read_long_term(event.team_id, event.user_id, event.space_id)
                new_lines = []
                for p in lt_items:
                    new_lines.append(f"- [{p['tag']}] {p['content']}")
                merged = existing_mem
                if merged and not merged.endswith("\n"):
                    merged += "\n"
                merged += "\n".join(new_lines) + "\n"
                store.write_long_term(event.team_id, event.user_id, event.space_id, merged)
        if context_update:
            existing = store.read_recent_context(
                event.team_id, event.user_id, event.space_id,
                include_recent_turns=True,
            )
            # Insert compression/threads BEFORE the Recent Turns markers so
            # they survive future rotations of the turns block.
            insert = ""
            compression = context_update.get("compression", [])
            threads = context_update.get("ongoing_threads", [])
            if compression:
                insert += "## Compression\n" + "\n".join(f"- {c}" for c in compression) + "\n\n"
            if threads:
                insert += "## Ongoing Threads\n" + "\n".join(f"- {t}" for t in threads) + "\n\n"
            if insert:
                marker_pos = existing.find("<!-- BEGIN Recent Turns -->")
                if marker_pos >= 0:
                    merged = existing[:marker_pos] + insert + existing[marker_pos:]
                else:
                    merged = existing + "\n\n" + insert
                store.write_recent_context(
                    event.team_id, event.user_id, event.space_id, merged,
                )

        # Emit events
        committed = ConsolidationCommitted(
            team_id=event.team_id, user_id=event.user_id,
            space_id=event.space_id, source_ref=source_ref,
            history_entries=entries, pending_items=pending,
        )
        self._bus.observe_event(committed)
        self._bus.fanout(MemoryUpdated(
            team_id=event.team_id, user_id=event.user_id,
            space_id=event.space_id, source_ref=source_ref,
            files=["RECENT_CONTEXT.md"] + (["HISTORY.md"] if entries else []) + (["PENDING.md"] if pending else []),
            summary="consolidation completed",
        ))

    def _extract_memory(self, event: TurnCommitted, turns: list[dict], source_ref: str):
        """LLM-powered extraction over the full N-turn window."""
        if self._llm is None or not self._fast_model_name:
            return [], [], {}
        # Read recent history for broader context
        recent_text = self._store.read_history(
            event.team_id, event.user_id, event.space_id, max_chars=2000,
        )
        # Build conversation window from buffered turns
        window_lines = []
        for i, t in enumerate(turns, 1):
            window_lines.append(f"Turn {i}: User: {t['input'][:300]}")
            window_lines.append(f"Turn {i}: Assistant: {t['response'][:300]}")
        window = "\n".join(window_lines)
        prompt = (
            f"Conversation window ({len(turns)} turns):\n{window}\n"
            f"Recent history: {recent_text or '(none)'}\n\n"
            "Extract long-term memory from the conversation window above. "
            "Return ONLY valid JSON:\n"
            '{"history_entries": [{"summary": "brief summary", "emotional_weight": 0}], '
            '"pending_items": [{"tag": "preference|key_info|identity|...", "content": "..."}], '
            '"recent_context": {"compression": ["key point"], "ongoing_threads": ["thread"]}}\n\n'
            "Rules:\n"
            "- Only extract user-stated long-term facts/identity/preferences into pending_items\n"
            "- Assistant suggestions, tool results, short-term state, one-time plans are NOT pending\n"
            "- Keep entries concise (max 200 chars each)\n"
            "- If nothing to extract, return empty arrays"
        )

        try:
            result = self._llm.complete_chat(
                messages=[
                    {"role": "system", "content": "You are a memory extraction assistant. Extract long-term user facts only."},
                    {"role": "user", "content": prompt},
                ],
                model=self._fast_model_name,
                base_url=self._fast_base_url,
                api_key=self._fast_api_key,
                tools=None,
            )
            text = (result.assistant_text or "").strip()
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                parsed = json.loads(text[start:end])
                raw_entries = parsed.get("history_entries", []) if isinstance(parsed, dict) else []
                raw_pending = parsed.get("pending_items", []) if isinstance(parsed, dict) else []
                ctx = parsed.get("recent_context", {}) if isinstance(parsed, dict) else {}
                # Validate: entries must be list of dicts with summary field
                entries = [
                    e for e in raw_entries[:10]
                    if isinstance(e, dict) and isinstance(e.get("summary"), str) and e["summary"].strip()
                ]
                pending = [
                    p for p in raw_pending[:10]
                    if isinstance(p, dict) and isinstance(p.get("tag"), str) and isinstance(p.get("content"), str)
                ]
                ctx = ctx if isinstance(ctx, dict) else {}
                # Normalize: compression and ongoing_threads must be list[str]
                ctx["compression"] = [
                    str(c) for c in ctx.get("compression", [])
                    if c is not None
                ] if isinstance(ctx.get("compression"), list) else []
                ctx["ongoing_threads"] = [
                    str(t) for t in ctx.get("ongoing_threads", [])
                    if t is not None
                ] if isinstance(ctx.get("ongoing_threads"), list) else []
                return entries, pending, ctx
        except Exception:
            _logger.exception("LLM extraction failed, using empty consolidation")

        return [], [], {}

    def _on_consolidation_committed(self, event: ConsolidationCommitted) -> None:
        """Bridge: write consolidated entries to the vector memory layer."""
        if not event.history_entries:
            return
        if self._memory_svc_factory is None:
            _logger.warning("ConsolidationCommitted has %d history_entries but no memory_service_factory — vector bridge disabled", len(event.history_entries))
            return
        svc = None
        try:
            svc = self._memory_svc_factory()
            svc.create_from_consolidation(
                team_id=event.team_id,
                user_id=event.user_id,
                space_id=event.space_id,
                history_entries=event.history_entries,
                source_ref=event.source_ref,
            )
        except Exception:
            _logger.exception("Vector bridge failed for %s", event.source_ref)
        finally:
            if svc is not None:
                try:
                    svc.db.close()
                except Exception:
                    pass
