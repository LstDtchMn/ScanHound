# Deploy ScanHound as a Docker container (for Claude Desktop)

You are setting up the **ScanHound** web app as a Docker container on this Windows
machine (Docker Desktop), reachable through the existing **Cloudflare Tunnel →
Nginx Proxy Manager (NPM)** stack. Work methodically and confirm each step's
output before moving on.

**Source code (already on this machine, includes all latest changes):**
`C:\Users\NLSur\OneDrive\Documents\MediaScout`

**Install/run location (target):**
`X:\Docker Apps\Media Scout`

---

## Step 1 — Get the code to the target

The repo (with all latest changes) is on GitHub. Clone it to the target:

```powershell
git clone https://github.com/LstDtchMn/MediaScout.git "X:\Docker Apps\Media Scout"
```

> To update later: `cd "X:\Docker Apps\Media Scout"; git pull`, then rebuild
> (Step 3).

**Alternative — copy from the local working tree** (use only if you don't want
to pull from GitHub), skipping heavy/generated folders:

```powershell
$src = "C:\Users\NLSur\OneDrive\Documents\MediaScout"
$dst = "X:\Docker Apps\Media Scout"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
robocopy $src $dst /E `
  /XD ".venv" "node_modules" ".git" "build" ".svelte-kit" "__pycache__" ".pytest_cache" ".tmp_pytest" ".tmp_pytest_history_fix" ".playwright-mcp" "data" "playwright-deep" "playwright-gui" `
  /XF "*.db" "*.log" "*.crdownload"
```

`robocopy` exit codes 0–7 are success (8+ is an error). Verify the key files
landed:

```powershell
Get-ChildItem "X:\Docker Apps\Media Scout" | Where-Object {
  $_.Name -in 'Dockerfile','docker-compose.yml','requirements-docker.txt','DOCKER.md','backend','frontend','docker'
} | Select-Object Name
```

You should see `Dockerfile`, `docker-compose.yml`, `requirements-docker.txt`,
`backend`, `frontend`, and `docker`.

---

## Step 2 — Decide how NPM will reach the container (inspect first)

NPM is itself a Docker container, so it **cannot** reach a port bound to the
host's `127.0.0.1`. Pick ONE of these and adjust `docker-compose.yml` accordingly.

First, inspect the existing setup:

```powershell
docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Ports}}"
docker network ls
```

Identify the NPM container and the Docker network it's on (e.g. inspect it):

```powershell
docker inspect <npm-container-name> --format "{{json .NetworkSettings.Networks}}"
```

### Option A (recommended) — put ScanHound on NPM's network

Edit `X:\Docker Apps\Media Scout\docker-compose.yml`:
- **Remove** the `ports:` block (no host port needed).
- **Add** the NPM network as external. Example (replace `npm_default` with the
  real network name from `docker network ls`):

```yaml
services:
  scanhound:
    # ...existing keys (build, image, container_name, restart, volumes,
    #    environment, extra_hosts, shm_size)...
    networks:
      - proxy

networks:
  proxy:
    external: true
    name: npm_default        # <-- the actual NPM network name
```

Then in NPM the proxy host forwards to **Forward Hostname** `scanhound`,
**Forward Port** `9721`.

### Option B (simpler) — publish a host port

In `docker-compose.yml` change the ports line to publish on all interfaces:

```yaml
    ports:
      - "9721:9721"
```

Then in NPM the proxy host forwards to **Forward Hostname**
`host.docker.internal`, **Forward Port** `9721`. (Windows firewall stays closed
to the public; only NPM/Cloudflare are exposed.)

---

## Step 3 — Build and start

```powershell
cd "X:\Docker Apps\Media Scout"
docker compose up -d --build
```

The build takes several minutes (it bundles Chromium for link scraping; final
image ~1.3 GB). Confirm it's healthy:

```powershell
docker compose ps
docker compose logs --tail 30 scanhound
# Expect: "All services initialized successfully" and "Application startup complete."
```

Quick local smoke test from the host:

```powershell
# Option B only (host port published):
curl.exe -s -o NUL -w "%{http_code}`n" http://localhost:9721/scan/status
```

---

## Step 4 — Nginx Proxy Manager + Cloudflare

1. In **NPM → Hosts → Proxy Hosts**, add (or reuse) a proxy host for the public
   hostname you want (e.g. `scanhound.turtleland.us`), forwarding per the Option
   A/B choice above. Enable **Websockets Support** (required — the app uses a
   live WebSocket) and request/force SSL.
2. Make sure your **Cloudflare Tunnel** routes that hostname to NPM (same pattern
   as your other services).
3. **Enable Cloudflare Access** (email OTP / SSO) on that hostname. ⚠️ The
   container has **no built-in login** — anyone who can reach it has full control
   (downloads, your Plex token). Cloudflare Access is the authentication layer.

---

## Step 5 — Configure the app (in the browser, via the public URL)

Open the hostname and go to **Settings**:

- **Plex** → URL `http://host.docker.internal:32400`, paste your Plex token, click
  **Test Connection** (the compose adds the `host-gateway` mapping so the
  container reaches Plex on this host). Then assign your movie/TV libraries and
  Refresh.
- **Metadata** → paste your **TMDB** and **OMDb** API keys.
- **JDownloader** → Method **MyJDownloader API**, enter email / password / device
  name, click **Test Connection** (the indicator should go green). This is
  cloud-based so it works from the container with no extra networking.

---

## Step 6 — Verify

- Settings shows JDownloader **connected** (green) and Plex connected with movie/TV
  counts.
- The scan page pre-flight checklist shows Plex / Metadata / Sources / JDownloader
  all green.
- Run a small **Deep Scan** (1 page) and confirm results populate with correct
  In Library / Upgrade / Missing statuses.
- The **Downloads** page shows the "JDownloader Links" panel with online/broken
  counts.

## Updating later

```powershell
cd "X:\Docker Apps\Media Scout"
git pull            # (or re-run the Step 1 robocopy if you copied instead)
docker compose up -d --build
```

Data (config, DB, Plex cache) persists in `X:\Docker Apps\Media Scout\data` and
survives rebuilds.
