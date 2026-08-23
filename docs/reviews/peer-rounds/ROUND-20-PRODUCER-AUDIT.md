# Producer audit — who could have written the 266 `listing_claims` rows

**Closes R20R2-3.** Evidence for the historical migration manifest.

The reviewer's objection was precise and correct: byte-identity between the
deployed image and commit `ef2fb188` proves what the deployed writer *would* have
written. It does not prove that the deployed writer wrote every row. I had noted
the existence of hand-written scratch scripts as an honesty marker; the reviewer
ruled that insufficient — it is an unresolved alternate-producer path, and a row
inserted by a script inherits no parser revision merely because the deployed image
had matching parser bytes.

This is the inventory, done by capability rather than by execution evidence.
Absence of execution evidence would prove nothing; absence of the *capability* to
write does.

## Method

Three populations, exhaustively:

1. every caller of the claim-writing function, on the branch that has it
2. every script in the repository that references the live database
3. every script in the session scratchpad — 209 files

For each, the question is not "did it run?" but "could it write
`/dbvol/crawler.db`?"

## 1. Callers of `record_listing_claims`

On `fix/round12-attestation-authority` — the only branch where the function
exists:

```text
backend/background_scanner.py:695    db.record_listing_claims(_claims)
backend/database.py:4694             (comment)
backend/database.py:4839             (comment)
backend/scanner_service.py:1113      (comment)
```

**Exactly one call site**, and it is the crawl path inside `scan_once()`. The
other three are prose.

`main` has no ledger code at all, which is why the ledger froze when `main`-based
work was deployed on 2026-08-22.

## 2. HTTP surface

```text
grep -rn record_listing_claims backend/api/   ->   no matches
```

**No API route writes claims.** There is no external path to the table.

## 3. Repository scripts

Eight scripts reference the live database. Their connection strings:

```text
scripts/rollback_snapshot.py         mode=ro
scripts/scanhound_check.py           mode=ro
scripts/turnstile_watch.py           mode=ro
scripts/watch_resume.py              mode=ro
scripts/requeue_throttled_grabs.py   mode=ro
scripts/reclassify_queue_sources.py  mode=ro unless --apply
scripts/migrate_challenge_episode.py sqlite3.connect(args.db)   writable
scripts/import_dv_seed.py            writable
```

Three are writable. **None references `listing_claims`** — checked directly, zero
occurrences in each. They write `download_queue_items`, `challenge_episode` state
and `dv_scan` respectively. None is invoked by any automation; the only reference
to any of them anywhere is a docstring mentioning `import_dv_seed`.

## 4. Scratchpad scripts

209 files. Every `sqlite3.connect` in the entire directory, classified:

```text
targeting /dbvol/crawler.db     ALL carry ?mode=ro, uri=True
targeting self.db_path          copies of database.py SOURCE, not runnable scripts
targeting dm.db_path / db.db_path   tempfile.mkdtemp() DatabaseManager instances
sqlite3.connect(path)           tempfile.mkdtemp()          (live_replay.py)
sqlite3.connect(p)              /tmp/mk-refut-ycol/legacy.db (refut2.py)
sqlite3.connect(uri, uri=True)  the browser profile Cookies db, immutable=1 (cookies.py)
```

**No scratchpad script can write `/dbvol/crawler.db`.** A `mode=ro` URI connection
is refused at the SQLite layer, not by convention.

The 67 files that matched `listing_claims` by text are source-patching scripts —
they edit `backend/*.py` on disk and never open a database. That is what made them
look like candidate writers in the first pass.

## Conclusion

The only thing capable of writing `listing_claims` is
`background_scanner.py:695`, running inside the deployed container. Combined with
the byte-identity of the request stack and parser to `ef2fb188`, and with all 266
`raw_url` netlocs resolving to `hdencode.org`, producer attribution is closed.

**Quarantine set for the existing 266 rows: empty.**

## What this does NOT establish

- **It is a capability argument, not an execution record.** No first-party scan
  log exists — `scan_history` is empty — so nothing here is a transcript of what
  ran. What is established is that no other code path *could* have produced a row.
- **It says nothing about rows written after a redeploy.** Whether `ddlbase` and
  `adithd` are enabled live is still unread, and `ddlbase:remux` remains
  unresolvable by design. "Quarantine set is empty" is a statement about these 266
  rows only.
- **`parser_version` remains RECONSTRUCTED.** Per the reviewer's ruling this is
  now acceptable, because producer provenance is closed — but the manifest records
  it as `parser_provenance: reconstructed` with the source commit and blob hash,
  never as contemporaneously recorded.
