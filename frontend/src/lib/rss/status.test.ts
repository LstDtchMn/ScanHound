import { describe, expect, it } from 'vitest';
import { canEnablePrimary, evidenceLabel, reasonLabel } from './status';

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
