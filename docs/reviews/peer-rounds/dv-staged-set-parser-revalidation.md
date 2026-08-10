# The staged FEL set must be re-derived under the corrected parser

**Why this exists.** The 716 bounded-FEL positives were produced by the OLD
`_classify`, before consolidation blocker 3 was fixed. `probe_fel_bounded()`
returns `_parse_info(...) == LAYER_FEL`, so the staged set — and the gate
evidence signed off at `7260499` (711 targets, 0 replacements, 0 removals) — is
a product of that parser.

**Why only the positives need re-testing.** The corrected rule is strictly
narrower:

```
old:  "FEL" in sub                       -> FEL   (any profile, raw substring)
new:  major == 7 AND tokens <= {FEL,MEL} AND "FEL" in tokens
```

Every input the new rule calls FEL contains the substring `FEL`, so the old rule
called it FEL too. Therefore **new-FEL ⊆ old-FEL**: no old negative can become a
positive, and re-testing 716 positives is sufficient rather than all 2,738. Any
disagreement is a FALSE POSITIVE that would have become a wrong Plex badge.

**How it is being checked.** `scripts/reverify_716.py` imports
`backend.rename.dv_detect` from a worktree of the consolidation itself — the
parser that will actually be deployed, not a reimplementation of it.

**Result at handoff: 291 of 716 re-verified, 0 disagreements.** The run was
still in flight when this session wound down; its output is
`scratchpad/reverify_716.jsonl` (one JSON line per title, `still_fel`
true/false) and the console log is the `bu2msb982` task file. Consistent with
the prior evidence: 24 real `dovi_tool` summaries only ever produced
`Profile: 7 (FEL|MEL)`, `Profile: 8` and `Profile: 5`, none of which the
tightening rejects.

**What to do with it.** If the completed run shows 0 disagreements, the signed-off
gate figures stand unchanged. If any title disagrees, drop it from
`staged_fel_apply.jsonl` and re-run `scripts/stage_fel_write.py`, which
re-derives all four gates and exits non-zero on any unmatched row lacking a
recorded reason.

**The general rule this is an instance of.** The reviewer's criterion 10 —
"nothing changes between the final snapshot and execution in a way that
invalidates the preflight" — is usually read as data changing. Here the thing
that changed was the **parser that produced the data**. A staged artifact is
only valid under the code that built it, so deploying the consolidation before
the write means re-deriving the artifact, not reusing it.
