"""共享数据结构：入站 / 出站消息 + SSE 事件类型。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InboundMessage:
    """QQ 通道 → MessageBus 的入站消息。"""

    channel_type: str  # "napcat" | "qqbot"
    chat_type: str  # "private" | "group"
    chat_id: str  # QQ 号 或 "g{群号}"
    user_id: str  # 发送者 QQ 号
    user_name: str  # 发送者昵称
    content: str  # 消息文本（纯文本）
    images: list[bytes] = field(default_factory=list)  # 图片附件（原始字节）
    message_id: str = ""  # QQ 消息 ID（用于回复引用）
    raw_event: dict[str, Any] = field(default_factory=dict)  # OneBot 原始事件


@dataclass
class OutboundMessage:
    """Agent Bridge → QQ 通道的出站消息。"""

    channel_type: str  # 目标通道类型
    chat_id: str  # 目标聊天 ID
    content: str  # 消息文本
    reply_to: str | None = None  # 回复某条消息的 message_id
    tool_status: str | None = None  # "thinking" | "tool_call" | "done" | None


@dataclass
class SSEEvent:
    """CaiBao SSE 事件的解析结果。"""

    event: str  # "llm.delta" | "tool.proposed" | "tool.started" | "tool.result"
    # | "confirmation.required" | "step.completed" | "run.completed" | "run.failed"
    run_id: str
    seq: int
    payload: dict[str, Any]

    @classmethod
    def from_sse_line(cls, raw: str) -> SSEEvent | None:
        """从 SSE 文本行解析事件。

        SSE 格式:
            event: llm.delta
            data: {"event":"llm.delta","run_id":"...","seq":1,"payload":{...}}
        """
        event_type = ""
        data_json = ""
        for line in raw.split("\n"):
            if line.startswith("event: "):
                event_type = line[len("event: "):].strip()
            elif line.startswith("data: "):
                data_json = line[len("data: "):].strip()

        if not data_json:
            return None

        try:
            obj = json.loads(data_json)
        except json.JSONDecodeError:
            return None

        return cls(
            event=event_type or obj.get("event", ""),
            run_id=obj.get("run_id", ""),
            seq=obj.get("seq", 0),
            payload=obj.get("payload", {}),
        )
