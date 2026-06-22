import { test, expect } from '@playwright/test';

// Runs under both the desktop and mobile projects — a baseline check that
// every top-level route renders its own page (via +layout.svelte's
// routeTitles map) rather than an error boundary or a blank shell.
const routes = [
  { path: '/', title: 'Scan | ScanHound' },
  { path: '/downloads', title: 'Downloads | ScanHound' },
  { path: '/watchlist', title: 'Watchlist | ScanHound' },
  { path: '/analytics', title: 'Analytics | ScanHound' },
  { path: '/settings', title: 'Settings | ScanHound' },
];

for (const { path, title } of routes) {
  test(`${path} loads`, async ({ page }) => {
    await page.goto(path);
    await expect(page).toHaveTitle(title);
  });
}
