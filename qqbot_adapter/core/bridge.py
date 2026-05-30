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
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .bus import MessageBus
from .events import InboundMessage, OutboundMessage, SSEEvent
from utils.formatter import find_flush_point, markdown_to_qq

_logger = logging.getLogger(__name__)

# 单条 QQ 消息最大长度（留余量给前缀）
_MAX_MESSAGE_CHARS = 3500
# 同 chat_id 最小发送间隔（秒），防止 QQ 频控
_MIN_SEND_INTERVAL = 0.3
_QQBOT_PASSIVE_REPLY_LIMIT = 5
_QQBOT_REPLY_COUNT_CACHE_LIMIT = 2048

# SSE 流式失败时触发同步降级的异常类型
_STREAMING_FALLBACK_ERRORS = (
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.ConnectTimeout,
    ConnectionError,
    OSError,
)

# 确认关键词（用户回复这些词触发确认）
_CONFIRM_KEYWORDS = ("确认", "是", "yes", "ok", "执行")
_CANCEL_KEYWORDS = ("取消", "否", "no", "跳过", "不执行")

# 需精确匹配的关键词（短英文词容易误匹配，如 "ok" in "smoke"）
_EXACT_MATCH_KEYWORDS = frozenset({"ok", "no", "yes"})


def _match_keyword(content: str, keywords: tuple[str, ...]) -> bool:
    """检查 content 是否匹配任一关键词。

    英文短词（ok/no/yes）需精确匹配防止误触发；
    中文词（确认/取消等）支持子串匹配（如「好的，确认」）。
    """
    stripped = content.strip().lower()
    for kw in keywords:
        if kw in _EXACT_MATCH_KEYWORDS:
            if stripped == kw:
                return True
        else:
            if kw in stripped:
                return True
    return False


