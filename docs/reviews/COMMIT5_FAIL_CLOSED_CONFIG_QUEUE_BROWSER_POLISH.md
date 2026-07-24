# Commit 5: Fail-Closed Config and Queue/Browser Polish

Base: `9ad3deca77341dadd97016236fccc6faa41f2ead`

## Included

- Strict `source_enabled()` semantics and normalization of explicitly present
  malformed HDEncode flags.
- `SCANHOUND_HDENCODE_ENABLED` deployment override.
- Corrupt/non-object config files disable HDEncode unless an explicit
  environment override re-enables it.
- Existing HDEncode source gates use the shared helper.
- Durable queue aggregate `download:batch_progress` events.
- Claimed retry rows cannot present an enabled Remove action.
- Known cache-only Chromium profile paths are pruned at startup only after
  exceeding 256 MiB; cookies and persistent site data are retained.
- Browser-version warnings describe the selected adapter.
- Active queue duplicates are filtered before stagger positions are assigned.
- Empty batches fail request validation with 422, active conflicts remain 409,
  queue unavailability maps to 503, invalid queue requests map to 400, and
  unexpected exceptions are not disguised as conflicts.

## Deliberately skipped

Native-Windows browser minimization remains unchanged. It is optional,
desktop-only behavior and is not required for the Docker production safety or
configuration objectives of this commit.

No CAPTCHA solving, challenge interaction, proxy rotation, fingerprint
manipulation, schema change, merge, or deployment is included.
