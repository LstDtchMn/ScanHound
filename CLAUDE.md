<!-- BEGIN CLAUDE-CODEX-PEER-REVIEW-WORKFLOW -->
# Claude + Codex Peer Review Workflow

Claude Code is the primary implementation agent.

Codex is an independent reviewer.

## Claude responsibilities

Claude should:

- inspect the real repository before changing code
- implement the requested work
- run appropriate tests
- explain meaningful design decisions
- treat Codex findings as independent peer-review findings
- investigate findings rather than blindly accepting or rejecting them
- fix valid findings
- explain rejected findings using repository evidence
- rerun relevant tests after remediation

Claude must not claim that Codex reviewed something unless an actual Codex
review was executed.

## Review cycle

For non-trivial changes, the preferred process is:

IMPLEMENT
    |
    v
RUN TESTS
    |
    v
CODEX ADVERSARIAL REVIEW
    |
    v
CLAUDE INVESTIGATES FINDINGS
    |
    v
REMEDIATE VALID FINDINGS
    |
    v
RUN TESTS
    |
    v
CODEX TARGETED RE-REVIEW
    |
    v
FINAL CODEX REVIEW
    |
    v
READY FOR HUMAN MERGE DECISION

## Handling findings

Claude should preserve the identity of findings across review rounds.

Example:

M1 - OPEN
M2 - CLOSED
M3 - PARTIALLY CLOSED

Claude should not silently drop findings.

## Reviewer independence

Do not ask Codex merely to confirm Claude's conclusions.

Ask Codex to challenge them.

For important changes, Codex should specifically examine:

- hidden assumptions
- failure modes
- concurrency
- data loss
- rollback
- recovery
- transactional boundaries
- regression risk
- test adequacy

Claude remains responsible for implementation decisions after considering the
review.
<!-- END CLAUDE-CODEX-PEER-REVIEW-WORKFLOW -->
