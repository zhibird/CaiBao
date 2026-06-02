"""Persona prompt assembly for CaiBao.

Per-channel persona routing:
  web / web-app / api / app  → enterprise  (严肃专业)
  qqbot / qq-group / wechat … → companion   (活泼陪伴)
  unknown / None             → enterprise  (安全默认)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.services.persona_profiles import (
    COMPANION_BEHAVIOR_RULES,
    COMPANION_IDENTITY,
    ENTERPRISE_BEHAVIOR_RULES,
    ENTERPRISE_IDENTITY,
    PersonaProfile,
    resolve_persona_name,
)

# ── backward-compatible fallback ───────────────────────────────────────────

DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are CaiBao, a helpful AI assistant with access to tools. "
    "Always respond in the same language the user uses."
)

CAIBAO_BEHAVIOR_RULES = ENTERPRISE_BEHAVIOR_RULES  # legacy alias

# ── builtin profiles (keyed by name) ───────────────────────────────────────

BUILTIN_PERSONAS: dict[str, PersonaProfile] = {
    "enterprise": PersonaProfile(
        name="enterprise",
        identity=ENTERPRISE_IDENTITY,
        behavior_rules=ENTERPRISE_BEHAVIOR_RULES,
    ),
    "companion": PersonaProfile(
        name="companion",
        identity=COMPANION_IDENTITY,
        behavior_rules=COMPANION_BEHAVIOR_RULES,
    ),
}


# ── result type ────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PersonaPromptParts:
    system_prompt: str
    section_labels: tuple[str, ...]
    persona_name: str  # which persona was used


# ── builder ────────────────────────────────────────────────────────────────


class PersonaPromptBuilder:
    """Akashic-style persona prompt assembly with per-channel routing.

    Builds a system prompt from:
      persona_profile.identity  →  "# CaiBao"
      persona_profile.behavior_rules  →  "## 行为规范"
      SELF.md  (optional, memory)
      MEMORY.md  (optional, memory)
      RECENT_CONTEXT.md  (optional, memory)

    High-frequency per-turn facts stay outside this builder so prompt
    cache can remain reasonably stable.
    """

    def __init__(
        self,
        memory_store: MemoryMarkdownStore | None = None,
        *,
        personas: dict[str, PersonaProfile] | None = None,
    ) -> None:
        self.memory_store = memory_store
        self._personas = personas or BUILTIN_PERSONAS

    def build(
        self,
        *,
        system_prompt: str | None,
        channel: str | None = None,
        team_id: str = "",
        user_id: str = "",
        space_id: str | None = None,
        include_memory: bool = True,
    ) -> PersonaPromptParts:
        settings = get_settings()
        persona_name = resolve_persona_name(channel)
        profile = self._personas.get(persona_name, self._personas["enterprise"])

        # identity: API override > config.toml override > persona profile default
        api_override = (system_prompt or "").strip()
        config_override = settings.agent_system_prompt.strip()
        if api_override:
            identity = api_override
        elif config_override and config_override != DEFAULT_AGENT_SYSTEM_PROMPT:
            identity = config_override
        else:
            identity = profile.identity

        sections: list[tuple[str, str]] = [
            ("identity", f"# CaiBao\n\n{identity}"),
            ("behavior_rules", profile.behavior_rules),
        ]

        if include_memory and settings.memory_markdown_enabled and self.memory_store is not None and team_id and user_id:
            self.memory_store.ensure_workspace(team_id, user_id, space_id)
            self_model = self.memory_store.read_self_model(team_id, user_id, space_id)
            if self_model:
                sections.append(("self_model", f"## CaiBao 自我认知\n\n{self_model}"))

            long_term = self.memory_store.read_long_term(team_id, user_id, space_id)
            if long_term:
                sections.append(("long_term_memory", f"## Long-term Memory\n\n{long_term[:6000]}"))

            recent = self.memory_store.read_recent_context(
                team_id, user_id, space_id, include_recent_turns=False,
            )
            if recent:
                sections.append(("recent_context", f"## Recent Context\n\n{recent[:3000]}"))

        return PersonaPromptParts(
            system_prompt="\n\n---\n\n".join(content for _, content in sections if content.strip()),
            section_labels=tuple(label for label, content in sections if content.strip()),
            persona_name=persona_name,
        )
