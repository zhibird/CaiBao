from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_embedding_model_service, require_current_active_user
from app.core.config import get_settings
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.embedding_model import (
    EmbeddingDefaultModelItem,
    EmbeddingModelConfigItem,
    EmbeddingModelConfigListResponse,
    EmbeddingModelConfigUpsertRequest,
)
from app.services.embedding_model_service import EmbeddingModelService

router = APIRouter(prefix="/embedding/models")


@router.get("", response_model=EmbeddingModelConfigListResponse)
def list_embedding_models(
    current_user: User = Depends(require_current_active_user),
    embedding_model_service: EmbeddingModelService = Depends(get_embedding_model_service),
) -> EmbeddingModelConfigListResponse:
    try:
        items = embedding_model_service.list_configs(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return EmbeddingModelConfigListResponse(
        team_id=current_user.team_id,
        user_id=current_user.user_id,
        default_model=_build_default_embedding_item(embedding_model_service),
        items=[
            EmbeddingModelConfigItem(
                config_id=item.config_id,
                team_id=item.team_id,
                user_id=item.user_id,
                model_name=item.model_name,
                provider=item.provider,
                base_url=item.base_url,
                has_api_key=bool((item.api_key or "").strip()),
                masked_api_key=embedding_model_service.mask_api_key(item.api_key),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in items
        ],
    )


def _build_default_embedding_item(embedding_model_service: EmbeddingModelService) -> EmbeddingDefaultModelItem:
    settings = get_settings()
    provider = settings.embedding_provider.strip().lower()
    base_url = settings.embedding_base_url.strip() or None
    api_key = settings.embedding_api_key or ""
    if provider == "mock" and base_url and api_key.strip():
        provider = "openai"
    model_name = "hashing_v1" if provider == "mock" else settings.embedding_model
    return EmbeddingDefaultModelItem(
        model_name=model_name,
        provider=provider,
        base_url=None if provider == "mock" else base_url,
        has_api_key=bool(api_key.strip()),
        masked_api_key=embedding_model_service.mask_api_key(api_key),
    )


@router.post("", response_model=EmbeddingModelConfigItem)
def upsert_embedding_model(
    payload: EmbeddingModelConfigUpsertRequest,
    current_user: User = Depends(require_current_active_user),
    embedding_model_service: EmbeddingModelService = Depends(get_embedding_model_service),
) -> EmbeddingModelConfigItem:
    try:
        item = embedding_model_service.upsert_config(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            model_name=payload.model_name,
            provider=payload.provider,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return EmbeddingModelConfigItem(
        config_id=item.config_id,
        team_id=item.team_id,
        user_id=item.user_id,
        model_name=item.model_name,
        provider=item.provider,
        base_url=item.base_url,
        has_api_key=bool((item.api_key or "").strip()),
        masked_api_key=embedding_model_service.mask_api_key(item.api_key),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.delete("/{model_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_embedding_model(
    model_name: str,
    current_user: User = Depends(require_current_active_user),
    embedding_model_service: EmbeddingModelService = Depends(get_embedding_model_service),
) -> None:
    try:
        embedding_model_service.delete_config(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            model_name=model_name,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
