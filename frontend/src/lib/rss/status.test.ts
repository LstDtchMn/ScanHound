import { describe, expect, it } from 'vitest';
import {
  activationRefusals,
  canEnablePrimary,
  canaryRows,
  costSummary,
  disagreementReason,
  evidenceLabel,
  formatAge,
  modeDisagrees,
  qualificationSummary,
  reasonLabel,
  severityLabel,
  type Promotion
} from './status';

function promotion(overrides: Partial<Promotion> = {}): Promotion {
  return {
    requested_mode: 'rss_shadow',
    effective_mode: 'rss_shadow',
    state: 'not_requested',
    primary_authorized: false,
    promotion_blockers: [],
    suspensions: [],
    revocations: [],
    activation: { eligible: false, blockers: [] },
    canary: { implemented: true, available: true, sources: {} },
    request_cost: { available: false, window_days: 7, floor: 0.5, target: 0.7 },
    ...overrides
  };
}

describe('RSS status helpers', () => {
  it('never treats unknown evidence as negative', () => {
    expect(evidenceLabel('unknown')).toBe('Unknown');
    expect(evidenceLabel('negated')).toBe('No');
  });

  it('locks primary until the backend gate is ready', () => {
    expect(canEnablePrimary({ ready: false, reasons: ['relevant_miss'] })).toBe(false);
    expect(canEnablePrimary({ ready: true, reasons: [] })).toBe(true);
  });

  it('formats diagnostic reason codes', () => {
    expect(reasonLabel('request_reduction_not_proven')).toBe('request reduction not proven');
  });
});

describe('what the page may offer', () => {
  it('follows the promotion authority rather than shadow readiness alone', () => {
    // Readiness says go; the authority refuses because the contract moved.
    const blocked = promotion({
      activation: { eligible: false, blockers: ['promotion_contract_changed'] }
    });
    expect(canEnablePrimary({ ready: true, reasons: [] }, blocked)).toBe(false);

    const allowed = promotion({ activation: { eligible: true, blockers: [] } });
    expect(canEnablePrimary({ ready: false, reasons: ['x'] }, allowed)).toBe(true);
  });

  it('falls back to readiness when no promotion block is present', () => {
    expect(canEnablePrimary({ ready: true, reasons: [] }, null)).toBe(true);
    expect(canEnablePrimary({ ready: false, reasons: [] }, undefined)).toBe(false);
  });
});

describe('the mode the page shows against the mode that runs', () => {
  it('reports a persisted primary that the runtime refuses', () => {
    const refused = promotion({
      requested_mode: 'rss_primary',
      effective_mode: 'rss_shadow',
      state: 'runtime_revoked',
      promotion_blockers: ['canary_stale', 'promotion_record_missing'],
      revocations: ['canary_stale']
    });
    expect(modeDisagrees(refused)).toBe(true);
    expect(disagreementReason(refused)).toBe('canary stale, promotion record missing');
    expect(severityLabel(refused)).toBe('Blocked');
  });

  it('says nothing when the mode is honoured', () => {
    const running = promotion({
      requested_mode: 'rss_primary',
      effective_mode: 'rss_primary',
      primary_authorized: true
    });
    expect(modeDisagrees(running)).toBe(false);
    expect(disagreementReason(running)).toBe('');
    expect(severityLabel(running)).toBe('Running');
  });

  it('separates a pause from a durable finding', () => {
    const paused = promotion({
      requested_mode: 'rss_primary',
      effective_mode: 'rss_shadow',
      state: 'runtime_suspended',
      promotion_blockers: ['database_unavailable'],
      suspensions: ['database_unavailable']
    });
    expect(severityLabel(paused)).toBe('Paused');
    expect(severityLabel(promotion({ revocations: ['gap_proven'] }))).toBe('Blocked');
  });

  it('is silent about a page with no promotion block at all', () => {
    expect(modeDisagrees(null)).toBe(false);
    expect(disagreementReason(undefined)).toBe('');
  });
});

