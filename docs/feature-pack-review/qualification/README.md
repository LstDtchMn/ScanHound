# ScanHound final production qualification bundle

Prepared after Jesse explicitly authorized all remaining gated actions on
2026-07-21.

The bundle is fail-closed and intentionally contains no production paths or
credentials. The real-server operator must discover and record them.

Read in order:

1. `AUTHORIZATION.md`
2. `runbook/EXECUTION_ORDER.md`
3. `CODEX_EXECUTION_PROMPT.md`
4. `qualification.env.example`

The scripts use only Python's standard library. Docker is required for the
image migration matrix.
