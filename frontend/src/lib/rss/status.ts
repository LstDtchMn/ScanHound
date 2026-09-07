export type RssReadiness = { ready: boolean; reasons: string[] };

export type CanarySource = {
  source_key?: string;
  last_attempt_at?: string | null;
  next_attempt_at?: string | null;
  last_success_at?: string | null;
  age_seconds?: number | null;
  last_outcome?: string | null;
  last_reason?: string | null;
  consecutive_failures?: number | null;
  consecutive_overlap_losses?: number | null;
  stale?: boolean;
  has_run?: boolean;
};

export type RequestCost = {
  available: boolean;
  window_days: number;
  since?: string;
  scope?: 'since_promotion' | 'trailing_window' | string;
  floor: number;
  target: number;
  totals?: Record<string, number>;
};

export type Promotion = {
  requested_mode: string;
  effective_mode: string;
  state: string;
  primary_authorized: boolean;
  promotion_blockers: string[];
  suspensions: string[];
  revocations: string[];
  activation: { eligible: boolean; blockers: string[] };
  canary: {
    implemented: boolean;
    available: boolean;
    interval_seconds?: number | null;
    max_age_seconds?: number | null;
    sources: Record<string, CanarySource>;
  };
  request_cost: RequestCost;
  canary_last_success?: string | null;
  canary_age_seconds?: number | null;
  auto_demotion_armed?: boolean;
  last_demotion?: { at?: string; reason?: string[] } | null;
};

/**
 * May primary be turned on?
 *
 * Shadow readiness alone stopped being the answer when the promotion
 * authority arrived: it asks a wider question (a canary that exists, a
 * contract that matches, auto-demotion armed) and the page must not offer a
 * promotion the backend will refuse. Readiness is still consulted when no
 * promotion block is present, so an older payload behaves as it always did.
 */
export function canEnablePrimary(
  readiness: RssReadiness | null | undefined,
  promotion?: Promotion | null
): boolean {
  if (promotion?.activation) return promotion.activation.eligible === true;
  return readiness?.ready === true;
}

/**
 * Is the mode on the page not the mode that is running?
 *
 * The selector shows what was REQUESTED, which is correct -- it is the
 * control. But a persisted rss_primary that the runtime refuses runs as
 * shadow, and a page that showed only the request would report a mode nothing
 * is in. This is what makes that visible.
 */
export function modeDisagrees(promotion: Promotion | null | undefined): boolean {
  if (!promotion) return false;
  return promotion.requested_mode !== promotion.effective_mode;
}

/** Why the runtime is not honouring the requested mode, in plain words. */
export function disagreementReason(promotion: Promotion | null | undefined): string {
  if (!promotion || !modeDisagrees(promotion)) return '';
  const blockers = promotion.promotion_blockers ?? [];
  if (!blockers.length) return 'no reason reported';
  return blockers.map(reasonLabel).join(', ');
}

/**
 * A pause or a durable finding?
 *
 * The distinction is the whole shape of the authority: a database that cannot
 * answer suspends and keeps the promotion, while a safety finding revokes it
 * and requires a fresh one. Collapsing them on the page would make a passing
 * outage look like a demotion.
 */
export function severityLabel(promotion: Promotion | null | undefined): string {
  if (!promotion) return '';
  if (promotion.revocations?.length) return 'Blocked';
  if (promotion.suspensions?.length) return 'Paused';
  return promotion.primary_authorized ? 'Running' : '';
}

export type CanaryRow = CanarySource & {
  name: string;
  status: 'never' | 'stale' | 'ok';
  ageLabel: string;
};

/**
 * The canary table, ordered worst first.
 *
 * "Never run" and "overdue" are both unprotected but they are different
 * situations -- one has not started, the other has stopped -- and an operator
 * reading the page needs to tell them apart.
 */
export function canaryRows(promotion: Promotion | null | undefined): CanaryRow[] {
  const sources = promotion?.canary?.sources ?? {};
  const rows: CanaryRow[] = Object.entries(sources).map(([name, source]) => ({
    ...source,
    name,
    status: source.has_run === false ? 'never' : source.stale ? 'stale' : 'ok',
    ageLabel: formatAge(source.age_seconds)
  }));
  const rank = { never: 0, stale: 1, ok: 2 } as const;
  return rows.sort(
    (a, b) => rank[a.status] - rank[b.status] || a.name.localeCompare(b.name)
  );
}

/** Rough elapsed time. Null is never rendered as "0 seconds ago". */
export function formatAge(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) return 'never';
  const s = Math.max(0, Math.floor(seconds));
  if (s < 90) return 'just now';
  const minutes = Math.round(s / 60);
  if (minutes < 90) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours} h ago`;
  return `${Math.round(hours / 24)} d ago`;
}

/**
 * What the hybrid spent, or an honest statement that it is unknown.
 *
 * Never "0 requests" for a ledger that could not be read: zero is a
 * measurement nobody made. The window is named because a figure taken since
 * the promotion and one taken over a flat trailing week describe different
 * things.
 */
export function costSummary(promotion: Promotion | null | undefined): string {
  const cost = promotion?.request_cost;
  if (!cost) return 'Not reported';
  if (!cost.available) return 'Ledger unavailable';
  const totals = cost.totals ?? {};
  const total = totals.total ?? 0;
  const window =
    cost.scope === 'since_promotion' ? 'since promotion' : `last ${cost.window_days} days`;
  return `${total} requests, ${window}`;
}

export function evidenceLabel(value: string): string {
  return value === 'asserted' ? 'Yes' : value === 'negated' ? 'No' : 'Unknown';
}

export function reasonLabel(reason: string): string {
  return reason.replaceAll('_', ' ');
}