describe('why a promotion is refused', () => {
  it('renders the reasons beside the control, not only in a toast', () => {
    /* The page explained a refusal inside setMode(), which fires from an
       option that is DISABLED whenever activation is ineligible -- so the
       explanation was unreachable exactly when it was needed. */
    const refused = promotion({
      activation: {
        eligible: false,
        blockers: ['qualification_window_incomplete', 'request_reduction_below_floor']
      }
    });
    expect(activationRefusals(refused)).toEqual([
      'qualification window incomplete',
      'request reduction below floor'
    ]);
  });

  it('names the uncrawled sources, because the blocker alone is not actionable', () => {
    const refused = promotion({
      activation: {
        eligible: false,
        blockers: ['canary_source_not_crawled'],
        uncrawled_canary_sources: ['remux', 'tv']
      }
    });
    expect(activationRefusals(refused)).toEqual([
      'canary source not crawled: remux, tv'
    ]);
  });

  it('says nothing when the promotion is allowed', () => {
    expect(activationRefusals(promotion({
      activation: { eligible: true, blockers: [] }
    }))).toEqual([]);
    expect(activationRefusals(null)).toEqual([]);
  });
});

describe('the qualification window', () => {
  it('reports progress the backend measured', () => {
    expect(qualificationSummary(promotion({
      qualification: {
        complete: false, epoch_started_at: '2026-08-25T00:00:00Z',
        required_days: 14, consecutive_clean_days: 9.4, max_gap_hours: 3.1,
        max_gap_hours_allowed: 6, eligible_cycles: 120, reasons: []
      }
    }))).toBe('9.4 of 14 clean days, worst gap 3.1 h of 6 allowed');
  });

  it('never invents progress it was not given', () => {
    expect(qualificationSummary(promotion({
      qualification: {
        complete: false, epoch_started_at: null, required_days: 14,
        consecutive_clean_days: null, max_gap_hours: null,
        max_gap_hours_allowed: 6, reasons: ['epoch_not_started']
      }
    }))).toBe('Qualification not started');
    expect(qualificationSummary(promotion())).toBe('');
  });

  it('says so when it is done', () => {
    expect(qualificationSummary(promotion({
      qualification: {
        complete: true, epoch_started_at: '2026-08-20T00:00:00Z',
        required_days: 14, consecutive_clean_days: 15.2, max_gap_hours: 2.0,
        max_gap_hours_allowed: 6, reasons: []
      }
    }))).toBe('Qualification complete (14 clean days)');
  });
});

describe('the canary table', () => {
  const withSources = promotion({
    canary: {
      implemented: true,
      available: true,
      sources: {
        remux: { source_key: 'hdencode:remux', age_seconds: 300, stale: false, has_run: true },
        tv: { source_key: 'hdencode:tv', age_seconds: null, stale: true, has_run: false },
        '4k': { source_key: 'hdencode:4k', age_seconds: 90000, stale: true, has_run: true }
      }
    }
  });

  it('puts the worst source first and tells never-run apart from overdue', () => {
    const rows = canaryRows(withSources);
    expect(rows.map((r) => r.name)).toEqual(['tv', '4k', 'remux']);
    expect(rows[0].status).toBe('never');
    expect(rows[1].status).toBe('stale');
    expect(rows[2].status).toBe('ok');
  });

  it('carries the key the crawler writes, so a row can be matched to the data', () => {
    expect(canaryRows(withSources)[0].source_key).toBe('hdencode:tv');
  });

  it('is empty rather than broken when the canary state cannot be read', () => {
    const blind = promotion({
      canary: { implemented: true, available: false, sources: {} }
    });
    expect(canaryRows(blind)).toEqual([]);
    expect(canaryRows(null)).toEqual([]);
  });
});

describe('elapsed time', () => {
  it('never renders "no measurement" as "just now"', () => {
    expect(formatAge(null)).toBe('never');
    expect(formatAge(undefined)).toBe('never');
    expect(formatAge(0)).toBe('just now');
  });

  it('scales', () => {
    expect(formatAge(600)).toBe('10 min ago');
    expect(formatAge(7200)).toBe('2 h ago');
    expect(formatAge(60 * 60 * 72)).toBe('3 d ago');
  });
});

describe('the request cost line', () => {
  it('never reports an unreadable ledger as zero requests', () => {
    expect(costSummary(promotion())).toBe('Ledger unavailable');
    expect(costSummary(null)).toBe('Not reported');
  });

  it('names the window, because two windows mean different things', () => {
    const sincePromotion = promotion({
      request_cost: {
        available: true, window_days: 7, scope: 'since_promotion',
        floor: 0.5, target: 0.7,
        totals: { rss_poll: 40, canary: 6, canary_retry: 0, fallback: 0, total: 46 }
      }
    });
    expect(costSummary(sincePromotion)).toBe('46 requests, since promotion');

    const trailing = promotion({
      request_cost: {
        available: true, window_days: 7, scope: 'trailing_window',
        floor: 0.5, target: 0.7, totals: { total: 12 }
      }
    });
    expect(costSummary(trailing)).toBe('12 requests, last 7 days');
  });
});
