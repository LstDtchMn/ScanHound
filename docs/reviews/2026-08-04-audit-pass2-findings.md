# ScanHound audit pass 2 — confirmed findings

**Run 2026-08-04.** 51 agents across 6 subsystems: download path, Plex sync,
notifications, auth surface, DB integrity, scan pipeline. Each candidate was
then put to an adversarial verifier told to refute it.

- **32 confirmed** (survived verification, most with a live repro)
- **13 refuted** (listed at the bottom — do not re-raise these)
- **3 unverified** (plausible, not reproduced)

That 13 findings were refuted is the reason to trust the other 32: the verify
pass was capable of saying no.

Nearly every finding shares one shape: **the failure is silent.** The operation
reports success, the log stays clean, and the damage is only visible later as
missing data. That is why they survived this long.

| Severity | Count | Fixed so far |
|---|---|---|
| critical | 1 | 1 |
| high | 12 | 12 |
| medium | 13 | 13 |
| low | 6 | 6 |

**Where this stands (2026-08-05). All 32 confirmed findings are fixed.**

Two were closed only after an adversarial verifier rejected the first attempt,
and both rejections were correct:

- **#19** was initially "fixed" by consuming the corruption flag on confirmed
  delivery — but `NotificationBridge.notify_error` returned `None` on every
  path, so confirmation was unreachable and the branch was dead code. Net
  effect would have been three duplicate alerts per incident, then the flag
  discarded unconfirmed anyway: worse than the bug. Closing it properly meant
  finishing the chain — `_send_notification` now RETURNS its channel success
  count instead of only logging it, `notify()` propagates it (`None` when
  batched, because a batched send has not happened yet), and the bridge gained
  a `notify_error_confirmed()` that waits for the result. `notify_error()`
  stays fire-and-forget so nothing on the hot path blocks on SMTP.
- **#17** was initially closed by having the frontend mint tickets — but the
  fallback fired on ANY failure, so a transient 5xx re-leaked the 30-day token,
  and a backend restart triggers that failure and a reconnect storm at the same
  moment. The fallback now fires only on 404/405 (route genuinely absent).
  Everything else retries.

Three pre-existing tests in `test_database_hardening.py` pinned the OLD #19
contract and were rewritten;
`test_bridge_exception_does_not_prevent_flag_rename` was deleted outright
rather than adapted, because it asserted the rejected policy in its own
docstring.

---

## CRITICAL

### 1. `backend/scanner_service.py:1356`  ✅ FIXED in `38768fe`

*scan-pipeline*

**What breaks.** A stop_scan_flag left set from an earlier stopped/blocked scan makes rematch_cache() blank every cached row's Plex match and rewrite the whole background cache as "missing", while logging it as a successful re-match.

**How it happens.** 10:00 the operator starts a scan and clicks Stop (POST /scan/stop sets scanner.stop_scan_flag = True; NOTHING ever clears it -- run_scan only clears it at the start of the NEXT scan, line 352). 10:05 the operator grabs a release; downloads.py:42 _persist_grab_annotations -> scanner.rematch_cache(). Plex is still loaded, so have_plex is True and every row is reset to download-history status with plex_info="-"/plex_versions="[]" (lines 1252-1259); _match_against_plex is then entered and breaks at idx 0 on the stale flag, so nothing is re-matched; the diff check at 1307 sees status changed in_library->missing and writes every row. Verified by execution: with stop_scan_flag=False the row is written back status='in_library' plex_info='4K DV 60 GB'; with stop_scan_flag=True the SAME row is written status='missing' plex_info='-' plex_versions='[]', and rematch_cache returns "1 row updated". Whole library shows as Missing in the UI; with auto-grab on, owned titles become grab candidates. Same path fires from main.py:181 (download-queue delivery hook) and background_scanner.py:509 when HDEncode is the last/only source and its crawl was Cloudflare-blocked (line 807 sets stop_scan_flag=True and never clears it).

```python
scanner_service.py:1252-1259  `if have_plex:` / `item.status = dl_status` / `item.plex_info = "-"` / `item.plex_versions = "[]"`
scanner_service.py:1355-1357  `for idx, item in enumerate(items_snapshot):` / `            if self.stop_scan_flag:` / `                break`
scanner_service.py:1322  `logger.info("Cache re-match: %d of %d cached item(s) updated", len(updates), len(items))`
api/routes/scanner.py:375-376  `if scanner:` / `        scanner.stop_scan_flag = True`
```

**Verifier.** CONFIRMED, and the trigger is broader than claimed. (1) The stale flag is real: `stop_scan_flag = False` appears EXACTLY ONCE in the whole codebase -- scanner_service.py:352 at the start of run_scan. api/routes/scanner.py:376 `scanner.stop_scan_flag = True` has no counterpart; _run_scan's finally (scanner.py:264-270) only does `scanner.release_scan()` + resets _scan_state, and background_scanner's finally (534-536) is the same. It is a threading.Event on the shared singleton (scanner_service.py:243-252 `return self._stop_event.is_set()`), so it persists across requests. (2) rematch_cache blanks first and depends on the matcher to restore: `if have_plex:` / `item.status = dl_status` / `item.plex_info = "-"` / `item.plex_versions = "[]"` (1252-1259), and _match_against_plex's loop body starts with `for idx, item in enumerate(items_snapshot):` / `if self.stop_scan_flag:` / `break` (1355-1357) -- it breaks at idx 0, so nothing is restored, and the 1307 diff (`d.get('status') != new_status`) writes every row. (3) Verified by execution against this tree: same cached row (status='in_library', plex_info='4K DV 60 GB'), same non-empty plex_index -- with stop_scan_flag=False it is written back `status=in_library plex_info=4K DV 60 GB plex_versions=[{"res":"4K",...}]`; with stop_scan_flag=True the SAME row is written `status=missing plex_info=- plex_versions=[]`, and both return 1. (4) Si

**Suggested fix.** Three layers, smallest first:

1. Make rematch_cache immune to the scan stop flag (the real fix). Give _match_against_plex an explicit stop predicate instead of reading self.stop_scan_flag directly:
   `async def _match_against_plex(self, scan_type="Deep Scan", items=None, stop_check=None):`
   `stop_check = stop_check or (lambda: self.stop_scan_flag)` and change line 1356 to `if stop_check(): break`. rematch_cache then calls it with `stop_check=lambda: False` -- it is a cheap in-memory pass with no network I/O, so a scan-stop request has no business aborting it.

2. Never persist a partially-matched pass. Have _match_against_plex report whether it broke early (return the interrupted flag, or set an attribute), and in rematch_cache abandon the write and log a warning if it did:
   `if interrupted: logger.warning("Cache re-match aborted mid-match; no rows written"); return 0`
   This turns any future variant of the bug from silent corruption into a visible no-op. Also change the 1322 lo

---

## HIGH

### 2. `backend/api/routes/auth.py:143`  ✅ FIXED — credential_state three-state read; fails CLOSED on an unreadable DB

*auth-surface*

**What breaks.** `has_password()` conflates "no password configured" with "could not read the password", and that False both un-gates `/auth/set-password` at the middleware AND skips the current-password check inside the route — so any DB read failure (or the automatic corrupt-DB quarantine) silently re-opens unauthenticated admin takeover.

**How it happens.** State: a password IS configured. The SELECT in `get_password_hash()` fails — `disk I/O error` from the failing WD in the X: mirror, `database is locked` from a second process, `no such table` after a partial migration, or the DB was auto-quarantined and rebuilt empty by `_quarantine_corrupt_db`. `_query` catches `Exception` and returns its `default` of None -> `has_password()` -> False -> `auth_enabled()` -> False -> `_request_requires_auth('/auth/set-password')` -> False (bootstrap exemption). Input: an unauthenticated `POST /auth/set-password {"new_password":"attacker-chosen-pw"}` with NO Authorization header and NO current_password, from any container on the shared `proxy` docker network (docker-compose.yml:143 records these reach scanhound:9721 directly, bypassing NPM/Cloudflare Access). Outcome (verified by invoking the real route function): HTTP 200 `{'ok': True}`, the stored hash is replaced with the attacker's, and the attacker now holds full API access including `/rename/apply` file moves and `/rename/trash` empty. No rate limit, no audit record, and no auth-specific alert — the only signal is the generic "Database corruption was detected and quarantined — check logs" notification, which says nothing about the credential being wiped or the takeover window opening.

```python
if reg.db.has_password():
        stored = reg.db.get_password_hash()
        if not auth_service.verify_password(body.current_password or "", stored):
            raise HTTPException(
                status_code=401, detail="Current password is incorrect")
    reg.db.set_password_hash(auth_service.hash_password(new_password))
```

**Verifier.** I tried hard to refute this and could not — the conflation is real and I reproduced full takeover end-to-end through the real app (no mocked route, production posture: no nonce, SCANHOUND_ALLOW_OPEN unset).

The conflation, backend/database.py:134-158 + 3612-3621:
    def _query(self, sql, params=(), *, one=False, default=None):
        ...
        except Exception as e:
            logger.error("DB query error: %s", e)
            return default          # <- any read failure looks like "no row"
    def get_password_hash(self):
        row = self._query('SELECT password_hash FROM auth_credentials WHERE id = 1', one=True, default=None)
        return row[0] if row else None
    def has_password(self):
        return self.get_password_hash() is not None

Both gates hang off that one boolean, with no second signal:
  dependencies.py:229-232  `if registry.auth_nonce: return True` / `return bool(db and db.has_password())`
  main.py:505-507          `if request.url.path in _BOOTSTRAP_EXEMPT_PATHS: return False  # let the first password be set`
  auth.py:143              `if reg.db.has_password():` ... current-password check

Production really is in the fail-closed-with-bootstrap-hole posture: docker/entrypoint.sh:43 is `exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth`, and __main__.py:18 sets `os.environ["SCANHOUND_AUTH_NONCE"] = ""` — so there is no nonce, and docke

**Suggested fix.** Make "could not read" a third state and fail closed on it, at all three consumers.

1. backend/database.py — add a failure-distinguishing read. Use a sentinel instead of collapsing into the `default`:
   _READ_FAILED = object()
   def credential_state(self) -> str:
       row = self._query('SELECT password_hash FROM auth_credentials WHERE id = 1', one=True, default=_READ_FAILED)
       if row is _READ_FAILED: return "unknown"
       return "present" if (row and row[0]) else "absent"
   Keep has_password() as `credential_state() == "present"` for existing callers, but stop letting it be the security decision.

2. backend/api/dependencies.py auth_enabled() — treat "unknown" as credentialed so the gate stays SHUT:
   state = db.credential_state(); return state in ("present", "unknown")
   (ws.py:136 inherits this for free since it shares auth_enabled().)

3. backend/api/main.py _request_requires_auth() — grant the /auth/set-password bootstrap exemption only on a definitive "absent", never

---

### 3. `backend/api/routes/auth.py:148`  ✅ FIXED — set_password checks both write results

*auth-surface*

**What breaks.** `set_password` ignores the return value of both `set_password_hash()` and `delete_all_sessions()` and unconditionally returns `{"ok": True}` — a failed write silently leaves the old password live and/or every existing session valid after a password change.

**How it happens.** `DatabaseManager._mutate` returns False (not raises) on any exception — disk full, `database is locked`, I/O error on the bind mount. Case A: `set_password_hash` returns False and `delete_all_sessions` returns True -> the owner is signed out everywhere, their NEW password does not work, only the OLD one does, and the UI toasted "Password updated / Your password was changed". Case B: `set_password_hash` succeeds and `delete_all_sessions` returns False -> the owner changes the password specifically to lock out a suspected compromise; the UI (ChangePassword.svelte) says "Changing it signs out all existing sessions" and toasts success, but every previously issued 30-day session token remains valid in `auth_sessions` and keeps working. Verified by calling the real route with a DB stub returning False from both writes: response `{'ok': True}`, stored hash unchanged, old password still verifies, new password does not.

```python
reg.db.set_password_hash(auth_service.hash_password(new_password))
    reg.db.delete_all_sessions()  # force re-login everywhere
    return {"ok": True}
```

**Verifier.** CONFIRMED — no guard exists anywhere on the path. (1) backend/database.py:185-195 `_mutate` swallows the exception and returns a bool rather than raising: `except Exception as e: logger.error("DB Error (%s): %s", label, e); return False` — plus a no-exception failure path `conn = self.get_connection(); if not conn: return False`. (2) backend/database.py:3623-3631 `set_password_hash` and :3661-3663 `delete_all_sessions` are both bare `return self._mutate(...)`, so the False reaches the caller intact. (3) backend/api/routes/auth.py:148-150 discards both: `reg.db.set_password_hash(auth_service.hash_password(new_password))` / `reg.db.delete_all_sessions()  # force re-login everywhere` / `return {"ok": True}` — no try/except, no re-read verification, no exception to be caught by any handler, and this route is the ONLY production caller (the two other hits are test teardown). (4) Case B is genuinely reachable, not a theoretical race: backend/api/dependencies.py:274-276 validates a token purely by row presence — `expires_at = db.get_session_expiry(auth_service.hash_token(token)); if expires_at and not auth_service.is_expired(expires_at): return True` — so a failed DELETE leaves every prior token valid for its full expiry on both HTTP and WebSocket. (5) Frontend copy quoted accurately: ChangePassword.svelte:57 "Changing it signs out all existing sessions" and :26-29 toasts "Password up

**Suggested fix.** Check both return values and never report success for a write that did not land. In backend/api/routes/auth.py replace lines 148-150 with:

    if not reg.db.set_password_hash(auth_service.hash_password(new_password)):
        raise HTTPException(status_code=500,
                            detail="Could not save the new password; it is unchanged.")
    if not reg.db.delete_all_sessions():
        # Password DID change - the caller must be told to use the new one,
        # and that existing sessions were NOT revoked.
        raise HTTPException(
            status_code=500,
            detail="Password changed, but existing sessions could not be revoked. "
                   "Sign out all devices manually and retry.")
    return {"ok": True}

Ordering matters: bailing out before touching auth_sessions on a set_password_hash failure keeps the failure atomic (Case A becomes a clean no-op instead of "signed out everywhere with the old password still live"). The partial-success case canno

---

### 4. `backend/api/routes/scanner.py:240`  ✅ FIXED — cancelled scans skip completion, last_scan_time and auto-grab

*scan-pipeline*

**What breaks.** A cancelled scan is reported as complete and auto-grabbed: run_scan returns items that were never matched against Plex, and the route broadcasts scan:complete, stamps last_scan_time and runs auto-grab over them.

**How it happens.** The operator clicks Stop during the detail-processing phase. _run_scan_async returns at line 533 BEFORE _match_against_plex/_mark_missing_seasons/_enrich, so every produced item still carries the download-history-only status, i.e. MISSING even for titles sitting in Plex. run_scan returns that partial list; _run_scan sees no exception and proceeds unconditionally: replaces _last_scan_items, broadcasts scan:complete (the UI toasts "Found N results"), stamps config["last_scan_time"], sends the Scan Complete notification, and -- if auto_grab_enabled -- calls process_items(items), which grabs everything MISSING that passes the rating/genre gates (rating is 0 because enrichment was skipped too, so a configured min_rating flips the gate the other way and skips everything). Verified by execution: after setting stop_scan_flag mid-processing, run_scan returned 1 item with status 'missing' and neither find_movie_matches nor find_tv_season_matches was ever called.

