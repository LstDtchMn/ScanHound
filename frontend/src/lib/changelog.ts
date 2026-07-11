export interface ChangelogEntry {
  version: string;
  date: string; // ISO YYYY-MM-DD
  summary: string;
  changes: string[];
}

export const changelog: ChangelogEntry[] = [
  {
    version: "2.25.2",
    date: "2026-07-10",
    summary: "TV shows no longer show as Missing; cleaner conflict row",
    changes: [
      "Fixed a bug where every TV show in your Plex library was flagged as \"Missing\" in scans. The Plex cache was silently discarding all TV entries every refresh (a key-matching bug that deleted TV rows the moment they were written), so nothing on the TV side ever had anything to match against. TV rows now persist — after this update, the next Plex cache refresh repopulates them.",
      "Redesigned the \"already in the library\" conflict row on the Renames page: instead of a long, cut-off line with raw byte counts, it now shows a compact badge plus a GB size chip — \"same size · 13.4 GB\" for a likely duplicate, or \"22.1 GB → 28.7 GB\" when the incoming file differs — with the full comparison one click away on Compare.",
    ],
  },
  {
    version: "2.25.1",
    date: "2026-07-10",
    summary: "Rename conflicts: readable message + side-by-side compare",
    changes: [
      "When a rename would land on a file that already exists, the Renames list now shows a plain-language summary (e.g. \"Already in the library at the same size (13.4 GB) — likely a duplicate\") instead of a raw path with two 11-digit byte counts.",
      "A same-size conflict is flagged as a likely duplicate so it's a one-glance skip.",
      "A new Compare button opens the existing side-by-side file comparison (resolution, HDR/DV, codec, audio, bitrate, size, duration) with a recommendation and Overwrite / Keep both / Skip actions — previously only available on mobile.",
      "Same-size and different-size files are still always held for review — nothing is ever auto-replaced or silently dropped.",
    ],
  },
  {
    version: "2.25.0",
    date: "2026-07-10",
    summary: "Pipeline Tracker: see what never made it to Plex",
    changes: [
      "New Pipeline page tracks every grab all the way through download, extraction, and renaming to Plex — and flags the ones that stalled: never started, download failed, rename failed, or renamed but never showed up in Plex.",
      "Each stalled item gets a 'Re-grab' button (retries the same links) and a 'Search sources' button that looks across your other configured sites for an alternative release to grab instead.",
      "Verified items are marked done automatically once Plex actually has the file — checked hourly in the background, so nothing needs a manual refresh.",
      "Dismiss hides an item for good; everything else keeps re-checking itself as your library changes.",
      "On mobile, Pipeline is a tab inside Downloads (Queue / Pipeline switch) instead of a separate nav entry.",
    ],
  },
  {
    version: "2.24.1",
    date: "2026-07-10",
    summary: "Duplicate downloads now coexist and cancel individually",
    changes: [
      "If the same release ends up grabbed twice (an accidental double-add), both copies now show up in Downloads instead of one silently overwriting the other's row — so the duplicate is actually visible, and 'Cancel' removes exactly the one copy you clicked (previously it could clear both).",
      "'Keep best' on a duplicate group now only ever cancels other still-downloading copies — it will never cancel a finished download or a historical record, so re-grabbing something you already have can't kill the new download.",
      "Under the hood, downloads are now tracked by JDownloader's durable package id instead of by name; the existing download history migrates automatically on update (every row preserved) and nothing else changes.",
    ],
  },
  {
    version: "2.24.0",
    date: "2026-07-09",
    summary: "Mobile Renames: one-at-a-time review + real conflict resolution",
    changes: [
      "The Renames screen on your phone is now a focused review instead of a cramped list: a summary shows how many matches are ready (100% confident) vs. how many need a look, 'Apply all' clears the confident ones in a tap, and 'Review' opens a full-screen deck that walks you through the uncertain ones one at a time — with the match percentage, the reason, and the full before → after name finally readable.",
      "Swipe or tap through the deck; applying, skipping, or removing an item advances automatically. A scope toggle switches between reviewing only the under-100% items or all of them.",
      "'A file already exists' is now resolvable, not just a warning. The card compares the existing file and the incoming one side by side — resolution, HDR/Dolby Vision, video and audio codecs, bitrate, size, duration — recommends which to keep (judged on the files' actual specs, so it won't tell you to overwrite a 2160p DV remux with a tag-heavy 1080p), and offers Overwrite, Keep both, or Skip.",
      "Overwrite is safe: the file it replaces is moved to the recoverable Trash (never deleted), and Undo restores it.",
      "When a Dolby Vision file's FEL vs MEL layer isn't known yet, a 'Scan DV layers' button detects it in the background and fills it into the comparison.",
      "The desktop Renames page is unchanged.",
    ],
  },
  {
    version: "2.23.2",
    date: "2026-07-09",
    summary: "Fix: bottom navigation was unreachable on phones",
    changes: [
      "Fixed the bottom navigation bar (Scan / Downloads / Renames / Watchlist / Stats / Settings) being pushed off the bottom of the screen on mobile browsers — it sat behind the address bar because the app used a fixed 100vh height. It now uses the dynamic viewport height, so the nav is always visible and you can reach Downloads and Renames from the scan page.",
    ],
  },
  {
    version: "2.23.1",
    date: "2026-07-09",
    summary: "Two minor fixes from a code-review pass",
    changes: [
      "Fixed the Missing count occasionally reading one too high after a failed dismiss that raced with a background refresh (self-corrected on the next load; now it never over-counts)",
      "The desktop Downloads page now loads correctly if you resize a browser window across the phone/desktop breakpoint instead of showing an empty view until reload",
    ],
  },
  {
    version: "2.23.0",
    date: "2026-07-09",
    summary: "Mobile Downloads: live progress, controls, and duplicate cleanup",
    changes: [
      "The mobile Downloads tab now shows your JDownloader queue live — per-item progress bars, size, host, and status (queued/downloading/extracting/finished/failed), refreshing automatically while the tab is open",
      "Pause, resume, or stop the whole queue from your phone, and clear finished/failed downloads with one tap",
      "Duplicate releases of the same title (or an accidental double-grab) are grouped together with a 'Keep best, cancel rest' action, so cleaning up a cluttered queue no longer means guessing which release is which",
      "Canceling a download now actually removes it from JDownloader, not just the list",
    ],
  },
  {
    version: "2.22.1",
    date: "2026-07-09",
    summary: "Group titles by their corrected name, not the raw scrape",
    changes: [
      "Fixed a title that was mis-parsed on scrape (a release-group name bleeding into the title — e.g. 'Killing Faith' briefly grouping under 'guakillingfaith') from appearing as a separate look-alike tile. Every title's group is now rebuilt from its final, corrected name after metadata lookup, so all releases of a title stay on one card. As a bonus this makes the TV upgrade/duplicate overlay match correctly for the first time. (Existing entries heal on the next background scan.)",
    ],
  },
  {
    version: "2.22.0",
    date: "2026-07-09",
    summary: "DV-aware upgrades + language on cards",
    changes: [
      "Upgrades no longer talk you into losing Dolby Vision: a bigger same-resolution file that would DROP DV (your copy has DV, the new one doesn't) is now only flagged as an upgrade if it clears a separate, higher threshold — new 'DV-loss Upgrade Threshold (%)' setting, default 20%. Normal DV-preserving upgrades still use your Upgrade Sensitivity. So a slightly-bigger HDR10 file no longer reads as an upgrade over your 4K Dolby Vision copy.",
      "Each title's language now shows on the triage-deck card (leading the genres line), so foreign-language releases are obvious at a glance.",
    ],
  },
  {
    version: "2.21.1",
    date: "2026-07-09",
    summary: "Fix wrong title/poster on mis-scraped docuseries releases",
    changes: [
      "Fixed a release showing another title's name and poster — and appearing as a separate look-alike tile in the deck — when it was scraped with the wrong IMDb ID. This mostly hit episodes of a docuseries that share a series name (e.g. 'Untold UK: Liverpool's Miracle of Istanbul' picking up 'Untold UK: Vinnie Jones'). ScanHound now checks that an IMDb-matched title actually matches the release before trusting it, and re-derives the correct match when it doesn't.",
    ],
  },
  {
    version: "2.21.0",
    date: "2026-07-09",
    summary: "Week-in-review bug sweep: duplicate protection, safer applies, accurate counts",
    changes: [
      "Fewer duplicate downloads: auto-grab now records each release's year, so a title you already own is matched precisely — and TV seasons you've grabbed are correctly recognized instead of quietly resurfacing as Missing after the source rolls out of cache",
      "Undo actually works again on the phone: undoing a dismissed or skipped title brings it right back to the deck (in the browse view it had been silently doing nothing), and a dismiss that fails to reach the server no longer makes the title vanish from your list",
      "The Missing count stays honest after a grab — sibling releases that drop out of the list no longer leave the counter stuck too high",
      "Applying a rename is safer: a file whose library isn't configured is now held for review with a clear message instead of being moved into a hidden location, and a rename you flagged for review is never silently promoted to auto-apply after an unclean shutdown or crash",
      "A Cloudflare-blocked source no longer purges still-listed titles from your results (they were being wrongly dropped as 'gone' and reappearing later as brand-new), and a normal end-of-list page is no longer mistaken for a block",
      "Under the hood: download history now persists reliably, re-identified items refresh their info immediately, and disposed files during a move are kept on the source drive instead of being copied into app storage",
    ],
  },
  {
    version: "2.20.0",
    date: "2026-07-09",
    summary: "Rename progress bars + crash-safe, corruption-verified moves",
    changes: [
      "Applying files now shows progress: a per-item bar with live GB/percent for a real cross-drive copy (instant same-drive moves just flash), plus an overall 'Applying X of N' bar for a whole batch",
      "Moves are now crash- and power-loss-safe: the file is streamed to a temp sidecar, flushed to disk, verified, then swapped into place in one atomic step — a crash never leaves a half-written file at the destination, and your original is always kept until the copy is confirmed good",
      "End-to-end corruption check: every cross-drive copy is checksum-verified by reading the bytes back from the physical disk (not memory), so a bad write or transfer error is caught and rejected instead of silently replacing your file",
      "If the app or PC dies mid-move, any job left half-applied is automatically recovered on restart so you can just re-apply it",
    ],
  },
  {
    version: "2.19.0",
    date: "2026-07-09",
    summary: "Instant 4K renames",
    changes: [
      "4K movie downloads can now land directly on the same drive as your 4K library (new '4K Movies Download Folder' setting), so the post-download rename is an instant move instead of a slow cross-drive copy — a 50GB remux goes from ~18 minutes to a fraction of a second",
      "Measured the difference: a cross-drive copy through the container runs ~4.7x slower than a native Windows transfer; keeping the file on one drive avoids the copy entirely",
      "Fixed a settings bug where saving your path mappings silently failed",
    ],
  },
  {
    version: "2.18.0",
    date: "2026-07-09",
    summary: "Self-healing scraper + far quieter logs",
    changes: [
      "The link scraper now recovers on its own when the browser hits a transient network error (it was previously misreporting these as a Cloudflare wall and giving up until a restart)",
      "A Cloudflare-blocked source no longer floods the log with hundreds of identical warnings — it backs off and reports once",
      "Silenced harmless headless-server noise (desktop-notification errors) and self-healed a Plex URL that was missing its http:// prefix",
    ],
  },
  {
    version: "2.17.0",
    date: "2026-07-08",
    summary: "Smarter downloads, reliable applies, and rename fixes",
    changes: [
      "Duplicate protection: grabbing a title you already have at the same-or-lower quality is now skipped automatically (only a genuine upgrade — higher resolution or added Dolby Vision — goes through), so you stop re-downloading the same movie",
      "Skipped and downloaded titles stay hidden when you reopen the app, and no longer resurface — unless a better version appears",
      "Applying renames no longer times out on big files: applies run in the background with an 'Applying…' status and land live, so moving a large 4K file can't fail the request anymore",
      "Fixed a matcher bug where underscore-named files (e.g. 'The_Threesome_2025') lost their last title word and matched the wrong movie; re-identifying now gets them right",
      "Posters now show for older items in the Renames list, and the list scrolls properly again",
    ],
  },
  {
    version: "2.16.0",
    date: "2026-07-07",
    summary: "Title-level skip (dismiss a title, not just one release)",
    changes: [
      "Swiping a title away ('skip') now hides not just that release but same-or-lower-quality re-uploads of the same title on future scans — so a smaller or identical re-encode won't keep resurfacing after you've said no",
      "A genuine upgrade over what you skipped — a higher resolution, or Dolby Vision the skipped copy lacked — still surfaces, so you never miss a real improvement",
      "The rule is applied server-side, so a dismissed title stays dismissed when you come back later, on both the app and the web",
    ],
  },
  {
    version: "2.15.0",
    date: "2026-07-07",
    summary: "Download memory, JDownloader auto-start, and deck grouping",
    changes: [
      "Grabbed releases are now remembered in a central database: a title you downloaded stays marked 'Downloaded' across reloads and on every device (app + web), without waiting for a re-scan",
      "The swipe deck now shows one card per title with a quality picker — choose the resolution/edition you want instead of swiping through every duplicate release",
      "Sending to JDownloader now auto-starts the download and skips anything already grabbed, so there are no duplicate entries",
      "The 'Grabbed' label now shows the resolution and size you got (e.g. 4K · 65.9 GB), and rating vote counts now appear next to scores",
      "Sibling releases of a grabbed title reclassify correctly — year-aware (a 2021 remake never gets confused with the 1984 original) and Dolby-Vision-aware — instead of lingering as red 'Missing'",
    ],
  },
  {
    version: "2.14.0",
    date: "2026-07-07",
    summary: "Mobile-native Scan experience",
    changes: [
      "A brand-new phone experience for Scan: a swipeable poster deck (swipe right to grab, left to dismiss), long-press for actions, and haptic feedback",
      "Pull-to-refresh, a bottom toolbar (search / filters / deck / bulk), and a drag-up detail sheet showing your In-Library versions and prior grab for upgrade decisions",
      "The scan bar and filter chips auto-hide as you scroll (like a browser address bar) to maximize the poster wall, with a 1-up / 2-up poster toggle",
      "Rotten Tomatoes Tomatometer theming, on-poster ownership, larger legible text on the deck and tiles, and a movies-only 4K/1080p filter",
    ],
  },
  {
    version: "2.13.0",
    date: "2026-06-29",
    summary: "Configurable columns + 'Downloaded Similar' status",
    changes: [
      "Grid columns are now configurable inline (Auto, or a fixed 2–8) in the Grid menu — Auto sizes by your chosen tile size; the default is now responsive instead of a fixed 5",
      "New orange 'Downloaded Similar' status: a release of a title you've already grabbed that is the same-or-worse quality (no resolution bump, no added Dolby Vision) is now marked orange instead of red 'Missing', so you can tell at a glance you effectively already have it",
      "Genuine upgrades (a higher-resolution sibling, or one that adds Dolby Vision the grab lacked) stay red 'Missing' — they're still worth grabbing",
    ],
  },
  {
    version: "2.12.4",
    date: "2026-06-29",
    summary: "Configurable grid view",
    changes: [
      "New Grid options (a 'Grid ▾' menu next to the view toggle, and in the mobile filter sheet): tile size (S/M/L), poster aspect (2:3 / 16:9 / 1:1), spacing (tight/normal/roomy), and a 'Poster only' mode that hides the meta for a clean poster wall",
      "All grid options are remembered per device and apply instantly; the fixed-column-count setting in Settings still overrides tile size when set",
    ],
  },
  {
    version: "2.12.3",
    date: "2026-06-29",
    summary: "Fix broken tile/grid view",
    changes: [
      "Grid view now renders as a proper responsive wall of compact poster cards instead of collapsing to one giant full-width tile — the grid cells couldn't shrink the cards (missing min-width:0 on the grid items), so a single oversized column took the whole row",
      "Applies to both flat results and expanded duplicate-release groups",
    ],
  },
  {
    version: "2.12.2",
    date: "2026-06-29",
    summary: "Grabbed-version note on sibling releases",
    changes: [
      "After you grab one version of a title, the OTHER versions now show a 'grabbed similar' note with the specs you got (e.g. 4K · DV · 19.7 GB) — even when they're the same resolution — instead of silently flipping to Downloaded or showing nothing",
      "Sibling versions stay visible and grabbable, so you can still pick a different edition (a DV upgrade, a different cut) after comparing",
      "The note now appears the instant you grab (no waiting for the next background scan) and persists across a reload",
    ],
  },
  {
    version: "2.12.1",
    date: "2026-06-29",
    summary: "DV scanner + keep-picker review fixes",
    changes: [
      "The keep-recommendation no longer mistakes camera/rip tags ('DV.Cam', 'dv-rip') for Dolby Vision, and won't recommend a worse copy when the better one is already in your library",
      "The Dolby Vision Scan button now stays disabled for the whole scan (driven by completion, not a fixed timer), so you can't accidentally kick off a second run",
      "DV scan now accounts for every file even when one fails to read — failures show as '?' in the inventory instead of silently vanishing and stalling the progress count",
      "Hardened the jobs/DV-inventory endpoints against an oversized result request",
    ],
  },
  {
    version: "2.12.0",
    date: "2026-06-29",
    summary: "Dolby Vision FEL/MEL scanner + duplicate keep-picker",
    changes: [
      "New 'Dolby Vision…' action on the Renames page: point it at a folder and it reads each file with dovi_tool to detect Dolby Vision FEL vs MEL, building an inventory with FEL/MEL/P5 badges and live progress (unchanged files are skipped between runs)",
      "Duplicate-target pairs now get a '★ Keep' recommendation on the better release — it reads the filenames for resolution, Dolby Vision, HDR, source (Remux/BluRay/WEB-DL) and audio (Atmos/TrueHD) and marks which copy to keep, with the reason",
      "Detection-only for now: the DV scan records results but doesn't move, tag, or label anything (Plex/Kometa labeling and file tagging come next)",
    ],
  },
  {
    version: "2.11.3",
    date: "2026-06-29",
    summary: "Review hardening",
    changes: [
      "Duplicate-target detection now also flags a new grab whose destination is already occupied by a file you've applied to the library — not just two pending jobs",
      "Closed a race where a manual 'process folder' running at the same time as an automatic post-download rename could create two jobs for the same file",
    ],
  },
  {
    version: "2.11.2",
    date: "2026-06-29",
    summary: "Duplicate-target detection + library guard",
    changes: [
      "When two releases of the same movie would be renamed to the same destination file, both are now flagged with a ⚠ Duplicate badge in the Renames list — so you pick one instead of discovering the clash only when the second apply fails",
      "A confident TV (or movie) match whose target library isn't configured now holds for review with a clear 'library not configured' message, instead of building a broken path that would drop the file in the wrong place",
    ],
  },
  {
    version: "2.11.1",
    date: "2026-06-29",
    summary: "Race-condition and category fixes",
    changes: [
      "Auto-rename no longer silently skips remaining files in a package that was interrupted mid-flight — it now resumes from exactly the files that don't yet have jobs (per-file dedup instead of per-package coarse check)",
      "Download history enrichment no longer holds the database lock while parsing JSON — other DB operations are no longer blocked during the startup backfill",
      "A release whose URL is first seen by the 4K scanner now keeps its '4K' category even if the Remux scanner later picks up the same URL — prevents a filter-invisible item caused by last-write-wins overwriting",
    ],
  },
  {
    version: "2.11.0",
    date: "2026-06-29",
    summary: "Dry-run preview, health check, and matching polish",
    changes: [
      "Process folder → Preview: see exactly what each file would be renamed to, with no jobs created and nothing moved",
      "New rename health check reports whether ffmpeg/ffprobe/tesseract and the Ollama model are actually available, so a silently-broken dependency is visible",
      "Year-aware retry: dropping the year to widen a search no longer lets a wrong-year remake outrank the correct match",
      "Per-file TMDB result caching removes the redundant searches the fallback chain used to repeat",
      "Background pre-cache category set is now configurable (background_scan_categories) for operators on a tight scrape/TMDB budget",
    ],
  },
  {
    version: "2.10.3",
    date: "2026-06-29",
    summary: "Code-review polish (minor batch)",
    changes: [
      "Owned-version comparison now shows ↓ Lower / = Same as well as ↑ Upgrade, so a worse or equal release is no longer indistinguishable from an upgrade",
      "Download buttons on the detail panel and grid tiles now send the release specs, so later 'already grabbed' chips show resolution/HDR/DV instead of blanks",
      "Multi-part files (Part 10+) no longer truncate to 'Part 1'; custom rename templates keep the Part suffix",
      "An embedded IMDb id no longer leaks into the search title; path mappings require a folder boundary (no more F:\\Downloads matching F:\\Downloads2)",
      "'Re-identify all' now only re-runs reviewable jobs (won't churn good matches) and bulk rename ops are single-flighted",
      "TV episode-detection failures degrade gracefully instead of risking the whole file; the process-folder path is remembered instead of hardcoded",
    ],
  },
  {
    version: "2.10.2",
    date: "2026-06-28",
    summary: "Code-review fixes",
    changes: [
      "Re-identify no longer risks losing a job — it now rebuilds the match first and only removes the old job once the new one exists",
      "A transient/empty Plex no longer downgrades owned titles to Missing in the background cache (download-history upgrades still apply)",
      "Fixed release-tag stripping deleting real one-word titles (Stan, Opus, Hybrid, Dual…) — the leading title word is now protected",
      "Stats now attribute 4K renames to 'Movies (4K)' instead of folding them into 'Movies' (library-root prefix collision)",
      "The IMDb id embedded in a filename is now persisted on the rename job instead of being dropped",
      "Background scan no longer ages out still-listed releases after an early-stopped crawl",
      "The 4K/Remux/TV display filter now survives a reload instead of being reset to 4K-only on every mount",
    ],
  },
  {
    version: "2.10.1",
    date: "2026-06-28",
    summary: "OCR cast-matching",
    changes: [
      "The OCR rung now deterministically matches the cast and director printed in the credits against each candidate's TMDB cast — so a film is identified even when its title never appears on-screen, with no AI model involved",
      "Guarded against false positives: requires at least two distinct people and a strictly clear winner before it will claim a match",
      "Credit frames are now extracted with accurate seeking, so OCR (and the cast match) is reproducible run-to-run",
      "Safety: matches found by the 'read the file' fallbacks (subtitles / OCR / vision) always go to needs-review and never auto-apply — they're assists, not authorities",
    ],
  },
  {
    version: "2.10.0",
    date: "2026-06-28",
    summary: "Smarter last-resort matching: IMDB, subtitles, OCR",
    changes: [
      "IMDB-id fast path: when a release filename carries a tt-id, it's resolved directly via TMDB /find — an exact, 100%-confidence match with no fuzzy guessing",
      "The vision fallback is now constrained to real TMDB candidates (multiple-choice) instead of guessing open-ended — far less hallucination",
      "New subtitle-based identification: reads the file's dialogue track and matches it to a candidate (a cheap text call, tried before vision)",
      "New OCR-the-credits identification: reads the title card / end credits with tesseract; a title printed on-screen is a decisive match",
      "Fallback order is now cheapest-first: filename → IMDB → subtitles → OCR → vision, each only running if the previous left the match weak",
    ],
  },
  {
    version: "2.9.1",
    date: "2026-06-28",
    summary: "Re-identify rename jobs",
    changes: [
      "New 'Re-identify' button on each needs-review/failed rename job re-runs the current matcher in place — no need to remove a stale job before retrying",
      "'Re-identify all' button in the Renames header re-runs identification across every reviewable job at once",
    ],
  },
  {
    version: "2.9.0",
    date: "2026-06-28",
    summary: "Smarter auto-rename matching",
    changes: [
      "Fixed the core bug: underscore-delimited scene releases (Title_Year_...) now parse their year correctly, so the year no longer pollutes the TMDB search",
      "Much more comprehensive release-junk stripping (Hybrid, MUBI, more platforms/codecs/audio/editions), case-insensitive",
      "Handles a.k.a. / dual titles — searches both the original and alternate (e.g. Ohikkoshi a.k.a. Moving)",
      "Identification now retries with principled variations (drop the year, strip a subtitle, try the alias) and a cross-type multi-search fallback, all gated on the confidence threshold",
      "Ollama fallback fixed: empty model now actually short-circuits instead of calling the LLM with no model; default URL points at the reachable instance",
      "Per-file rename trace logging (parse, each query, decision, move) at INFO so failures are diagnosable from logs",
    ],
  },
  {
    version: "2.8.0",
    date: "2026-06-28",
    summary: "Process a folder for auto-rename",
    changes: [
      "New 'Process folder…' action on the Renames page: point it at a folder (e.g. F:\\Downloads) and it identifies every video and creates rename jobs — no JDownloader needed, so you can rename an existing backlog",
      "Host paths are translated to the container's mounted view; already-tracked files are skipped; matches still go through review before moving",
    ],
  },
  {
    version: "2.7.0",
    date: "2026-06-28",
    summary: "Upgrade-vs-owned comparison in groups",
    changes: [
      "When a release group has a version you've already downloaded, the still-missing siblings now show a comparison against it — e.g. '↑ Upgrade +DV · +7.6 GB vs your 4K · 12.1 GB' — so it's obvious which missing versions are actual upgrades",
    ],
  },
  {
    version: "2.6.2",
    date: "2026-06-28",
    summary: "Site Search results no longer filtered out",
    changes: [
      "HDEncode 'Site Search' results are now tagged so the 4K/Remux/TV category toggles never hide them — explicit searches always show their full results",
    ],
  },
  {
    version: "2.6.1",
    date: "2026-06-28",
    summary: "Detail panel poster no longer cropped",
    changes: [
      "The poster at the top of the detail panel now shows in full (object-contain) with a blurred fill behind it, instead of center-cropping and cutting off the top/bottom",
    ],
  },
  {
    version: "2.6.0",
    date: "2026-06-28",
    summary: "Auto-rename file access (host ⇄ container)",
    changes: [
      "Bind-mounted the F:/G: download/library drives into the container so auto-rename can actually read and move files",
      "JDownloader's Windows save paths are now translated to the container's mounted view via a configurable mapping (Settings → Renaming → Download path mappings)",
      "Set the 1080p library to F:\\Downloads and 4K library to G:\\Downloads",
    ],
  },
  {
    version: "2.5.0",
    date: "2026-06-28",
    summary: "Auto-rename stats on the Stats page",
    changes: [
      "Stats page now has an Auto-Rename card: total files renamed and moved, total jobs, needs-review and failed counts",
      "Plus a breakdown of how many files were moved into each library directory",
    ],
  },
  {
    version: "2.4.0",
    date: "2026-06-28",
    summary: "Background scan refreshes cached statuses",
    changes: [
      "Each background scan now re-checks the WHOLE cached list against your current Plex library and download history — no re-scraping — so already-cached items update their In Library / Upgrade / Downloaded / Grabbed status instead of staying frozen from when they were first seen",
      "Only changed rows are rewritten, and retention (last seen) is left untouched, so the refresh is cheap and doesn't keep stale items alive",
      "Trigger it on demand any time via Settings → Background → Scan now",
    ],
  },
  {
    version: "2.3.6",
    date: "2026-06-28",
    summary: "Status labels next to the title",
    changes: [
      "Status labels now sit inline right after the title/year — both on individual rows and (as per-status counts) on collapsed group headers — instead of being right-aligned, so they read consistently regardless of row width",
    ],
  },
  {
    version: "2.3.5",
    date: "2026-06-28",
    summary: "Right-align grouped status badges",
    changes: [
      "On collapsed group rows, the status badges (e.g. 2 Upgrade · 1 In Library) now align flush to the right edge — the title cell spans the empty actions column instead of leaving a gap",
    ],
  },
  {
    version: "2.3.4",
    date: "2026-06-28",
    summary: "Aligned status bars + In Plex placement",
    changes: [
      "Every row's left status bar — individual and collapsed group — now uses one shared technique, so they line up into a single continuous vertical strip",
      "The In Plex versions capsule moved to just right of the release size, with an 'In Library' caption above it",
    ],
  },
  {
    version: "2.3.3",
    date: "2026-06-28",
    summary: "Accurate grouped-row status colors",
    changes: [
      "Collapsed group bar is now status-accurate: a group of upgrades reads yellow, not green",
      "The bar is multicolored again for mixed groups — one vertical segment per status present (e.g. top-half yellow upgrade, bottom-half green in-library)",
      "Collapsed group header now lists every status in the group with counts (e.g. 2 Upgrade · 1 In Library)",
    ],
  },
  {
    version: "2.3.2",
    date: "2026-06-28",
    summary: "In-Plex versions moved up + restyled",
    changes: [
      "The 'In Plex' owned versions now sit on the top stat line, just left of the release size, as a distinct gold Plex capsule with a chevron glyph so what you already have stands out at a glance",
    ],
  },
  {
    version: "2.3.1",
    date: "2026-06-28",
    summary: "Clearer end of expanded groups",
    changes: [
      "Expanded version groups now have a gap and divider after their last row, so it's obvious where the group ends",
    ],
  },
  {
    version: "2.3.0",
    date: "2026-06-27",
    summary: "4K/Remux/TV toggles filter instantly",
    changes: [
      "The background scan now pre-caches every category (4K, Remux, TV), and items are tagged with their source category",
      "Ticking 4K/Remux/TV now filters the loaded list instantly — selecting Remux shows Remux releases alongside the 4K you already have, no re-scan needed",
      "Legacy cached items (no category yet) are inferred as 4K movies / TV packs so the filter works right away; the next background scan tags everything precisely",
    ],
  },
  {
    version: "2.2.0",
    date: "2026-06-27",
    summary: "Fix Renaming settings tab",
    changes: [
      "Settings → Renaming now opens correctly. The template-token tooltips contained literal {{token}} placeholders that Svelte parsed as code, throwing at render and freezing the page on the previous tab (it looked like Renaming showed 'Background Scan'). The real fix, not the earlier scroll workaround.",
    ],
  },
  {
    version: "2.1.9",
    date: "2026-06-27",
    summary: "Indent grouped version rows",
    changes: [
      "When a release group is expanded in list view, its individual version rows are now slightly indented so they read as nested under the group",
    ],
  },
  {
    version: "2.1.8",
    date: "2026-06-27",
    summary: "Grabbed chip now shows real info",
    changes: [
      "Bulk grabs (Download All, swipe deck, downloads queue) now record resolution, size, HDR, and DV — previously they only stored the title, so the Grabbed chip had nothing to show",
      "Existing download history is backfilled with resolution/size/HDR/DV from the scan cache (matched by URL) on startup, so already-grabbed items show their details too",
    ],
  },
  {
    version: "2.1.7",
    date: "2026-06-27",
    summary: "Fix missing list posters",
    changes: [
      "Posters reappear in list view — the wider title column (2.1.5) was squeezing the poster cell, and the base img max-width then collapsed the thumbnail; the poster column now has a hard min-width floor",
    ],
  },
  {
    version: "2.1.6",
    date: "2026-06-27",
    summary: "Background scanner reliability",
    changes: [
      "Background pre-cache scans can no longer run concurrently with a manual or scheduled scan — they share one scanner and were corrupting each other's results (shared scan lock)",
      "Background scans now yield to any foreground scan instead of competing with it",
      "Background scans no longer reset the incremental-scan baseline the scheduler relies on",
      "Background scans skip already-cached releases and stop crawling a source at the previously-seen endpoint — far less redundant scraping; still-listed items are kept fresh so they aren't wrongly purged",
      "Background status now reports per-source results and errors, and the next-run time no longer drifts after a failed run",
    ],
  },
  {
    version: "2.1.5",
    date: "2026-06-27",
    summary: "Glanceable decision stat line",
    changes: [
      "Rating, Rotten Tomatoes %, resolution/HDR/DV, size, and status now sit in one stat line directly under each title — everything needed to grab at a glance, in a fixed spot every row shares",
      "Collapsed release groups show the same stat line (resolution set + size range)",
      "Retired the separate Rating/Res/Size/Status columns and the Columns show/hide toggle (the info moved into the title area)",
      "Title column now fills the reclaimed width instead of leaving the right side empty",
    ],
  },
  {
    version: "2.1.4",
    date: "2026-06-27",
    summary: "Richer list rows",
    changes: [
      "Description/synopsis now shown in each list row (2-line excerpt, hidden in compact mode)",
      "TV season and episode count shown in the meta line (e.g. S02 · 6 eps)",
      "Color bar on collapsed group rows now uses border-left for consistent alignment with individual rows",
      "Mixed group bar color changed to amber (was split red/green which caused alignment offset)",
    ],
  },
  {
    version: "2.1.3",
    date: "2026-06-27",
    summary: "Grabbed chip & download metadata",
    changes: [
      "Previously-grabbed indicator now shows resolution, DV, HDR, and size as separate badges",
      "Download history now records hdr and dovi per item — shown immediately on next scan",
      "Resolution, size, HDR, DV are now passed to the download service when sending to JDownloader",
      "Unknown-value placeholders (?) no longer shown in the grabbed chip",
    ],
  },
  {
    version: "2.1.2",
    date: "2026-06-27",
    summary: "Collapsed group status bar",
    changes: [
      "Left bar on collapsed release groups is now colored: red = all missing, green = all grabbed/in-library, split red/green = mixed",
      "Bar width doubled (3 px → 6 px) for better visibility",
    ],
  },
  {
    version: "2.1.1",
    date: "2026-06-27",
    summary: "Settings tab fix & review fixes",
    changes: [
      "Fixed Renaming tab showing Background Scan content (scroll position was preserved across tab switches)",
      "Settings tab nav is now sticky — always visible when scrolling long tabs",
      "Accepting an episode correction now uses the corrected episode's title in the filename",
      "Hardened static-file path checks; build output no longer tracked in git",
    ],
  },
  {
    version: "2.1.0",
    date: "2026-06-27",
    summary: "Episode intelligence & UI polish",
    changes: [
      "StatusBar connection dots — Plex, Meta, JD always visible on every screen",
      "Fixed combined episode filename bug (S01E01E02 now generates correctly)",
      "DB persistence for suggested_correction, combined_episode, split_file",
      "Accept buttons for combined-episode and episode-correction proposals",
      "New API: /rename/jobs/{id}/accept-combined and accept-correction",
    ],
  },
  {
    version: "2.0.0",
    date: "2026-06-20",
    summary: "Episode intelligence engine",
    changes: [
      "Runtime-gated episode/season correction via TMDB runtime matching",
      "Combined double-episode detection (S01E01E02 when runtime ≈ 2× single ep)",
      "Split file detection — flags Part 1/Part 2 sibling pairs",
      "LLM disambiguator for tied episode candidates (Ollama)",
      "Download page hint extraction — detects combined/split before download",
      "Multi-episode filename parsing (E01E02, E01-E02, Part 1 patterns)",
      "Tighter runtime confidence thresholds (neutral zone 10% not 15%)",
    ],
  },
  {
    version: "1.9.0",
    date: "2026-06-19",
    summary: "Scraper reliability",
    changes: [
      "Fixed Chromium/ChromeDriver version mismatch causing zero-link grabs",
      "Fixed winreg-only version detection for undetected-chromedriver",
    ],
  },
];

export const latestVersion = changelog[0];
