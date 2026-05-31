from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_llm_model_service, require_current_active_user
from app.core.config import get_settings
from app.core.exceptions import DomainValidationError, EntityNotFoundError
from app.models.user import User
from app.schemas.llm_model import (
    LLMDefaultModelItem,
    LLMModelConfigItem,
    LLMModelConfigListResponse,
    LLMModelConfigUpsertRequest,
)
from app.services.llm_model_service import LLMModelService

router = APIRouter(prefix="/llm/models")


@router.get("", response_model=LLMModelConfigListResponse)
def list_llm_models(
    current_user: User = Depends(require_current_active_user),
    llm_model_service: LLMModelService = Depends(get_llm_model_service),
) -> LLMModelConfigListResponse:
    try:
        items = llm_model_service.list_configs(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return LLMModelConfigListResponse(
        team_id=current_user.team_id,
        user_id=current_user.user_id,
        default_model=_build_default_model_item(llm_model_service),
        items=[
            LLMModelConfigItem(
                config_id=item.config_id,
                team_id=item.team_id,
                user_id=item.user_id,
                model_name=item.model_name,
                base_url=item.base_url,
                has_api_key=bool(item.api_key.strip()),
                masked_api_key=llm_model_service.mask_api_key(item.api_key),
                created_at=item.created_at,
                updated_at=item.updated_at,
            )
            for item in items
        ],
    )


def _build_default_model_item(llm_model_service: LLMModelService) -> LLMDefaultModelItem:
    settings = get_settings()
    api_key = settings.llm_api_key or ""
    return LLMDefaultModelItem(
        model_name=settings.llm_model,
        provider=settings.llm_provider,
        base_url=settings.llm_base_url,
        has_api_key=bool(api_key.strip()),
        masked_api_key=llm_model_service.mask_api_key(api_key),
    )


@router.post("", response_model=LLMModelConfigItem)
def upsert_llm_model(
    payload: LLMModelConfigUpsertRequest,
    current_user: User = Depends(require_current_active_user),
    llm_model_service: LLMModelService = Depends(get_llm_model_service),
) -> LLMModelConfigItem:
    try:
        item = llm_model_service.upsert_config(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            model_name=payload.model_name,
            base_url=payload.base_url,
            api_key=payload.api_key,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return LLMModelConfigItem(
        config_id=item.config_id,
        team_id=item.team_id,
        user_id=item.user_id,
        model_name=item.model_name,
        base_url=item.base_url,
        has_api_key=bool(item.api_key.strip()),
        masked_api_key=llm_model_service.mask_api_key(item.api_key),
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.delete("/{model_name}", status_code=status.HTTP_204_NO_CONTENT)
def delete_llm_model(
    model_name: str,
    current_user: User = Depends(require_current_active_user),
    llm_model_service: LLMModelService = Depends(get_llm_model_service),
) -> None:
    try:
        llm_model_service.delete_config(
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            model_name=model_name,
        )
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DomainValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
