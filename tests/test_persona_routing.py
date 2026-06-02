"""Tests for persona per-channel routing."""

from __future__ import annotations

from app.core.config import reload_settings
from app.services.persona_prompt import PersonaPromptBuilder
from app.services.persona_profiles import resolve_persona_name


class TestResolvePersonaName:
    def test_web_channels_map_to_enterprise(self):
        assert resolve_persona_name("web") == "enterprise"
        assert resolve_persona_name("web-app") == "enterprise"
        assert resolve_persona_name("api") == "enterprise"
        assert resolve_persona_name("app") == "enterprise"

    def test_qq_channels_map_to_companion(self):
        assert resolve_persona_name("qqbot") == "companion"
        assert resolve_persona_name("qq-group") == "companion"
        assert resolve_persona_name("qq-channel") == "companion"
        assert resolve_persona_name("wechat") == "companion"

    def test_case_insensitive(self):
        assert resolve_persona_name("QQBOT") == "companion"
        assert resolve_persona_name("Web") == "enterprise"

    def test_unknown_defaults_to_enterprise(self):
        assert resolve_persona_name("slack") == "enterprise"
        assert resolve_persona_name("discord") == "enterprise"
        assert resolve_persona_name("") == "enterprise"
        assert resolve_persona_name(None) == "enterprise"


class TestPersonaPromptRouting:
    def test_build_defaults_to_enterprise_when_no_channel(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-routing-test.db")
        monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
        reload_settings()
        builder = PersonaPromptBuilder()

        result = builder.build(system_prompt=None, include_memory=False)

        assert result.persona_name == "enterprise"
        assert "高级 AI 助手" in result.system_prompt
        assert "撒娇拌嘴" not in result.system_prompt  # companion phrase should not leak

    def test_build_uses_companion_for_qqbot_channel(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-routing-test.db")
        monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
        reload_settings()
        builder = PersonaPromptBuilder()

        result = builder.build(system_prompt=None, channel="qqbot", include_memory=False)

        assert result.persona_name == "companion"
        assert "长期 AI 伙伴" in result.system_prompt
        assert "撒娇拌嘴" in result.system_prompt
        assert "颜文字" in result.system_prompt  # companion mentions kaomoji

    def test_build_uses_enterprise_for_web_channel(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-routing-test.db")
        monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
        reload_settings()
        builder = PersonaPromptBuilder()

        result = builder.build(system_prompt=None, channel="web", include_memory=False)

        assert result.persona_name == "enterprise"
        assert "企业用户" in result.system_prompt  # check enterprise identity phrase
        assert "撒娇拌嘴" not in result.system_prompt  # companion phrase should not leak

    def test_api_override_takes_precedence_over_channel_persona(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-routing-test.db")
        monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
        reload_settings()
        builder = PersonaPromptBuilder()

        result = builder.build(
            system_prompt="Custom override prompt.", channel="qqbot", include_memory=False,
        )

        assert result.persona_name == "companion"
        # identity section uses API override, but behavior_rules are still companion
        assert "Custom override prompt." in result.system_prompt
        assert "撒娇拌嘴" in result.system_prompt  # behavior rules preserved

    def test_both_personas_include_memory_when_enabled(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DATABASE_URL", "sqlite:///persona-routing-test.db")
        monkeypatch.delenv("AGENT_SYSTEM_PROMPT", raising=False)
        reload_settings()
        from app.services.memory_markdown_store import MemoryMarkdownStore

        store = MemoryMarkdownStore(root_dir=str(tmp_path))
        store.ensure_workspace("team-1", "user-1", "space-1")
        store.write_long_term("team-1", "user-1", "space-1", "- User likes cats.")

        builder = PersonaPromptBuilder(memory_store=store)

        # enterprise with memory
        r1 = builder.build(
            system_prompt=None, channel="web",
            team_id="team-1", user_id="user-1", space_id="space-1",
            include_memory=True,
        )
        assert "User likes cats." in r1.system_prompt
        assert r1.persona_name == "enterprise"

        # companion with memory — same store, same user
        r2 = builder.build(
            system_prompt=None, channel="qqbot",
            team_id="team-1", user_id="user-1", space_id="space-1",
            include_memory=True,
        )
        assert "User likes cats." in r2.system_prompt
        assert r2.persona_name == "companion"
