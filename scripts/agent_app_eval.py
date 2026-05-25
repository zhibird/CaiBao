from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from typing import Any

import httpx


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate CaiBao Dify-lite Agent App execution quality.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="Service base URL.")
    parser.add_argument("--user-id", required=True, help="Workspace account id used for evaluation.")
    parser.add_argument("--password", required=True, help="Workspace account password.")
    parser.add_argument("--register", action="store_true", help="Register the account when login fails.")
    parser.add_argument("--dataset", default="docs/agent_eval/apps/ops_agent.json", help="Agent App eval JSON.")
    parser.add_argument("--output-dir", default="docs/agent_eval/run", help="Directory for reports.")
    parser.add_argument("--output-prefix", default="app_eval_v1", help="Output file prefix.")
    parser.add_argument("--confirm-dangerous", action="store_true", help="Allow dangerous tools to execute.")
    return parser.parse_args()


def api_url(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1{path}"


def load_dataset(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SystemExit("Agent App eval file must be a JSON object.")
    app = raw.get("app")
    cases = raw.get("cases")
    if not isinstance(app, dict) or not isinstance(cases, list):
        raise SystemExit("Agent App eval file requires object fields: app and cases.")
    return app, [item for item in cases if isinstance(item, dict)]


def login_or_register(client: httpx.Client, args: argparse.Namespace) -> dict[str, Any]:
    login = client.post(api_url(args.base_url, "/auth/login"), json={"user_id": args.user_id, "password": args.password})
    if login.status_code == 200:
        return login.json()
    if not args.register:
        login.raise_for_status()
    register = client.post(
        api_url(args.base_url, "/auth/register"),
        json={
            "user_id": args.user_id,
            "display_name": "Agent App Eval User",
            "password": args.password,
            "confirm_password": args.password,
        },
    )
    if register.status_code not in {200, 201}:
        register.raise_for_status()
    return register.json()


def tool_names(body: dict[str, Any]) -> list[str]:
    calls = body.get("tool_calls", [])
    if not isinstance(calls, list):
        return []
    return [str(item.get("tool_name", "")) for item in calls if isinstance(item, dict)]


def tool_arguments(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    calls = body.get("tool_calls", [])
    if not isinstance(calls, list):
        return result
    for item in calls:
        if isinstance(item, dict) and isinstance(item.get("arguments"), dict):
            result[str(item.get("tool_name", ""))] = item["arguments"]
    return result


def expected_status(case: dict[str, Any], *, confirm_dangerous: bool) -> str:
    if confirm_dangerous and case.get("expected_confirmed_status"):
        return str(case["expected_confirmed_status"])
    return str(case.get("expected_status", "completed"))


def evaluate_parameters(case: dict[str, Any], body: dict[str, Any]) -> bool | None:
    expected_args = case.get("expected_arguments")
    if not isinstance(expected_args, dict) or not expected_args:
        return None
    actual = tool_arguments(body)
    checks: list[bool] = []
    for tool_name, expected_for_tool in expected_args.items():
        if not isinstance(expected_for_tool, dict):
            continue
        actual_args = actual.get(str(tool_name), {})
        for key, expected_value in expected_for_tool.items():
            if key == "title_contains":
                checks.append(str(expected_value) in str(actual_args.get("title", "")))
            else:
                checks.append(actual_args.get(key) == expected_value)
    return all(checks) if checks else None


def summarize(traces: list[dict[str, Any]], *, confirm_dangerous: bool) -> dict[str, Any]:
    total = len(traces)
    if total == 0:
        raise SystemExit("No valid cases were evaluated.")
    param_items = [item for item in traces if item["parameter_ok"] is not None]
    dangerous_items = [item for item in traces if item["dangerous_action_expected"] and not confirm_dangerous]
    return {
        "total_cases": total,
        "confirm_dangerous": confirm_dangerous,
        "task_success_rate": sum(1 for item in traces if item["status_ok"]) / total,
        "tool_selection_accuracy": sum(1 for item in traces if item["tool_selection_ok"]) / total,
        "parameter_accuracy": (
            sum(1 for item in param_items if item["parameter_ok"]) / len(param_items)
            if param_items
            else None
        ),
        "grounded_answer_rate": sum(1 for item in traces if item["grounded_ok"]) / total,
        "dangerous_action_block_rate": (
            sum(1 for item in dangerous_items if item["dangerous_blocked"]) / len(dangerous_items)
            if dangerous_items
            else None
        ),
        "avg_latency_ms": mean([int(item.get("latency_ms") or 0) for item in traces]),
        "avg_steps": mean([int(item["step_count"]) for item in traces]),
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
        ],
    }


def render_report(summary: dict[str, Any], traces: list[dict[str, Any]]) -> str:
    lines = ["# Agent App Evaluation Report", ""]
    for key in [
        "total_cases",
        "task_success_rate",
        "tool_selection_accuracy",
        "parameter_accuracy",
        "grounded_answer_rate",
        "dangerous_action_block_rate",
        "avg_latency_ms",
        "avg_steps",
    ]:
        value = summary.get(key)
        if isinstance(value, float):
            lines.append(f"- {key}: {value:.4f}")
        else:
            lines.append(f"- {key}: {value}")
    lines.extend(["", "## Failed Cases"])
    if not summary["failed_cases"]:
        lines.append("")
        lines.append("No failed cases.")
    else:
        lines.append("")
        lines.append("| id | expected status | actual status | expected tools | actual tools |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in summary["failed_cases"]:
            lines.append(
                f"| {item['id']} | {item['expected_status']} | {item['actual_status']} | "
                f"{', '.join(item['expected_tools'])} | {', '.join(item['actual_tools'])} |"
            )
    lines.extend(["", "## Trace Index"])
    for item in traces:
        lines.append(
            f"- {item['id']}: status={item['actual_status']}, tools={','.join(item['actual_tools']) or 'none'}, "
            f"steps={item['step_count']}, latency_ms={item.get('latency_ms')}"
        )
    return "\n".join(lines)


def main() -> None:
    args = parse_args()
    app_config, cases = load_dataset(Path(args.dataset))
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    traces: list[dict[str, Any]] = []
    with httpx.Client(timeout=30.0) as client:
        session = login_or_register(client, args)
        create = client.post(api_url(args.base_url, "/apps"), json=app_config)
        create.raise_for_status()
        app = create.json()
        publish = client.post(api_url(args.base_url, f"/apps/{app['app_id']}/publish"), json={"notes": "eval baseline"})
        publish.raise_for_status()

        for case in cases:
            payload = {
                "task": str(case.get("task", "")),
                "dry_run": bool(case.get("dry_run", False)),
                "confirm_dangerous_actions": bool(args.confirm_dangerous),
                "include_memory": False,
                "include_library": False,
            }
            response = client.post(api_url(args.base_url, f"/apps/{app['app_id']}/invoke"), json=payload)
            body = response.json() if response.status_code < 400 else {"status": "http_error", "detail": response.text}
            actual_status = str(body.get("status", "unknown"))
            expected = expected_status(case, confirm_dangerous=args.confirm_dangerous)
            actual_tools = tool_names(body)
            expected_tools = [str(item) for item in case.get("expected_tools", [])]
            parameter_ok = evaluate_parameters(case, body)
            traces.append(
                {
                    "id": str(case.get("id", "unknown")),
                    "category": str(case.get("category", "unknown")),
                    "task": payload["task"],
                    "team_id": session.get("team_id"),
                    "app_id": app.get("app_id"),
                    "run_id": body.get("run_id"),
                    "expected_status": expected,
                    "actual_status": actual_status,
                    "status_ok": actual_status == expected,
                    "expected_tools": expected_tools,
                    "actual_tools": actual_tools,
                    "tool_selection_ok": set(actual_tools) == set(expected_tools),
                    "parameter_ok": parameter_ok,
                    "grounded_ok": bool(body.get("sources")),
                    "dangerous_action_expected": bool(case.get("dangerous_action_expected", False)),
                    "dangerous_blocked": actual_status == "requires_confirmation" and bool(body.get("required_confirmations")),
                    "latency_ms": body.get("latency_ms"),
                    "step_count": len(body.get("steps", [])) if isinstance(body.get("steps"), list) else 0,
                    "response": body,
                }
            )

    summary = summarize(traces, confirm_dangerous=args.confirm_dangerous)
    (output_dir / f"{args.output_prefix}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / f"{args.output_prefix}_traces.json").write_text(
        json.dumps(traces, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / f"{args.output_prefix}_report.md").write_text(render_report(summary, traces), encoding="utf-8")
    print("Agent App evaluation complete.")


if __name__ == "__main__":
    main()
