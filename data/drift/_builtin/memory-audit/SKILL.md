---
skill_name: memory-audit
description: Audit MEMORY.md for stale or contradictory entries against recent conversation history
max_steps: 6
allowed_tools: [read_work_file, write_work_file, list_work_files, finish_drift]
trigger_condition: idle
---

# Memory Audit

Audit the user's long-term memory (MEMORY.md) for quality issues.

## Steps

1. Read the current MEMORY.md from the work files.
2. For each entry, check:
   - Is it still factually accurate?
   - Is there any contradiction with other entries?
   - Is the entry stale (last updated > 30 days)?
3. Write a report to `audit-report.md` with findings.
4. If issues found, list them clearly with suggested fixes.
5. Call finish_drift(decision="complete", message_result="no push") when done.
   If critical issues found, call finish_drift(decision="complete", message_result="push draft").
