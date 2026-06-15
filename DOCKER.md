# ScanHound — Docker (remote access)

Runs the v2 web app (FastAPI API + Svelte frontend, one origin, one port) in a
container, fronted by your existing **Cloudflare Tunnel → Nginx Proxy Manager**.

## Build & run

```bash
docker compose up -d --build
```

The app listens on `127.0.0.1:9721` on the Docker host. Point an **NPM proxy
host** at `http://<docker-host>:9721` (or `http://scanhound:9721` if NPM shares
the compose network), and route your Cloudflare Tunnel hostname to that proxy.

Data (config, SQLite DB, Plex cache, logs) persists in `./data`.

## ⚠️ Security — authentication is your proxy's job

The container runs with **no built-in login** (`--no-auth`). Anything that can
reach `:9721` has full control (triggers downloads, reads your Plex token). So:

- **Put authentication in front of it.** Enable **Cloudflare Access** (email
  OTP / SSO) on the public hostname, or NPM Access Lists / HTTP auth.
- Keep the host port bound to `127.0.0.1` (as in the compose file) so it is
  never directly reachable from the LAN/internet — only via NPM.

## First-time configuration (in the web UI → Settings)

- **Plex**: set the URL to `http://host.docker.internal:32400` (the compose file
  adds the `host-gateway` mapping so the container can reach Plex on the host).
  Paste your Plex token, then Test Connection.
- **TMDB / OMDb**: paste your API keys.
- **JDownloader**: Method = *MyJDownloader API*, enter your email / password /
  device name. The MyJDownloader API is cloud-based, so it works from the
  container with no extra networking.
- **Libraries**: assign your movie/TV libraries and refresh.

## Notes

- **Image size** ~1 GB — it bundles Chromium + Xvfb so the HDEncode "View links"
  scrape can run headful (required to clear Cloudflare). `shm_size: 1gb` is set
  for Chromium stability.
- The v1 PySide6/QML desktop UI is **not** included (the API doesn't need it).
- To update: `docker compose up -d --build` after pulling new code.
