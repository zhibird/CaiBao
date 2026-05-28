from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import require_current_active_user
from app.core.exceptions import DomainValidationError
from app.models.user import User
from app.services.drift_service import DriftService
from app.services.drift_state_store import DriftStateStore
from app.services.llm_service import LLMService
from app.api.deps import get_llm_service

from app.core.config import get_settings

router = APIRouter(prefix="/drift")


def _check_drift_enabled():
    if not get_settings().drift_enabled:
        raise HTTPException(status_code=403, detail="Drift features are disabled. Set DRIFT_ENABLED=true.")


@router.get("/skills")
def list_skills(
    current_user: User = Depends(require_current_active_user),
):
    _check_drift_enabled()
    store = DriftStateStore()
    return store.list_skills(current_user.team_id, current_user.user_id)


@router.post("/skills")
def register_skill(
    body: dict,
    current_user: User = Depends(require_current_active_user),
):
    skill_name = str(body.get("skill_name", "")).strip()
    skill_body = str(body.get("body", "")).strip()
    if not skill_name or not skill_body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="skill_name and body required")
    # Validate skill_name is safe before path construction
    import re
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$", skill_name):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid skill_name format")
    store = DriftStateStore()
    skill_dir = store._skills_dir(current_user.team_id, current_user.user_id) / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_body, "utf-8")
    return {"skill_name": skill_name, "status": "registered"}


@router.get("/runs")
def list_runs(
    current_user: User = Depends(require_current_active_user),
):
    _check_drift_enabled()
    store = DriftStateStore()
    skills = store.list_skills(current_user.team_id, current_user.user_id)
    return [{"skill_name": s["skill_name"], "state": s.get("state", {})} for s in skills]


@router.post("/run")
def run_drift(
    body: dict,
    current_user: User = Depends(require_current_active_user),
    llm_service: LLMService = Depends(get_llm_service),
):
    _check_drift_enabled()
    try:
        skill_name = str(body.get("skill_name", "")).strip()
        if not skill_name:
            raise DomainValidationError("skill_name is required")
        store = DriftStateStore()
        svc = DriftService(store=store, llm_service=llm_service)
        result = svc.run_drift(
            skill_name=skill_name,
            team_id=current_user.team_id,
            user_id=current_user.user_id,
            space_id=body.get("space_id"),
        )
        return result
    except (DomainValidationError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
