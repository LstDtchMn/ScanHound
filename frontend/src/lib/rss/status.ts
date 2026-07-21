export type RssReadiness = { ready: boolean; reasons: string[] };

export function canEnablePrimary(readiness: RssReadiness | null | undefined): boolean {
  return readiness?.ready === true;
}

export function evidenceLabel(value: string): string {
  return value === 'asserted' ? 'Yes' : value === 'negated' ? 'No' : 'Unknown';
}

export function reasonLabel(reason: string): string {
  return reason.replaceAll('_', ' ');
}
