# Secret-scan TRUE POSITIVE — response procedure (Track C, contract C-2)

**Scope:** a finding in this repo (CI secret-scan or manual run) that is a
real credential, not an allowlisted false positive. The 2026-07-31 review
found zero true positives; this procedure exists BEFORE the first one so the
response is a checklist, not an improvisation.

## The clock starts at confirmation, not at detection

A finding is CONFIRMED when a human (Jesse or Claude) has looked at the
matched text and cannot classify it as a false positive. Confirmation should
take minutes, not hours — when in doubt, treat it as real.

## Steps, in order — revoke before anything else

1. **REVOKE / ROTATE first (target: within 1 hour of confirmation).**
   The credential is dead the moment it hits a public remote — rotation is
   damage control, not prevention. Who acts: **Jesse** for anything
   account-bound (API keys, tokens, passwords — the guardrail applies);
   Claude stages the exact rotation steps and verifies the OLD credential no
   longer works afterward (a rotation without a negative test is not done).
2. **Contain usage.** Identify every consumer of the credential (grep repo,
   infra-ops, compose files, container env) and update them with the new
   value — otherwise rotation becomes an outage.
3. **Assess exposure.** How long was it public (commit timestamp → rotation
   time)? Which services could it reach? Record what an attacker COULD have
   done; check available logs (service-side access logs, Cloudflare, Gotify
   client list) for evidence they DID.
4. **Decide on history.** Default per the accepted-risk record: no history
   rewrite for a rotated credential (rewriting ~700+ public commits breaks
   every clone and proves little — the secret must be treated as burned
   regardless). Jesse may overrule for high-sensitivity cases; record either
   way.
5. **Suppress the burned corpse via `.gitleaksignore`** (one full
   fingerprint per line, `commit:path:rule:line` — this is gitleaks' real
   fingerprint mechanism; a `fingerprints` key in `.gitleaks.toml` is a
   SILENT NO-OP, measured) ONLY IF the text remains in history after
   rotation, with an adjacent comment in this file's incident record saying
   "rotated + burned on <date>". Never widen the `.gitleaks.toml` paths
   allowlist for an incident.
6. **Record the incident** in `C:\DockerData\infra-ops\INCIDENTS\` — what
   leaked, when, exposure window, rotation evidence (the failed negative
   test), consumers updated, history decision.
7. **Fix the source.** Why did a secret reach a commit? Close that path
   (env var, gitignored file, pre-commit hook) in the same incident.

## Notification order

Jesse immediately (Gotify priority 8 if unattended, plus the session
report). ChatGPT gets the incident write-up as a review round — a second
reader on "what could this reach" has caught scope errors before.

## Preventive controls (deferred, tracked)

Detection-only is a recorded limitation: every future true positive is
burned before this procedure starts. Deferred candidates, in value order:
GitHub push protection (server-side, free for public repos); a local
pre-commit/pre-push hook running `gitleaks protect --staged`; an
Uptime-Kuma/Gotify hook so a failed or disabled scan workflow alerts
someone (nothing watches the watcher today).
