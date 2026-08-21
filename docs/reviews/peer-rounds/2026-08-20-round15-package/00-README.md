# Round 15 review package — M14-1 / M14-2 closed, ledger reshaped

**Self-contained.** The full diff travels with the package.

## Identity

```text
repository    LstDtchMn/ScanHound
branch        fix/round12-attestation-authority
code head     ef2fb188342350507eeb649f533f3b197fc031e2
base          6ac5cd2aefb81bb7d85354577a69af269b8e05e5   (main, 0 behind)
working tree  clean
deployed      NOTHING. The running container predates all media-kind work.
```

## Contents

| File | What it is |
|---|---|
| `01-request.md` | **Start here.** M14-1, M14-2, the ledger reshape, and the one open question. |
| `02-code-changes.patch` | Complete diff of `backend/` and `tests/` against `main`. |
| `03-evidence.md` | Commands and results: mutations, suite figures, corpus measurements. |
| `04-provenance.md` | SHAs, blob hashes, container identity, what is NOT covered. |

## The three things worth your attention

1. **You were right that my restart tests were circular.** They seeded the
   journal by hand, so they proved recovery works *given* a journal — never that
   one exists after the failure it describes. Worse: my own comment said a marker
   in the same SQLite file cannot protect that case, and the implementation
   depended on it anyway. Now an independent append+fsync journal beside the
   database, with a process-wide interlock for the case where even that fails.
   **The new tests never seed anything** — they break the database and restart
   from whatever actually persisted.

2. **Cross-crawl contradictions now revoke.** `positive evidence may narrow
   immediately; authority may widen only through a coverage proof.` The claim
   writer stays inert and a test asserts it.

3. **The ledger is reshaped** — canonical identity, stable arm key,
   `posted_date_raw` rather than a pre-blessed order key, and a changed date
   raises an anomaly instead of being coalesced. Done now, while the table has
   zero rows anywhere.

## Round-14 dispositions

```text
whole-identity mask        APPROVED by you; unchanged
L13-1 parser health        CLOSED by you; unchanged
M14-1 restart durability   ADDRESSED -- independent journal + interlock
M14-2 cross-crawl conflict ADDRESSED -- inert writer, separate consumer
ledger shape A-F           DONE
coverage proof / frontier  NOT BUILT -- min(posted_date) accepted as rejected;
                           one design question in 01-request.md
legacy aged-off policy     ACCEPTED, unchanged
```

## Open question in one line

Should the crawler **emit** the coverage frontier, or emit raw traversal facts a
separate consumer derives it from? I lean the second, for the same reason the
claim ledger is inert — but it is more machinery.
