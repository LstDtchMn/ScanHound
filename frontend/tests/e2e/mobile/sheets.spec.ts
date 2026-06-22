import { test, expect } from '@playwright/test';

// FilterBar.svelte and ScanControls.svelte each swap a `md:hidden` toolbar +
// BottomSheet in for their desktop controls below the md breakpoint.
test('filter sheet opens and closes', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Filters' }).click();
  await expect(page.getByRole('heading', { name: 'View & filters' })).toBeVisible();

  await page.getByRole('button', { name: 'Close' }).click();
  await expect(page.getByRole('heading', { name: 'View & filters' })).not.toBeVisible();
});

test('scan options sheet opens and closes', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Scan options' }).click();
  await expect(page.getByRole('heading', { name: 'Scan options' })).toBeVisible();

  await page.getByRole('button', { name: 'Close' }).click();
  await expect(page.getByRole('heading', { name: 'Scan options' })).not.toBeVisible();
});
