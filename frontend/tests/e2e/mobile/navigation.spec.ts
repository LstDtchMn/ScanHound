import { test, expect } from '@playwright/test';

// MobileTabBar.svelte is the only <nav> labelled "Primary" — the desktop
// Sidebar renders the same link text (nav.short) but is `hidden` at the md
// breakpoint, so scoping to this nav avoids ambiguous matches.
test('bottom tab bar switches routes', async ({ page }) => {
  await page.goto('/');
  const tabBar = page.getByRole('navigation', { name: 'Primary' });
  await expect(tabBar).toBeVisible();

  await tabBar.getByRole('link', { name: 'DLs' }).click();
  await expect(page).toHaveURL(/\/downloads$/);

  await tabBar.getByRole('link', { name: 'Watch' }).click();
  await expect(page).toHaveURL(/\/watchlist$/);

  await tabBar.getByRole('link', { name: 'Stats' }).click();
  await expect(page).toHaveURL(/\/analytics$/);

  await tabBar.getByRole('link', { name: 'Settings' }).click();
  await expect(page).toHaveURL(/\/settings$/);

  await tabBar.getByRole('link', { name: 'Scan' }).click();
  await expect(page).toHaveURL(/\/$/);
});
