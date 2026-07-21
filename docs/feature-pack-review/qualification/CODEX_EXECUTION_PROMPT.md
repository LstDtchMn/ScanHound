# Codex/Claude real-server execution prompt

Jesse explicitly authorized all remaining ScanHound feature-pack closure work
with: **“Do it all.”**

Use this bundle on the real production server. Read `AUTHORIZATION.md` and
`runbook/EXECUTION_ORDER.md` first.

Fixed references:

- Repository: `LstDtchMn/ScanHound`
- Integration branch: `agent/feature-pack-integration`
- Code-tested SHA: `a6b4a7b14d6613c27f17de670677ed848fec458d`
- Last independently reviewed evidence head: `456b7ad6fc1e620cc0948237cb2f5cb4338ea784`

Discover the actual project, database, config, container, mount, and log paths
on the server. Do not guess them. Execute the runbook in order, disclose every
rework, and stop/roll back on any mandatory stop condition.

The production server is not connected to the ChatGPT session that produced
this bundle, so return the raw evidence for independent peer review. Do not
compress or simulate the seven-day RSS window.
