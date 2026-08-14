# The qualification collector, under version control

`collect_shadow_evidence.py` here is the CANONICAL COPY of the script the
scheduled task "ScanHound Qualification Evidence" runs every 6 hours from
`X:\Docker Apps\scanhound-qualification-evidence\` on the server.

It lived ONLY on the server until 2026-08-14. That meant: no history, no review
surface, and edits proven only by a `.bak` beside the file. It is committed now
because the alert-dedup change needed a peer review and there was nothing to
point the reviewer at.

**The live copy is the one that runs.** After changing this file, copy it to the
server directory; after changing the live one, commit it back here. They are two
copies of one fact, and this README is the reminder that they drift.

State files the collector keeps beside itself on the server (not in git):
  - `stop-condition.last`  dedup signature of the last ALERTED stop set
  - `gate-passed.notified` one-shot marker for the gate-passed alert
  - `auth-token.txt`       readiness cross-check credential
  - `shadow-window.log`    the full every-run record (alerts are deduped;
                           this log never is)
