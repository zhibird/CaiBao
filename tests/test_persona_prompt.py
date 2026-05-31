from __future__ import annotations

from app.core.config import reload_settings
from app.services.memory_markdown_store import MemoryMarkdownStore
from app.services.persona_prompt import PersonaPromptBuilder


def test_persona_prompt_uses_configured_system_prompt_without_memory(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-test.db")
    monkeypatch.setenv("AGENT_SYSTEM_PROMPT", "You are Test CaiBao.")
    reload_settings()
    builder = PersonaPromptBuilder(memory_store=MemoryMarkdownStore(root_dir=str(tmp_path)))

    result = builder.build(system_prompt=None, include_memory=False)

    assert "You are Test CaiBao." in result.system_prompt
    assert "行为规范" in result.system_prompt
    assert "CaiBao 自我认知" not in result.system_prompt
    assert result.section_labels == ("identity", "behavior_rules")


def test_persona_prompt_injects_self_memory_and_recent_context(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-test.db")
    monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
    reload_settings()
    store = MemoryMarkdownStore(root_dir=str(tmp_path))
    store.ensure_workspace("team-1", "user-1", "space-1")
    store.write_long_term("team-1", "user-1", "space-1", "- User prefers concise answers.")
    store.write_recent_context("team-1", "user-1", "space-1", "## Ongoing\n\nWorking on CaiBao persona.")
    builder = PersonaPromptBuilder(memory_store=store)

    result = builder.build(
        system_prompt="Custom app prompt.",
        team_id="team-1",
        user_id="user-1",
        space_id="space-1",
        include_memory=True,
    )

    assert "Custom app prompt." in result.system_prompt
    assert "CaiBao 自我认知" in result.system_prompt
    assert "User prefers concise answers." in result.system_prompt
    assert "Working on CaiBao persona." in result.system_prompt
    assert result.section_labels == (
        "identity",
        "behavior_rules",
        "self_model",
        "long_term_memory",
        "recent_context",
    )
