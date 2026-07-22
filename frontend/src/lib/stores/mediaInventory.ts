import { writable } from 'svelte/store';
import { api } from '$lib/api/client';
import type {
  MediaInventoryFacets,
  MediaInventoryItem,
  MetadataScanRun
} from '$lib/api/types';

export type InventoryFilters = {
  q?: string;
  library?: string;
  resolution?: string;
  hdr?: string;
  hdr10plus_state?: string;
  dv_layer?: string;
  dv_profile?: string;
  scan_state?: string;
  discrepancy?: string;
  sort?: string;
  page?: string;
  page_size?: string;
};

export const inventoryItems = writable<MediaInventoryItem[]>([]);
export const inventoryTotal = writable(0);
export const inventoryFacets = writable<MediaInventoryFacets>({});
export const inventoryFilters = writable<InventoryFilters>({ page: '1', page_size: '50' });
export const inventoryLoading = writable(false);
export const inventoryError = writable<string | null>(null);
export const activeMetadataRun = writable<MetadataScanRun | null>(null);

export function compactInventoryFilters(filters: InventoryFilters): Record<string, string> {
  return Object.fromEntries(
    Object.entries(filters).filter((entry): entry is [string, string] => Boolean(entry[1]))
  );
}

export async function loadInventory(filters: InventoryFilters = {}): Promise<void> {
  inventoryLoading.set(true);
  inventoryError.set(null);
  const normalized = { page: '1', page_size: '50', ...compactInventoryFilters(filters) };
  inventoryFilters.set(normalized);
  try {
    const [result, facets] = await Promise.all([
      api.getMediaInventory(normalized), api.getMediaInventoryFacets()
    ]);
    inventoryItems.set(result.items);
    inventoryTotal.set(result.total);
    inventoryFacets.set(facets);
  } catch (error) {
    inventoryError.set(error instanceof Error ? error.message : 'Inventory could not be loaded');
  } finally {
    inventoryLoading.set(false);
  }
}

export async function setInventoryFilters(filters: InventoryFilters): Promise<void> {
  await loadInventory({ ...compactInventoryFilters(filters), page: '1' });
}

export async function startPilot(ids: string[]): Promise<MetadataScanRun> {
  const run = await api.startMetadataInventoryScan('pilot', ids);
  activeMetadataRun.set(run);
  return run;
}

export async function pauseRun(runUuid: string): Promise<void> {
  activeMetadataRun.set(await api.pauseMetadataInventoryScan(runUuid));
}

export async function resumeRun(runUuid: string): Promise<void> {
  activeMetadataRun.set(await api.resumeMetadataInventoryScan(runUuid));
}

export async function cancelRun(runUuid: string): Promise<void> {
  activeMetadataRun.set(await api.cancelMetadataInventoryScan(runUuid));
}

export async function retryFailures(runUuid: string): Promise<void> {
  activeMetadataRun.set(await api.retryMetadataInventoryFailures(runUuid));
}

export async function refreshRun(runUuid: string): Promise<MetadataScanRun> {
  const run = await api.getMetadataInventoryScan(runUuid);
  activeMetadataRun.set(run);
  return run;
}
