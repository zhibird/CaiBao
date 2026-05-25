import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    get_action_chat_service,
    get_chat_history_service,
    get_chat_service,
    get_rag_chat_service,
    require_current_active_user,
)
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.chat import (
    ChatActionRequest,
    ChatActionResponse,
    ChatAskRequest,
    ChatAskResponse,
    ChatEchoRequest,
    ChatEchoResponse,
    ChatHistoryEditRequest,
    ChatHistoryItem,
    ChatHistoryListResponse,
)
from app.services.action_chat_service import ActionChatService
from app.services.chat_history_service import ChatHistoryService
from app.services.chat_service import ChatService
from app.services.rag_chat_service import RagChatService

router = APIRouter(prefix="/chat")


@router.post("/echo", response_model=ChatEchoResponse)
def chat_echo(
    payload: ChatEchoRequest,
    current_user: User = Depends(require_current_active_user),
    chat_service: ChatService = Depends(get_chat_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatEchoResponse:
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        response = chat_service.echo(effective_payload)
        chat_history_service.record_message(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=effective_payload.conversation_id,
            channel="echo",
            request_text=effective_payload.message,
            response_text=response.answer,
            request_payload=effective_payload.model_dump(),
            response_payload=response.model_dump(),
        )
        return response
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/ask", response_model=ChatAskResponse)
def chat_ask(
    payload: ChatAskRequest,
    current_user: User = Depends(require_current_active_user),
    rag_chat_service: RagChatService = Depends(get_rag_chat_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatAskResponse:
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        response = rag_chat_service.ask(effective_payload)
        chat_history_service.record_message(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=effective_payload.conversation_id,
            channel="ask",
            request_text=effective_payload.question,
            response_text=response.answer,
            request_payload=effective_payload.model_dump(),
            response_payload=response.model_dump(),
        )
        return response
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/action", response_model=ChatActionResponse)
def chat_action(
    payload: ChatActionRequest,
    current_user: User = Depends(require_current_active_user),
    action_chat_service: ActionChatService = Depends(get_action_chat_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatActionResponse:
    try:
        effective_payload = payload.model_copy(
            update={"team_id": current_user.team_id, "user_id": current_user.user_id}
        )
        response = action_chat_service.execute(effective_payload)

        response_text = str(response.result.get("message", "")).strip()
        if not response_text:
            response_text = str(response.result)

        chat_history_service.record_message(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=effective_payload.conversation_id,
            channel="action",
            request_text=effective_payload.action,
            response_text=response_text,
            request_payload=effective_payload.model_dump(),
            response_payload=response.model_dump(),
        )
        return response
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.delete("/history/{message_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chat_history_message(
    message_id: str,
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    current_user: User = Depends(require_current_active_user),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> None:
    try:
        chat_history_service.delete_message(
            message_id=message_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=conversation_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.put("/history/{message_id}", response_model=ChatHistoryItem)
def edit_chat_history_message(
    message_id: str,
    payload: ChatHistoryEditRequest,
    current_user: User = Depends(require_current_active_user),
    chat_service: ChatService = Depends(get_chat_service),
    rag_chat_service: RagChatService = Depends(get_rag_chat_service),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatHistoryItem:
    try:
        message = chat_history_service.ensure_latest_message(
            message_id=message_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )

        channel = message.channel.strip().lower()
        if channel in {"action", "agent"}:
            raise DomainValidationError("Action and agent messages do not support editing.")

        if channel == "echo":
            echo_payload = ChatEchoRequest(
                user_id=current_user.user_id,
                team_id=current_user.team_id,
                conversation_id=message.conversation_id,
                message=payload.request_text,
            )
            echo_response = chat_service.echo(echo_payload)

            request_text = echo_payload.message
            response_text = echo_response.answer
            request_payload = echo_payload.model_dump()
            response_payload = echo_response.model_dump()
        else:
            previous_payload = _safe_json_to_dict(message.request_payload_json)
            top_k = _resolve_top_k(payload.top_k, previous_payload)
            document_id = _resolve_optional_string(payload.document_id, previous_payload.get("document_id"))
            selected_document_ids = _resolve_selected_document_ids(
                payload.selected_document_ids,
                previous_payload,
            )
            use_document_scope = _resolve_use_document_scope(
                payload.use_document_scope,
                previous_payload,
            )
            include_memory = _resolve_bool(
                payload.include_memory,
                previous_payload,
                "include_memory",
                default=True,
            )
            include_conclusions = _resolve_bool(
                payload.include_conclusions,
                previous_payload,
                "include_conclusions",
                default=False,
            )
            include_library = _resolve_bool(
                payload.include_library,
                previous_payload,
                "include_library",
                default=True,
            )
            model = _resolve_optional_string(payload.model, previous_payload.get("model"))
            embedding_model = _resolve_optional_string(
                payload.embedding_model,
                previous_payload.get("embedding_model"),
            )

            ask_payload = ChatAskRequest(
                user_id=current_user.user_id,
                team_id=current_user.team_id,
                conversation_id=message.conversation_id,
                question=payload.request_text,
                top_k=top_k,
                document_id=document_id,
                selected_document_ids=selected_document_ids,
                use_document_scope=use_document_scope,
                include_memory=include_memory,
                include_conclusions=include_conclusions,
                include_library=include_library,
                model=model,
                embedding_model=embedding_model,
            )
            ask_response = rag_chat_service.ask(
                ask_payload,
                before_message_id=message.message_id,
            )

            request_text = ask_payload.question
            response_text = ask_response.answer
            request_payload = ask_payload.model_dump()
            response_payload = ask_response.model_dump()

        updated = chat_history_service.update_message(
            message_id=message_id,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            request_text=request_text,
            response_text=response_text,
            request_payload=request_payload,
            response_payload=response_payload,
        )
        return ChatHistoryItem.from_record(updated)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get("/history", response_model=ChatHistoryListResponse)
def chat_history(
    conversation_id: str | None = Query(default=None, min_length=1, max_length=36),
    limit: int = Query(default=20, ge=1, le=200),
    current_user: User = Depends(require_current_active_user),
    chat_history_service: ChatHistoryService = Depends(get_chat_history_service),
) -> ChatHistoryListResponse:
    try:
        records = chat_history_service.list_history(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=conversation_id,
            limit=limit,
        )
        items = [ChatHistoryItem.from_record(item) for item in records]
        return ChatHistoryListResponse.from_result(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            conversation_id=conversation_id,
            limit=limit,
            items=items,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


def _safe_json_to_dict(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {}


def _resolve_optional_string(current: str | None, previous: object) -> str | None:
    if current is not None:
        return current
    if isinstance(previous, str) and previous.strip():
        return previous
    return None


def _resolve_top_k(current: int | None, previous_payload: dict[str, Any]) -> int:
    if current is not None:
        return current
    previous = previous_payload.get("top_k")
    if isinstance(previous, int) and 1 <= previous <= 20:
        return previous
    return 5


def _resolve_selected_document_ids(
    current: list[str] | None,
    previous_payload: dict[str, Any],
) -> list[str] | None:
    values: list[str] = []
    if current is not None:
        values.extend(current)
    else:
        previous = previous_payload.get("selected_document_ids")
        if isinstance(previous, list):
            values.extend(str(item) for item in previous)
        else:
            previous_document_id = previous_payload.get("document_id")
            if isinstance(previous_document_id, str) and previous_document_id.strip():
                values.append(previous_document_id)

    deduped: list[str] = []
    for item in values:
        normalized = str(item).strip()
        if normalized and normalized not in deduped:
            deduped.append(normalized)
    return deduped or None


def _resolve_use_document_scope(
    current: bool | None,
    previous_payload: dict[str, Any],
) -> bool:
    if isinstance(current, bool):
        return current

    previous = previous_payload.get("use_document_scope")
    if isinstance(previous, bool):
        return previous

    previous_selected = previous_payload.get("selected_document_ids")
    if isinstance(previous_selected, list) and any(str(item).strip() for item in previous_selected):
        return True

    previous_document_id = previous_payload.get("document_id")
    if isinstance(previous_document_id, str) and previous_document_id.strip():
        return True

    if previous_payload.get("include_library") is True:
        return True
    if previous_payload.get("include_conclusions") is True:
        return True

    return False


def _resolve_bool(
    current: bool | None,
    previous_payload: dict[str, Any],
    key: str,
    *,
    default: bool,
) -> bool:
    if isinstance(current, bool):
        return current

    previous = previous_payload.get(key)
    if isinstance(previous, bool):
        return previous

    return default
