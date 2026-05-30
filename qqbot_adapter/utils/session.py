"""会话管理：QQ 用户 ↔ CaiBao conversation_id 映射。

设计：
- 私聊：每个 QQ 用户对应一个独立的 CaiBao conversation_id
  格式：qq_{user_id}
- 群聊：每个群对应一个独立的 CaiBao conversation_id
  格式：qq_group_{group_id}
- 这确保了 QQ 侧的会话隔离与 CaiBao 的 conversation 机制一致
"""

from __future__ import annotations


def make_private_conversation_id(qq_user_id: str) -> str:
    """私聊会话 ID。"""
    return f"qq_{qq_user_id}"


def make_group_conversation_id(group_id: str) -> str:
    """群聊会话 ID（group_id 不带 'g' 前缀）。"""
    return f"qq_group_{group_id}"


def parse_conversation_id(conversation_id: str) -> tuple[str, str]:
    """解析 conversation_id，返回 (chat_type, identifier)。

    >>> parse_conversation_id("qq_123456")
    ("private", "123456")
    >>> parse_conversation_id("qq_group_789012")
    ("group", "789012")
    """
    if conversation_id.startswith("qq_group_"):
        return ("group", conversation_id[len("qq_group_"):])
    if conversation_id.startswith("qq_"):
        return ("private", conversation_id[len("qq_"):])
    return ("unknown", conversation_id)
