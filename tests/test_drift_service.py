from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest

from app.services.drift_state_store import DriftStateStore
from app.services.drift_tools import (
    drift_finish_drift,
    drift_list_work_files,
    drift_message_push,
    drift_read_work_file,
    drift_write_work_file,
    get_drift_state,
    reset_drift_state,
)


@pytest.fixture
def tmp_store(tmp_path):
    return DriftStateStore(root_dir=str(tmp_path))


@pytest.fixture
def skill_setup(tmp_store):
    team_id = "team-1"
    user_id = "user-1"
    skill_name = "test-skill"
    skill_dir = tmp_store._skills_dir(team_id, user_id) / skill_name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("""---
skill_name: test-skill
description: A test drift skill
max_steps: 4
allowed_tools: ["read_work_file", "write_work_file", "list_work_files", "finish_drift"]
---

# Test Skill

Do some test operations.
""")
    return tmp_store, team_id, user_id, skill_name


class TestDriftStateStore:
    def test_list_skills_empty(self, tmp_store):
        skills = tmp_store.list_skills("team-1", "user-1")
        assert skills == []

    def test_get_skill_with_frontmatter(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        skill = store.get_skill(team_id, user_id, skill_name)
        assert skill is not None
        assert skill["skill_name"] == skill_name
        assert skill["description"] == "A test drift skill"
        assert skill["max_steps"] == 4

    def test_update_and_read_state(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        store.update_state(team_id, user_id, skill_name, {"last_run": "2026-01-01", "status": "ok"})
        skill = store.get_skill(team_id, user_id, skill_name)
        assert skill["state"]["status"] == "ok"

    def test_work_file_read_write(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        store.write_work_file(team_id, user_id, skill_name, "notes.md", "hello")
        assert store.read_work_file(team_id, user_id, skill_name, "notes.md") == "hello"

    def test_list_work_files(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        store.write_work_file(team_id, user_id, skill_name, "a.md", "a")
        store.write_work_file(team_id, user_id, skill_name, "b.md", "b")
        files = store.list_work_files(team_id, user_id, skill_name)
        assert "a.md" in files
        assert "b.md" in files


class TestDriftTools:
    def test_write_and_read_work_file(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        result = drift_write_work_file(
            team_id=team_id, user_id=user_id,
            arguments={"filename": "out.txt", "content": "test content"},
            store=store, skill_name=skill_name,
        )
        assert result["written_chars"] == 12
        result2 = drift_read_work_file(
            team_id=team_id, user_id=user_id,
            arguments={"filename": "out.txt"},
            store=store, skill_name=skill_name,
        )
        assert result2["content"] == "test content"

    def test_message_push_only_once(self):
        reset_drift_state()
        drift_message_push(team_id="t1", user_id="u1", arguments={"message": "first"})
        with pytest.raises(RuntimeError, match="already called"):
            drift_message_push(team_id="t1", user_id="u1", arguments={"message": "second"})

    def test_finish_drift_only_once(self):
        reset_drift_state()
        drift_finish_drift(team_id="t1", user_id="u1", arguments={"decision": "complete"})
        with pytest.raises(RuntimeError, match="already called"):
            drift_finish_drift(team_id="t1", user_id="u1", arguments={"decision": "complete"})

    def test_list_work_files_after_writes(self, skill_setup):
        store, team_id, user_id, skill_name = skill_setup
        drift_write_work_file(team_id=team_id, user_id=user_id,
            arguments={"filename": "notes.md", "content": "x"}, store=store, skill_name=skill_name)
        result = drift_list_work_files(team_id=team_id, user_id=user_id,
            arguments={}, store=store, skill_name=skill_name)
        assert "notes.md" in result["files"]

    def test_finish_drift_records_state(self):
        reset_drift_state()
        result = drift_finish_drift(team_id="t1", user_id="u1",
            arguments={"decision": "reply", "message_result": "pushed"})
        assert result["decision"] == "reply"
        assert get_drift_state()["message_result"] == "pushed"
