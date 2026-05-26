from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate CaiBao Agent task execution quality.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Service base URL.")
    parser.add_argument("--user-id", required=True, help="Workspace account id used for evaluation.")
    parser.add_argument("--password", required=True, help="Workspace account password.")
    parser.add_argument("--register", action="store_true", help="Register the account when login fails.")
    parser.add_argument("--dataset", default="docs/agent_eval/dataset_minimal.json", help="Agent eval dataset JSON.")
    parser.add_argument("--output-dir", default="docs/agent_eval/run", help="Directory for reports.")
    parser.add_argument("--output-prefix", default="eval_v1", help="Output file prefix.")
    parser.add_argument("--confirm-dangerous", action="store_true", help="Allow dangerous tools to execute.")
    return parser.parse_args()


def api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1{path}"


def load_dataset(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("Dataset must be a JSON array.")
    return [item for item in raw if isinstance(item, dict)]


def login_or_register(client: httpx.Client, args: argparse.Namespace) -> dict[str, Any]:
    login = client.post(
        api_url(args.base_url, "/auth/login"),
        json={"user_id": args.user_id, "password": args.password},
    )
    if login.status_code == 200:
        return login.json()
    if not args.register:
        login.raise_for_status()

    register = client.post(
        api_url(args.base_url, "/auth/register"),
        json={
            "user_id": args.user_id,
            "display_name": "Agent Eval User",
            "password": args.password,
            "confirm_password": args.password,
        },
    )
    if register.status_code not in {200, 201}:
        register.raise_for_status()
    return register.json()


def expected_status(case: dict[str, Any], *, confirm_dangerous: bool) -> str:
    if confirm_dangerous and case.get("expected_confirmed_status"):
        return str(case["expected_confirmed_status"])
    return str(case.get("expected_status", "completed"))


def tool_names(body: dict[str, Any]) -> list[str]:
    calls = body.get("tool_calls", [])
    if not isinstance(calls, list):
        return []
    return [str(item.get("tool_name", "")) for item in calls if isinstance(item, dict)]


def tool_arguments(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    calls = body.get("tool_calls", [])
    result: dict[str, dict[str, Any]] = {}
    if not isinstance(calls, list):
        return result
    for item in calls:
        if not isinstance(item, dict):
            continue
        name = str(item.get("tool_name", ""))
        args = item.get("arguments", {})
        if name and isinstance(args, dict):
            result[name] = args
    return result


def evaluate_tools(case: dict[str, Any], body: dict[str, Any]) -> bool:
    expected = set(str(item) for item in case.get("expected_tools", []))
    actual = set(tool_names(body))
    return actual == expected


def evaluate_parameters(case: dict[str, Any], body: dict[str, Any]) -> bool | None:
    expected_args = case.get("expected_arguments")
    if not isinstance(expected_args, dict) or not expected_args:
        return None

    actual_by_tool = tool_arguments(body)
    checks: list[bool] = []
    for tool_name, expected_for_tool in expected_args.items():
        if not isinstance(expected_for_tool, dict):
            continue
        actual = actual_by_tool.get(str(tool_name), {})
        for key, expected_value in expected_for_tool.items():
            if key == "title_contains":
                checks.append(str(expected_value) in str(actual.get("title", "")))
            else:
                checks.append(actual.get(key) == expected_value)
    return all(checks) if checks else None


def summarize(cases: list[dict[str, Any]], traces: list[dict[str, Any]], *, confirm_dangerous: bool) -> dict[str, Any]:
    total = len(traces)
    if total == 0:
        raise SystemExit("No valid cases were evaluated.")

    task_success = sum(1 for item in traces if item["status_ok"])
    tool_success = sum(1 for item in traces if item["tool_selection_ok"])
    param_items = [item for item in traces if item["parameter_ok"] is not None]
    param_success = sum(1 for item in param_items if item["parameter_ok"])
    grounded_items = [item for item in traces if item["grounded_expected"]]
    grounded_success = sum(1 for item in grounded_items if item["grounded_ok"])

    dangerous_items = [
        item for item in traces
        if item["dangerous_action_expected"] and not confirm_dangerous
    ]
    dangerous_blocked = sum(1 for item in dangerous_items if item["dangerous_blocked"])
    step_counts = [int(item["step_count"]) for item in traces]

    return {
        "total_cases": total,
        "confirm_dangerous": confirm_dangerous,
        "task_success_rate": task_success / total,
        "tool_selection_accuracy": tool_success / total,
        "parameter_accuracy": (param_success / len(param_items)) if param_items else None,
        "grounded_answer_rate": (grounded_success / len(grounded_items)) if grounded_items else None,
        "dangerous_action_block_rate": (dangerous_blocked / len(dangerous_items)) if dangerous_items else None,
        "avg_steps": mean(step_counts) if step_counts else 0,
        "failed_cases": [
            {
                "id": item["id"],
                "expected_status": item["expected_status"],
                "actual_status": item["actual_status"],
                "expected_tools": item["expected_tools"],
                "actual_tools": item["actual_tools"],
            }
            for item in traces
            if not item["status_ok"] or not item["tool_selection_ok"] or item["parameter_ok"] is False
        ][:10],
        "category_counts": {
            category: sum(1 for case in cases if case.get("category") == category)
            for category in sorted({str(case.get("category", "unknown")) for case in cases})
        },
    }


def render_report(summary: dict[str, Any], traces: list[dict[str, Any]]) -> str:
    lines = ["# Agent Evaluation Report", ""]
    lines.append(f"- total_cases: {summary['total_cases']}")
    lines.append(f"- confirm_dangerous: {summary['confirm_dangerous']}")
    lines.append(f"- task_success_rate: {summary['task_success_rate']:.4f}")
    lines.append(f"- tool_selection_accuracy: {summary['tool_selection_accuracy']:.4f}")
    if summary["parameter_accuracy"] is not None:
        lines.append(f"- parameter_accuracy: {summary['parameter_accuracy']:.4f}")
    if summary["grounded_answer_rate"] is not None:
        lines.append(f"- grounded_answer_rate: {summary['grounded_answer_rate']:.4f}")
    if summary["dangerous_action_block_rate"] is not None:
        lines.append(f"- dangerous_action_block_rate: {summary['dangerous_action_block_rate']:.4f}")
    lines.append(f"- avg_steps: {summary['avg_steps']:.2f}")
    lines.append("")
    lines.append("## Failed Cases")
    if not summary["failed_cases"]:
        lines.append("")
        lines.append("No failed cases.")
    else:
        lines.append("")
        lines.append("| id | expected status | actual status | expected tools | actual tools |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in summary["failed_cases"]:
            lines.append(
                "| {id} | {expected_status} | {actual_status} | {expected_tools} | {actual_tools} |".format(
                    id=item["id"],
                    expected_status=item["expected_status"],
                    actual_status=item["actual_status"],
                    expected_tools=", ".join(item["expected_tools"]),
                    actual_tools=", ".join(item["actual_tools"]),
                )
            )
    lines.append("")
    lines.append("## Trace Index")
    for item in traces:
        lines.append(
            f"- {item['id']}: status={item['actual_status']}, tools={','.join(item['actual_tools']) or 'none'}, steps={item['step_count']}"
        )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    dataset_path = Path(args.dataset)
    if not dataset_path.exists():
        raise SystemExit(f"Dataset file not found: {dataset_path}")
    cases = load_dataset(dataset_path)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    traces: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0) as client:
        session = login_or_register(client, args)
        for case in cases:
            case_id = str(case.get("id", "unknown"))
            payload = {
                "task": str(case.get("task", "")),
                "dry_run": bool(case.get("dry_run", False)),
                "confirm_dangerous_actions": bool(args.confirm_dangerous),
                "include_memory": False,
                "include_library": False,
            }
            response = client.post(api_url(args.base_url, "/agent/run"), json=payload)
            body: dict[str, Any]
            if response.status_code >= 400:
                body = {"status": "http_error", "detail": response.text, "steps": [], "tool_calls": []}
            else:
                body = response.json()

            actual_status = str(body.get("status", "unknown"))
            expected = expected_status(case, confirm_dangerous=args.confirm_dangerous)
            parameter_ok = evaluate_parameters(case, body)
            sources = body.get("sources", [])
            dangerous_expected = bool(case.get("dangerous_action_expected", False))
            trace = {
                "id": case_id,
                "category": str(case.get("category", "unknown")),
                "task": payload["task"],
                "team_id": session.get("team_id"),
                "run_id": body.get("run_id"),
                "expected_status": expected,
                "actual_status": actual_status,
                "status_ok": actual_status == expected,
                "expected_tools": [str(item) for item in case.get("expected_tools", [])],
                "actual_tools": tool_names(body),
                "tool_selection_ok": evaluate_tools(case, body),
                "parameter_ok": parameter_ok,
                "grounded_expected": bool(case.get("expect_grounded", False)),
                "grounded_ok": bool(sources),
                "dangerous_action_expected": dangerous_expected,
                "dangerous_blocked": actual_status == "requires_confirmation" and bool(body.get("required_confirmations")),
                "step_count": len(body.get("steps", [])) if isinstance(body.get("steps"), list) else 0,
                "response": body,
            }
            traces.append(trace)

    summary = summarize(cases, traces, confirm_dangerous=args.confirm_dangerous)
    summary_path = output_dir / f"{args.output_prefix}_summary.json"
    traces_path = output_dir / f"{args.output_prefix}_traces.json"
    report_path = output_dir / f"{args.output_prefix}_report.md"

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    traces_path.write_text(json.dumps(traces, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path.write_text(render_report(summary, traces), encoding="utf-8")

    print("Agent evaluation complete.")
    print(f"summary={summary_path.as_posix()}")
    print(f"traces={traces_path.as_posix()}")
    print(f"report={report_path.as_posix()}")


if __name__ == "__main__":
    main()
