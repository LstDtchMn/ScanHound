import { defineConfig, devices } from '@playwright/test';

// Mirrors scripts/dev.py's port convention: backend on 9721 (--no-auth), Vite
// dev server on 5174 (NOT Vite's default 5173, which tauri dev reserves as
// its devUrl). frontend/src/lib/api/endpoint.ts special-cases port 5174 to
// point the app at http://localhost:9721, so the app under test needs no
// stored server URL to reach the backend.
const FRONTEND_PORT = 5174;
const BACKEND_PORT = 9721;

export default defineConfig({
  testDir: './tests/e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? 'github' : 'list',
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
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
    {
      command: `npm run dev -- --port ${FRONTEND_PORT}`,
      cwd: '.',
      url: `http://localhost:${FRONTEND_PORT}`,
      reuseExistingServer: !process.env.CI,
      timeout: 30_000,
    },
  ],
});
