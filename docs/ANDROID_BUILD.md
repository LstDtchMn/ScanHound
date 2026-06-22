# Building the ScanHound Android app

The Android app is the existing SvelteKit frontend packaged with **Tauri v2's
Android target**. It is a thin client: the Python backend keeps running in your
Docker container, and the app talks to it over the network. There is **no Python
on the phone** — the sidecar and system tray are desktop-only (gated behind
`#[cfg(desktop)]` in `src-tauri/src/lib.rs`).

All commands below run from `frontend/`.

## 1. Prerequisites (build machine)

- **Node.js** 18+ and the repo's npm deps (`npm install`).
- **Rust** with the Android targets:
  ```bash
  rustup target add aarch64-linux-android armv7-linux-androideabi i686-linux-android x86_64-linux-android
  ```
- **JDK 17** (Android Gradle Plugin requirement).
- **Android SDK + NDK** (easiest via Android Studio → SDK Manager). Then export:
  ```bash
  export ANDROID_HOME="$HOME/Android/Sdk"
  export NDK_HOME="$ANDROID_HOME/ndk/<version>"
  ```

Verify with `npx tauri info` — it reports any missing Android bits.

## 2. One-time project init

```bash
npm run android:init      # = tauri android init
```

This generates `src-tauri/gen/android/` (a Gradle project). It's regenerable, so
it's fine to leave it git-ignored and re-run on a fresh checkout.

## 3. Develop on a device/emulator

```bash
npm run android:dev       # = tauri android dev
```

Builds the SvelteKit app, installs a debug APK on the connected device/emulator,
and hot-reloads. Enable USB debugging on the phone, or start an emulator first.

## 4. Build a release APK

```bash
npm run android:build     # = tauri android build --apk
```

Output: `src-tauri/gen/android/app/build/outputs/apk/universal/release/`.

For Play Store / signed installs, generate a keystore and configure signing in
`gen/android` per the Tauri docs (Distribute → Android). An unsigned/debug APK is
fine for sideloading onto your own device.

## 5. First launch — point the app at your server

Because the bundled app isn't served from your domain, it can't assume
same-origin. On first launch (when it can't reach a backend) it shows
**Connect to your ScanHound server**; you can also reach it any time under
**Settings → Connection**. Enter:

- **Server URL** — e.g. `https://scanhound.turtleland.us`
- **Auth token** — the value of `SCANHOUND_AUTH_NONCE` on the server (leave blank
  if the server has no token auth)

The app stores these, sends `Authorization: Bearer <token>` on every request, and
opens the WebSocket at `wss://<host>/ws?token=…`. "Test" hits `/health` to verify
before saving.

### Server-side checklist
- The backend must be reachable from the phone's network (LAN IP, VPN/Tailscale,
  or a public tunnel like the existing Cloudflare one).
- If a layer in front (e.g. Cloudflare Access) guards the hostname, the app's
  token won't satisfy it — either scope that layer to allow the API or expose the
  API through an allowed path.
- CORS: when the app origin differs from the API origin, the API must allow it.
  (Same-origin browser/PWA use is unaffected.)

## Notes
- `tauri.conf.json` sets `bundle.android.minSdkVersion = 24` (Android 7.0+).
- The app identifier is `com.scanhound.app`.
- Desktop builds are unchanged — sidecar + tray still run via `#[cfg(desktop)]`.
