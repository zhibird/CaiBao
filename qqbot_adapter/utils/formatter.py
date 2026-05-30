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

# 句子结束符（用于智能分段）
_SENTENCE_END_PATTERN = re.compile(r"[。！？!?\n](?=\s*)")
# 段落结束（双换行，优先级最高）
_PARAGRAPH_END_PATTERN = re.compile(r"\n\s*\n")

# 智能分段参数
_MIN_FLUSH_CHARS = 120   # 低于此字符数不急于 flush
_SOFT_FLUSH_CHARS = 400  # 超过此字符数在句子边界 flush
_HARD_FLUSH_CHARS = 700  # 超过此字符数强制 flush（在最后空格处）


def find_flush_point(text: str, *, hard_max: int = _HARD_FLUSH_CHARS) -> int | None:
    """在文本中找到最佳 flush 断点。

    优先级（从高到低）：
    1. 段落结束（\\n\\n）且超过 _MIN_FLUSH_CHARS
    2. 句子结束（。！？!?\\n）且超过 _SOFT_FLUSH_CHARS
    3. 超过 _HARD_FLUSH_CHARS 时在最后一个空格处强制截断
    4. 不超过 _HARD_FLUSH_CHARS 时返回 None（暂不 flush）

    返回 None 表示当前不应 flush，应继续累积。
    """
    if len(text) < _MIN_FLUSH_CHARS:
        return None

    # 1. 优先在段落结束处分段
    para_matches = list(_PARAGRAPH_END_PATTERN.finditer(text))
    if para_matches:
        for m in reversed(para_matches):
            end = m.end()
            if _MIN_FLUSH_CHARS <= end <= hard_max:
                return end

    # 2. 在句子结束处分段（超过软阈值时）
    if len(text) >= _SOFT_FLUSH_CHARS:
        sent_matches = list(_SENTENCE_END_PATTERN.finditer(text))
        if sent_matches:
            for m in reversed(sent_matches):
                end = m.end()
                if end <= hard_max:
                    return end

    # 3. 超过硬阈值，强制在空格处截断
    if len(text) >= hard_max:
        space_at = text.rfind(" ", 0, hard_max)
        if space_at > _MIN_FLUSH_CHARS:
            return space_at
        # 找不到空格，在硬阈值处直接截断
        return hard_max

    # 4. 还不够长，继续累积
    return None


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
