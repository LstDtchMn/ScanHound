// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { confidenceVariant, renameStatusVariant } from './constants';

describe('confidenceVariant', () => {
  it('is success at and above 95', () => {
    expect(confidenceVariant(95)).toBe('success');
    expect(confidenceVariant(100)).toBe('success');
  });
  it('is warning in 70..94', () => {
    expect(confidenceVariant(70)).toBe('warning');
    expect(confidenceVariant(94)).toBe('warning');
  });
  it('is error below 70', () => {
    expect(confidenceVariant(69)).toBe('error');
    expect(confidenceVariant(0)).toBe('error');
  });
  it('is default for null/undefined', () => {
    expect(confidenceVariant(null)).toBe('default');
    expect(confidenceVariant(undefined)).toBe('default');
  });
});

describe('renameStatusVariant', () => {
  it('maps known rename statuses', () => {
    expect(renameStatusVariant('needs_review')).toBe('warning');
    expect(renameStatusVariant('matched')).toBe('accent');
    expect(renameStatusVariant('applied')).toBe('success');
    expect(renameStatusVariant('failed')).toBe('error');
  });
  it('falls back to default for unknown', () => {
    expect(renameStatusVariant('zzz')).toBe('default');
    expect(renameStatusVariant(null)).toBe('default');
  });
});
