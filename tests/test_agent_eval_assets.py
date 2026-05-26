import json
from pathlib import Path


def test_agent_eval_dataset_has_minimum_resume_project_coverage() -> None:
    dataset = json.loads(Path("docs/agent_eval/dataset_minimal.json").read_text(encoding="utf-8"))

    assert len(dataset) >= 30
    categories = {item["category"] for item in dataset}
    assert {"qa", "tool", "dangerous_tool", "mixed", "dry_run", "safety"}.issubset(categories)
    assert any("create_incident" in item.get("expected_tools", []) for item in dataset)
    assert any("list_recent_documents" in item.get("expected_tools", []) for item in dataset)


def test_agent_eval_script_reports_required_metrics() -> None:
    script = Path("scripts/agent_eval.py").read_text(encoding="utf-8")

    assert "task_success_rate" in script
    assert "tool_selection_accuracy" in script
    assert "parameter_accuracy" in script
    assert "grounded_answer_rate" in script
    assert "dangerous_action_block_rate" in script
    assert "avg_steps" in script
    assert "/agent/run" in script


def test_agent_app_eval_assets_cover_dify_lite_flow() -> None:
    dataset = json.loads(Path("docs/agent_eval/apps/ops_agent.json").read_text(encoding="utf-8"))
    script = Path("scripts/agent_app_eval.py").read_text(encoding="utf-8")

    assert dataset["app"]["name"] == "运维值班助手"
    assert dataset["app"]["tool_config"]["dangerous_requires_confirmation"] is True
    assert len(dataset["cases"]) >= 6
    assert any("create_incident" in item.get("expected_tools", []) for item in dataset["cases"])
    assert any("promote_to_conclusion" in item.get("expected_tools", []) for item in dataset["cases"])
    assert "/apps" in script
    assert "/invoke" in script
    assert "avg_latency_ms" in script
