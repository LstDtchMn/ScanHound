import { defineConfig, devices } from '@playwright/test';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Mirrors scripts/dev.py's port convention: backend on 9721 (--no-auth), Vite
// dev server on 5174 (NOT Vite's default 5173, which tauri dev reserves as
// its devUrl). frontend/src/lib/api/endpoint.ts special-cases port 5174 to
// point the app at http://localhost:9721, so the app under test needs no
// stored server URL to reach the backend.
const FRONTEND_PORT = 5174;
const BACKEND_PORT = 9721;
// A unique credential-free data directory prevents E2E from inheriting a
// developer's real crawler.db/password and keeps CI deterministic.
const E2E_DATA_DIR = join(
  tmpdir(),
  `scanhound-playwright-${process.pid}-${Date.now()}`,
);

// CI already runs `npm run build` before Playwright. Exercise that production
// artifact instead of paying Vite's cold on-demand compilation cost inside the
// first route assertion. Local development keeps the fast, reusable dev server.
const FRONTEND_COMMAND = process.env.CI
  ? `npm run preview -- --host localhost --port ${FRONTEND_PORT} --strictPort`
  : `npm run build && npm run preview -- --host localhost --port ${FRONTEND_PORT} --strictPort`;
process.env.SCANHOUND_E2E_DATA_DIR = E2E_DATA_DIR;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
  globalTeardown: './global-teardown.ts',
  use: {
    baseURL: `http://localhost:${FRONTEND_PORT}`,
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'desktop',
      testMatch: /shared\/.*\.spec\.ts/,
      use: { ...devices['Desktop Chrome'], viewport: { width: 1280, height: 800 } },
    },
    {
      name: 'mobile',
      // Mobile picks up both the shared smoke tests and the mobile-only UI
      // (bottom tab bar, filter/scan sheets) that's `hidden` at the md breakpoint.
      testMatch: /(shared|mobile)\/.*\.spec\.ts/,
      use: { ...devices['Pixel 7'] },
    },
  ],
  webServer: [
    {
      command: `python -m backend.api --port ${BACKEND_PORT} --host 127.0.0.1 --no-auth`,
      cwd: '..',
      url: `http://127.0.0.1:${BACKEND_PORT}/health`,
      env: {
        // The backend's POSIX config/data paths derive from HOME. Keep every
        // path inside the unique E2E directory, not the developer's profile.
        HOME: E2E_DATA_DIR,
        XDG_CONFIG_HOME: join(E2E_DATA_DIR, '.config'),
        XDG_DATA_HOME: join(E2E_DATA_DIR, '.local', 'share'),
        APPDATA: E2E_DATA_DIR,
        LOCALAPPDATA: E2E_DATA_DIR,
        SCANHOUND_DATA_DIR: E2E_DATA_DIR,
        SCANHOUND_DB_DIR: E2E_DATA_DIR,
        SCANHOUND_ALLOW_OPEN: '1',
      },
      // Reusing a backend can attach E2E to a real user process and bypass all
      // of the isolation above. Port conflicts must fail loudly instead.
      reuseExistingServer: false,
      timeout: 30_000,
    },
    {
      command: FRONTEND_COMMAND,
      cwd: '.',
      url: `http://localhost:${FRONTEND_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
});
