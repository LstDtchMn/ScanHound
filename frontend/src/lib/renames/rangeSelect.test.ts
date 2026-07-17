import { describe, it, expect } from 'vitest';
import { computeRange } from './rangeSelect';

describe('computeRange', () => {
  const order = [10, 20, 30, 40, 50];

  it('forward range (anchor before target)', () => {
    expect(computeRange(order, 20, 40)).toEqual([20, 30, 40]);
  });

  it('backward range (anchor after target)', () => {
    expect(computeRange(order, 40, 20)).toEqual([20, 30, 40]);
  });

  it('anchor equals target -> single', () => {
    expect(computeRange(order, 30, 30)).toEqual([30]);
  });

  it('full span', () => {
    expect(computeRange(order, 10, 50)).toEqual([10, 20, 30, 40, 50]);
  });

  it('anchor not in list -> falls back to target only', () => {
    expect(computeRange(order, 999, 30)).toEqual([30]);
  });

  it('target not in list -> falls back to target only', () => {
    expect(computeRange(order, 20, 999)).toEqual([999]);
  });

  it('single-element list', () => {
    expect(computeRange([7], 7, 7)).toEqual([7]);
  });
});
