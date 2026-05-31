from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response, status

from app.api.deps import get_user_service, require_dev_admin
from app.core.exceptions import EntityConflictError, EntityNotFoundError
from app.schemas.user import UserCreate, UserEnsure, UserResponse
from app.services.user_service import UserService

router = APIRouter(prefix="/users", dependencies=[Depends(require_dev_admin)])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    user_service: UserService = Depends(get_user_service),
) -> UserResponse:
    try:
        user = user_service.create_user(payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EntityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return UserResponse.model_validate(user)


@router.put("/{user_id}", response_model=UserResponse)
def ensure_user(
    payload: UserEnsure,
    response: Response,
    user_id: str = Path(min_length=1, max_length=64),
    user_service: UserService = Depends(get_user_service),
) -> UserResponse:
    try:
        user, created = user_service.ensure_user(user_id=user_id, payload=payload)
    except EntityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except EntityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return UserResponse.model_validate(user)


@router.get("/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str = Path(min_length=1, max_length=64),
    user_service: UserService = Depends(get_user_service),
) -> UserResponse:
    user = user_service.get_by_id(user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"User '{user_id}' not found.",
        )

    return UserResponse.model_validate(user)


@router.get("", response_model=list[UserResponse])
def list_users(
    team_id: str | None = Query(default=None),
    user_service: UserService = Depends(get_user_service),
) -> list[UserResponse]:
    users = user_service.list_users(team_id=team_id)
    return [UserResponse.model_validate(user) for user in users]
