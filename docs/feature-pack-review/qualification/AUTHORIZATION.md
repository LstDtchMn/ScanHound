# Jesse final execution authorization

**Authorized by:** Jesse Ewing  
**Authorization date:** 2026-07-21  
**Instruction:** “Do it all.”

This authorization covers all remaining ScanHound feature-pack closure work:

- production database/configuration backup and image-digest capture;
- full production-schema migration, interruption, restart, old-image-reopen,
  restore, and rollback testing on disposable byte-for-byte database copies;
- non-force merge and deployment after the migration matrix passes;
- deployment with Auto-rename, RSS-primary, RSS auto-grab, general auto-grab,
  and HDEncode traffic disabled;
- HDEncode zero-traffic proof;
- synthetic file-operation smoke tests;
- checksum-controlled filesystem sentinel execution in newly created,
  sentinel-only directories on applicable production/CIFS/SMB/NTFS/bind-volume
  surfaces;
- RSS shadow-only enablement;
- at least seven calendar days and at least 20 valid comparison cycles;
- final evidence reconciliation, mark-ready, merge, deployment, and staged
  rollout after every gate passes.

This authorization does not waive safety invariants or evidence requirements.

## Sentinel restriction

The sentinel may run only inside newly created directories whose names contain
`scanhound-sentinel`. A sentinel directory must contain no user data and must not
overlap a media library, download directory, database directory, trash root,
configuration directory, or source checkout.

## Mandatory stop conditions

Rollback or disable immediately on:

- unexpected destination replacement;
- source-byte loss;
- false-success restore/delete;
- second-writer acceptance;
- manifest/database divergence;
- stale-worker publication;
- unexpected HDEncode traffic while disabled;
- heavier fallback after 403/429/503/challenge;
- validator advancement without committed candidates;
- unknown DV/HDR treated as false;
- any relevant RSS miss;
- migration inconsistency;
- unexplained candidate-count collapse;
- database integrity failure or material row-count discrepancy;
- uncertain sentinel cleanup.
