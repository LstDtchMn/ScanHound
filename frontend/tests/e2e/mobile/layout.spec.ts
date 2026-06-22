import { test, expect } from '@playwright/test';

// Guards against a regression where a fixed-width child (a table, a wide
// chip row, etc.) forces the page wider than the viewport and breaks the
// thumb-scroll layout on phones.
const routes = ['/', '/downloads', '/watchlist', '/analytics', '/settings'];

for (const path of routes) {
  test(`${path} has no horizontal overflow at mobile width`, async ({ page }) => {
    await page.goto(path);
    const { scrollWidth, clientWidth } = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(scrollWidth).toBeLessThanOrEqual(clientWidth);
  });
}