@dataclass
class PendingConfirmation:
    """等待用户确认的危险操作。"""

    run_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    requester_user_id: str = ""  # 发起人，确认/取消必须同一用户或群管理员
    created_at: float = field(default_factory=time.monotonic)


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
        system_prompt: str | None = None,
    ) -> None:
        self.bus = bus
        self.caibao_url = caibao_base_url.rstrip("/")
        self.bot_user_id = bot_user_id
        self.bot_password = bot_password
        self.http_timeout = http_timeout
        self.system_prompt = system_prompt

        # httpx 客户端（带 cookie 持久化，用于 CaiBao JWT 认证）
        self._client: httpx.AsyncClient | None = None
        self._authenticated = False
        # 频控：chat_id → 上次发送时间戳
        self._last_send_time: dict[str, float] = {}
        # 待确认的危险操作：chat_id → PendingConfirmation
        self._pending_confirmations: dict[str, PendingConfirmation] = {}
        self._reply_counts: dict[tuple[str, str, str], int] = {}
        # QQ 标识 → CaiBao 真实 conversation UUID
        # TODO(P2): Persist conversation mapping.
        #   Suggest implementing via CaiBao backend resolve API at POST /api/v1/conversations/resolve.
        #   Currently in-memory only; restart loses mapping and creates new conversations.
        self._conversation_cache: dict[str, str] = {}
        # 会话创建锁（防止并发创建重复会话）
        self._conversation_locks: dict[str, asyncio.Lock] = {}

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
        """处理一条入站消息：确认拦截 → SSE 流式 → 同步 fallback。

        策略：
        0. 如果用户有待确认操作，先检查消息是否为「确认」/「取消」
        1. 创建 queued run → SSE 流式消费（实时推送）
        2. 若 SSE 连接失败 / 超时 / 协议不兼容 → 降级为同步调用
        """
        # 0. 检查是否是对待确认操作的回复
        if await self._try_handle_confirmation(inbound):
            return

        try:
            if inbound.channel_type == "qqbot":
                await self._handle_message_sync(inbound)
                return
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
                await self._publish_reply(inbound, "⚠️ Agent 处理请求时出错，请稍后重试。")
        except httpx.HTTPStatusError as exc:
            _logger.error(
                "CaiBao API error for user %s: %s %s",
                inbound.user_id, exc.response.status_code, exc.response.text[:200],
            )
            await self._publish_reply(
                inbound,
                f"⚠️ CaiBao 服务返回错误 ({exc.response.status_code})，请检查 Bot 账号配置。",
            )
        except Exception:
            _logger.exception(
                "Failed to handle message from %s: %.100s",
                inbound.user_id, inbound.content,
            )
            await self._publish_reply(inbound, "⚠️ Agent 处理请求时出错，请稍后重试。")

    async def _handle_message_streaming(self, inbound: InboundMessage) -> None:
        """SSE 流式路径：创建 queued run → 流式消费 → 智能分段推送。

        改进点 (Phase 3)：
        - 句子边界分段（非固定字符数切割）
        - 流式开始即发送「正在思考」提示
        - 已流式推送过的文本不重复发送（去重）
        - 同 chat_id 频控（最小 0.3s 间隔）
        """
        # 0. 发送「正在思考」提示
        await self._send_throttled(inbound, "⏳ 正在思考...", tool_status="thinking")

        # 1. 创建 CaiBao Agent Run
        run_id = await self._create_run(inbound)

        # 2. SSE 流式消费，边收边发
        acc_text: list[str] = []
        streamed_text: list[str] = []  # 记录已推送的文本（用于最终去重）
        final_answer = ""

        async for event in self._stream_run(run_id):
            if event.event == "llm.delta":
                delta = str(event.payload.get("text", ""))
                if delta:
                    acc_text.append(delta)
                    # 检查是否到了 flush 时机
                    flush_point = find_flush_point("".join(acc_text))
                    if flush_point is not None:
                        full = "".join(acc_text)
                        chunk = full[:flush_point].strip()
                        rest = full[flush_point:].lstrip()
                        if chunk:
                            formatted = markdown_to_qq(chunk)
                            await self._send_throttled(inbound, formatted, tool_status="thinking")
                            streamed_text.append(formatted)
                        # 保留剩余部分继续累积
                        acc_text = [rest] if rest else []
            else:
                await self._dispatch_sse_event(inbound, event)
                # 捕获最终答案
                if event.event in ("step.completed", "run.completed"):
                    answer = str(event.payload.get("answer", ""))
                    if answer:
                        final_answer = answer
                # 保存待确认状态
                if event.event == "confirmation.required":
                    self._save_pending_confirmation(
                        inbound=inbound,
                        run_id=run_id,
                        tool_name=str(event.payload.get("tool_name", "unknown")),
                        arguments=event.payload.get("arguments", {}),
                    )

        # 3. 推送剩余文本
        if acc_text:
            remaining = "".join(acc_text).strip()
            if remaining:
                formatted = markdown_to_qq(remaining)
                # 去重：如果和已推送的最后一条相同则跳过
                if not streamed_text or formatted != streamed_text[-1]:
                    await self._send_throttled(inbound, formatted, tool_status="done")
                    streamed_text.append(formatted)
            if not final_answer:
                final_answer = remaining

        # 4. 最终答案去重推送
        if final_answer:
            formatted_final = markdown_to_qq(final_answer)
            # 只有和已流式推送的内容不同时才发送
            already_sent = "".join(streamed_text)
            if formatted_final.strip() and formatted_final.strip() not in already_sent:
                await self._send_long_message(inbound, formatted_final, is_final=True)

    # ------------------------------------------------------------------
    # 危险操作确认交互
    # ------------------------------------------------------------------

    async def _try_handle_confirmation(self, inbound: InboundMessage) -> bool:
        """检查入站消息是否为确认/取消回复。是则处理并返回 True。

        安全约束：群聊中只有发起人（或私聊中本人）可以确认/取消。
        其他成员发消息不接管待确认状态。
        """
        pending = self._pending_confirmations.get(inbound.chat_id)
        if pending is None:
            return False

        # 安全：群聊中只有发起人可以确认/取消
        if pending.requester_user_id and inbound.user_id != pending.requester_user_id:
            _logger.info(
                "Confirmation ignored: chat=%s requester=%s sender=%s",
                inbound.chat_id, pending.requester_user_id, inbound.user_id,
            )
            return False  # 其他用户的消息走正常流程

        if _match_keyword(inbound.content, _CONFIRM_KEYWORDS):
            await self._handle_confirm(inbound, pending)
            return True
        if _match_keyword(inbound.content, _CANCEL_KEYWORDS):
            await self._handle_cancel(inbound, pending)
            return True

        # 发起人发了其他消息，视为取消等待
        self._pending_confirmations.pop(inbound.chat_id, None)
        _logger.info(
            "Confirmation dismissed for chat=%s (new message received)",
            inbound.chat_id,
        )
        return False

    async def _handle_confirm(
        self, inbound: InboundMessage, pending: PendingConfirmation,
    ) -> None:
        """用户确认：调用 CaiBao confirm API 执行危险操作。"""
        await self._send_throttled(inbound, "⏳ 正在执行确认的操作...", tool_status="tool_call")

        try:
            result = await self.confirm_run(pending.run_id)
            answer = str(result.get("answer", "") or "")
            status = str(result.get("status", ""))

            if answer:
                formatted = markdown_to_qq(answer)
                await self._send_long_message(inbound, formatted, is_final=True)
            elif status == "failed":
                await self._send_throttled(
                    inbound, "❌ 确认执行失败，请检查任务内容。",
                )
            else:
                await self._send_throttled(
                    inbound, "✅ 已确认执行。",
                )
        except Exception:
            _logger.exception("Confirm API failed for run=%s", pending.run_id)
            await self._send_throttled(
                inbound, "⚠️ 确认请求失败，请稍后重试。",
            )
        finally:
            self._pending_confirmations.pop(inbound.chat_id, None)

    async def _handle_cancel(
        self, inbound: InboundMessage, pending: PendingConfirmation,
    ) -> None:
        """用户取消：清除待确认状态。"""
        await self._send_throttled(
            inbound,
            f"✅ 已取消操作「{pending.tool_name}」，Agent 不会执行此步骤。",
        )
        self._pending_confirmations.pop(inbound.chat_id, None)
        _logger.info(
            "Confirmation cancelled: run=%s tool=%s chat=%s",
            pending.run_id, pending.tool_name, inbound.chat_id,
        )

    def _save_pending_confirmation(
        self,
        *,
        inbound: InboundMessage,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> None:
        """保存待确认状态，记录发起人防止其他用户接管。"""
        self._pending_confirmations[inbound.chat_id] = PendingConfirmation(
            run_id=run_id,
            tool_name=tool_name,
            arguments=dict(arguments),
            requester_user_id=inbound.user_id,
        )
        _logger.info(
            "Pending confirmation saved: chat=%s run=%s tool=%s requester=%s",
            inbound.chat_id, run_id, tool_name, inbound.user_id,
        )

    # ------------------------------------------------------------------
    # 频控发送
    # ------------------------------------------------------------------

    async def _send_throttled(
        self,
        inbound: InboundMessage,
        content: str,
        *,
        tool_status: str | None = None,
    ) -> None:
        """带频控的发消息：同 chat_id 至少间隔 _MIN_SEND_INTERVAL 秒。"""
        chat_id = inbound.chat_id
        now = time.monotonic()
        last = self._last_send_time.get(chat_id, 0)
        wait = _MIN_SEND_INTERVAL - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)

        await self._publish_reply(inbound, content, tool_status=tool_status)
        self._last_send_time[chat_id] = time.monotonic()

    async def _publish_reply(
        self,
        inbound: InboundMessage,
        content: str,
        *,
        tool_status: str | None = None,
    ) -> None:
        if not self._consume_reply_budget(inbound):
            return
        await self.bus.publish_outbound(OutboundMessage(
            channel_type=inbound.channel_type,
            chat_id=inbound.chat_id,
            content=content,
            reply_to=inbound.message_id or None,
            tool_status=tool_status,
        ))

    def _consume_reply_budget(self, inbound: InboundMessage) -> bool:
        if inbound.channel_type != "qqbot" or not inbound.message_id:
            return True

        key = (inbound.channel_type, inbound.chat_id, inbound.message_id)
        count = self._reply_counts.get(key, 0)
        if count >= _QQBOT_PASSIVE_REPLY_LIMIT:
            _logger.warning(
                "QQBot passive reply limit reached: chat=%s msg_id=%s",
                inbound.chat_id,
                inbound.message_id,
            )
            return False

        if key not in self._reply_counts and len(self._reply_counts) >= _QQBOT_REPLY_COUNT_CACHE_LIMIT:
            self._reply_counts.pop(next(iter(self._reply_counts)), None)
        self._reply_counts[key] = count + 1
        return True

    async def _handle_message_sync(self, inbound: InboundMessage) -> None:
        """同步降级路径：POST /api/v1/agent/run → 一次性返回完整答案。"""
        await self._publish_reply(
            inbound,
            "⏳ 正在处理（同步模式，请稍候）...",
            tool_status="thinking",
        )

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
            await self._publish_reply(
                inbound,
                "\n".join(lines),
                tool_status="done",
            )

        # 推送确认提示 + 保存待确认状态（支持交互式回复）
        confirmations = result.get("required_confirmations") or []
        if confirmations:
            # 取第一个待确认操作
            first = confirmations[0] if isinstance(confirmations[0], dict) else {}
            tool_name = first.get("tool_name", "unknown")
            args = first.get("arguments", {})
            args_text = json.dumps(args, ensure_ascii=False)[:200]

            await self._publish_reply(
                inbound,
                (
                    f"⚠️ 需要确认危险操作\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"工具: {tool_name}\n"
                    f"参数: {args_text}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"回复「确认」执行，回复「取消」跳过"
                ),
            )
            # 保存待确认状态，支持用户交互式回复
            run_id = str(result.get("run_id", ""))
            if run_id:
                self._save_pending_confirmation(
                    inbound=inbound,
                    run_id=run_id,
                    tool_name=tool_name,
                    arguments=dict(args),
                )

        # 推送最终答案
        if answer:
            await self._send_long_message(inbound, answer, is_final=True)
        elif status == "failed":
            await self._publish_reply(inbound, "❌ Agent 执行失败，请检查任务内容或稍后重试。")

    # ------------------------------------------------------------------
    # CaiBao API 调用
    # ------------------------------------------------------------------

    async def _ensure_conversation(self, msg: InboundMessage) -> str:
        """获取或创建 CaiBao conversation 的真实 UUID。

        首次发消息时 POST /api/v1/conversations，后续复用缓存。
        使用 per-key 锁防止并发消息创建重复会话。
        """
        cache_key = self._make_conversation_id(msg)
        cached = self._conversation_cache.get(cache_key)
        if cached is not None:
            return cached

        # 获取或创建锁，防止并发创建同一会话
        lock = self._conversation_locks.setdefault(cache_key, asyncio.Lock())
        async with lock:
            # 双重检查：锁内可能有其他协程已经创建了
            cached = self._conversation_cache.get(cache_key)
            if cached is not None:
                return cached

            assert self._client is not None
            try:
                resp = await self._api_post_with_retry(
                    f"{self.caibao_url}/api/v1/conversations",
                    {"title": f"QQ {msg.chat_type} {msg.user_name or msg.user_id}"},
                )
                resp.raise_for_status()
                real_id = resp.json()["conversation_id"]
            except Exception:
                _logger.warning(
                    "Failed to create conversation for %s, running without context",
                    cache_key,
                )
                return ""

            self._conversation_cache[cache_key] = real_id
            _logger.info("Conversation created: %s → %s", cache_key, real_id)
            return real_id

    async def _auth_refresh(self) -> None:
        """401 时尝试刷新 JWT token。"""
        assert self._client is not None
        try:
            resp = await self._client.post(
                f"{self.caibao_url}/api/v1/auth/refresh",
                timeout=30.0,
            )
            resp.raise_for_status()
            self._authenticated = True
            _logger.info("JWT token refreshed")
        except Exception:
            _logger.warning("Token refresh failed, re-logging in")
            self._authenticated = False
            await self._ensure_authenticated()

    def _build_run_payload(self, msg: InboundMessage) -> dict[str, Any]:
        """构建 CaiBao AgentRunRequest payload（流式和同步共用）。

        注意：conversation_id 在调用前由 _ensure_conversation 填入真实 UUID。
        """
        payload: dict[str, Any] = {
            "task": msg.content,
            "trigger_channel": "qqbot",
            "include_memory": True,
            "include_library": True,
            "dry_run": False,
            "confirm_dangerous_actions": False,  # QQ 侧走确认流程
        }
        if self.system_prompt:
            payload["system_prompt"] = self.system_prompt
        return payload

    async def _create_run(self, msg: InboundMessage) -> str:
        """POST /api/v1/agent/runs 创建队列运行，返回 run_id。"""
        assert self._client is not None

        conv_id = await self._ensure_conversation(msg)
        payload = self._build_run_payload(msg)
        if conv_id:
            payload["conversation_id"] = conv_id

        resp = await self._api_post_with_retry(
            f"{self.caibao_url}/api/v1/agent/runs",
            payload,
        )
        resp.raise_for_status()
        data = resp.json()
        run_id = data["run_id"]
        _logger.info("Run created: run_id=%s user=%s", run_id, msg.user_id)
        return run_id

    async def _run_sync(self, msg: InboundMessage) -> dict[str, Any]:
        """POST /api/v1/agent/run 同步调用，阻塞等待完整结果。"""
        assert self._client is not None

        conv_id = await self._ensure_conversation(msg)
        payload = self._build_run_payload(msg)
        if conv_id:
            payload["conversation_id"] = conv_id

        resp = await self._api_post_with_retry(
            f"{self.caibao_url}/api/v1/agent/run",
            payload,
        )
        resp.raise_for_status()
        data = resp.json()
        _logger.info(
            "Sync run completed: user=%s status=%s answer_len=%s",
            msg.user_id, data.get("status"), len(data.get("answer", "") or ""),
        )
        return data

    async def _api_post_with_retry(
        self, url: str, payload: dict[str, Any],
    ) -> httpx.Response:
        """POST 请求，401 时自动刷新 token 重试一次。"""
        assert self._client is not None
        resp = await self._client.post(url, json=payload, timeout=self.http_timeout)
        if resp.status_code == 401:
            await self._auth_refresh()
            resp = await self._client.post(url, json=payload, timeout=self.http_timeout)
        return resp

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

        resp = await self._api_post_with_retry(
            f"{self.caibao_url}/api/v1/agent/runs/{run_id}/confirm",
            {},
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # SSE 事件分发
    # ------------------------------------------------------------------

    async def _dispatch_sse_event(self, inbound: InboundMessage, event: SSEEvent) -> None:
        """根据 SSE 事件类型生成对应的 QQ 出站消息。

        注意：
        - llm.delta 事件由 _handle_message_streaming 累积推送
        - step.completed / run.completed 的最终答案也由 _handle_message_streaming
          统一发送（去重），此处仅记录不发送
        - 所有消息通过 _send_throttled 走频控
        """
        event_type = event.event

        if event_type in ("llm.delta", "step.completed", "run.completed"):
            # 由 _handle_message_streaming 统一处理
            return

        if event_type == "tool.proposed":
            tool_name = event.payload.get("tool_name", "unknown")
            await self._send_throttled(
                inbound, f"🤔 LLM 提议调用工具: `{tool_name}`", tool_status="thinking",
            )

        elif event_type == "tool.started":
            tool_name = event.payload.get("tool_name", "unknown")
            await self._send_throttled(
                inbound, f"🔧 正在执行: `{tool_name}` ...", tool_status="tool_call",
            )

        elif event_type == "tool.result":
            tool_name = event.payload.get("tool_name", "unknown")
            result = event.payload.get("result", {})
            if isinstance(result, dict):
                msg_text = str(result.get("message", ""))
                if not msg_text:
                    msg_text = json.dumps(result, ensure_ascii=False, default=str)[:200]
            else:
                msg_text = str(result)[:200]

            await self._send_throttled(
                inbound,
                f"✅ `{tool_name}` 完成: {msg_text}" if msg_text else f"✅ `{tool_name}` 完成",
                tool_status="done",
            )

        elif event_type == "confirmation.required":
            tool_name = event.payload.get("tool_name", "unknown")
            args = event.payload.get("arguments", {})
            args_text = json.dumps(args, ensure_ascii=False)[:200]

            await self._send_throttled(
                inbound,
                (
                    f"⚠️ 危险操作需要确认\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"工具: {tool_name}\n"
                    f"参数: {args_text}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"回复「确认」执行，回复「取消」跳过"
                ),
            )

        elif event_type == "run.failed":
            error = event.payload.get("error", "未知错误")
            await self._send_throttled(
                inbound, f"❌ Agent 执行失败: {error}",
            )

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
            await self._publish_reply(
                inbound,
                text,
                tool_status="done" if is_final else None,
            )
            return

        # 分段发送
        parts = self._split_text(text, _MAX_MESSAGE_CHARS)
        for i, part in enumerate(parts):
            suffix = f"  ({i + 1}/{len(parts)})" if len(parts) > 1 else ""
            await self._publish_reply(inbound, part + suffix)

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
                    "user_id": self.bot_user_id,
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
