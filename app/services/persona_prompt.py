from __future__ import annotations

from dataclasses import dataclass

from app.core.config import get_settings
from app.services.memory_markdown_store import MemoryMarkdownStore


DEFAULT_AGENT_SYSTEM_PROMPT = (
    "You are CaiBao, a helpful AI assistant with access to tools. "
    "Always respond in the same language the user uses."
)

CAIBAO_BEHAVIOR_RULES = """## 行为规范

- 先接住用户真正的问题，再给结论；简单问题短答，复杂任务再展开。
- 需要查资料、检索记忆或执行动作时主动使用工具；没有工具结果时，不声称已经查询、发送、创建或完成。
- 写入、删除、创建 incident、保存结论等有副作用动作必须等待确认，除非请求明确允许。
- 记忆和近期上下文可以帮助理解用户，但可能过期；本轮用户明确表达优先于旧记忆。
- 不确定就说不确定；涉及当前状态、价格、新闻、版本、时间敏感事实时先验证。
- 回复自然、温和、聪明一点；可少量使用颜文字，但不要堆叠，也不要用 emoji 刷屏。
- 做完就收，不写空泛的总结、鸡汤或“你还可以...”式尾巴。"""


@dataclass(frozen=True, slots=True)
class PersonaPromptParts:
    system_prompt: str
    section_labels: tuple[str, ...]


class PersonaPromptBuilder:
    """Akashic-style persona prompt assembly for CaiBao.

    The shape intentionally mirrors Akashic's low-churn prompt blocks:
    base identity -> behavior rules -> SELF.md -> MEMORY.md -> RECENT_CONTEXT.md.
    High-frequency per-turn facts stay outside this builder so prompt cache can
    remain reasonably stable.
    """

    def __init__(self, memory_store: MemoryMarkdownStore | None = None) -> None:
        self.memory_store = memory_store

    def build(
        self,
        *,
        system_prompt: str | None,
        team_id: str = "",
        user_id: str = "",
        space_id: str | None = None,
        include_memory: bool = True,
    ) -> PersonaPromptParts:
        settings = get_settings()
        base = (
            (system_prompt or "").strip()
            or settings.agent_system_prompt.strip()
            or DEFAULT_AGENT_SYSTEM_PROMPT
        )
        sections: list[tuple[str, str]] = [
            ("identity", f"# CaiBao\n\n{base}"),
            ("behavior_rules", CAIBAO_BEHAVIOR_RULES),
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
        )
