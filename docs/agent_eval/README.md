# Agent 任务评测最小集

本目录用于验证 CaiBao Agent 的“会不会做事”和“会不会克制”。

## 评测内容

- 纯问答：不应误触工具。
- 安全工具：应能自动选择 `list_recent_documents`。
- 危险工具：默认阻断 `create_incident`，等待确认。
- 混合任务：先检索/列文档，再识别待确认动作。
- 异常参数：确认执行后应失败并保留 trace，不能写入脏数据。

## 运行方式

```powershell
python scripts/agent_eval.py `
  --base-url http://127.0.0.1:8000 `
  --user-id agent_eval_user `
  --password Str0ngPass! `
  --register `
  --dataset docs/agent_eval/dataset_minimal.json `
  --output-dir docs/agent_eval/run `
  --output-prefix eval_v1
```

如果想评估“确认后执行危险工具”的表现：

```powershell
python scripts/agent_eval.py `
  --base-url http://127.0.0.1:8000 `
  --user-id agent_eval_user `
  --password Str0ngPass! `
  --dataset docs/agent_eval/dataset_minimal.json `
  --confirm-dangerous `
  --output-dir docs/agent_eval/run `
  --output-prefix eval_confirmed
```

App 级评测会先创建并发布一个 Dify-lite Agent App，然后通过 `/api/v1/apps/{app_id}/invoke` 运行任务：

```powershell
python scripts/agent_app_eval.py `
  --base-url http://127.0.0.1:8000 `
  --user-id agent_app_eval_user `
  --password Str0ngPass! `
  --register `
  --dataset docs/agent_eval/apps/ops_agent.json `
  --output-dir docs/agent_eval/run `
  --output-prefix app_eval_v1
```

## 输出指标

- `task_success_rate`
- `tool_selection_accuracy`
- `parameter_accuracy`
- `grounded_answer_rate`
- `dangerous_action_block_rate`
- `avg_latency_ms`
- `avg_steps`

输出文件：

- `*_summary.json`
- `*_traces.json`
- `*_report.md`
