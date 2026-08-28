# ScanHound

A self-hosted web app that compares your Plex library against online release
listings, flags missing titles and quality upgrades, and manages the pipeline
from grab to renamed file in the library — including Dolby Vision detection
and Kometa label integration.

ScanHound runs as a single Docker container (FastAPI backend + Svelte
frontend) fronted by whatever reverse proxy you already use.

> ScanHound began life as a desktop app (PySide6/QML). That stratum has been
> retired; the web app is the only supported way to run it.

## What it does

- **Scan & compare** — scrapes release sources (HDEncode, DDLBase, Adit-HD)
  for 4K/1080p releases and compares them against your Plex library using
  IMDb-id and fuzzy title matching. Results are classified as missing,
  in-library, or an upgrade (resolution, Dolby Vision, or size — each rule
  configurable).
- **Scheduler & background crawler** — periodic scans and background
  pre-caching of source pages, with per-source enable switches.
- **Downloads** — scrapes the host links for selected items and hands them to
  JDownloader (MyJDownloader API, watch-folder, Click'n'Load, or clipboard),
  with a download queue, retry handling, and delivery verification.
- **Renaming pipeline** — identifies finished downloads (TMDB, with optional
  local-LLM assist via Ollama), renames them to Plex conventions, and moves
  them into the right library folder. Conflicts go through explicit
  resolution; replaced files are trashed with a retention window, never
  deleted in place.
- **Dolby Vision detection** — a host-side detector script
  (`scripts/host-detector/`) walks your libraries with `dovi_tool`, records
  DV profile and FEL/MEL layer evidence, and imports the results into the
  app, which keeps Plex labels in sync (hourly, additive-only). Kometa can
  then badge DV FEL/MEL from those labels.
- **Metadata & UI** — TMDB/OMDb enrichment (ratings, posters, genres), a
  watchlist, live progress over WebSocket, and an optional login gate.

## Running it

Prerequisites: Docker, a Plex server + token, and (optionally) JDownloader
and TMDB/OMDb API keys.

```bash
git clone https://github.com/LstDtchMn/ScanHound.git
cd ScanHound
# review docker-compose.yml first: the volume mounts (media folders, ./data)
# and network setup are specific to the author's deployment — adjust them
# to your host before starting.
docker compose up -d --build
```

The container serves the web UI and API on port 9721 (the compose file binds
it to 127.0.0.1 only; put your reverse proxy in front for remote access).
Configuration lives in the UI under Settings and persists in the mounted
`/data` volume. `config.example.json` documents the available keys.

See `DOCKER.md` for image details and `DEVELOPMENT.md` for working on the
code (backend tests run with `pytest`, frontend with `npm run test:unit`).

## Project layout

```
backend/          # FastAPI app, scanner, matching, downloads, rename pipeline
backend/api/      # HTTP routes + WebSocket
frontend/         # Svelte web UI
scripts/host-detector/  # host-side Dolby Vision scanner (dovi_tool)
docs/             # design docs, runbooks, review history
tests/            # pytest suite
```

## Disclaimer

For personal use. Respect content creators and copyright law; you are
responsible for how you use this software.
