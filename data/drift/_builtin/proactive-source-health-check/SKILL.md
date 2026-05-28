---
skill_name: proactive-source-health-check
description: Poll all configured proactive sources and report their status
max_steps: 6
allowed_tools: [read_work_file, write_work_file, list_work_files, message_push, finish_drift]
trigger_condition: idle
---

# Proactive Source Health Check

Verify that all configured proactive data sources are responsive.

## Steps

1. Read any previous health check results from work files.
2. For each configured source, attempt to pull events.
3. Classify each source as healthy, degraded, or down.
4. Write a report to `source-health.md` with status per source.
5. If any source is down, call message_push with a notification.
6. Call finish_drift to complete.
