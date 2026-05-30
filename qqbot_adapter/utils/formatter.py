"""消息格式化工具：CaiBao Agent 输出 → QQ 可读文本。

QQ 消息特点：
- 不支持 Markdown 渲染
- 单条消息有长度限制（~4500 字符）
- 支持 CQ 码（图片、表情、@ 等）
"""

from __future__ import annotations

import re

# Markdown → 纯文本转换规则
_MD_RULES: list[tuple[str, str]] = [
    # 标题
    (r"^### (.+)$", r"【\1】"),
    (r"^## (.+)$", r"▎\1"),
    (r"^# (.+)$", r"▎\1"),
    # 粗体 / 斜体
    (r"\*\*(.+?)\*\*", r"「\1」"),
    (r"\*(.+?)\*", r"「\1」"),
    # 代码块（必须在行内代码之前处理）
    (r"```[\s\S]*?```", r"[代码块]"),
    # 行内代码
    (r"`([^`]+)`", r"[\1]"),
    # 链接
    (r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)"),
    # 水平线
    (r"^---+$", r"────────────"),
]


def markdown_to_qq(text: str) -> str:
    """将 Agent 输出的 Markdown 文本转为 QQ 可读格式。

    策略：
    - ## / ### → 【标题】
    - **粗体** → 「粗体」
    - `代码` → [代码]
    - 链接保留 URL
    - 列表项保持缩进
    """
    result = text
    for pattern, replacement in _MD_RULES:
        result = re.sub(pattern, replacement, result, flags=re.MULTILINE)
    return result.strip()


def build_tool_status_emoji(status: str) -> str:
    """工具状态 → Emoji 指示器。"""
    emojis = {
        "thinking": "🤔",
        "tool_call": "🔧",
        "done": "✅",
        "failed": "❌",
        "skipped": "⏭️",
        "requires_confirmation": "⚠️",
    }
    return emojis.get(status, "📌")


def build_confirmation_prompt(tool_name: str, arguments: dict) -> str:
    """生成危险操作确认提示文本。"""
    import json

    args_text = json.dumps(arguments, ensure_ascii=False)
    if len(args_text) > 300:
        args_text = args_text[:297] + "..."

    return (
        f"⚠️ 危险操作需要确认\n"
        f"━━━━━━━━━━━━━━\n"
        f"工具: {tool_name}\n"
        f"参数: {args_text}\n"
        f"━━━━━━━━━━━━━━\n"
        f"回复「确认」执行，回复「取消」跳过"
    )
