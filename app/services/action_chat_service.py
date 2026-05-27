from app.core.exceptions import DomainValidationError
from app.schemas.chat import ChatActionRequest, ChatActionResponse
from app.services.tool_service import ToolService
from app.services.user_service import UserService


class ActionChatService:
    def __init__(self, user_service: UserService, tool_service: ToolService) -> None:
        self.user_service = user_service
        self.tool_service = tool_service

    def execute(self, payload: ChatActionRequest) -> ChatActionResponse:
        self.user_service.ensure_user_in_team(
            user_id=payload.user_id,
            team_id=payload.team_id,
        )

        # Block dangerous generic/MCP tools from the chat-action fast path.
        # Builtin dangerous tools (create_incident, create_memory_card, …)
        # were part of Phase 1 and are accepted with implicit confirmation.
        # write_file / edit_file / shell_exec / MCP dangerous tools must go
        # through the Agent confirmation loop instead.
        definition = self.tool_service.get_tool_definition(payload.action)
        if definition is None:
            raise DomainValidationError(f"Unsupported action: {payload.action}")
        if definition.dangerous and definition.source != "builtin":
            raise DomainValidationError(
                f"'{payload.action}' is dangerous and cannot be executed "
                "via /chat/action. Use the Agent run/confirm flow instead."
            )

        confirmed = True  # builtin tools: implicit confirm (pre-vetted Phase 1 set)

        result = self.tool_service.execute(
            team_id=payload.team_id,
            user_id=payload.user_id,
            action=payload.action,
            arguments=payload.arguments,
            confirmed=confirmed,
        )

        return ChatActionResponse.from_result(
            user_id=payload.user_id,
            team_id=payload.team_id,
            conversation_id=payload.conversation_id,
            action=payload.action,
            result=result,
        )
