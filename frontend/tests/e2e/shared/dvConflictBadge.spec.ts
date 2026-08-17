import { test, expect } from '@playwright/test';

/**
 * A file whose two scan rows disagree about its Dolby Vision layer is left
 * strictly alone by the labeler, so it moves none of the ordinary counts. The
 * alert that announces it is best-effort — it reaches whoever is connected at
 * that instant and then dedups — so the only reliable way to learn about one is
 * to see it in the UI.
 *
 * The DV panel is COLLAPSED by default. An earlier version put the conflict
 * card inside the panel body, which meant a correctly-loaded page showed
 * nothing at all until the user expanded a panel they had no reason to open
 * (peer review rounds 3 and 4). These tests pin the part that is actually
 * visible without prior knowledge.
 */

const SAMPLE = [
  { path: 'C:/4K Drives/4K Columbo/Movies 2/Alpha (2001).mkv', layers: ['fel', 'mel'] },
  { path: 'C:/4K Drives/4K Columbo/Movies 2/Beta (2002).mkv', layers: ['none', 'fel'] }
];

// Must be the PANEL TOGGLE, not the other two "Dolby Vision" buttons on this
// page — the scan-a-folder button (name exactly "Dolby Vision") and the
// read-only inventory card (name "FEL 0 MEL 0 Dolby Vision"). Only the toggle's
// accessible name contains the full "Dolby Vision FEL/MEL scan", and matching
// on a prefix keeps it stable when the badge appends to that name.
const dvPanelToggle = (page: import('@playwright/test').Page) =>
  page.getByRole('button', { name: /Dolby Vision FEL\/MEL scan/ });

test('a conflict is visible while the DV panel is still collapsed', async ({ page }) => {
  await page.route('**/rename/dv-scans*', (route) =>
    route.fulfill({
      json: { scans: [], counts: {}, conflicts: { count: 2, sample: SAMPLE, truncated: false } }
    })
  );

  await page.goto('/renames');

  // Collapsed — the state the user arrives in, and the whole point.
  await expect(dvPanelToggle(page)).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByText('2 need attention')).toBeVisible();
});

test('opening the panel re-reads status, and the badge survives collapsing', async ({ page }) => {
  // The inventory load reports a clean library...
  await page.route('**/rename/dv-scans*', (route) =>
    route.fulfill({
      json: { scans: [], counts: {}, conflicts: { count: 0, sample: [], truncated: false } }
    })
  );
  // ...while the narrow status endpoint knows better. Opening the panel must
  // consult it rather than trust whatever was cached at page load.
  await page.route('**/rename/dv-conflicts*', (route) =>
    route.fulfill({ json: { count: 2, sample: SAMPLE, truncated: false } })
  );

  await page.goto('/renames');
  await expect(page.getByText('2 need attention')).toHaveCount(0); // nothing yet

  await dvPanelToggle(page).click();
  await expect(dvPanelToggle(page)).toHaveAttribute('aria-expanded', 'true');
  await expect(page.getByText('2 need attention')).toBeVisible();

  // Collapse again: the badge lives in the header, so it must persist. If it
  // only existed inside the panel body this is where it would vanish.
  await dvPanelToggle(page).click();
  await expect(dvPanelToggle(page)).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByText('2 need attention')).toBeVisible();
});

test('a clean library shows no attention badge', async ({ page }) => {
  // Negative control. Without it both tests above would still pass if the badge
  // rendered unconditionally, which would train the owner to ignore it.
  await page.route('**/rename/dv-scans*', (route) =>
    route.fulfill({
      json: { scans: [], counts: {}, conflicts: { count: 0, sample: [], truncated: false } }
    })
  );
  await page.route('**/rename/dv-conflicts*', (route) =>
    route.fulfill({ json: { count: 0, sample: [], truncated: false } })
  );

  await page.goto('/renames');
  await expect(dvPanelToggle(page)).toBeVisible();
  await expect(page.getByText(/need attention/)).toHaveCount(0);
});
