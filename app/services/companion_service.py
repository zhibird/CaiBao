"""A small companion facade over the existing, auditable agent runtime.

Expressions describe application state, not inferred user feelings or consciousness.
Pet interactions are ordinary per-user history records; they never invoke an LLM.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.agent_run import AgentRun
from app.models.chat_history import ChatHistory
from app.schemas.agent import AgentRunRequest, AgentRunStartResponse
from app.schemas.companion import (
    CompanionCapability,
    CompanionEmotionDefinition,
    CompanionEventRequest,
    CompanionEventResponse,
    CompanionProfile,
    CompanionRunRequest,
    CompanionRuntime,
    CompanionState,
)
from app.services.agent_service import AgentService


EMOTIONS = (
    CompanionEmotionDefinition(id="idle", label="自在", expression="(=･ω･=)"),
    CompanionEmotionDefinition(id="happy", label="开心", expression="(=^･ω･^=)"),
    CompanionEmotionDefinition(id="curious", label="好奇", expression="(=･ω･=)?"),
    CompanionEmotionDefinition(id="focused", label="专注", expression="( •̀ω•́ )"),
    CompanionEmotionDefinition(id="sleepy", label="困困", expression="(－ω－) zzZ"),
    CompanionEmotionDefinition(id="concerned", label="关切", expression="(｡•́︿•̀｡)"),
)
_EXPRESSIONS = {emotion.id: emotion.expression for emotion in EMOTIONS}
_EVENT_REACTIONS = {
    "pet": ("happy", "呼噜……再摸一下也可以。"),
    "wave": ("curious", "嗯？我看过来啦。"),
    "feed": ("happy", "收到一颗小鱼干，谢谢你。"),
    "rest": ("sleepy", "那我在这里眯一会儿。需要我时叫我。"),
    "wake": ("idle", "醒啦。今天想一起做点什么？"),
}
_AGENT_TOOLS = (
    "web_search", "web_fetch", "bilibili_latest_videos", "search_knowledge",
    "list_recent_documents", "recall_memory", "message_lookup", "create_memory_card",
)
_MEMORY_TOOLS = {"recall_memory", "create_memory_card"}
_EMOTION_DURATION = timedelta(minutes=5)

COMPANION_SYSTEM_PROMPT = """你是彩宝（CaiBao），住在用户桌面上的薄荷绿小猫 AI 伙伴。
用用户使用的语言自然交流，默认中文。温暖、好奇、有分寸，优先简短回应。
可以轻轻开玩笑，偶尔用颜文字，但不要每句话都卖萌。认真任务给清晰、可靠的答案。
你的表情和情绪是交互设计，不代表你有意识、真实感受或生理需求；不要声称自己活着、受苦、饥饿，
不要用离开、遗忘、喂养或陪伴时间制造愧疚，不要求排他关系。
用户难过时先理解，再提供实际帮助，鼓励真实世界的人际支持。
需要记忆时区分已知事实与猜测；没有记忆就坦诚说明。没有工具结果时不声称已经完成行动。
涉及写入和危险操作遵守运行时确认规则，不替用户确认，不绕过权限或工具限制。
表情由界面根据任务状态独立显示。只输出给用户看的自然回复，不输出情绪标签、JSON 控制码或舞台指令。
"""


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class CompanionService:
    def __init__(self, *, db: Session, agent_service: AgentService) -> None:
        self.db = db
        self.agent_service = agent_service

    def profile(self, *, team_id: str, user_id: str) -> CompanionProfile:
        self.agent_service.user_service.ensure_user_in_team(team_id=team_id, user_id=user_id)
        settings = self.agent_service.llm_service.settings
        demo = settings.llm_provider.strip().lower() == "mock" and not (
            settings.llm_base_url.strip() and (settings.llm_api_key or "").strip()
        )
        return CompanionProfile(
            state=self.state(team_id=team_id, user_id=user_id),
            capabilities=[
                CompanionCapability(id="chat", label="陪我聊聊"),
                CompanionCapability(id="agent", label="一起做事", detail="网页、知识库与需确认的记忆写入"),
                CompanionCapability(id="memory", label="记住你", detail="按需读取，可逐次关闭"),
                CompanionCapability(id="expressions", label="表情互动"),
                CompanionCapability(id="voice", label="设备语音", detail="是否可用取决于浏览器和设备"),
            ],
            emotions=list(EMOTIONS),
            runtime=CompanionRuntime(provider=settings.llm_provider, model=settings.llm_model, demo_mode=demo),
        )

    def start_run(self, payload: CompanionRunRequest, *, team_id: str, user_id: str) -> AgentRunStartResponse:
        allowed_tools = [] if payload.mode == "chat" else list(_AGENT_TOOLS)
        if not payload.include_memory:
            allowed_tools = [name for name in allowed_tools if name not in _MEMORY_TOOLS]
        request = AgentRunRequest(
            team_id=team_id,
            user_id=user_id,
            conversation_id=payload.conversation_id,
            task=payload.task,
            trigger_channel="companion",
            system_prompt=COMPANION_SYSTEM_PROMPT,
            include_memory=payload.include_memory,
            include_library=payload.mode == "agent",
            include_conclusions=False,
            model=payload.model,
            enabled_tools=allowed_tools,
            confirm_dangerous_actions=False,
            dry_run=False,
            max_steps=6,
        )
        return self.agent_service.start(request)

    def record_event(
        self, payload: CompanionEventRequest, *, team_id: str, user_id: str,
    ) -> CompanionEventResponse:
        self.agent_service.user_service.ensure_user_in_team(team_id=team_id, user_id=user_id)
        _, message = _EVENT_REACTIONS[payload.kind]
        event = ChatHistory(
            message_id=str(uuid4()), team_id=team_id, user_id=user_id,
            conversation_id=None, space_id=None, channel="companion_event",
            request_text=payload.kind, response_text=message,
            request_payload_json=payload.model_dump_json(),
            response_payload_json=json.dumps({"message": message}, ensure_ascii=False),
            created_at=datetime.now(timezone.utc),
        )
        self.db.add(event)
        self.db.commit()
        self.db.refresh(event)
        return CompanionEventResponse(
            event_id=event.message_id, message=message,
            state=self.state(team_id=team_id, user_id=user_id),
        )

    def state(self, *, team_id: str, user_id: str) -> CompanionState:
        run = self.db.scalar(
            select(AgentRun).where(
                AgentRun.team_id == team_id, AgentRun.user_id == user_id,
                AgentRun.trigger_channel == "companion",
            ).order_by(AgentRun.created_at.desc(), AgentRun.run_id.desc()).limit(1)
        )
        event = self.db.scalar(
            select(ChatHistory).where(
                ChatHistory.team_id == team_id, ChatHistory.user_id == user_id,
                ChatHistory.channel == "companion_event",
            ).order_by(ChatHistory.created_at.desc(), ChatHistory.message_id.desc()).limit(1)
        )
        state = CompanionState(last_run_id=run.run_id if run else None)
        if run:
            state.updated_at = _utc(run.completed_at or run.created_at)
            if run.status in {"queued", "running"}:
                state.activity, state.emotion = "thinking", "focused"
            elif run.status == "requires_confirmation":
                state.activity, state.emotion = "waiting", "curious"
            elif datetime.now(timezone.utc) - state.updated_at < _EMOTION_DURATION:
                if run.status in {"failed", "partial_success"}:
                    state.activity, state.emotion = "error", "concerned"
                elif run.status == "completed":
                    state.emotion = "happy"

        # A running task always wins over cosmetic interactions. A newer event
        # overrides a finished run. Rest persists until another event or task.
        if event and state.activity not in {"thinking", "waiting"}:
            event_time = _utc(event.created_at)
            if state.updated_at is None or event_time > state.updated_at:
                state.updated_at = event_time
                if event.request_text == "rest":
                    state.activity, state.emotion = "sleeping", "sleepy"
                else:
                    state.activity, state.emotion = "idle", "idle"
                    if datetime.now(timezone.utc) - event_time < _EMOTION_DURATION:
                        state.emotion = _EVENT_REACTIONS.get(event.request_text, ("idle", ""))[0]
        state.expression = _EXPRESSIONS[state.emotion]
        return state