```python
scanner_service.py:533-534  `        if self.stop_scan_flag:` / `            return`   (returns before `await self._match_against_plex(scan_type)` at line 556)
api/routes/scanner.py:209-217  `stats = _compute_stats(items)` / `ws_manager.broadcast_sync({` / `    "type": "scan:complete",` ...
api/routes/scanner.py:240-246  `if reg.auto_grab and reg.auto_grab.enabled and items:` / ... / `                grabbed = reg.auto_grab.process_items(items)`
```

**Verifier.** Confirmed, not refuted. scanner_service.py:533-534 `if self.stop_scan_flag:` / `return` sits before `await self._match_against_plex(scan_type)` at line 556, and run_scan still hands back `return list(self.items)` (line 380) — items whose status came only from `_download_status_for` (line 1069: `status = ScanStatus.DOWNLOADED if url in self.download_history else ScanStatus.MISSING`). The route's `_run_scan` contains no stop check anywhere between `items = scanner.run_scan(...)` (line 189) and `grabbed = reg.auto_grab.process_items(items)` (line 246); the only gate is `if reg.auto_grab and reg.auto_grab.enabled and items:` (line 240), and `/scan/stop`'s `_scan_state["state"] = "stopping"` (line 378) is never read by `_run_scan`. AutoGrabService default allowed statuses are `{ScanStatus.MISSING, ScanStatus.UPGRADE, ScanStatus.DV_UPGRADE}` (auto_grab_service.py:67), so unmatched MISSING items qualify. Reproduced by executing the real `_run_scan` with a stub scanner that sets stop_scan_flag and returns one unmatched MISSING item: broadcasts were ['scan:result','scan:complete','autograb:started','autograb:complete'], `last_scan_time stamped: True`, `notify_scan_complete calls: [(1, 1, 0)]`, `auto_grab.process_items calls: [[('A Movie In Plex','ScanStatus.MISSING',0.0)]]`. Two consequences the finder missed: frontend/src/lib/stores/scanner.ts:27-35 toasts "Scan Complete / Found N resu

**Suggested fix.** Make cancellation an explicit outcome instead of an indistinguishable partial result. (1) In scanner_service, record it: set `self.last_scan_cancelled = bool(self.stop_scan_flag)` in run_scan's `finally` before `return list(self.items)` (a bare read of `scanner.stop_scan_flag` in the route also works today — run_scan only clears the flag at line 352 of the NEXT run and the scan slot is still held — but an explicit attribute survives future refactors and covers the block-detector path too). (2) In backend/api/routes/scanner.py `_run_scan`, right after `items = scanner.run_scan(...)`, branch: if cancelled, still store `_last_scan_items` and broadcast the results (the operator should see what was found), but broadcast `{"type": "scan:cancelled", "data": {"partial": True, "stats": stats}}` instead of `scan:complete`, and skip all three completion side effects — the `reg.config["last_scan_time"]` stamp, `notify_scan_complete`, and the entire `if reg.auto_grab ...: process_items(items)` bloc

---

### 5. `backend/api/routes/settings.py:310`  ✅ FIXED — NotificationBridge.reconfigure() on settings save

*notifications*

**What breaks.** Notification channel settings never reach the running NotificationBridge — it is configured exactly once at startup and caches channel objects built from that config snapshot — while the "Test" button probes reg.config directly and reports success, so the operator is actively told a channel works when the live notification path has zero channels until the process restarts.

**How it happens.** Fresh container (config default discord_webhook=""). Startup runs backend/api/main.py:129 `notif.configure(backend.config)` -> NotificationBridge sees an empty webhook -> `self._manager._channels == []`. Operator pastes a Discord webhook in Settings; the frontend (frontend/src/routes/settings/+page.svelte:176) calls saveSettings() then POST /settings/test/discord, which reads `cfg.get("discord_webhook")` and POSTs with `requests` (settings.py:334-345) -> HTTP 204 -> toast "Discord test sent" + green check. update_settings only does `reg.config.update(real_updates)`; nothing re-invokes bridge.configure (_init_services is called only from the lifespan, main.py:598). Every subsequent scan-complete (backend/api/routes/scanner.py:233 `reg.notifications.notify_scan_complete(...)`) reaches _send_notification with `tasks == []`, so the `if tasks:` guard at notifications.py:740 skips even the DEBUG counter — no log line at ANY level. Verified by execution: after configure({discord_webhook:""}) then mutating the config dict, channels stayed `[]`, notify_scan_complete recorded the item in get_history() as if delivered, and the only output at production INFO level was "NotificationBridge configured". Same holds in the Qt desktop app (ui/controllers/main_controller.py:193 configures once; SettingsController is constructed without the bridge and its test methods read self._backend.config directly at settings_controller.py:803).

```python
reg.config.update(real_updates)
    if reg.backend:
        reg.backend.save_config()
    return {"status": "ok", "updated_keys": list(real_updates.keys())}
```

**Verifier.** CONFIRMED — no guard exists. NotificationBridge.configure() snapshots scalars out of the config dict rather than holding a reference: `discord_url = config.get("discord_webhook", "")` / `if discord_url: notif_config["discord_webhook"] = discord_url` ... `self._manager.configure_from_dict(notif_config)`, and configure_from_dict builds `DiscordWebhookChannel(config['discord_webhook'], ...)` from the string. Later mutation of reg.config therefore cannot reach the channel list.

Only call site in the API process is backend/api/main.py:128-130 `notif = NotificationBridge(); notif.configure(backend.config); reg._notification_bridge = notif`, and _init_services is invoked solely from the lifespan (main.py:598). The update path at settings.py:310-313 is exactly `reg.config.update(real_updates)` / `if reg.backend: reg.backend.save_config()` / `return {"status": "ok", ...}` — it never touches reg.notifications.

Silence confirmed at notifications.py:735-743: `for channel in self._channels: if channel.should_handle(...)` then `if tasks:` gates the ONLY log line (`logger.debug(f"Notification sent to {successes}/{len(tasks)} channels")`), so an empty channel list emits nothing at any level — while `self._history.append(notification)` at the top of _send_notification still records it as delivered. NotificationBridge.send() does not short-circuit either, since its guard is `if not self._manag

**Suggested fix.** 1. Split the config->channel mapping out of NotificationBridge.configure() into `_build_notif_config(config)`, then add a real rebuild method:

   def reconfigure(self, config):
       if self._manager is None:
           return self.configure(config)
       self._manager._channels.clear()   # better: add NotificationManager.clear_channels()
       self._manager.configure_from_dict(self._build_notif_config(config))

   Rebuilding on the EXISTING manager (rather than re-running configure(), which does `self._manager = NotificationManager()`) preserves _history, _callbacks and any in-flight batch, and avoids leaking a second manager. _start_loop() already early-returns when the thread is alive, so the loop is reused either way.

2. In update_settings (settings.py, after line 312), reconfigure when any notification key changed — and do it AFTER save_config(), because save_config restores sensitive keys from disk and can change the effective value:

   NOTIF_KEYS = {"desktop_notifications"

---

### 6. `backend/background_scanner.py:470`  ✅ FIXED — _listing_arm_incomplete: a blocked crawl is no longer promotion evidence

*scan-pipeline*

**What breaks.** The RSS shadow comparison records outcome="success" with zero misses for a cycle whose listing crawl fetched nothing, because the listing_error guard reads an `err` that can never be non-None -- and those cycles count as promotion evidence.

**How it happens.** HDEncode listing pages return 403/time out for the whole crawl (the per-page handler at scanner_service.py:905 catches connection errors and the 403 branch at 799 just backs off), so `items` is empty while _last_crawl_request_count > 0. run_scan swallows everything so `err` is None (see the run_scan finding), hence _rss_normal_feeds_complete's listing_error guard never trips and normal_feeds_complete stays True. compare_shadow then computes listing_only = {} -> relevant_miss_count = 0 -> outcome 'success' (hdencode_shadow.py:74-75). database.get_hdencode_shadow_summary counts exactly these rows as eligible (outcome IN ('success','relevant_miss') AND normal_feeds_complete=1 AND rss_requests>0 AND listing_requests>0), so a cycle where the control arm was blocked stretches the observation window, adds to successful_cycles toward the 20-cycle/7-day gate, and inflates request_reduction_pct. The gate that is supposed to prove "RSS misses nothing the listing crawl finds" is fed cycles where the listing crawl found nothing because it was broken.

```python
background_scanner.py:461-472  `metrics = compare_shadow(` / `    rss_urls=rss_cycle.get("candidate_urls", []),` / `    listing_items=items,` / `    rss_requests=rss_cycle.get("requests", 0),` / `    listing_requests=getattr(` / `        scanner, "_last_crawl_request_count", source_pages` / `    ),` / `    normal_feeds_complete=self._rss_normal_feeds_complete(` / `        rss_cycle.get("feeds", []),` / `        listing_error=err,` / `    ),` / `).as_dict()`
hdencode_shadow.py:74-75  `outcome='success' if normal_feeds_complete else 'incomplete_feeds'` / `if misses: outcome='relevant_miss'`
database.py:2081-2084  `WHERE outcome IN ('success','relevant_miss')` / `  AND normal_feeds_complete=1` / `  AND rss_requests>0` / `  AND listing_requests>0`
```

**Verifier.** Confirmed. The listing-arm failure signal is structurally unable to reach the shadow flag. (a) `err` is set only if `_scan_source` raises, but `run_scan` swallows everything: `scanner_service.py:376-378` `except Exception as e: self._log(f"Scan error: {e}", "error")` ... `return list(self.items)`, and the per-page handler at `scanner_service.py:905-906` `except Exception as e: self._log(f"Crawl error: {e}", "error")` plus the 403/429/503 branch that only `await asyncio.sleep(min(0.5 * blocked_streak, 3.0))` then `continue`. So `background_scanner.py:470` `listing_error=err` is None for every fetch/block failure. (b) `listing_requests` is still >0 because `scanner_service.py:766` `self._last_crawl_request_count += 1` increments BEFORE `get_hdencode_coordinator().request(...)` and before `client.get`, so denied/403 pages count. (c) Smoking gun: the code already computes incompleteness and routes it only to purge safety — `scanner_service.py:908-913` `if blocked_total:` / `# A blocked source's crawl is INCOMPLETE` / `early_stopped = True`, exposed at 948 as `self._last_crawl_early_stopped = early_stopped`, and grep shows exactly ONE consumer in backend/: `background_scanner.py:444-445` `if getattr(scanner, "_last_crawl_early_stopped", False): purge_safe = False`. It never reaches `compare_shadow`. (d) Ordering makes it likely, not exotic: the RSS poll runs first (`background_scann

**Suggested fix.** Give the listing arm a real completion signal instead of relying on `err`. In `scanner_service.py`, alongside `self._last_crawl_early_stopped = early_stopped` (line 948), record the crawl's health explicitly: count pages that returned HTTP 200 (`ok_pages += 1` right after `blocked_streak = 0` at line ~814) and persist `self._last_crawl_ok_pages = ok_pages` and `self._last_crawl_blocked_pages = blocked_total`. Then in `background_scanner.py:461-472`, compute `listing_incomplete = err or getattr(scanner, "_last_crawl_blocked_pages", 0) > 0 or getattr(scanner, "_last_crawl_ok_pages", 0) == 0` and pass `listing_error=listing_incomplete` — this also covers the swallowed-`run_scan`-exception path, where a hard scan failure returns items=[] with err=None. Optionally add a distinct `outcome='incomplete_listing'` in `hdencode_shadow.compare_shadow` so the row is still recorded for forensics but is excluded by the existing `outcome IN ('success','relevant_miss')` eligibility filter at `database.

---

### 7. `backend/background_scanner.py:405`  ✅ FIXED — purge_safe=False when the listing is skipped or only partially visited

*scan-pipeline*

**What breaks.** In rss_primary mode the HDEncode listing source is skipped without setting purge_safe=False, so every cycle purges a cache that nothing refreshed -- the entire cached catalogue silently ages out.

**How it happens.** hdencode_discovery_mode="rss_primary" with fallback not qualified (the normal, healthy case). The source loop appends {"skipped": "rss_primary"} and `continue`s BEFORE the touch_background_cache/purge_safe block at 440-445, so last_seen_at is never refreshed for any cached HDEncode row -- and unlike the disabled-source branch three blocks above (which explicitly sets purge_safe = False with the comment "A disabled source is intentionally not visited"), this branch leaves purge_safe True. Nothing else writes background_scan_cache in rss_primary mode (RSS candidates go to hdencode_candidates; update_background_status deliberately does not touch last_seen). After background_scan_retain_days (default 7) db.purge_background_cache deletes every row, the browse view (results.py reads get_background_cache()) goes empty, and the run still logs "Background scan complete". The 1-page fallback crawl (source_pages = 1 at line 412) has the same partial-visit problem.

```python
background_scanner.py:400-411  `if is_hdencode and discovery_mode == "rss_primary":` / `    if not (` / `        rss_cycle` / `        and rss_cycle.get("fallback_qualified")` / `    ):` / `        source_results.append({` / `            "source": source,` / `            "new": 0,` / `            "error": None,` / `            "skipped": "rss_primary",` / `        })` / `        continue`
background_scanner.py:396-397 (the branch that gets it right)  `                    purge_safe = False` / `                    continue`
background_scanner.py:522-527  `else:` / `    try:` / `        retain = max(1, int(cfg.get("background_scan_retain_days", 7)))` / `    except (TypeError, ValueError):` / `        retain = 7` / `    db.purge_background_cache(retain)`
```

**Verifier.** CONFIRMED. The rss_primary skip at background_scanner.py:400-411 appends `{"skipped": "rss_primary"}` and `continue`s with no `purge_safe = False`, unlike the disabled-source branch 15 lines above which does `purge_safe = False` / `continue`. Only three writes to purge_safe exist in the file (grep): `purge_safe = True` (282), disabled-source (396), early-stop (445) — the rss_primary path hits none, so line 517 `if not purge_safe:` falls through to `db.purge_background_cache(retain)` (527).

The strongest candidate refutation — `scanner.rematch_cache()` at line 509, which runs over the WHOLE cache every cycle — is explicitly ruled out by its own contract: "Only rows whose status/info actually changed are written, and ``last_seen`` is left untouched so retention is unaffected" (scanner_service.py:1218), and its writer is `"UPDATE background_scan_cache SET status = :status, data = :data "` (database.py:4522) — no timestamp. There is even a pinning test, `test_update_background_status_preserves_last_seen`. Grepping the whole tree, the only production writers of last_seen_at are background_scanner.py:443 (`touch_background_cache`, skipped by the `continue`) and :493 (`upsert_background_cache`, never reached because no crawl ran), plus the single-item rescan route api/routes/scanner.py:458. RSS output lands in `hdencode_candidates`, a different table.

Worse than the finding states i

**Suggested fix.** Two one-line additions in backend/background_scanner.py, mirroring the disabled-source precedent at 396:

1. In the rss_primary skip branch, before `continue` (line 411):
       source_results.append({... "skipped": "rss_primary"})
       purge_safe = False   # HDEncode listing is intentionally not visited in rss_primary
       continue

2. For the qualified 1-page fallback (line 412-413), which is also a deliberate partial visit — `source_pages = 1` while `pages` defaults to 3 — so its seen-set cannot justify aging out deeper rows:
       source_pages = 1
       rss_cycle["listing_fallback_started"] = True
       purge_safe = False

Optional follow-up (better, larger): make retention source-scoped — `purge_background_cache(retain, sources=<fully-crawled sources>)` filtering on source_category — so one non-visited source stops aging out only its own rows instead of globally disabling the purge. Note that with fix 1 alone, an rss_primary + background_scan_enabled=False deployment never 

---

### 8. `backend/config.py:325`  ✅ FIXED — checkpoint verified (checkpointed==log) + copy verified by row counts

*db-integrity*

**What breaks.** The one-time DB relocation migration never checks the result of PRAGMA wal_checkpoint(TRUNCATE) and never verifies the copy, so a partially-checkpointed source produces a stale/empty destination DB while printing 'Migrated DB ... ->' and returning True. REPRODUCED: 501 rows in the source, the migrated copy had no 'downloads' table at all.

**How it happens.** SCANHOUND_DB_DIR is set (or changed) and /dbvol/crawler.db does not yet exist -> _resolve_db_path runs _checkpoint_and_copy at import time of backend.config. If ANY other connection holds an older read snapshot on the legacy file at that moment (a second container overlapping during `up -d --build`, the desktop UI, scripts/import_dv_seed.py, docs/.../02_migration_matrix.py), the checkpoint can only fold frames older than that snapshot. Measured: `PRAGMA wal_checkpoint(TRUNCATE)` returned (busy=1, log=1011, checkpointed=5) -- 5 of 1011 frames folded. copy2 then captures a main file that predates almost everything, os.replace makes it authoritative, the function returns True, and startup prints 'Migrated DB legacy -> new'. DatabaseManager.init_db() then runs PRAGMA integrity_check on the copy, which passes ('ok' -- it is internally consistent, just old), recreates the missing tables, and the app comes up with an empty download history, empty dismissals, empty plex_cache and empty rename_jobs. Unattended, every previously grabbed release is now eligible to be grabbed again. The legacy file survives untouched so the data is recoverable, but nothing signals that recovery is needed.

```python
conn = sqlite3.connect(legacy_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
    finally:
        conn.close()

    temp_path = new_path + ".migrating"
    if os.path.exists(temp_path):
        os.remove(temp_path)  # stale leftover from a prior interrupted attempt
    shutil.copy2(legacy_path, temp_path)
    os.replace(temp_path, new_path)
    return True
```

**Verifier.** Refutation attempt failed — the defect is real and I reproduced it. backend/config.py:323-335 discards the pragma result: `conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")` / `conn.commit()` ... `shutil.copy2(legacy_path, temp_path)` / `os.replace(temp_path, new_path)` / `return True`. No `fetchone()`, no busy/log/checkpointed inspection, no post-copy verification, and no `PRAGMA busy_timeout` on this connection (unlike backend/database.py:108 `self.conn.execute("PRAGMA busy_timeout=5000")`), so a competing reader causes an instant give-up. Live repro of that exact code body with a stale reader snapshot: `wal_checkpoint(TRUNCATE) -> (1, 506, 0)`, migration reported success, `tables in migrated copy: []`, `no such table: downloads`, `integrity_check on migrated copy: ok`, `rows still in SOURCE: 501`. The `integrity_check` result proves the downstream guard at backend/database.py:250 cannot detect it, and `_resolve_db_path:351` `if os.path.exists(new_path): return new_path` freezes the stale copy in permanently. docker/entrypoint.sh does no verification, and tests/test_config_db_dir.py covers copy-failure and crash-before-replace but never a busy/partial checkpoint. One correction to the finder: a concurrent reader alone is not enough — with a current snapshot I measured `(1, 6, 6)`, busy but fully folded and all 501 rows intact; the loss needs a reader whose snapshot predates som

**Suggested fix.** In `_checkpoint_and_copy` (backend/config.py:304-335): (1) set `PRAGMA busy_timeout=5000` on the checkpoint connection before the pragma, and open it with `isolation_level=None`; (2) fetch and assert the result — `busy, log, ckpt = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()`, and if `busy` or `ckpt != log` (with `log > 0`), retry a few times and then `raise RuntimeError(f"incomplete checkpoint: busy={busy} log={log} checkpointed={ckpt}")`; (3) after `shutil.copy2` and before `os.replace`, verify the temp copy against the source — open both read-only and compare `PRAGMA user_version` plus `SELECT count(*) FROM sqlite_master` (or per-table row counts for `downloads`/`dismissals`), raising on mismatch so the temp file is discarded. Raising is the correct outcome: `_resolve_db_path:362-370` already catches, logs loudly, and falls back to `legacy_path`, so a rejected migration degrades to "keep using the legacy DB" instead of silently adopting a stale one. A retry-safe alter

---

### 9. `backend/database.py:4135`  ✅ FIXED in `d749bb2`

*db-integrity*

**What breaks.** upsert_media_inventory (4135), upsert_dv_scan (4360) and upsert_media_probe (4483) all return `self._mutate(...) is not None`. _mutate returns True or False and NEVER None, so all three return True even when the write failed -- every caller's failure guard is dead code. REPRODUCED: with the target tables dropped, _mutate returned False while all three helpers returned True.

**How it happens.** A durable metadata scan is running. The media_inventory INSERT fails (disk full, 'database is locked' after the 5s busy_timeout on the bind-mounted volume, or a CHECK violation). _mutate catches it, logs one generic 'DB Error (upsert_media_inventory)' line, returns False -- and the helper converts that to True. plex_metadata_scan.py:314 `if not self._db.upsert_media_inventory({...}): ...status='failed', error_code='inventory_write_failed'` therefore never fires; line 325 runs instead and records the item as status='current', and self.succeeded += 1. The run reports success, the item is durably marked scanned so no resume or retry will ever pick it up, and search_media_inventory returns nothing for that file forever. Same dead guard at plex_metadata_scan.py:301 (upsert_dv_scan -> 'dv_cache_write_failed') and :371 (_record_failed_inventory's 'could not persist failed inventory state' error log). The only trace is that one generic DB Error line, which directly contradicts the item status and the success counter.

```python
), label="upsert_media_inventory") is not None
```

**Verifier.** REFUTATION FAILED — reproduced. `_mutate` (backend/database.py:174) has three exits and none is None: `if not conn: return False` / `conn.commit(); return True` / `except Exception as e: logger.error("DB Error (%s): %s", label, e); return False`. The three helpers end with `... label="upsert_media_inventory") is not None` (:4135), `... label="upsert_dv_scan") is not None` (:4360), `... label="upsert_media_probe") is not None` (:4483), so `False is not None` -> True. Live repro against the real DatabaseManager with the target tables dropped: the log shows `DB Error (upsert_media_inventory): no such table: media_inventory` while the helper returned True; the same failing statement through `db._mutate(...)` returned False, and the control helper `clear_dv_scans` (which returns `_mutate` directly) returned False. DatabaseManager is the only class in the file and `_mutate` is never overridden outside tests; no caller re-reads to verify. The guards are real and load-bearing: plex_metadata_scan.py:314 `if not self._db.upsert_media_inventory({...})` -> `error_code="inventory_write_failed"`, :301 -> `dv_cache_write_failed`, :371 `logger.error("could not persist failed inventory state for %s", path)`, and :443 `return bool(self._db.upsert_dv_scan(...))`. The codebase documents the very contract these break at backend/rename/service.py:1696: "SH-H08: DatabaseManager._mutate returns False 

**Suggested fix.** Delete the ` is not None` suffix at backend/database.py:4135, :4360 and :4483 so each helper returns the `_mutate` boolean directly — e.g. `), label="upsert_media_inventory")` — matching every other helper in the file (`clear_dv_scans` at :4467 is the pattern). Then add a regression test that monkeypatches/forces `_mutate` to False (or drops the table) and asserts each of the three helpers returns False, plus a test that plex_metadata_scan records status='failed' with error_code='inventory_write_failed' and does not increment `succeeded` when the inventory write fails. Pick the assertion so an inverted implementation fails: assert `is False`, not just falsy, since the pre-fix value is True.

---

### 10. `backend/database.py:1246`  ✅ FIXED — WAL checkpointed first; sidecars moved WITH the backup

*db-integrity*

**What breaks.** The corruption quarantine renames only the main DB file, leaving the -wal/-shm sidecars behind under the original name; the 'backup' it points the operator at can therefore be missing every transaction not yet checkpointed, and the stranded WAL is then reset by the fresh DB created at that same path. REPRODUCED: 300 committed rows, backup file opened afterwards had zero tables.

**How it happens.** The process is SIGKILLed (container recreate, OOM) leaving crawler.db-wal holding the last N commits; on restart PRAGMA integrity_check fails, so _quarantine_corrupt_db runs. `self.conn.close()` normally checkpoints the WAL away -- but it is wrapped in `except sqlite3.Error: pass`, and this code path only executes when the DB is corrupt, i.e. exactly when the close-time checkpoint is likely to fail. os.rename then moves ONLY crawler.db to crawler.db.corrupt.<ts>. crawler.db-wal and -shm stay under the old name, where init_db() immediately creates a new database and resets them. Verified end-to-end: a 20,632-byte orphan WAL containing all 300 rows was consumed by the fresh DB, and crawler.db.corrupt.1 returned 'no such table: downloads'. The corruption itself is logged loudly, but nothing anywhere says the preserved backup is truncated -- the operator is told 'Renamed corrupt DB to <path>. Creating fresh DB.' and reasonably believes the data is safe there.

```python
if os.path.exists(self.db_path):
            backup_name = f"{self.db_path}.corrupt.{int(time.time())}"
            try:
                os.rename(self.db_path, backup_name)
                logger.warning("Renamed corrupt DB to %s. Creating fresh DB.", backup_name)
```

**Verifier.** NOT REFUTED — reproduced end-to-end against the real DatabaseManager, though the finder's stated trigger and mechanism are both partly wrong.

The code at backend/database.py:1243-1248 renames one path and nothing else:

    if os.path.exists(self.db_path):
        backup_name = f"{self.db_path}.corrupt.{int(time.time())}"
        try:
            os.rename(self.db_path, backup_name)
            logger.warning("Renamed corrupt DB to %s. Creating fresh DB.", backup_name)

with the preceding close swallowing failure:

    if self.conn:
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

get_connection() confirms WAL mode is in force: `self.conn.execute("PRAGMA journal_mode=WAL")`.

E2E against the real class (real schema, writer process committing 300 rows then `os._exit(0)` for a 45,352-byte hot WAL, main file truncated by two pages to simulate a partial write on the bind mount this code's own comments cite as "a flaky bind-mounted filesystem"):
  LOG WARNING Renamed corrupt DB to ...e2e.db.corrupt.1785886652. Creating fresh DB.
  sidecars left under original name: ['e2e.db-shm', 'e2e.db-wal']
  BACKUP downloads -> database disk image is malformed
  LIVE  downloads rows -> 0
  corruption flag -> e2e.db.corrupt.1785886652
Final directory state after the app closes: only e2e.db, e2e.db.corrupt.1785886652, and the flag JSON — no sidecars. The 

**Suggested fix.** In `_quarantine_corrupt_db` (backend/database.py ~1236-1252): (1) explicitly checkpoint before closing, and (2) move the sidecars with the backup — the codebase already does exactly this in `config.py:_migrate_db` ("Also move WAL/SHM sidecars if present"), so this is restoring an existing convention, not inventing one.

    if self.conn:
        try:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            wal_folded = True
        except sqlite3.Error:
            wal_folded = False
            logger.warning("Could not checkpoint WAL before quarantine; "
                           "preserving sidecars alongside the backup")
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        self.conn = None

    if os.path.exists(self.db_path):
        backup_name = f"{self.db_path}.corrupt.{int(time.time())}"
        try:
            os.rename(self.db_path, backup_name)
            preserved = []
            for suffix in ("-wal", "-

---

### 11. `backend/download_service.py:2773`  ✅ FIXED in `ed1e1ec` (guarded in `database.add_to_history`)

*download-path*

**What breaks.** A failed forced re-grab overwrites the existing downloads row with status='failed', destroying the record of the earlier SUCCESSFUL grab — the release stops counting as downloaded and loses its duplicate protection.

**How it happens.** downloads row for URL X exists with status='completed' (a real, delivered grab). User clicks Regrab in the pipeline tracker (routes/pipeline.py:86 calls _run_grab(..., force=True), which skips both dedup gates). Scrape succeeds, but JDownloader is unreachable, so send_to_jdownloader() returns False and download_item falls through to the block below, calling save_to_history(status='failed'). database.py:2796 (`status = excluded.status`) is an unconditional ON CONFLICT(url) overwrite, so the row flips completed -> failed. VERIFIED by execution against a real DB: before = is_downloaded True, get_downloaded_urls {X}, get_downloaded_title_quality [('magellan',2026,None,'2160p',1)]; after the failed regrab = is_downloaded False, get_downloaded_urls set(), get_downloaded_title_quality []. Consequences, none of which are reported: the release stops rendering as Downloaded in results, the pipeline reconcile loses it, and _best_prior_grab() no longer blocks a re-grab, so the next auto-grab or batch re-downloads a 60 GB file already on disk. The regrab failure itself is notified; the destruction of the prior success record is not.

```python
try:
            self.save_to_history(url, title, season, resolution, size,
                                 status="failed", hdr=hdr, dovi=dovi, year=year,
                                 package_name=package_name, service_type=service_type)
        except Exception:
            pass
```

**Verifier.** Confirmed, with two sub-claims corrected. The chain is unbroken: pipeline.py:86 `background_tasks.add_task(_run_grab, dl, reg, dl_req, True)` re-grabs THE SAME url; download_service.py:2596 `if self.db is not None and not force:` is the guard that would otherwise short-circuit an already-completed URL before any scrape, and force=True skips it; download_service.py:2773 `self.save_to_history(url, title, season, resolution, size, status="failed", ...)`; database.py:2796 `status = excluded.status,` is unconditional under `ON CONFLICT(url) DO UPDATE SET` on a `url TEXT PRIMARY KEY` table (database.py:265). Every "was this downloaded" consumer then drops the row: is_downloaded (database.py:3039) `SELECT 1 FROM downloads WHERE url = ? AND COALESCE(status, 'completed') != 'failed'`, get_downloaded_urls (3049), get_downloaded_title_quality (3018) — feeding the read-time overlay at api/routes/results.py:119/136 and the title-level dedup gate _best_prior_grab (download_service.py:501). No `UPDATE downloads SET status` exists anywhere in backend/, so nothing repairs it. I reproduced the exact add_to_history SQL in sqlite: BEFORE is_downloaded=True / urls={'http://x/rel'} / title_quality=[('magellan',2026,None,'2160p',1)] -> AFTER is_downloaded=False / urls=set() / title_quality=[]. CORRECTIONS to the finding: (1) "the pipeline reconcile loses it" is FALSE — get_downloads_needing_reconcile

**Suggested fix.** Make the demotion impossible at the SQL layer so both call sites (download_service.py:2773 and api/routes/downloads.py:157) are covered. In database.py add_to_history, replace `status = excluded.status,` with a guard that never lets a 'failed' write overwrite an existing non-failed row: `status = CASE WHEN excluded.status = 'failed' AND COALESCE(downloads.status,'completed') != 'failed' THEN downloads.status ELSE excluded.status END`. Do the same for the metadata fields the failed attempt would clobber (title/resolution/size/hdr/dovi) — only overwrite them when the incoming status is not 'failed' — so a failed regrab cannot rewrite the recorded quality of a delivered grab. Keep `last_grabbed_at = CURRENT_TIMESTAMP` unconditional (intentional per the docstring). Add a regression test asserting: seed url X status='completed' 2160p, call add_to_history(X, status='failed'), then assert is_downloaded(X) is True, X in get_downloaded_urls(), and get_downloaded_title_quality() still returns th

---

### 12. `backend/plex_service.py:245`  ✅ FIXED — an unresolved library marks the load incomplete, blocking the prune

*plex-sync*

**What breaks.** A Plex library whose section lookup returns None (any exception: timeout, NotFound after a rename, auth blip) is skipped WITHOUT setting movies_load_incomplete, so the load is still treated as complete and save_plex_cache(full_replace=True) prunes every cached row belonging to that library.

**How it happens.** config movie_libs = ["Movies 1080p", "Movies 4K"]. During a Deep Scan the section fetch for "Movies 4K" raises (Plex busy/restarting, or the library was renamed in Plex). plex_manager.get_library_section swallows it and returns None (plex_manager.py:432-434). The loop logs "Movie library 'Movies 4K' not found" and continues; movies_load_incomplete stays False because only the `except` branch sets it. _movies now holds ONLY the 1080p library, is non-empty, so line 427 runs save_plex_cache(..., full_replace=True), whose prune is content_type-wide, not library-scoped (database.py:2603-2615: `SELECT key FROM plex_cache WHERE content_type = ?` then delete everything not in the fresh set). Every 4K row is deleted from plex_cache and from the in-memory index. Result: every 4K movie the user owns now reports Missing (or 'UPGRADE to 4K'), the durable metadata-scan target list loses them, find_library_duplicate can no longer see them, and with auto_grab_statuses defaulting to "missing,upgrade,dv_upgrade" an enabled auto-grab re-downloads owned films. If the cause is a rename or a stale name in movie_libs, the deletion repeats every run and is permanent. I reproduced this by driving the unmodified load_libraries with two libraries where the second section lookup returns None: save_plex_cache(Movies) was called once with full_replace=True carrying only library 1's item, and the run's final log line was `[success] Loaded Plex: 1 movies, 0 TV seasons`. The identical hole exists for a library that returns zero items (line 251-252 logs "may be a Plex connection issue" and then does nothing about it — also reproduced, full_replace=True) and for TV at lines 302-304. The project's own tests enshrine the opposite intent ("Partial library load must not full_replace the cache", tests/test_plex_service.py:643) but only ever configure ONE library, so the multi-library skip path is untested.

```python
backend/plex_service.py:244-247
                    lib = self.plex_manager.get_library_section(lib_name)
                    if not lib:
                        self._log(f"Movie library '{lib_name}' not found", "warning")
                        continue

backend/plex_service.py:251-252
                    if total_items == 0:
                        self._log(f"Movie library '{lib_name}' returned 0 items — may be a Plex connection issue", "warning")

(neither sets movies_load_incomplete; only the per-library `except` at 285-287 does)

backend/plex_service.py:426-427
                if self.plex_movies and not movies_load_incomplete:
                    self.db.save_plex_cache(self.plex_movies, "Movies", full_replace=True)

backend/plex_manager.py:430-434
        try:
            return self._server.library.section(name)
        except Exception as e:
            logger.error(f"Failed 
```

**Verifier.** Confirmed and reproduced. backend/plex_service.py:243-247 skips a failed library without marking the load incomplete: `lib = self.plex_manager.get_library_section(lib_name)` / `if not lib:` / `self._log(f"Movie library '{lib_name}' not found", "warning")` / `continue`. `movies_load_incomplete` is set ONLY at line 287 (`except Exception as e: ... movies_load_incomplete = True`) and line 293 (`if movie_extract_fail:`), neither of which the `continue` reaches. plex_manager.py:430-434 swallows every exception (`except Exception as e: logger.error(...); return None`), so a timeout / NotFound-after-rename / auth blip lands on the skip path, not the except. The gate at plex_service.py:426-427 then fires: `if self.plex_movies and not movies_load_incomplete: self.db.save_plex_cache(self.plex_movies, "Movies", full_replace=True)`. The prune in database.py:2605-2608 is content_type-wide with no library scoping: `all_existing = cursor.execute("SELECT key FROM plex_cache WHERE content_type = ?", (mode,)).fetchall()` then `stale_keys = [row[0] for row in all_existing if row[0] not in fresh_db_keys]` -> DELETE. I drove the unmodified load_libraries with movie_libs = ["Movies 1080p", "Movies 4K"] where the 2nd section lookup returns None: exactly one save_plex_cache call, mode "Movies", full_replace=True, items list length 1 (only library_name='Movies 1080p'). The zero-items variant (lines 251

**Suggested fix.** Two layers; do at least the first.

1) Minimal, targeted (plex_service.py). Mark the content type incomplete whenever a configured library fails to resolve, so the existing gate at 426/436 refuses the full-replace:
   - line 245-247 (movies): `if not lib: self._log(...); movies_load_incomplete = True; continue`
   - line 302-304 (TV): `if not lib: self._log(...); tv_load_incomplete = True; continue`
   Do NOT blanket-set the flag on `total_items == 0` (line 251) — a legitimately empty library would then permanently disable pruning for that user. Handle that case in layer 2 instead.

2) Correct fix (removes the whole class of bug): make the prune library-scoped instead of content-type-wide. Accumulate `loaded_movie_libs`/`loaded_tv_libs` (only names whose section resolved AND whose iteration finished) and pass them to save_plex_cache; change database.py:2605-2608 from
     SELECT key FROM plex_cache WHERE content_type = ?
   to
     SELECT key FROM plex_cache WHERE content_type = ? AND 

---

### 13. `backend/plex_service.py:163`  ✅ FIXED — cache accepted only when every CONFIGURED content type is present

*plex-sync*

**What breaks.** A cache containing only ONE content type is accepted as a complete, authoritative index: load_libraries returns early on `cached_movies or cached_tv`, and check_cache_status reports the cache valid while only checking the age of the content types that happen to exist.

**How it happens.** plex_cache holds Movies rows but no TV Shows rows (e.g. the user assigned TV libraries after the movie cache was already built, or TV rows were pruned by the defect above). On startup, backend/api/main.py:237 calls load_libraries(use_cache=True): cached_tv is [] but cached_movies is not, so the `or` passes, self.plex_tv is set to [], tv_seasons is recorded as 0, the index is built with zero TV items, and the method returns without ever falling back to a live load. The log line is `[success] Loaded Cache: N movies, 0 seasons` — a success. The loud 'library is empty' alarm at scanner_service.py:458 never fires because it requires BOTH lists to be empty. check_cache_status then returns (True, "") because `ages` only contains the Movies entry (verified: with timestamps={"Movies": now-600} it returns (True, '')), so an Incremental scan keeps using it; and with plex_refresh_mode="auto" even a Deep Scan started within 5 minutes skips the reload (scanner_service.py:439-444). Every TV item in the scan is matched against an index with no TV and reported Missing. Worse, rematch_cache's guard is all-or-nothing — `have_plex = bool(self.plex and self.plex.plex_index.get("all_items"))` (scanner_service.py:1235) is True because of the movies, so it wipes plex_info/plex_versions and downgrades every previously IN_LIBRARY TV row to Missing and writes that to the background cache. I reproduced the load path: plex_tv == [], tv_seasons == 0, index all_items == 1, get_library_section never called, final log level 'success'.

```python
backend/plex_service.py:160-176
                cached_movies = self.db.load_plex_cache("Movies")
                cached_tv = self.db.load_plex_cache("TV Shows")

                if cached_movies or cached_tv:
                    self.plex_movies = cached_movies
                    self.plex_tv = cached_tv
                    ...
                    self._log(
                        f"Loaded Cache: {len(self.plex_movies)} movies, {self.stats['tv_seasons']} seasons",
                        "success",
                    )
                    return

backend/plex_service.py:675-684
            # Only check ages for content types actually present in cache
            ages = {}
            if timestamps.get("Movies"):
                ages["movies"] = _age(timestamps["Movies"])
            if timestamps.get("TV Shows"):
                ages["tv"] = _age(timestamps["TV Shows"])
            i
```

**Verifier.** The defect is real and no guard exists. (1) The cache read path never consults the configured libraries: `cached_movies = self.db.load_plex_cache("Movies"); cached_tv = self.db.load_plex_cache("TV Shows"); if cached_movies or cached_tv: ... return` (backend/plex_service.py:160-176) — `tv_libs` is only read on the full-load path below (line 188), so a movies-only cache is accepted as authoritative even when TV libraries are configured. (2) The state is reachable and self-perpetuating: `clear_plex_cache` has exactly ONE caller in the entire tree — `ui/controllers/settings_controller.py:686` (`purgeCache`, the legacy Qt manual maintenance button). The web API library-assignment handler neither clears the cache nor reloads: `if body.tv_libraries is not None: reg.config["tv_libs"] = body.tv_libraries; ... return {"status": "ok"}` (backend/api/routes/plex.py:119-122), and `update_settings` just does `reg.config.update(real_updates)` (backend/api/routes/settings.py:310). The shipped default `"tv_libs": ["TV Shows"]` (backend/config.py:444) means a new install whose Plex TV section is named anything else takes `"TV library '{lib_name}' not found" ... continue` → `_tv == []` → `"Skipping TV Shows cache save — load returned 0 (preserving existing cache)"` (plex_service.py:437-438), producing a permanent movies-only cache. (3) The write side enforces exactly the invariant the read side om

**Suggested fix.** Make cache validity per-content-type, mirroring the protections the write path already has. (1) In `PlexService.load_libraries` cache path, resolve the configured types first (`movie_libs`/`tv_libs` with the same `known_*` legacy fallbacks used at lines 183-191) and require the cache to cover each configured type: `expect_movies = bool(movie_libs); expect_tv = bool(tv_libs)`; only take the early return when `(not expect_movies or cached_movies) and (not expect_tv or cached_tv)`. Otherwise log at "warning" (e.g. "Plex cache has no TV Shows rows but TV libraries are configured — falling back to a full load") and fall through to the full load instead of returning. (2) In `check_cache_status`, treat a configured-but-absent content type as invalid: after building `ages`, add `if tv_libs and not timestamps.get("TV Shows"): return False, "TV Shows cache missing. Full load required."` and the symmetric check for Movies — replacing the current "only check ages for content types actually present

---

## MEDIUM

### 14. `backend/api/routes/auth.py:122`  ✅ FIXED — login fails 500 when the session was never persisted

*auth-surface*

**What breaks.** `login` ignores the return value of `create_session()` and returns 200 with a token even when the session was never persisted, producing a "successful" login whose token 401s on every subsequent request.

**How it happens.** `create_session` is `_mutate(...)` -> returns False on any write exception. Verified by calling the real route with a stub whose `create_session` returns False: the route responds 200 `{'token': '<32-byte token>', 'expires_at': '...'}`, the client stores it (`setStoredToken` + `authRequired.set(true)`), and `token_authorized(<that token>)` then returns False. Every protected fetch 401s, `client.ts:84` fires `unauthorizedHandler()` which clears the token and redirects to /login, and the login page shows no error because the login call itself succeeded — an unattended user sees an endless login loop with the correct password and no diagnostic anywhere except a `DB Error (create_session)` line in the log.

```python
token = auth_service.new_session_token()
    expires_at = auth_service.session_expiry()
    reg.db.create_session(auth_service.hash_token(token), expires_at)
    reg.db.purge_expired_sessions(auth_service.now_iso())  # opportunistic cleanup
    return {"token": token, "expires_at": expires_at}
```

**Verifier.** Confirmed, not refuted. The route discards the boolean: `reg.db.create_session(auth_service.hash_token(token), expires_at)` followed unconditionally by `return {"token": token, "expires_at": expires_at}` (backend/api/routes/auth.py:118-124). `create_session` is `return self._mutate(... label="create_session")` and `_mutate` is `except Exception as e: logger.error("DB Error (%s): %s", label, e); return False` (backend/database.py:174-193, 3640-3646). The only candidate guard, `if not reg.db or not reg.db.has_password(): raise HTTPException(400)`, is a READ (`_query`) and does not cover a write failure — proven on the real DatabaseManager with `PRAGMA query_only=ON`: `has_password() -> True`, `create_session() -> False`, `get_session_expiry() -> None`, log line `DB Error (create_session): attempt to write a readonly database`. Calling the real route function with that condition returns 200 `{'token': 'VbV2EKBQ...', 'expires_at': ...}` while `dependencies.token_authorized(<that token>)` returns False, since it resolves via `db.get_session_expiry(auth_service.hash_token(token))`. Consumer chain verified: `auth.ts:41` `const { token } = await api.authLogin(password); setStoredToken(token)` -> `login/+page.svelte` `window.location.href = '/'` -> protected route -> `_request_requires_auth` -> 401 -> `client.ts:84` `unauthorizedHandler?.()` -> token cleared -> `/login` with no error. R

**Suggested fix.** Backend (backend/api/routes/auth.py, in `login`): capture and check the write, e.g. `if not reg.db.create_session(auth_service.hash_token(token), expires_at): raise HTTPException(status_code=500, detail="Could not persist the login session; the database is not writable")`. Leave `purge_expired_sessions` unchecked — it is genuinely opportunistic. Frontend (frontend/src/routes/login/+page.svelte): the backend fix alone is not enough, because `catch { error = 'Incorrect password. Please try again.'; }` relabels every failure as a bad password; change it to `catch (e) { error = e instanceof Error && e.message ? e.message : 'Incorrect password. Please try again.'; }` so the 500's `detail` (already extracted by `formatErrorDetail` in client.ts) reaches the user. Same ignored-return pattern sits at `set_password`: `reg.db.set_password_hash(...)` then `return {"ok": True}` — a failed write there reports success and leaves the user unable to log in ("No password is configured"); worth the ident

---

### 15. `backend/api/routes/scanner.py:249`  ✅ FIXED — auto-grab reports the AutoGrabReport counts, not a hardcoded 0

*scan-pipeline*

**What breaks.** The auto-grab completion counter always broadcasts grabbed=0, because process_items returns an AutoGrabReport and the route type-checks for int -- so unattended grabs produce no user-visible confirmation at all.

**How it happens.** auto_grab_enabled is on and a scan qualifies 12 items. AutoGrabService.process_items downloads them and returns an AutoGrabReport(grabbed=12); the route's `grabbed if isinstance(grabbed, int) else 0` evaluates False and broadcasts {"grabbed": 0, "total": N}. frontend/src/lib/stores/scanner.ts:118-124 only toasts `if (grabbed > 0)`, so the toast never fires. AutoGrabService.set_log_callback is never called on the server (only the definition at auto_grab_service.py:41 exists; no caller in backend/), so its per-item "Grabbed ..." lines never reach the WebSocket log either. Twelve downloads start with the UI reporting nothing -- and the same 0 would be reported if auto-grab had genuinely grabbed nothing, so the two states are indistinguishable.

```python
api/routes/scanner.py:246-250  `grabbed = reg.auto_grab.process_items(items)` / `ws_manager.broadcast_sync({` / `    "type": "autograb:complete",` / `    "data": {"grabbed": grabbed if isinstance(grabbed, int) else 0, "total": len(items)},` / `})`
auto_grab_service.py:130-139  `def process_items(self, items: List[MediaItem]) -> AutoGrabReport:` ... `report = AutoGrabReport()`
frontend/src/lib/stores/scanner.ts:119-123  `const grabbed = (data.grabbed as number) || 0;` / ... / `if (grabbed > 0) {`
```

**Verifier.** CONFIRMED. auto_grab_service.py:130-139 `def process_items(self, items: List[MediaItem]) -> AutoGrabReport:` / `report = AutoGrabReport()` — AutoGrabReport is a plain @dataclass (line 18-30), never an int, so at scanner.py:249 `"grabbed": grabbed if isinstance(grabbed, int) else 0` is unconditionally False and the payload is always 0. reg.auto_grab is confirmed to be that exact class: api/main.py:200-201 `auto_grab_svc = AutoGrabService(backend.config, download_svc)` / `reg._auto_grab_service = auto_grab_svc`, returned by dependencies.py:138-139; scanner.py:246 is the ONLY caller of process_items in backend/, so no wrapper coerces the report. Frontend suppression confirmed: scanner.ts:119-121 `const grabbed = (data.grabbed as number) || 0;` ... `if (grabbed > 0) {` — the completion toast can never fire. The log fallback is genuinely unwired: the only set_log_callback call in backend/ is scanner.py:178 `scanner.set_log_callback(_log_callback)` (ScannerService), so AutoGrabService._log_fn stays None and its "Auto-Grab: Grabbed ..." lines reach only the rotating file/console logger; InMemoryLogBuffer (app_service.py:388) is read solely by ui/controllers/settings_controller.py:658, the desktop QML path, with no HTTP/WS route exposing it. Two mitigations the finder omitted, neither of which refutes: scanner.py:242-245 does fire an `autograb:started` toast, but with count=len(items) 

**Suggested fix.** In backend/api/routes/scanner.py:246-250, read the report's fields instead of type-checking for int, and surface failures too:

    report = reg.auto_grab.process_items(items)
    grabbed = getattr(report, "grabbed", report if isinstance(report, int) else 0)
    ws_manager.broadcast_sync({
        "type": "autograb:complete",
        "data": {
            "grabbed": grabbed,
            "failed": getattr(report, "failed", 0),
            "evaluated": getattr(report, "evaluated", len(items)),
            "total": len(items),
        },
    })

In backend/api/main.py right after line 201, wire the service's log callback to the same WS "log" forwarder the scanner uses (backend/api/routes/scanner.py:129-134 `_log_callback`) — lift it to a shared helper (e.g. backend/api/ws.py) and call `auto_grab_svc.set_log_callback(_log_callback)` so per-item "Auto-Grab: Grabbed ..." lines reach the UI log.

In frontend/src/lib/stores/scanner.ts:118-124, add an else branch so a completed run with grabbed

---

### 16. `backend/api/ws.py:139`  ✅ FIXED — sockets re-check their credential periodically, so logout reaches them

*auth-surface*

**What breaks.** The WebSocket is authorized once at handshake and never re-checked; logout, password change, and session expiry do not close established sockets, so revocation does not apply to the transport that streams every result.

**How it happens.** An attacker with a token (leaked from an nginx/Cloudflare access log — see the `?token=` finding) opens `wss://.../ws?token=…`. `token_authorized` passes once, then the handler enters `while True: await ws.receive_text()` and never revalidates. The owner then calls `/auth/logout` (deletes that session row) or changes the password (`delete_all_sessions()`), and the HTTP side correctly 401s. The socket stays open and keeps receiving every broadcast — `download:results`, `plex:status`, `rename:*`, `dv:*`, `notification` — indefinitely, until the process restarts. Same holds when the 30-day `expires_at` passes: the socket outlives its own session. The owner sees "all sessions signed out" and has no way to observe or drop the live connection.

```python
await ws_manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
```

**Verifier.** Confirmed — no guard exists anywhere on this path. (1) ws.py:135-142 authorizes once then loops forever: `if not token_authorized(token): ... await ws.close(code=1008)` / `await ws_manager.connect(ws)` / `while True: raw = await ws.receive_text()` — no revalidation. (2) ConnectionManager cannot revoke even in principle: `self._connections: List[WebSocket] = []` stores no token or token hash, and `disconnect()` is called only from the endpoint's own `finally`. (3) The revocation side has no hook: auth.py:158 `reg.db.delete_session(auth_service.hash_token(token))` and auth.py:149 `reg.db.delete_all_sessions()  # force re-login everywhere` touch only the DB; neither route imports ws_manager — the comment's "everywhere" is false for the socket. (4) The HTTP middleware cannot cover it: main.py:660 `@app.middleware("http")` wraps as Starlette BaseHTTPMiddleware, whose `__call__` begins `if scope["type"] != "http": await self.app(scope, receive, send); return` (verified against the installed Starlette), so `_request_requires_auth` never runs for a websocket scope even though "ws" is in `protected_segments`. (5) Expiry too: token_authorized checks `if expires_at and not auth_service.is_expired(expires_at)` only at handshake, with SESSION_TTL_DAYS = 30. tests/test_api_ws.py covers handshake rejection thoroughly but has no post-handshake revocation test. Severity corrected down from high

**Suggested fix.** Give the socket an identity and a heartbeat re-check. (a) In `websocket_endpoint`, replace the bare `await ws.receive_text()` with a bounded wait that revalidates on idle: wrap in `try: raw = await asyncio.wait_for(ws.receive_text(), timeout=60)` / `except asyncio.TimeoutError:` then `if not token_authorized(token): await ws.close(code=1008, reason="Session revoked"); return` and `continue`. Also revalidate before processing each received frame. This alone closes logout, password change, AND the 30-day expiry uniformly, with no cross-module hooks, and covers direct DB edits — worst case exposure is one interval. (b) For immediate close rather than up-to-60s lag, additionally have `ConnectionManager.connect` take the token hash (`self._connections: List[tuple[WebSocket, str]]`, updating `broadcast`/`disconnect` to unpack), add `async def revoke(self, token_hashes: set[str] | None)` that closes matching sockets with 1008 (`None` = all), plus a `revoke_sync` mirroring `broadcast_sync`'s `

---

### 17. `backend/api/ws.py:127`  ✅ FIXED — the frontend mints a fresh single-use ticket per attempt. The fallback to ?token= now fires ONLY on 404/405 (route genuinely absent); a transient 5xx, a malformed response or a network failure retries instead, because falling back on any error re-leaked the token during exactly the reconnect storm a backend restart causes

*auth-surface*

**What breaks.** The 30-day session bearer token is passed to the WebSocket in the URL query string, so it is written verbatim into every intermediary's access log (NPM/nginx, Cloudflare) instead of staying in an Authorization header.

**How it happens.** `frontend/src/lib/stores/connection.ts:97` builds `${base}?token=${encodeURIComponent(nonce)}` where `nonce` is the same session token used for HTTP `Authorization: Bearer`. nginx's default `$request` log format records `GET /ws?token=<live-30-day-token> HTTP/1.1`, and Cloudflare logs the full URI. Anyone with read access to the NPM container's logs, a log-shipping pipeline, or a Cloudflare log retention export holds a working admin credential — and, because logout can silently fail (see above) and open sockets are never re-authorized, revoking it is unreliable. Nothing in the app indicates the credential was ever logged.

```python
async def websocket_endpoint(ws: WebSocket, token: str = Query(default="")):
```

**Verifier.** CONFIRMED, and understated. The chain is intact with no guard anywhere. (1) connection.ts:96-97 puts the credential in the URL: `const nonce = getAuthNonce(); const wsUrl = nonce ? `${base}?token=${encodeURIComponent(nonce)}` : base;`. (2) It is NOT a WS-specific value — client.ts:38-39 `export function getAuthNonce(): string { return authNonce; }` returns the same variable client.ts:77-78 uses for HTTP: `if (authNonce) { headers.set('Authorization', `Bearer ${authNonce}`); }`. (3) auth.ts:41-43 makes it the session token after login: `const { token } = await api.authLogin(password); setStoredToken(token); setAuthNonce(token);`. (4) auth_service.py:31 `SESSION_TTL_DAYS = 30` — the "30-day" claim is exact. (5) dependencies.py:270-274 accepts a logged token as a working credential: `expires_at = db.get_session_expiry(auth_service.hash_token(token)); if expires_at and not auth_service.is_expired(expires_at): return True`. No subprotocol/cookie/ticket alternative exists in backend/ — `token: str = Query(default="")` is the only WS auth input, and docs/CODE_REVIEW_PLAN.md:56 already concedes "token as a `?token=` query param (can land in proxy/access logs)". BROADER THAN CLAIMED: ScanHound's own container log leaks it too. docker/entrypoint.sh ends `exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth`, reaching backend/api/__main__.py:26 `uvicorn.run(app, host=args.host,

**Suggested fix.** Stop putting the long-lived token in the URL; carry it where it is not logged. Two options.

PREFERRED — short-lived single-use ticket (safe against every log surface, including uvicorn's own):
1. backend/api/routes/auth.py: add `POST /auth/ws-ticket`, protected by the existing bearer middleware. Generate `secrets.token_urlsafe(32)`, store `hash_token(ticket)` with a ~30s expiry (reuse the sessions table or a small in-process dict), return the ticket.
2. backend/api/dependencies.py: add `ticket_authorized(ticket) -> bool` that looks up the hash, checks expiry, and DELETES it on first use (single-use).
3. backend/api/ws.py:127: change the endpoint to `async def websocket_endpoint(ws: WebSocket, ticket: str = Query(default="")):` and gate on `ticket_authorized(ticket)` while keeping the identical fail-closed shape (`if auth_enabled() or not allow_open(): await ws.close(code=1008, reason="Unauthorized")`).
4. frontend/src/lib/stores/connection.ts: make doConnect() async — call `api.wsTick

---

### 18. `backend/app_service.py:785`  ✅ FIXED — the scheduler either genuinely triggers a scan or reports itself unable to

*scan-pipeline*

**What breaks.** On the server/Docker build the scheduler thread can never start a scan -- no scan trigger is ever registered -- yet it logs "Scheduled scan triggered" each interval and /scheduler/status reports the scheduler active.

**How it happens.** Operator ticks Enable Scheduler + 24h in Settings and restarts. app_service.py:576 starts _scheduler_loop. Every 24h the loop stamps _last_scheduler_fire, logs INFO "Scheduled scan triggered", then hits `if self._scan_trigger:` -- which is None, because the only caller of set_scan_trigger in the entire tree is ui/controllers/scanner_controller.py:368 (the legacy Qt desktop app), never backend/api/main.py. self._log_callback is also never set on the server, so the `elif self._log_callback` warning branch is a no-op too. Net effect: no scan ever runs, no warning is emitted anywhere, and the Settings > Scheduler card shows a green dot reading "Scheduler active" (frontend/src/routes/settings/+page.svelte:1290 keyed off scheduler_active = thread is alive). The only trace is an INFO line that claims the opposite of what happened.

```python
app_service.py:783-800  `self._last_scheduler_fire = now` / `logger.info("Scheduled scan triggered")` / `if self._scan_trigger:` / `    with self._config_lock:` / `        self.config["last_scan_time"] = now` / `        self.save_config()` / `    try:` / `        self._scan_trigger()` ... `elif self._log_callback:`
api/routes/scheduler.py:44-48  `"scheduler_active": bool(` / `    backend and` / `    getattr(backend, '_scheduler_thread', None) and` / `    backend._scheduler_thread.is_alive()`
(grep for set_scan_trigger outside tests returns only app_service.py:733 and ui/controllers/scanner_controller.py:368)
```

**Verifier.** Confirmed against the code; no guard rescues it. The Docker build runs the API server (docker/entrypoint.sh:43 `exec python -m backend.api --host 0.0.0.0 --port 9721 --no-auth`), whose lifespan builds `backend = AppService()` (backend/api/main.py:102) and never registers a trigger — a tree-wide grep for `set_scan_trigger` yields only the definition (app_service.py:733), tests, and `ui/controllers/scanner_controller.py:368` (Qt desktop). The thread nevertheless starts on the server: app_service.py:576-577 `if self.config.get("scheduler_enabled", False): / self._start_scheduler()` inside `_init_optional_subsystems`, called from `startup()` at line 488. The loop then does, in order, app_service.py:783-800: `self._last_scheduler_fire = now` / `logger.info("Scheduled scan triggered")` / `if self._scan_trigger:` (None on server) / `elif self._log_callback:`. That elif is also dead — `AppService.set_log_callback` (app_service.py:857) has zero callers; the only `set_log_callback` in the server path is `scanner.set_log_callback(_log_callback)` at backend/api/routes/scanner.py:178, which targets the ScannerService instance, not AppService. Meanwhile api/routes/scheduler.py:44-48 computes `"scheduler_active": bool(backend and getattr(backend, '_scheduler_thread', None) and backend._scheduler_thread.is_alive())`, and frontend/src/routes/settings/+page.svelte:1288-1291 renders `bg-green-500

**Suggested fix.** Two-part fix. (1) Register a trigger on the server: extract the body of `scheduler_trigger` in backend/api/routes/scheduler.py into a helper `def start_scheduled_scan(reg) -> bool` that takes `_scan_lock`, returns False if `_scan_state["state"] == "running"` or `reg.scanner.scan_in_progress`, else sets state running and spawns the `_run_scan` thread. Have the HTTP route call it and raise 409 when it returns False, and in backend/api/main.py's lifespan — after `reg._scanner_service` is wired, before/around the BackgroundScanner block near line 277 — add `backend.set_scan_trigger(lambda: start_scheduled_scan(reg))` (import locally to avoid a circular import). (2) Make the failure mode loud and stop the log from lying: in backend/app_service.py move `logger.info("Scheduled scan triggered")` to after a successful `self._scan_trigger()` call, and replace the dead `elif self._log_callback:` branch with an unconditional `logger.warning("Scheduler interval elapsed but no scan trigger is regist

---

### 19. `backend/database.py:5014`  ✅ FIXED — the flag is consumed only on CONFIRMED delivery, with a bounded retry budget. Required closing the chain: NotificationManager._send_notification now RETURNS its success count instead of only logging it, notify() propagates it (None when batched = not yet sent), and NotificationBridge gained notify_error_confirmed(). Without that the confirmation branch was dead code and one incident became three duplicate alerts

*notifications*

**What breaks.** notify_db_corruption_once consumes the on-disk corruption flag with os.replace immediately after a fire-and-forget notification dispatch, so a database quarantine-and-rebuild (total history loss) is marked "notified" whether or not anything was ever delivered — and because the rename happens during startup, the /rename/health db_corruption_flag field can never be True for any HTTP caller.

**How it happens.** DatabaseManager.__init__ -> init_db() hits corruption -> _quarantine_corrupt_db renames the DB and writes crawler.db.corrupt_flag.json. init_db is reachable only from AppService.startup() (the sole DatabaseManager() construction, backend/app_service.py:467), which runs inside _init_services BEFORE the lifespan yields (backend/api/main.py:598). At the end of that same _init_services, main.py:336 calls notify_db_corruption_once, which calls bridge.notify_error -> NotificationBridge.send -> asyncio.run_coroutine_threadsafe with the comment "Don't block - fire and forget" (notification_bridge.py:123-127), returns None, and then unconditionally os.replace()s the flag to .corrupt_flag.notified.json. Verified by execution with a channel whose send() takes 1.0s (a normal Discord/SMTP round-trip): notify_db_corruption_once returned True and db_corruption_flag_present() was already False before a single byte left the process; a subsequent bridge.shutdown() (which _teardown_services calls, main.py:566) stopped the loop and destroyed the pending task with delivered==[]. Net: a container that restarts within a second of startup (crash-loop, `up -d --build`, health-check flap) loses the alert permanently, and combined with finding 1 the alert usually has no channel to go to anyway. Meanwhile /rename/health advertises db_corruption_flag as one of "two otherwise-invisible failure signals" (backend/api/routes/rename.py:484-486) but it is structurally always False, because the flag is erased before the app accepts its first request.

```python
try:
        if bridge is not None:
            bridge.notify_error(
                "Database corruption was detected and quarantined — check logs")
    except Exception:
        logger.warning("DB corruption notification failed (non-fatal)", exc_info=True)
    notified_path = f"{db_path}.corrupt_flag.notified.json"
    try:
        os.replace(flag_path, notified_path)
```

**Verifier.** Not refuted — both mechanical claims are true, and no guard exists that the finder overlooked.

(a) Consumption is decoupled from delivery, deliberately and test-locked. `notify_db_corruption_once` does `bridge.notify_error(...)` -> `NotificationBridge.send()` -> `future = asyncio.run_coroutine_threadsafe(self._manager.notify(...), self._loop)` followed by the comment `# Don't block — fire and forget`, and returns None. The caller then unconditionally runs `os.replace(flag_path, notified_path)`. The docstring states the intent outright: "the rename only happens if we got as far as attempting notification, so the 'fire once' behavior holds even when the bridge silently fails". tests/test_database_hardening.py:363 `test_none_bridge_still_renames_flag` locks it in with `result = notify_db_corruption_once(db_path, None)` / `assert not os.path.exists(flag_path)` — the durable retry token is consumed when literally nothing was attempted.

(b) The health field is structurally unreachable over HTTP. `db_corruption_flag_present` returns `os.path.exists(corruption_flag_path(db_path))` where `corruption_flag_path` is `f"{db_path}.corrupt_flag.json"` — the pre-rename name only; its own docstring says "once notify_db_corruption_once renames it to .notified.json, this returns False again." The flag is written only by `_write_corruption_flag`, called only from `_quarantine_corrupt_db`, called

**Suggested fix.** Three separable changes:

1. Gate the rename on delivery, not dispatch. Add a blocking variant to NotificationBridge — in `send()`, keep the `future = asyncio.run_coroutine_threadsafe(...)` but expose `send_sync(..., timeout=15) -> bool` that returns `future.result(timeout)`; have `NotificationManager.notify` return whether >=1 channel actually succeeded, and `notify_error_sync()` propagate that bool. Then in `notify_db_corruption_once`:
   delivered = bridge.notify_error_sync(msg) if bridge is not None else False
   ... only `os.replace(flag_path, notified_path)` when `delivered` is True; otherwise leave the flag so the next startup retries. This costs at most `timeout` seconds on a corruption event only (the flag exists on approximately zero startups), so it does not slow normal boot.

2. Bound the retries so a permanently-unconfigured install does not alert forever: store an `attempts` counter and `last_attempt_at` inside the flag JSON, increment on each failed attempt, and fall bac

---

### 20. `backend/database.py:3241`  ✅ FIXED — the scan route now writes a scan_history row (not for cancelled/failed runs)

*scan-pipeline*

**What breaks.** No scan ever records a scan_history row -- save_scan_history has zero non-test callers -- so the Analytics dashboard permanently reports 0 scans and 0 items scanned.

**How it happens.** The operator opens Analytics after months of scanning. StatsDashboard.get_scan_stats() selects from scan_history, gets no rows, and returns an empty ScanStats -> total_scans 0, total_items_scanned 0, last_scan_time None; /analytics/scan-history returns [] and the trends chart renders empty. A grep of the whole tree for save_scan_history( finds only its definition here and calls in tests/ -- neither api/routes/scanner.py's _run_scan (which computes exactly the right numbers via _compute_stats and a duration, then throws them away after the WebSocket broadcast) nor background_scanner ever calls it. The dashboard is indistinguishable from a system that has never scanned, which is the same reading it would give if scanning were genuinely broken.

```python
database.py:3241-3245  `def save_scan_history(self, scan_data):` / ... / `            INSERT INTO scan_history (` / `                timestamp, scan_type, items_scanned, missing_count,`
analytics.py:304-312  `cursor.execute('''` / `    SELECT * FROM scan_history` / `    WHERE timestamp > ?` / ... / `if not rows:` / `    return stats`
api/routes/scanner.py:209  `stats = _compute_stats(items)`   (computed, broadcast, never persisted)
```

**Verifier.** Genuine. `save_scan_history` at backend/database.py:3241 holds the only `INSERT INTO scan_history` in the tree (no triggers, no second writer), and a tree-wide grep for `save_scan_history(` finds the definition plus calls only under tests/ — none in backend/ or ui/. The scan completion path in backend/api/routes/scanner.py:197-229 computes the exact payload and throws it away: `duration = time.time() - start_time` ... `stats = _compute_stats(items)` ... `ws_manager.broadcast_sync({"type": "scan:complete", "data": {"stats": stats, "total": stats["total"], "duration": round(duration, 1)}})` ... and the only persistence that follows is `reg.config["last_scan_time"] = time.time()` into the config file, never the DB. background_scanner.py:583 `run_scan(...)` likewise only feeds `_to_cache_rows`. So analytics.py:311 `if not rows: return stats` always fires, get_trend_data returns empty arrays, and /analytics/scan-history returns []. The empty-state guard does NOT hide it: frontend/src/routes/analytics/+page.svelte:203 is `{#if data.library.total_items === 0 && data.scans.total_scans === 0}` — with a populated Plex cache total_items > 0, so the page renders a literal `0` in the "Scans ({trendDays}d)" card whose tooltip claims "Number of full or partial scans run during this period." The repo's own evidence files (docs/feature-pack-review/qualification-evidence/01_snapshot.json) record

**Suggested fix.** In `_run_scan` (backend/api/routes/scanner.py), right after the `scan:complete` broadcast and inside its own try/except so a DB error can never fail the scan, persist the run:

```python
try:
    if reg.db:
        reg.db.save_scan_history({
            "timestamp": datetime.now().isoformat(),   # ISO required
            "scan_type": scan_type,
            "items_scanned": stats["total"],
            "missing_count": stats["missing"],
            "upgrade_count": stats["upgrade"],
            "in_library_count": stats["library"],
            "duration_seconds": round(duration, 2),
            "sources_scanned": source_type,
        })
except Exception:
    logger.warning("Failed to record scan history", exc_info=True)
```

Timestamp format is load-bearing: analytics.py compares `timestamp > cutoff` against `datetime.now() - timedelta(days=days)).isoformat()`, calls `datetime.fromisoformat(row['timestamp'])` at line 327/337, and groups by SQLite `date(timestamp)` at line 481 — so it mu

---

### 21. `backend/download_queue.py:1146`  ✅ FIXED — retry_item checks the UPDATE rowcount and rolls back before touching the batch

*download-path*

**What breaks.** retry_item() never checks the item UPDATE's rowcount, so retrying an item in a state the WHERE clause excludes (claimed / completed / cancelled) returns HTTP 200 with the unchanged row while still clobbering the batch's paused state.

**How it happens.** User cancels item 0 of a source-paused 2-item batch, then clicks Retry on it (a stale UI list, or a double-click on a claimed row). The UPDATE's `AND state IN ('verification_required','waiting_source','failed','scheduled','ready')` matches nothing, but no rowcount is inspected; the method proceeds to force the batch to 'scheduled'/cooldown NULL, emits download:queue_updated, and returns the row. POST /download/retries/{uuid}/retry returns 200 with state='cancelled'. VERIFIED: after retry_item on a cancelled item the item stayed 'cancelled', yet batch.state went paused_source -> scheduled and cooldown_until -> NULL, and the surviving sibling in 'waiting_source' was then skipped by _maybe_auto_resume forever. So the user is told the retry was accepted, nothing is retried, and a sibling item is silently stranded as collateral.

```python
conn.execute(
                """
                UPDATE download_queue_items
                SET state = 'ready', scheduled_for = ?, cooldown_until = NULL,
                    queue_reason = 'manual_retry',
...
                WHERE item_uuid = ?
                  AND state IN (
                    'verification_required', 'waiting_source', 'failed',
                    'scheduled', 'ready'
                  )
                """,
                (now, now, item_uuid),
            )
```

**Verifier.** CONFIRMED by execution, not just reading. retry_item (backend/download_queue.py:1136-1179) discards the item UPDATE's rowcount -- the call is a bare `conn.execute(...)` with no assignment -- while every sibling mutator in the same file checks it: cancel_item does `updated = conn.execute(... WHERE item_uuid = ? AND state NOT IN ('claimed','completed','cancelled')").rowcount; if updated != 1: return False` (1379-1389), and _fail_item (922) / _pause_for_source (969) both bail with "ignored stale ...". The batch UPDATE that follows is unconditional: `UPDATE download_queue_batches SET state = 'scheduled', cooldown_until = NULL, updated_at = ? WHERE batch_uuid = ?` (1167-1174). _refresh_batch_locked does not undo it -- it writes `state = COALESCE(?, state)` with the param NULL unless active==0 (1056-1073), preserving the clobbered 'scheduled'. Live repro on a source-paused 2-item batch after cancelling item 0: `returned state (what HTTP 200 body says): cancelled / item0 cancelled / item1 waiting_source / batch scheduled cooldown None auto_resume_used 0`, then after _maybe_auto_resume: `item1 waiting_source / batch scheduled cooldown None`. The stranding is permanent: _maybe_auto_resume selects `WHERE state = 'paused_source' AND auto_resume_after_cooldown = 1 AND auto_resume_used = 0` and skips batches with a NULL cooldown_until (1086-1100) -- both preconditions destroyed; _claim_due 

**Suggested fix.** In retry_item, capture the item UPDATE's rowcount and abort the transaction before touching the batch when it is 0, mirroring cancel_item's convention: `updated = conn.execute(<same UPDATE>, (now, now, item_uuid)).rowcount` then `if updated != 1: raise DownloadQueueError(f"That item cannot be retried while it is {item.get('state')}.")` -- raising inside `with self.db.transaction()` rolls back and the route's except maps it to HTTP 409 instead of a lying 200. Only run the `UPDATE download_queue_batches SET state='scheduled', cooldown_until=NULL` and _refresh_batch_locked after that check passes, so a rejected retry cannot destroy the batch's paused_source/cooldown_until and orphan siblings. Re-read state inside the transaction (rather than trusting the pre-transaction get_item) so the message is accurate under a race, and add a regression test asserting that retry_item on a cancelled/claimed item raises, leaves the item state unchanged, and leaves batch.state == 'paused_source' with coo

---

### 22. `backend/download_service.py:1915`  ✅ FIXED — DDLBase/Adit-HD failures carry a ScrapeDiagnostic; transient stays retryable

*download-path*

**What breaks.** The DDLBase and Adit-HD scrapers return a bare [] with no ScrapeDiagnostic on every failure, so a transient shortlink/CAPTCHA/driver failure is laundered into a confident, permanent, non-retryable 'No download links found on the source page' with reason_code NULL and transport_attempted=0.

**How it happens.** A DDLBase post's cuty.io shortlinks time out or hit Turnstile. _scrape_ddlbase_links logs a warning and returns [] (also `except Exception: return []` at line 2149-2151). scrape_links wraps it as ScrapedLinks([]) with diagnostic=None, so download_item's `if diagnostic is not None` branch (line 2668) is skipped and it emits the hardcoded message instead. VERIFIED by execution: result = {success: False, message: 'No download links found on the source page.', reason_code: None, retryable: False, retry_mode: 'none', deferred: False}. Downstream: is_source_wide_denial() is False and reason_code is not in {'interactive_challenge','source_temporarily_blocked'}, so routes/downloads.py:124-131 never enqueues a retry; in the durable queue _fail() writes state='failed', last_reason_code=NULL, transport_attempted=0 — i.e. the durable record asserts no request was made when a full Selenium session, Cloudflare wait and shortlink resolution all ran. retry_ready() (line 1192, `WHERE source = 'hdencode'`) also excludes ddlbase, so 'Retry all' never picks these up. An unattended DDLBase batch therefore ends with items permanently marked failed for a transient cause, and the notification states a specific fact about the page that is false.

```python
if source_kind == "ddlbase":
                    return ScrapedLinks(
                        self._scrape_ddlbase_links(
                            url,
                            progress_callback=progress_callback,
                        )
                    )
```

**Verifier.** CONFIRMED — I tried to refute it and every candidate guard is absent or discarded.

1) The signature itself settles the wrapping claim: `def _scrape_ddlbase_links(self, url, progress_callback=None) -> List[str]` (download_service.py:2063) and `def _scrape_adithd_links(self, url, service_type) -> List[str]` (:2368). Neither can carry a diagnostic, and `scrape_links` passes no `diagnostic=` kwarg: `return ScrapedLinks(self._scrape_ddlbase_links(url, progress_callback=progress_callback))` (:1915-1920) / `return ScrapedLinks(self._scrape_adithd_links(url, service_type))` (:1922-1924). `ScrapedLinks.__init__` defaults `diagnostic: Optional[ScrapeDiagnostic] = None` (scrape_outcome.py:100).

2) The DDLBase path COMPUTES diagnostics and throws them away — this is stronger than the finder claimed. Line 2081: `self._wait_past_cloudflare(driver)` — return value discarded, so a real INTERACTIVE_CHALLENGE is destroyed. Line 2114-2117:
```
self._log("[DDLBase] No shortlinks or download links found", "warning")
self._log_page_diagnostics(driver, source_kind="ddlbase")
return []
```
`_log_page_diagnostics` is declared `-> ScrapeDiagnostic` (:1458) and its result is dropped on the floor. Line 2075 uses `self._navigate(...)` — the "Backward-compatible driver-only wrapper" (:1447) that explicitly drops the diagnostic: `driver, _diagnostic = self._navigate_with_diagnostic(...)`. Plus `except Exce

**Suggested fix.** Make both non-HDEncode scrapers return `ScrapedLinks` instead of `List[str]`, and stop discarding the diagnostics the code already builds.

In `_scrape_ddlbase_links` (download_service.py:2063):
- `:2075` swap `self._navigate(url, tag="DDLBase")` for `self._navigate_with_diagnostic(...)` and return `ScrapedLinks(diagnostic=nav_diag)` when driver is None.
- `:2081` capture `wait_diag = self._wait_past_cloudflare(driver, source_kind="ddlbase")` and return it when non-None (this is the CAPTCHA/Turnstile case the finding is about).
- `:2114` capture the already-computed `diag = self._log_page_diagnostics(driver, source_kind="ddlbase")` and `return ScrapedLinks(diagnostic=diag)` instead of `return []`.
- `:2141-2147` when `resolvable and not resolved`, return `ScrapedLinks(diagnostic=ScrapeDiagnostic(ScrapeCode.SCRAPE_EXCEPTION or a new SHORTLINK_UNRESOLVED, retryable=True, transport_attempted=True, stage="shortlink_resolution", retry_mode="immediate"))` — a timed-out shortlink is transient

---

### 23. `backend/notifications.py:574`  ✅ FIXED — email_to accepts a single or comma-separated value (_normalize_addrs)

*notifications*

**What breaks.** email_to is a plain string everywhere in the config schema, but EmailChannel treats it as a list of addresses — so every notification email carries a per-character garbage To: header, and any multi-recipient value is passed to smtplib as one malformed address and rejected outright, while the /settings/test/email endpoint uses a different construction path that works.

**How it happens.** config.py:229 declares `email_to: str` with default "" (config.py:617); notification_bridge.py:60-63 copies it through verbatim; configure_from_dict passes it as to_addrs. Case A (single recipient, the common case): to_addrs="jesse@example.com" -> `", ".join(self.to_addrs)` at notifications.py:405 produces `To: j, e, s, s, e, @, e, x, a, m, p, l, e, ., c, o, m`. smtplib wraps the bare string as one envelope recipient so the mail is delivered with a nonsense To: header — zero errors logged anywhere. Case B (two recipients, e.g. "jesse@example.com, alerts@example.com" — a free-text field invites this): sendmail issues a single RCPT TO:<jesse@example.com, alerts@example.com>, the MTA returns 501, SMTPRecipientsRefused is caught by the blanket `except Exception` in EmailChannel.send (notifications.py:465-467), send() returns False, and _send_notification reports "0/1 channels" at DEBUG only — invisible at the production INFO level (app_service.py:280). Both verified by execution against a stubbed smtplib.SMTP that replicates the real bare-string handling. Crucially the built-in test path does NOT share this bug: settings.py:397-405 sets msg["To"] itself and calls server.send_message(msg), which parses the comma list correctly — so the operator's test passes while real notifications are malformed or undelivered.

```python
to_addrs=config.get('email_to', []),
...
        msg['To'] = ", ".join(self.to_addrs)
```

**Verifier.** The type mismatch is real and I reproduced it through the production path, but the finding's "SILENT" framing is only half right.

CONFIRMED — the schema says str, the channel says List[str], and nothing in between coerces:
- backend/config.py:230 `    email_to: str` with backend/config.py:617 `    "email_to": "",`
- backend/config.py:670 `validate_config` touches no email key (grep for "email" in config.py returns only the 4 declarations/defaults — no normalization)
- backend/notification_bridge.py:60-63 copies verbatim: `for k in ("smtp_host", "smtp_port", "smtp_username", "smtp_password", "email_from", "email_to", "smtp_tls"): if k in config: notif_config[k] = config[k]`
- backend/notifications.py:574 `                to_addrs=config.get('email_to', []),` into `to_addrs: List[str]` (line 388), stored raw at line 397 `self.to_addrs = to_addrs`
- backend/notifications.py:405 `        msg['To'] = ", ".join(self.to_addrs)`
- Production entry point is real: backend/api/main.py:128-129 `notif = NotificationBridge()` / `notif.configure(backend.config)`

Executed repro driving the REAL bridge with the REAL `get_default_config()` (email_to left as the str the schema mandates):
```
channel type: EmailChannel
to_addrs repr: 'jesse@example.com'
To header: 'j, e, s, s, e, @, e, x, a, m, p, l, e, ., c, o, m'
RCPT TO (single): ['<jesse@example.com>']
To header (two): 'j, e, s, s, e, @, ...

**Suggested fix.** Normalize at the boundary — coerce a string into a list of addresses, and don't build a channel with no recipients.

In backend/notifications.py, add a helper and use it in EmailChannel.__init__ so every construction path (including the tests' list form) is safe:

```python
def _normalize_addrs(value) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = re.split(r"[,;]", value)
    else:
        parts = list(value)
    return [p.strip() for p in parts if isinstance(p, str) and p.strip()]
```

Then in `EmailChannel.__init__` (line ~397) replace `self.to_addrs = to_addrs` with `self.to_addrs = _normalize_addrs(to_addrs)`, and widen the annotation at line 388 to `to_addrs: Union[str, List[str]]`. That fixes both the `", ".join(...)` header at line 405 and the two `server.sendmail(self.from_addr, self.to_addrs, ...)` calls at 477/481 in one place.

Add a guard in `configure_from_dict` (line 566) so an enabled-but-unaddressed config doesn't cr

---

### 24. `backend/notifications.py:339`  ✅ FIXED — bodyless verbs send the payload as a query string, not a JSON body

*notifications*

**What breaks.** With webhook_method="GET" the generic webhook channel sends the notification payload as a JSON request body on a GET, which virtually every receiver ignores, yet any 2xx is counted as success — the operator gets a permanent stream of empty webhook hits reported as delivered.

**How it happens.** Operator sets webhook_method to "GET" (an allowed value: config.py:222 `webhook_method: Literal["POST", "GET", "PUT"]`; the bridge forwards it at notification_bridge.py:73). GenericWebhookChannel.send builds the payload then calls _post_webhook, which sets `kwargs["json"] = payload` (notifications.py:109) and issues `session.request("GET", url, ...)`. Verified by execution against a local HTTP server: the server saw `GET /hook` with NO query string and Content-Length 202 — the payload was in the body, which nginx and most frameworks/automation endpoints (Zapier/n8n GET hooks) never read — and send() returned True, so _send_notification counts it as a successful delivery and logs "1/1 channels". The /settings/test/webhook endpoint does the opposite and correct thing (settings.py:381-383 `requests.get(url, params=payload)` -> query string), so the test button confirms the receiver works while production sends it nothing. There is no fallback, no warning, and no way to distinguish this from real delivery.

```python
return await self._post_webhook(
            self.webhook_url, payload,
            expected=tuple(range(200, 300)),
            method=self.method, headers=self.headers,
        )
```

**Verifier.** notifications.py:102-116 `_post_webhook` sets the body unconditionally — `if use_data: kwargs["data"] = payload else: kwargs["json"] = payload` — then `session.request(method, url, **kwargs)`, with no method branch. GenericWebhookChannel.send (339-343) passes `method=self.method` and `expected=tuple(range(200,300))`, so any 2xx returns True. GET is a supported, UI-selectable value (config.py:223 `webhook_method: Literal["POST","GET","PUT"]`; the Svelte select emits `k.__value="GET"`), forwarded verbatim by notification_bridge.py:73 into `GenericWebhookChannel(config['webhook_url'], config.get('webhook_method','POST'), ...)` at notifications.py:596-603. I reproduced it independently against a local server: `send()` returned True while the server logged `GET /hook` with NO query string, `Content-Length: 192`, `Content-Type: application/json`, and the full notification JSON in the body. Both test buttons do the opposite and correct thing — settings.py:382 `resp = requests.get(url, params=payload, timeout=10)` and ui/controllers/settings_controller.py:881 the same — which establishes query-string as the intended GET contract that production violates. Failure is silent: the only log is `logger.debug(f"{self.name} notification sent")` and _send_notification:741-743 counts it via `successes = sum(1 for r in results if r is True)` into "Notification sent to {successes}/{len(tasks)} cha

**Suggested fix.** In `_post_webhook` (backend/notifications.py:96-119), branch on method so bodyless verbs carry the payload as a query string, matching what settings.py:382 and settings_controller.py:881 already do:

```python
m = (method or "POST").upper()
kwargs = {"timeout": aiohttp.ClientTimeout(total=10)}
if m in ("GET", "HEAD", "DELETE"):
    # aiohttp params accept only str/int/float — verified: a dict value raises
    # "TypeError: Invalid variable type: value should be str, int or float".
    # Flatten nested values (Notification.to_dict()['data'] is a dict) and drop Nones.
    kwargs["params"] = {
        k: (v if isinstance(v, (str, int, float)) and not isinstance(v, bool)
            else json.dumps(v) if isinstance(v, (dict, list))
            else str(v))
        for k, v in payload.items() if v is not None
    }
elif use_data:
    kwargs["data"] = payload
else:
    kwargs["json"] = payload
```

Two follow-ons in the same edit:
1. On the query-string path, strip `Content-Type: application

---

### 25. `backend/plex_service.py:707`  ✅ FIXED — the new-content probe fails closed and verifies a library resolved

*plex-sync*

**What breaks.** The 'new content in Plex' cache-invalidation probe cannot distinguish 'no new items' from 'the check failed' — every failure path returns an empty list or is swallowed at debug level, and the function then positively asserts the cache is valid.

**How it happens.** check_cache_status is inside the 4h cache window and asks Plex whether anything was added since the cache timestamp. plex_manager.get_recently_added catches per-library errors, logs them, and returns whatever it collected — [] when every library call fails (plex_manager.py:588-589), and [] when the connection is gone (plex_manager.py:563-566). check_cache_status treats a falsy result as 'no new content' and falls through to `return True, ""`; an outright exception is swallowed at logger.debug. The guard `self.plex_manager.is_connected` does not help because is_connected is just `self._server is not None` (plex_manager.py:172-174) — a stale flag that stays True after the server goes away. Concrete: the user adds three 4K films to Plex at 19:00; at 20:00 an Incremental scan runs while Plex is mid-restart; get_recently_added returns [] with an error in the log; check_cache_status returns (True, "") with an EMPTY message, so the scan proceeds on the 3-hour-old cache; those three films are matched against an index that predates them and are reported Missing (and are auto-grab-eligible under the default auto_grab_statuses). It self-heals only when the cache passes cache_duration (default 4h).

```python
backend/plex_service.py:697-710
                try:
                    from datetime import datetime, timezone
                    since = datetime.fromtimestamp(cache_ts, tz=timezone.utc)
                    new_items = self.plex_manager.get_recently_added(since)
                    if new_items:
                        ...
                except Exception as e:
                    logger.debug("New content check failed: %s", e)

            return True, ""

backend/plex_manager.py:585-589
                    items = section.search(addedAt__gte=ts)
                    recent_items.extend(items)
                    logger.info(f"Incrementally synced {len(items)} items from '{lib.title}' (since {since})")
                except Exception as e:
                    logger.error(f"Failed to fetch recent items from '{lib.title}': {e}")
```

**Verifier.** Could not refute — every link in the chain is present in the code, and I found no guard the finder missed.

1) The probe conflates failure with emptiness. `plex_manager.get_recently_added` returns `[]` on all three failure paths: connect failure (`if not success: return []`, plex_manager.py:563-566), per-library failure (`except Exception as e: logger.error(f"Failed to fetch recent items from '{lib.title}': {e}")` — loop continues, plex_manager.py:588-589), and outer failure (`except Exception as e: logger.error(f"Error getting recently added items: {e}")` then `return recent_items`). The return type is a bare `List[Any]`, so `[]` is indistinguishable from "nothing new."

2) check_cache_status then positively asserts validity. `if new_items:` is falsy on the failure `[]`, and the `except Exception as e: logger.debug("New content check failed: %s", e)` swallow both fall through to the same `return True, ""`.

3) The `is_connected` guard is confirmed a stale flag: `def is_connected(self) -> bool: return self._server is not None` (plex_manager.py:172-174). Grepping every `_server = None` assignment shows it is nulled ONLY in connect() failure branches (lines 225/228/231/282) and explicit `disconnect()` (line 313) — never when an already-established server stops responding. So it stays True after Plex goes away, exactly as claimed.

4) The consequence is real, which I verified at t

**Suggested fix.** Make "could not determine" a distinct outcome from "nothing new," and fail closed.

1) Let get_recently_added report failure instead of masking it. Either change the signature to `Optional[List[Any]]` and `return None` on the connect-failure and outer-exception paths, or return `(ok: bool, items: List[Any])`. Critically, the per-library `except` at plex_manager.py:588-589 must also mark the result partial — a partial success is still "unknown," since the failed library is exactly where the new items may be.

2) In check_cache_status, treat unknown as invalid so the caller refreshes:
   - `if new_items is None: return False, "Could not verify new Plex content — refreshing cache."`
   - Change `except Exception as e: logger.debug(...)` to `logger.warning("New content check failed: %s", e)` and `return False, f"New-content check failed ({e}) — refreshing cache."`
   This is the safe direction: scanner_service.py already handles `not is_valid` by setting `force_plex_reload = True` while ke

---

### 26. `backend/scanner_service.py:376`  ✅ FIXED — run_scan records last_scan_error and re-arms early_stopped, so a crashed scan no longer reports clean and no longer lets the cache be purged

*scan-pipeline*

**What breaks.** run_scan() swallows every exception from the async scan and returns normally, so background_scanner records the failed source as error-free, leaves purge_safe=True, and still purges the cache.

**How it happens.** DNS/network failure, a Plex load exception, or any DB error inside _run_scan_async: the `except Exception` logs "Scan error: ..." and run_scan returns list(self.items) (partial or empty). background_scanner._scan_source's docstring claims "Raises on hard failure so the caller can record a per-source error" -- it cannot, so `err` stays None. Consequences per cycle: (a) source_results records {"source": "HDEncode", "new": 0, "error": None} -- /background/status shows a clean run; (b) the log says "Background scan complete: 0 new/updated from 1 source(s)"; (c) _last_crawl_seen_urls is empty so touch_background_cache is skipped; (d) _last_crawl_early_stopped is NEVER reset by run_scan (only lines 357-358 reset seen_urls/request_count), so it keeps the previous source's value -- if that was False, purge_safe stays True and db.purge_background_cache(retain_days) runs anyway. Repeat for retain_days (default 7) and the whole cached catalogue is deleted with no error anywhere. Verified by execution: run_scan with a raising _run_scan_async returned [] without raising, and _last_crawl_early_stopped kept its stale False.

```python
scanner_service.py:375-380  `        except Exception as e:` / `            self._log(f"Scan error: {e}", "error")` / `        finally:` / `            self.is_scanning = False` / `` / `        return list(self.items)`
background_scanner.py:416-424  `err: Optional[str] = None` ... `except Exception as e:` / `    err = str(e)`
background_scanner.py:440-445  `if not err:` / `    seen = getattr(scanner, "_last_crawl_seen_urls", None)` / `    if seen:` / `        db.touch_background_cache(seen)` / `    if getattr(scanner, "_last_crawl_early_stopped", False):` / `        purge_safe = False`
```

**Verifier.** CONFIRMED, with two corrections to the finding's own causal story.

1) The swallow is real. backend/scanner_service.py:365-380 — `try: loop.run_until_complete(self._run_scan_async(...))` / `except Exception as e:` / `    self._log(f"Scan error: {e}", "error")` / `finally: self.is_scanning = False` / `return list(self.items)`. Executed: with `_run_scan_async` raising, `run_scan` returned `[]` and did not raise.

2) The stale flag is real. run_scan resets only two fields — scanner_service.py:357-358 `self._last_crawl_seen_urls = set()` / `self._last_crawl_request_count = 0`. `_last_crawl_early_stopped` is written only at line 948 (`self._last_crawl_early_stopped = early_stopped`), at the END of `_crawl_pages`, so an aborted scan keeps the prior cycle's value. Executed: prior False stayed False, prior True stayed True.

3) The caller cannot see it. background_scanner.py:572-576 `_scan_source` calls `run_scan` directly and its docstring "Raises on hard failure so the caller can record a per-source error" is false; at :419-424 `err` stays None, and at :495 `source_results.append({"source": source, "new": len(rows), "error": err})` reports a clean run.

End-to-end repro against the real DatabaseManager + BackgroundScanner (scratch DB dir), fake scanner mimicking the swallowed failure: stale flag False -> `last_run_sources=[{'source':'HDEncode','new':0,'error':None}] rows_left=0` (age

**Suggested fix.** Three small changes, the middle one being the load-bearing fix:

1. background_scanner.py (~:439) — gate the purge on failure, which is what actually prevents the deletion:
   `if err:` -> `purge_safe = False` (a source that errored was not fully crawled), before the existing `if not err:` block. Also treat a source that returned zero items with an empty seen-set as incomplete.

2. scanner_service.py run_scan (~:357) — reset the crawl-completeness flag with the others so it can never carry over:
   `self._last_crawl_early_stopped = False` alongside `_last_crawl_seen_urls`/`_last_crawl_request_count`; and in the `except Exception` handler set `self._last_crawl_early_stopped = True` (an aborted scan is by definition an incomplete crawl) plus `self._last_scan_error = str(e)`.

3. Make the failure visible: either re-raise after the `finally` (backend/api/routes/scanner.py:257 already has `except Exception: logger.exception("Scan failed")` + a `scan:error` broadcast, so the foreground UI im

---

## LOW

### 27. `backend/api/routes/auth.py:157`  ✅ FIXED — logout reports 500 when the session delete did not land

*auth-surface*

**What breaks.** `logout` returns `{"ok": True}` regardless of whether the session row was actually deleted, and the frontend additionally swallows any error — so "signed out" can leave a live 30-day token on the server.

**How it happens.** `delete_session` is `_mutate(...)`, which returns False on any exception rather than raising. The route discards that value. Input: owner clicks Sign out on a shared or possibly-compromised device while the DB is momentarily unwritable -> `DELETE FROM auth_sessions WHERE token_hash = ?` fails -> route still returns 200 `{'ok': True}`. `frontend/src/lib/stores/auth.ts:55` does `api.authLogout().catch(() => {})` and then clears localStorage, so the UI shows a clean sign-out. The bearer token remains valid server-side for the remainder of its 30-day TTL, and anyone who captured it (browser history, proxy log, the `?token=` WS URL) still has full API access.

```python
token = _bearer(request)
    if reg.db and token:
        reg.db.delete_session(auth_service.hash_token(token))
    return {"ok": True}
```

**Verifier.** The defect is real and no guard prevents it. backend/database.py:185-195 — `_mutate` is `try: ... return True / except Exception as e: logger.error("DB Error (%s): %s", label, e); return False`; backend/database.py:3655-3659 — `def delete_session(self, token_hash): return self._mutate("DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,), label="delete_session")`; backend/api/routes/auth.py:157-159 — `if reg.db and token: reg.db.delete_session(auth_service.hash_token(token))` then `return {"ok": True}` with the boolean discarded. Authorization is a READ, not a write — backend/api/dependencies.py:274-276: `expires_at = db.get_session_expiry(auth_service.hash_token(token)); if expires_at and not auth_service.is_expired(expires_at): return True` — so a failed DELETE leaves the row readable and the token valid for the full `SESSION_TTL_DAYS = 30` (backend/auth_service.py:31). The `if reg.db` guard only covers `db is None`, and that flavor self-mitigates (reads fail too, token rejected); the "connection alive but write fails" flavor does not, and the codebase itself documents it as reachable here — backend/database.py:1183-1185: `it covers transient conditions ("database is locked" after busy_timeout expires, "disk I/O error" from a flaky bind-mounted filesystem)`. Nothing else rescues the row: `purge_expired_sessions` deletes only `WHERE expires_at <= ?`. The only test, t

**Suggested fix.** Backend (backend/api/routes/auth.py:153-159): stop discarding the boolean and fail loud. `token = _bearer(request); if reg.db and token:  ok = reg.db.delete_session(auth_service.hash_token(token));  if not ok: logger.error("logout: session delete failed; token remains valid until expiry"); raise HTTPException(status_code=500, detail="Sign-out could not be completed on the server; the session may still be active")` — return `{"ok": True}` only on the success path. Note the limit of this fix: `_mutate` returns True for a zero-row DELETE, so it distinguishes exception/no-connection from success, NOT "row existed". That is the right behavior here because a nonce-authorized caller has no `auth_sessions` row and the route documents itself as a "no-op for the nonce" — do NOT switch to a `cursor.rowcount > 0` check without special-casing the nonce, or every desktop-sidecar logout starts 500ing. Frontend (frontend/src/lib/stores/auth.ts:54-58): make `logout()` async, `await api.authLogout()` in

---

### 28. `backend/database.py:2616`  ✅ FIXED — prune counter accumulates per batch instead of reading the last one

*plex-sync*

**What breaks.** The prune counter reports cursor.rowcount from the LAST delete batch only, so it logs 'Pruned -1 stale rows' on every healthy full load and under-reports a mass deletion by up to 500 rows per batch — the one log line that would expose the cache-wipe above is wrong in both directions.

**How it happens.** Verified against sqlite3 directly. Case A (healthy, nothing stale): stale_keys is empty, the delete loop never executes, so cursor.rowcount still holds the value from the preceding SELECT, which sqlite3 reports as -1; `if deleted:` is truthy for -1, so every successful full_replace save logs 'Pruned -1 stale rows from plex_cache (Movies)'. Case B (the damaging case): 1200 rows are pruned in batches of 500; the loop leaves cursor.rowcount at 200, so the log says 'Pruned 200 stale rows' when 1200 were deleted. An operator investigating why 4K titles went Missing sees a number that understates the deletion 6x, or a nonsense -1 that trains them to ignore the line entirely.

```python
backend/database.py:2608-2618
                    stale_keys = [row[0] for row in all_existing if row[0] not in fresh_db_keys]
                    for i in range(0, len(stale_keys), 500):
                        batch = stale_keys[i:i+500]
                        placeholders = ','.join('?' for _ in batch)
                        cursor.execute(
                            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
                            batch,
                        )
                    deleted = cursor.rowcount
                    if deleted:
                        logger.info("Pruned %d stale rows from plex_cache (%s)", deleted, mode)
```

**Verifier.** The code at backend/database.py:2609-2618 is exactly as reported, with no guard the finder missed:

    all_existing = cursor.execute(
        "SELECT key FROM plex_cache WHERE content_type = ?", (mode,)
    ).fetchall()
    stale_keys = [row[0] for row in all_existing if row[0] not in fresh_db_keys]
    for i in range(0, len(stale_keys), 500):
        batch = stale_keys[i:i+500]
        placeholders = ','.join('?' for _ in batch)
        cursor.execute(
            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
            batch,
        )
    deleted = cursor.rowcount
    if deleted:
        logger.info("Pruned %d stale rows from plex_cache (%s)", deleted, mode)

`deleted` is read ONCE, after the loop, from the same cursor — never accumulated per batch, and never clamped.

I reproduced both cases against stdlib sqlite3 using this exact structure:
  rowcount right after SELECT+fetchall: -1
  Case A deleted = -1 -> if deleted truthy? True
  Case B reported deleted = 200   actual deleted = 1200   remaining rows: 0

Case A (healthy load, nothing stale): `range(0, 0, 500)` is empty so the DELETE never runs, and the last statement on `cursor` is the SELECT, whose rowcount sqlite3 defines as -1. `if deleted:` is truthy for -1, so every clean full_replace save emits "Pruned -1 stale rows from plex_cache (Movies)".

Case B (mass deletion): rowcount reflects only the final `c

**Suggested fix.** Accumulate the count per batch instead of sampling rowcount once after the loop, and clamp the -1 sentinel — matching the `max(cursor.rowcount, 0)` pattern already used at backend/database.py:3840 and :3957:

    deleted = 0
    for i in range(0, len(stale_keys), 500):
        batch = stale_keys[i:i+500]
        placeholders = ','.join('?' for _ in batch)
        cursor.execute(
            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
            batch,
        )
        deleted += max(cursor.rowcount, 0)
    if deleted:
        logger.info("Pruned %d stale rows from plex_cache (%s)", deleted, mode)

Simplest equivalent alternative: `deleted = len(stale_keys)` (the keys came from a SELECT on the same table inside the same lock and transaction, so the two agree), but the accumulator is preferable because it reports what the DB actually did rather than what was intended.

Test that distinguishes right from wrong (the axis the bug is on is the batch count, so a single-batch te

---

### 29. `backend/database.py:1263`  ✅ FIXED — dead _notify_corruption removed (it read a nonexistent module attribute)

*notifications*

**What breaks.** DatabaseManager._notify_corruption looks up notification_bridge as a module-level attribute of backend.app_service, which does not exist — the getattr always returns None, so the in-init corruption alert is unreachable dead code that reports nothing and logs nothing above DEBUG.

**How it happens.** _quarantine_corrupt_db calls _notify_corruption during init_db when a corrupt database is detected and destroyed. The lookup targets the MODULE object `backend.app_service`, but that module defines no top-level `notification_bridge` name (verified: `hasattr(backend.app_service, 'notification_bridge')` is False; AppService instances hold `self.notification_manager`, a different object on a different attribute, assigned at app_service.py:562). getattr therefore always returns None, `isinstance(bridge, NotificationBridge)` is always False, and notify_error is never invoked — for every corruption event, on every code path, forever. The only trace is the pre-existing ERROR log line. This matters because the docstring at database.py:1255-1258 presents it as an attempted alert channel, and it is the ONLY notification attempt that happens at the moment corruption is detected — the startup-time replacement (notify_db_corruption_once) fires later and has the flag-consumption defect described separately.

```python
import backend.app_service as _app_service
            bridge = getattr(_app_service, "notification_bridge", None)
            if isinstance(bridge, NotificationBridge):
```

**Verifier.** The mechanism is real but the "SILENT" impact is not. CONFIRMED dead: backend/app_service.py defines no module-level `notification_bridge` (AST scan of module-level names => False; no `global` stmts; no `globals()[...]` writes), and a repo-wide `grep -rn "\.notification_bridge\s*=" --include=*.py` returns ZERO matches — only `reg._notification_bridge = notif` (api/main.py:130), a different name on a different object. So in `_notify_corruption` (database.py:1261-1264) `bridge = getattr(_app_service, "notification_bridge", None)` is permanently None and `notify_error` never runs. REFUTED impact: the incident is loudly surfaced three other ways, all working. (1) database.py:1229 `logger.error("DATABASE CORRUPTION DETECTED at %s — quarantining and rebuilding a fresh database: %s", self.db_path, e)` always fires. (2) `self._write_corruption_flag(backup_name, e)` persists `{db_path}.corrupt_flag.json`. (3) api/main.py:336 `notify_db_corruption_once(backend.db.db_path, reg._notification_bridge)` runs in the SAME `_init_services` call with a non-None bridge (line 130 precedes line 336), and does `bridge.notify_error("Database corruption was detected and quarantined — check logs")`. The code documents this exact design: database.py:1257 "falls back silently (the ERROR log line above is always emitted regardless, so this is a bonus channel, not the primary signal)" and api/main.py:329-33

**Suggested fix.** Delete `_notify_corruption` (database.py:1253-1268) and its call site at database.py:1234, rather than repointing the lookup — the API entrypoint constructs the NotificationBridge (api/main.py:130) strictly after init_db() runs inside `backend.startup()` (api/main.py:105), so NO in-init bridge lookup can ever succeed there. Replace the call with a one-line comment noting that `notify_db_corruption_once()` (api/main.py:336) is the delivery path, and amend the `_quarantine_corrupt_db` docstring accordingly. If a same-moment channel is genuinely wanted later, it must be a callback injected into DatabaseManager.__init__ by the caller, not a module-attribute probe. Optional hardening: assert in a test that `_write_corruption_flag` + `notify_db_corruption_once` deliver exactly once, since no test currently references `_notify_corruption` at all (grep: 0 test hits), which is why the dead branch went unnoticed.

---

### 30. `backend/database.py:3417`  ✅ FIXED — a failed read is no longer cached as "nothing is dismissed"

*db-integrity*

**What breaks.** _dismissed_urls_set() caches the result of a single SELECT and treats the _query error default ([]) as a legitimate empty set, so one transient read failure permanently blanks the dismissed-URL set for the life of the process. REPRODUCED: 2 rows on disk, cache returned set() forever after one simulated blip.

**How it happens.** First request after startup that needs dismissals hits a transient DB error (lock timeout, bind-mount I/O hiccup). _query returns default=[] and the set comprehension stores an empty set into self._dismissed_cache. Because the lazy guard is `if self._dismissed_cache is None`, an empty set is indistinguishable from 'loaded and genuinely empty' -- there is no invalidation path anywhere (only add/remove mutate it in place, and clear_dismissed_items resets it to set()). For the rest of the process, /results (backend/api/routes/results.py:546 `dismissed = reg.db.get_dismissed_urls()`) filters nothing by exact URL, so every legacy per-URL dismissal (rows with no group_key, which get_dismissed_title_quality explicitly ignores) silently reappears in the deck. Verified: after restoring the healthy _query, get_dismissed_urls() still returned set() while the table still held both rows. The only trace is one 'DB query error' line at the moment of the blip.

```python
if self._dismissed_cache is None:
            rows = self._query('SELECT url FROM dismissed_items', default=[])
            self._dismissed_cache = {row[0] for row in rows}
        return self._dismissed_cache
```

**Verifier.** Not refuted — the defect is real, and I found no guard anywhere. `_query` swallows every exception and hands back the caller's default: `except Exception as e: logger.error("DB query error: %s", e); return default` (database.py:156-158). The lazy loader then treats that default as truth: `if self._dismissed_cache is None: rows = self._query('SELECT url FROM dismissed_items', default=[]); self._dismissed_cache = {row[0] for row in rows}` (3415-3417). `[]` and "empty table" are indistinguishable, and the only two other writes to `_dismissed_cache` in the whole file are `self._dismissed_cache = None` in `__init__` (line 49) and `self._dismissed_cache = set()` in `clear_dismissed_items` (line 3530) — there is no invalidation path, and `close()` nulls `self.conn` without touching the cache. The manager is a process-lifetime singleton (`self.db = DatabaseManager()`, app_service.py:467), so the poisoning lasts for the process. REPRODUCED against the real class: seeded 2 rows, raised one `sqlite3.OperationalError("database is locked")` on the first `SELECT ... FROM dismissed_items`, then restored the healthy connection — "during blip -> set()", "after blip -> set()", "rows still on disk: 2", "raw table: ['http://x/a', 'http://x/b']". The finder's severity is the part I'd correct. The sole consumer is display-side filtering (`dismissed = reg.db.get_dismissed_urls()`, results.py:546) — n

**Suggested fix.** Distinguish "query failed" from "table is empty" by using a sentinel default and refusing to cache a failed read, so the next call retries: in `_dismissed_urls_set()` (database.py:3415), change to `rows = self._query('SELECT url FROM dismissed_items', default=None)` followed by `if rows is None: return set()` before `self._dismissed_cache = {row[0] for row in rows}`. `fetchall()` on an empty table returns `[]`, not `None`, so a genuinely empty table still caches normally; `get_connection()` returning `None` correctly counts as a failure. The two in-place mutators are safe under this: `add_dismissed_items` (3459) and `remove_dismissed_items` (3488) would mutate a throwaway set on the failure path, but since the cache stays `None` the next read reloads from disk and already includes their committed rows. VERIFIED by monkeypatching this exact body onto the real class and re-running the repro: "during blip -> set()", "after blip -> {'http://x/a', 'http://x/b'}", and "after clear -> set() c

---

### 31. `backend/database.py:4749`  ✅ FIXED — reset_applying_rename_jobs returns what it RECOVERED, not what it found

*db-integrity*

**What breaks.** reset_applying_rename_jobs() returns the pre-UPDATE COUNT and ignores the _mutate return value, so startup logs 'Recovered N rename job(s)' even when the recovery UPDATE failed and the jobs are still wedged in 'applying'.

**How it happens.** The box lost power mid-apply, leaving 3 rename jobs in status='applying'. On restart the COUNT(*) succeeds (returns 3) but the UPDATE fails -- e.g. the write hits 'database is locked' because init_db's post-init checkpoint or another startup thread is mid-write, or a disk error on the bind mount. _mutate logs one 'DB Error (reset_applying_rename_jobs)' line and returns False, which is discarded; `return count` still yields 3. backend/api/main.py:310-313 then logs 'Recovered 3 rename job(s) stuck in "applying" after an unclean shutdown'. The jobs remain 'applying', queue_apply skips that state by design, and reset only runs at startup -- so those 3 files are never renamed, never retried, and never appear in the needs-attention lists. The startup log affirmatively states the opposite.

```python
if count:
            self._mutate(
                "UPDATE rename_jobs SET status = COALESCE(prior_status, 'matched'), "
                "prior_status = NULL WHERE status = 'applying'",
                label="reset_applying_rename_jobs")
        return count
```

**Verifier.** The code shape is real and no guard was missed — `return count` is unconditional and nothing consumes `_mutate`'s bool. This file's own precedent proves it is an omission, not a design: backend/rename/service.py:1693-1704 — "SH-H08: DatabaseManager._mutate returns False on a DB failure and never raises -- a bare unchecked call here would let a silently-failed write fall straight through to 'return {\"ok\": True}' below with the file already placed but the job stuck 'applying' forever." Severity is overstated on two counts, though. (a) The stated trigger cannot happen: `get_connection()` hands out one shared `self.conn` and `_query`, `_mutate`, and `checkpoint` every one of them run under `self._lock = threading.RLock()` (database.py:42), so "init_db's post-init checkpoint or another startup thread is mid-write" can never produce a lock error, and `conn.execute("PRAGMA busy_timeout=5000")` covers cross-process contention; a dead connection makes `_query` return its `default=[0]`, so `count = 0` and NO false line is logged at all. Only a disk/corruption error striking between the SELECT and the next UPDATE on the same connection qualifies. (b) "never appear in the needs-attention lists" is false: backend/pipeline_service.py:13 `_PENDING_RENAME_STATUSES = {"pending", "matched", "applying"}` counts them as pending renames, `count_rename_jobs_by_status()` feeds the `applying` bucket

**Suggested fix.** Return the count actually updated, using the rowcount pattern this file already has (`delete_download_result`, database.py:3159-3178), and log loudly on failure:

```python
if not count:
    return 0
try:
    with self._lock:
        conn = self.get_connection()
        if not conn:
            logger.error("reset_applying_rename_jobs: %d job(s) left wedged in "
                         "'applying' — no DB connection", count)
            return 0
        cur = conn.execute(
            "UPDATE rename_jobs SET status = COALESCE(prior_status, 'matched'), "
            "prior_status = NULL WHERE status = 'applying'")
        conn.commit()
        return cur.rowcount
except Exception as e:
    logger.error("DB Error (reset_applying_rename_jobs): %s — %d job(s) remain "
                 "stuck in 'applying'", e, count)
    return 0
```

Minimal alternative if the rowcount rewrite is unwanted: `if not self._mutate(...): logger.error(...); return 0`. Either way main.py:310-313 then logs "Reco

---

### 32. `backend/database.py:2616`  ✅ FIXED — prune counter accumulates per batch instead of reading the last one

*db-integrity*

**What breaks.** The plex_cache full_replace prune reads cursor.rowcount AFTER the batching loop, so it reports only the last batch's deletions; with zero stale keys it reports the SELECT's rowcount of -1. REPRODUCED: 1200 rows deleted, the code's counter said 200.

**How it happens.** A full Plex refresh prunes 1,200 stale rows in three batches (500/500/200). cursor.rowcount after the loop holds 200, so the operator sees 'Pruned 200 stale rows from plex_cache (Movies)' -- an 83% undercount of a destructive operation, which is the only record that the prune happened at all. Separately, when stale_keys is empty the loop never runs and cursor.rowcount still holds the value from the preceding `SELECT key FROM plex_cache`, which sqlite3 reports as -1 (verified); `if deleted:` treats -1 as truthy and logs 'Pruned -1 stale rows from plex_cache (Movies)' on a refresh that deleted nothing.

```python
for i in range(0, len(stale_keys), 500):
                        batch = stale_keys[i:i+500]
                        placeholders = ','.join('?' for _ in batch)
                        cursor.execute(
                            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
                            batch,
                        )
                    deleted = cursor.rowcount
```

**Verifier.** Could not refute; both halves reproduce exactly. The code at database.py:2609-2618 is: `for i in range(0, len(stale_keys), 500): batch = stale_keys[i:i+500]; ... cursor.execute(f"DELETE FROM plex_cache WHERE key IN ({placeholders})", batch,)` then `deleted = cursor.rowcount` / `if deleted:` / `logger.info("Pruned %d stale rows from plex_cache (%s)", deleted, mode)`. sqlite3's rowcount reflects only the LAST executed statement, so after the loop it holds the final batch's count. Verified empirically with the loop copied verbatim: 1200 stale keys -> batches logged 500, 500, 200 -> `deleted (as code computes it): 200`, while `actual remaining rows: 0` (the deletes themselves are correct). Zero-stale half also confirmed: the preceding `all_existing = cursor.execute("SELECT key FROM plex_cache WHERE content_type = ?", (mode,)).fetchall()` leaves `rowcount == -1` (printed "rowcount right after SELECT: -1"); with an empty stale_keys the loop body never runs, so `deleted = -1` and `bool(-1) is True`, logging "Pruned -1 stale rows". No guard was missed: `deleted` is consumed ONLY by that log line (grep found exactly two hits in the repo, both on lines 2616/2618), so there is no data-integrity impact, and no test asserts on the message. The path is live, not dead code -- plex_service.py:427 `self.db.save_plex_cache(self.plex_movies, "Movies", full_replace=True)` and plex_service.py:437 `

**Suggested fix.** Accumulate the count inside the loop instead of reading rowcount after it. Replace lines 2609-2616 with:

    deleted = 0
    for i in range(0, len(stale_keys), 500):
        batch = stale_keys[i:i+500]
        placeholders = ','.join('?' for _ in batch)
        cursor.execute(
            f"DELETE FROM plex_cache WHERE key IN ({placeholders})",
            batch,
        )
        deleted += cursor.rowcount

Initializing to 0 fixes both halves at once: the sum now reports all 1200 rows, and the empty-stale case leaves deleted == 0, which is falsy, so `if deleted:` correctly suppresses the log rather than printing -1. (`len(stale_keys)` would also work as the count, but summing rowcount reports rows actually removed rather than rows attempted.) Optionally add a regression test that prunes >500 stale rows and asserts the logged count equals the number of rows the table actually lost, plus a zero-stale case asserting no "Pruned" record is emitted -- pick >500 so the batching boundary is 

---

## Refuted — do not re-raise

These were raised by a finder and knocked down on verification. Recorded so
the next audit does not spend budget rediscovering them.

- `backend/download_queue.py:1170` — retry_item() unconditionally forces the whole batch back to 'scheduled' and NULLs its cooldown_until, which permanently disqualifies every OTHER deferred item in that batch from _maybe_auto_resume() —
- `backend/download_queue.py:492` — enqueue_retry() omits auto_resume_after_cooldown from its INSERT, so it always defaults to 0 — every single-item grab deferred by a source block is parked forever even when the user has enabled auto-r
- `backend/download_queue.py:1092` — Automated resume is one-shot per batch for the batch's entire lifetime: the guard is `auto_resume_used = 0` and _resume_batch increments it, so a second source block strands the batch permanently.
- `backend/download_queue.py:1057` — _refresh_batch_locked marks a batch state='completed' whenever no items remain active — including a batch in which every single item failed or was cancelled.
- `backend/download_queue.py:1524` — list_retries reports due=True and retry_available=True for items in waiting_source/verification_required that no code path will ever claim, because 'due' is derived from a stale scheduled_for that _pa
- `backend/auto_grab_service.py:184` — AutoGrabService counts skipped duplicates as successful grabs: download_item returns success=True with method 'duplicate'/'duplicate_similar' when it deliberately does nothing, and the report incremen
- `backend/plex_service.py:293` — A single per-item extraction failure anywhere in the library disables the stale-row prune for that whole content type, and nothing else ever removes cache rows — so movies deleted from Plex keep repor
- `backend/notifications.py:741` — _send_notification is the single aggregation point for delivery outcome and it discards everything: channel exceptions captured by return_exceptions=True are never logged, total delivery failure is re
- `backend/notification_bridge.py:109` — NotificationBridge.send() returns bare on an unconfigured or already-shut-down bridge with no log at any level, and configure()'s import-failure path logs at DEBUG and returns leaving the bridge perma
- `backend/api/main.py:497` — The auth gate fails OPEN when `app.state.protected_segments` is missing (`getattr` default is an empty frozenset), and `_compute_protected_segments` silently skips any prefix-less router whose route p
- `backend/api/__main__.py:17` — `--no-auth`, which the Docker entrypoint passes unconditionally on every start, *overwrites* an operator-supplied `SCANHOUND_AUTH_NONCE` with an empty string rather than leaving it alone, silently dis
- `backend/database.py:106` — PRAGMA foreign_keys is never enabled on any connection (grep across backend/ shows only journal_mode, synchronous, busy_timeout), so SQLite's default OFF applies and every FK constraint and ON DELETE 
- `backend/scanner_service.py:488` — A Deep Scan wipes the incremental scanned_urls baseline before crawling; if the scan is stopped or fails, nothing is written back and the baseline is left permanently empty.

## Unverified — plausible, not reproduced

- `backend/download_service.py:1033` — When JD's query_links call fails but query_packages succeeds, poll_results derives every package's state from zero child links and persists a regression: 'extracted' silently becomes 'downloaded' and 
- `backend/api/routes/auth.py:42` — The login rate limiter keys on `request.client.host`, which behind NPM/Cloudflare is a single proxy container IP — so all remote clients share one bucket and any attacker can hold the only credential-
- `backend/api/dependencies.py:269` — `secrets.compare_digest` raises `TypeError` on a non-ASCII `str`, so a non-ASCII bearer token or `?token=` value crashes the auth check whenever a nonce is configured.
