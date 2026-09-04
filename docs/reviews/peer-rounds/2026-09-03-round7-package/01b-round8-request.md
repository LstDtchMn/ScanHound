# Round 8 request — TST-1 (2026-09-03, late)

One PR this round: TST-1, the first item in your round-7c closure order. Package on `review/2026-09-02-complete-review`; the patch is `patches/tst1-suite-trash-isolation.patch`, full diff against `main` @ `0a2751d`.

## What we are asking

1. Does the redirect contain every path by which the suite could reach a real volume root? We traced `_trash_root_for`, `_TRASH_ROOT`, `_same_volume_trash_roots` (its ancestor walk stops at `dirname(primary)` = tmp_path for sources under tmp_path), `all_trash_roots`, and the registered-roots index (already isolated). Name any path we missed.
2. Is the guard's snapshot (one level deep, entry names plus mtimes) the right instrument, or do you want it deeper? It is designed to be cheap enough to run before and after every one of 5,400 tests.
3. The guard reports and never deletes. Agree, or do you want the per-test guard to remove exactly the entries the test added (we chose not to touch a real root from test code at all)?
4. The 400 pre-existing buckets: we left them; removing them is the owner's call. Anything you want measured about them first?

## What we are not asking

TST-2 is not started; nothing in this PR assumes TST-1 caused it. No merge, deployment, permission change or enablement is authorized.
