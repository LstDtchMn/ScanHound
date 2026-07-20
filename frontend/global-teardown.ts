import { rm } from 'node:fs/promises';

export default async function globalTeardown() {
  const directory = process.env.SCANHOUND_E2E_DATA_DIR;
  if (!directory) return;
  await rm(directory, { recursive: true, force: true });
}
