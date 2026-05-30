"""AgentBridge：QQ 消息 ←→ CaiBao Agent API 的双向翻译层。

职责：
1. 消费 MessageBus 入站消息
2. 翻译为 CaiBao AgentRunRequest → 调用 REST API
3. SSE 流式消费 → 实时翻译为 QQ 消息段
4. 发布出站消息到 MessageBus

设计要点：
- 每个 QQ 用户对应一个独立的 conversation_id（"qq_{user_id}"），实现会话隔离
- 流式模式下，每累积足够字符就发一条 QQ 消息（QQ 单条长度有限）
- 工具调用状态通过 tool_status 字段传递，前端可展示进度
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from .bus import MessageBus
from .events import InboundMessage, OutboundMessage, SSEEvent

_logger = logging.getLogger(__name__)

# 流式推送阈值：每累积这么多字符发一条 QQ 消息
_STREAMING_FLUSH_CHARS = 300
# 单条 QQ 消息最大长度（留余量给前缀）
_MAX_MESSAGE_CHARS = 3500

# SSE 流式失败时触发同步降级的异常类型
_STREAMING_FALLBACK_ERRORS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    ConnectionError,
    OSError,
)


class AgentBridge:
    """QQ → CaiBao 桥接器。"""

    def __init__(
        self,
        bus: MessageBus,
        *,
        caibao_base_url: str,
        bot_user_id: str,
        bot_password: str,
        http_timeout: float = 120.0,
    ) -> None:
        self.bus = bus
        self.caibao_url = caibao_base_url.rstrip("/")
        self.bot_user_id = bot_user_id
        self.bot_password = bot_password
        self.http_timeout = http_timeout

        # httpx 客户端（带 cookie 持久化，用于 CaiBao JWT 认证）
        self._client: httpx.AsyncClient | None = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """主循环：认证 → 消费入站队列 → 调用 CaiBao → 发布出站。"""
        await self._ensure_authenticated()

        _logger.info("AgentBridge main loop started")
        while True:
            inbound = await self.bus.consume_inbound()
            asyncio.create_task(self._handle_message(inbound))

    async def _handle_message(self, inbound: InboundMessage) -> None:
        """处理一条入站消息：优先 SSE 流式 → 失败则同步 fallback。

        策略：
        1. 创建 queued run → SSE 流式消费（实时推送）
        2. 若 SSE 连接失败 / 超时 / 协议不兼容 → 降级为同步调用
        3. 同步调用一次性返回完整答案
        """
        try:
            await self._handle_message_streaming(inbound)
        except _STREAMING_FALLBACK_ERRORS as exc:
            _logger.warning(
                "SSE streaming failed for user %s, falling back to sync: %s",
                inbound.user_id, exc,
            )
            try:
                await self._handle_message_sync(inbound)
            except Exception:
                _logger.exception(
                    "Sync fallback also failed for user %s: %.100s",
                    inbound.user_id, inbound.content,
                )
                await self.bus.publish_outbound(OutboundMessage(
                    channel_type=inbound.channel_type,
                    chat_id=inbound.chat_id,
                    content="⚠️ Agent 处理请求时出错，请稍后重试。",
                ))
        except httpx.HTTPStatusError as exc:
            _logger.error(
                "CaiBao API error for user %s: %s %s",
                inbound.user_id, exc.response.status_code, exc.response.text[:200],
            )
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=f"⚠️ CaiBao 服务返回错误 ({exc.response.status_code})，请检查 Bot 账号配置。",
            ))
        except Exception:
            _logger.exception(
                "Failed to handle message from %s: %.100s",
                inbound.user_id, inbound.content,
            )
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content="⚠️ Agent 处理请求时出错，请稍后重试。",
            ))

    async def _handle_message_streaming(self, inbound: InboundMessage) -> None:
        """SSE 流式路径：创建 queued run → 流式消费 → 分段推送。"""
        # 1. 创建 CaiBao Agent Run
        run_id = await self._create_run(inbound)

        # 2. SSE 流式消费，边收边发
        acc_text: list[str] = []
        char_count = 0
        final_answer = ""

        async for event in self._stream_run(run_id):
            if event.event == "llm.delta":
                delta = str(event.payload.get("text", ""))
                if delta:
                    acc_text.append(delta)
                    char_count += len(delta)
                    if char_count >= _STREAMING_FLUSH_CHARS:
                        chunk = "".join(acc_text)
                        await self.bus.publish_outbound(OutboundMessage(
                            channel_type=inbound.channel_type,
                            chat_id=inbound.chat_id,
                            content=chunk,
                            tool_status="thinking",
                        ))
                        acc_text.clear()
                        char_count = 0
            else:
                await self._dispatch_sse_event(inbound, event)
                if event.event == "step.completed":
                    final_answer = str(event.payload.get("answer", ""))

        # 3. 推送剩余文本
        if acc_text:
            remaining = "".join(acc_text)
            if not final_answer:
                final_answer = remaining
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=remaining,
                tool_status="done",
            ))

        # 4. 最终答案
        if final_answer and final_answer != "".join(acc_text):
            await self._send_long_message(inbound, final_answer, is_final=True)

    async def _handle_message_sync(self, inbound: InboundMessage) -> None:
        """同步降级路径：POST /api/v1/agent/run → 一次性返回完整答案。"""
        await self.bus.publish_outbound(OutboundMessage(
            channel_type=inbound.channel_type,
            chat_id=inbound.chat_id,
            content="⏳ 正在处理（同步模式，请稍候）...",
            tool_status="thinking",
        ))

        result = await self._run_sync(inbound)

        answer = str(result.get("answer", "") or "")
        status = str(result.get("status", ""))

        # 推送工具调用摘要
        tool_calls = result.get("tool_calls") or []
        if tool_calls:
            lines = [f"🔧 已执行 {len(tool_calls)} 个工具："]
            for tc in tool_calls:
                name = tc.get("tool_name", "unknown")
                dangerous = "⚠️" if tc.get("dangerous") else ""
                lines.append(f"  - {dangerous}{name}")
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content="\n".join(lines),
                tool_status="done",
            ))

        # 推送确认提示
        confirmations = result.get("required_confirmations") or []
        if confirmations:
            for conf in confirmations:
                tool_name = conf.get("tool_name", "unknown")
                args = conf.get("arguments", {})
                args_text = json.dumps(args, ensure_ascii=False)[:200]
                await self.bus.publish_outbound(OutboundMessage(
                    channel_type=inbound.channel_type,
                    chat_id=inbound.chat_id,
                    content=(
                        f"⚠️ 需要确认危险操作\n"
                        f"工具: {tool_name}\n"
                        f"参数: {args_text}\n\n"
                        f"回复「确认」执行，回复「取消」跳过。"
                    ),
                ))

        # 推送最终答案
        if answer:
            await self._send_long_message(inbound, answer, is_final=True)
        elif status == "failed":
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content="❌ Agent 执行失败，请检查任务内容或稍后重试。",
            ))

    # ------------------------------------------------------------------
    # CaiBao API 调用
    # ------------------------------------------------------------------

    def _build_run_payload(self, msg: InboundMessage) -> dict[str, Any]:
        """构建 CaiBao AgentRunRequest payload（流式和同步共用）。"""
        return {
            "conversation_id": self._make_conversation_id(msg),
            "task": msg.content,
            "trigger_channel": "qqbot",
            "include_memory": True,
            "include_library": True,
            "dry_run": False,
            "confirm_dangerous_actions": False,  # QQ 侧走确认流程
        }

    async def _create_run(self, msg: InboundMessage) -> str:
        """POST /api/v1/agent/runs 创建队列运行，返回 run_id。"""
        assert self._client is not None

        payload = self._build_run_payload(msg)
        resp = await self._client.post(
            f"{self.caibao_url}/api/v1/agent/runs",
            json=payload,
            timeout=self.http_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        run_id = data["run_id"]
        _logger.info("Run created: run_id=%s user=%s", run_id, msg.user_id)
        return run_id

    async def _run_sync(self, msg: InboundMessage) -> dict[str, Any]:
        """POST /api/v1/agent/run 同步调用，阻塞等待完整结果。

        当 SSE 流式路径不可用时（网络异常、CaiBao 不兼容等）作为降级方案。
        返回 AgentRunResponse dict，含 answer / status / tool_calls 等字段。
        """
        assert self._client is not None

        payload = self._build_run_payload(msg)
        resp = await self._client.post(
            f"{self.caibao_url}/api/v1/agent/run",
            json=payload,
            timeout=self.http_timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        _logger.info(
            "Sync run completed: user=%s status=%s answer_len=%s",
            msg.user_id, data.get("status"), len(data.get("answer", "") or ""),
        )
        return data

    async def _stream_run(self, run_id: str) -> AsyncIterator[SSEEvent]:
        """GET /api/v1/agent/runs/{run_id}/stream 返回 SSE 事件迭代器。"""
        assert self._client is not None

        async with self._client.stream(
            "GET",
            f"{self.caibao_url}/api/v1/agent/runs/{run_id}/stream",
            timeout=self.http_timeout,
        ) as resp:
            resp.raise_for_status()

            buffer = ""
            async for chunk in resp.aiter_text():
                if not chunk:
                    continue
                buffer += chunk
                # SSE 事件以 \n\n 分隔
                while "\n\n" in buffer:
                    raw, buffer = buffer.split("\n\n", 1)
                    event = SSEEvent.from_sse_line(raw)
                    if event is not None:
                        yield event

            # 处理末尾残留
            if buffer.strip():
                event = SSEEvent.from_sse_line(buffer)
                if event is not None:
                    yield event

    async def confirm_run(self, run_id: str) -> dict[str, Any]:
        """POST /api/v1/agent/runs/{run_id}/confirm 确认危险操作。"""
        assert self._client is not None

        resp = await self._client.post(
            f"{self.caibao_url}/api/v1/agent/runs/{run_id}/confirm",
            json={},
            timeout=self.http_timeout,
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # SSE 事件分发
    # ------------------------------------------------------------------

    async def _dispatch_sse_event(self, inbound: InboundMessage, event: SSEEvent) -> None:
        """根据 SSE 事件类型生成对应的 QQ 出站消息。

        注意：llm.delta 事件由 _handle_message 统一累积推送，此处不处理。
        """
        event_type = event.event

        if event_type == "llm.delta":
            # 已由 _handle_message 累积推送
            return

        if event_type == "tool.proposed":
            tool_name = event.payload.get("tool_name", "unknown")
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=f"🤔 LLM 提议调用工具: `{tool_name}`",
                tool_status="thinking",
            ))

        elif event_type == "tool.started":
            tool_name = event.payload.get("tool_name", "unknown")
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=f"🔧 正在执行: `{tool_name}` ...",
                tool_status="tool_call",
            ))

        elif event_type == "tool.result":
            tool_name = event.payload.get("tool_name", "unknown")
            result = event.payload.get("result", {})
            if isinstance(result, dict):
                msg_text = str(result.get("message", ""))
                if not msg_text:
                    msg_text = json.dumps(result, ensure_ascii=False, default=str)[:200]
            else:
                msg_text = str(result)[:200]

            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=f"✅ `{tool_name}` 完成: {msg_text}" if msg_text else f"✅ `{tool_name}` 完成",
                tool_status="done",
            ))

        elif event_type == "confirmation.required":
            tool_name = event.payload.get("tool_name", "unknown")
            args = event.payload.get("arguments", {})
            args_text = json.dumps(args, ensure_ascii=False)[:200]

            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=(
                    f"⚠️ **需要确认危险操作**\n"
                    f"工具: `{tool_name}`\n"
                    f"参数: {args_text}\n\n"
                    f"回复「确认」执行，回复「取消」跳过。"
                ),
            ))

        elif event_type == "run.failed":
            error = event.payload.get("error", "未知错误")
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=f"❌ Agent 执行失败: {error}",
            ))

        elif event_type == "run.completed":
            # 如果 step.completed 没有提供答案，从 run.completed 中提取
            answer = event.payload.get("answer", "")
            if answer:
                await self._send_long_message(inbound, answer, is_final=True)

        elif event_type == "step.completed":
            # 步骤完成，可能包含最终答案
            answer = event.payload.get("answer", "")
            if answer:
                await self._send_long_message(inbound, answer, is_final=True)

    # ------------------------------------------------------------------
    # 消息发送辅助
    # ------------------------------------------------------------------

    async def _send_long_message(
        self,
        inbound: InboundMessage,
        text: str,
        is_final: bool = False,
    ) -> None:
        """将长文本分段发送到 QQ（QQ 有单条消息长度限制）。"""
        if not text.strip():
            return

        # 如果文本不太长，一次发送
        if len(text) <= _MAX_MESSAGE_CHARS:
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=text,
                tool_status="done" if is_final else None,
            ))
            return

        # 分段发送
        parts = self._split_text(text, _MAX_MESSAGE_CHARS)
        for i, part in enumerate(parts):
            suffix = f"  ({i + 1}/{len(parts)})" if len(parts) > 1 else ""
            await self.bus.publish_outbound(OutboundMessage(
                channel_type=inbound.channel_type,
                chat_id=inbound.chat_id,
                content=part + suffix,
            ))

    @staticmethod
    def _split_text(text: str, max_chars: int) -> list[str]:
        """按换行边界分段，尽量保持语义完整。"""
        if len(text) <= max_chars:
            return [text]

        parts: list[str] = []
        remaining = text
        while len(remaining) > max_chars:
            # 在 max_chars 范围内找最后一个换行
            split_at = remaining.rfind("\n", 0, max_chars)
            if split_at == -1 or split_at < max_chars // 2:
                split_at = max_chars
            parts.append(remaining[:split_at].strip())
            remaining = remaining[split_at:].lstrip("\n")
        if remaining.strip():
            parts.append(remaining.strip())
        return parts

    # ------------------------------------------------------------------
    # 认证
    # ------------------------------------------------------------------

    async def _ensure_authenticated(self) -> None:
        """登录 CaiBao 获取 JWT cookie，保持 httpx 会话。

        失败时给出明确错误信息（如 Bot 账号不存在、密码错误）。
        """
        if self._authenticated and self._client is not None:
            return

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.http_timeout),
            follow_redirects=False,
        )

        login_url = f"{self.caibao_url}/api/v1/auth/login"
        try:
            resp = await self._client.post(
                login_url,
                json={
                    "account_id": self.bot_user_id,
                    "password": self.bot_password,
                },
            )
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = ""
            try:
                detail = exc.response.json().get("detail", "")
            except Exception:
                detail = exc.response.text[:200]
            _logger.error(
                "Login failed for bot user '%s' at %s: HTTP %s — %s",
                self.bot_user_id, login_url, exc.response.status_code, detail,
            )
            raise SystemExit(
                f"无法登录 CaiBao，请检查:\n"
                f"  1. CaiBao 是否已启动 ({self.caibao_url})\n"
                f"  2. Bot 账号 '{self.bot_user_id}' 是否已注册\n"
                f"  3. Bot 密码是否正确\n"
                f"  API 返回: {detail or str(exc)}"
            ) from exc
        except httpx.ConnectError as exc:
            _logger.error("Cannot connect to CaiBao at %s: %s", self.caibao_url, exc)
            raise SystemExit(
                f"无法连接 CaiBao ({self.caibao_url})，请确认服务已启动。"
            ) from exc

        self._authenticated = True
        _logger.info("AgentBridge authenticated as user=%s", self.bot_user_id)

    async def close(self) -> None:
        """关闭 HTTP 客户端。"""
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._authenticated = False

    # ------------------------------------------------------------------
    # 会话映射
    # ------------------------------------------------------------------

    @staticmethod
    def _make_conversation_id(msg: InboundMessage) -> str:
        """为 QQ 用户生成 CaiBao conversation_id。

        私聊：qq_{user_id} （每个用户独立会话）
        群聊：qq_group_{group_id} （每个群独立会话）
        """
        if msg.chat_type == "group":
            group_id = msg.chat_id.removeprefix("g")
            return f"qq_group_{group_id}"
        return f"qq_{msg.user_id}"
