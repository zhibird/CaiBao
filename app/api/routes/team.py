from fastapi import APIRouter, Depends, HTTPException, Path, Response, status

from app.api.deps import get_team_service, require_dev_admin
from app.core.exceptions import EntityConflictError
from app.schemas.team import TeamCreate, TeamEnsure, TeamResponse
from app.services.team_service import TeamService

router = APIRouter(prefix="/teams", dependencies=[Depends(require_dev_admin)])


@router.post("", response_model=TeamResponse, status_code=status.HTTP_201_CREATED)
def create_team(
    payload: TeamCreate,
    team_service: TeamService = Depends(get_team_service),
) -> TeamResponse:
    try:
        team = team_service.create_team(payload)
    except EntityConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return TeamResponse.model_validate(team)


@router.put("/{team_id}", response_model=TeamResponse)
def ensure_team(
    payload: TeamEnsure,
    response: Response,
    team_id: str = Path(min_length=1, max_length=64),
    team_service: TeamService = Depends(get_team_service),
) -> TeamResponse:
    team, created = team_service.ensure_team(team_id=team_id, payload=payload)
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return TeamResponse.model_validate(team)


@router.get("/{team_id}", response_model=TeamResponse)
def get_team(
    team_id: str = Path(min_length=1, max_length=64),
    team_service: TeamService = Depends(get_team_service),
) -> TeamResponse:
    team = team_service.get_by_id(team_id)
    if team is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Team '{team_id}' not found.",
        )

    return TeamResponse.model_validate(team)


@router.get("", response_model=list[TeamResponse])
def list_teams(team_service: TeamService = Depends(get_team_service)) -> list[TeamResponse]:
    teams = team_service.list_teams()
    return [TeamResponse.model_validate(team) for team in teams]
