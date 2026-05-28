from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from app.core.config import get_settings

_logger = logging.getLogger(__name__)

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


class DriftStateStore:
    """Scans data/drift/ for SKILL.md files and manages per-skill state."""

    def __init__(self, root_dir: str | None = None) -> None:
        self.root = Path(root_dir or get_settings().drift_root_dir)

    def list_skills(self, team_id: str, user_id: str) -> list[dict[str, Any]]:
        skills_dir = self._skills_dir(team_id, user_id)
        if not skills_dir.exists():
            return []
        result = []
        for item in sorted(skills_dir.iterdir()):
            if item.is_dir():
                skill_file = item / "SKILL.md"
                if skill_file.exists():
                    meta = self._parse_frontmatter(skill_file.read_text("utf-8"))
                    state = self._read_state(item)
                    meta["skill_name"] = item.name
                    meta["state"] = state
                    result.append(meta)
        return result

    def get_skill(self, team_id: str, user_id: str, skill_name: str) -> dict[str, Any] | None:
        self._validate_name(skill_name, "skill_name")
        skills_dir = self._skills_dir(team_id, user_id)
        skill_dir = skills_dir / skill_name
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            return None
        meta = self._parse_frontmatter(skill_file.read_text("utf-8"))
        state = self._read_state(skill_dir)
        meta["skill_name"] = skill_name
        meta["state"] = state
        return meta

    def update_state(self, team_id: str, user_id: str, skill_name: str, state: dict[str, Any]) -> None:
        self._validate_name(skill_name, "skill_name")
        skill_dir = self._skills_dir(team_id, user_id) / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")

    _SAFE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")

    def _validate_name(self, name: str, label: str) -> None:
        if not self._SAFE_NAME_RE.match(name):
            raise ValueError(f"Unsafe {label}: {name!r}")

    def read_work_file(self, team_id: str, user_id: str, skill_name: str, filename: str) -> str:
        self._validate_name(skill_name, "skill_name")
        self._validate_name(filename, "filename")
        f = self._skills_dir(team_id, user_id) / skill_name / "work" / filename
        if not f.exists():
            return ""
        return f.read_text("utf-8")

    def write_work_file(self, team_id: str, user_id: str, skill_name: str, filename: str, content: str) -> None:
        self._validate_name(skill_name, "skill_name")
        self._validate_name(filename, "filename")
        d = self._skills_dir(team_id, user_id) / skill_name / "work"
        d.mkdir(parents=True, exist_ok=True)
        (d / filename).write_text(content, "utf-8")

    def list_work_files(self, team_id: str, user_id: str, skill_name: str) -> list[str]:
        self._validate_name(skill_name, "skill_name")
        d = self._skills_dir(team_id, user_id) / skill_name / "work"
        if not d.exists():
            return []
        return sorted([f.name for f in d.iterdir() if f.is_file()])

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _skills_dir(self, team_id: str, user_id: str) -> Path:
        import hashlib
        t = hashlib.sha256(team_id.encode()).hexdigest()[:16]
        u = hashlib.sha256(user_id.encode()).hexdigest()[:16]
        return self.root / t / u / "skills"

    @staticmethod
    def _parse_frontmatter(text: str) -> dict[str, Any]:
        m = _FRONTMATTER_RE.match(text)
        if not m:
            return {}
        result: dict[str, Any] = {}
        for line in m.group(1).strip().split("\n"):
            if ":" in line:
                key, _, val = line.partition(":")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # Parse lists like [a, b, c]
                if val.startswith("[") and val.endswith("]"):
                    val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",")]
                # Parse ints
                elif val.isdigit():
                    val = int(val)
                result[key] = val
        return result

    @staticmethod
    def _read_state(skill_dir: Path) -> dict[str, Any]:
        sf = skill_dir / "state.json"
        if not sf.exists():
            return {}
        try:
            return json.loads(sf.read_text("utf-8"))
        except (json.JSONDecodeError, TypeError):
            return {}
