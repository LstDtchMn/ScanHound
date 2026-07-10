// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { confidenceVariant, renameStatusVariant, renameStatusBorderColor, dvLayerVariant, resolutionRank } from './constants';

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
  it('covers reverted and pending statuses', () => {
    expect(renameStatusVariant('reverted')).toBe('default');
    expect(renameStatusVariant('pending')).toBe('info');
  });
});

describe('renameStatusBorderColor', () => {
  it('returns error color for failed status', () => {
    const color = renameStatusBorderColor('failed');
    expect(typeof color).toBe('string');
    expect(color.length).toBeGreaterThan(0);
    expect(color).toBe('var(--error)');
  });
  it('returns border color for unknown status (default branch)', () => {
    const color = renameStatusBorderColor('zzz_unknown');
    expect(color).toBe('var(--border)');
  });
  it('returns border color for null (default branch)', () => {
    expect(renameStatusBorderColor(null)).toBe('var(--border)');
  });
});

describe('resolutionRank', () => {
  it('orders 480p < 720p < 1080p < 1440p < 2160p/4k', () => {
    expect(resolutionRank('480p')).toBeLessThan(resolutionRank('720p'));
    expect(resolutionRank('720p')).toBeLessThan(resolutionRank('1080p'));
    expect(resolutionRank('1080p')).toBeLessThan(resolutionRank('1440p'));
    expect(resolutionRank('1440p')).toBeLessThan(resolutionRank('2160p'));
    expect(resolutionRank('2160p')).toBe(resolutionRank('4k'));
  });
  it('unknown/null resolution falls back to the lowest rank (0)', () => {
    expect(resolutionRank(null)).toBe(0);
    expect(resolutionRank('?')).toBe(0);
  });
});

describe('dvLayerVariant', () => {
  it('maps fel → error', () => {
    expect(dvLayerVariant('fel')).toBe('error');
  });
  it('maps mel → orange', () => {
    expect(dvLayerVariant('mel')).toBe('orange');
  });
  it('falls back to default for unknown layer', () => {
    expect(dvLayerVariant('unknown_layer')).toBe('default');
  });
  it('falls back to default for null', () => {
    expect(dvLayerVariant(null)).toBe('default');
  });
  it('falls back to default for undefined', () => {
    expect(dvLayerVariant(undefined)).toBe('default');
  });
});
