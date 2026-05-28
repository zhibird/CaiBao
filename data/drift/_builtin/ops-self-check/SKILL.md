---
skill_name: ops-self-check
description: Verify MCP server connectivity, tool availability, and recent error logs
max_steps: 6
allowed_tools: [read_work_file, write_work_file, list_work_files, message_push, finish_drift]
trigger_condition: idle
---

# Ops Self-Check

Run a health check on the CaiBao platform itself.

## Steps

1. Check the work files for any previous check results.
2. List available work files for any diagnostic logs.
3. Write a health check report to `health-report.md`:
   - Service status
   - Tool availability status
   - Any recent errors or warnings found
4. If issues found that need attention, call message_push with a summary.
5. Call finish_drift with appropriate decision.
