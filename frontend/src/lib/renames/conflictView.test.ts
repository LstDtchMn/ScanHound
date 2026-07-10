// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { formatBytes, formatMbps, specRows, needsDvScan } from './conflictView';
import type { FileSpec } from '$lib/api/types';

const spec = (o: Partial<FileSpec>): FileSpec => ({
  present: true, path: '/x', size_bytes: null, resolution: null, video_codec: null,
  hdr: null, dv_layer: null, audio: null, duration_min: null, bitrate: null, ...o,
});

describe('formatBytes', () => {
  it('null/undefined -> em dash', () => {
    expect(formatBytes(null)).toBe('—');
    expect(formatBytes(undefined)).toBe('—');
  });
  it('sub-KB stays in bytes', () => expect(formatBytes(500)).toBe('500 B'));
  it('formats KB', () => expect(formatBytes(2048)).toBe('2.0 KB'));
  it('formats GB (40e9 bytes)', () => expect(formatBytes(40e9)).toBe('37.3 GB'));
  it('formats GB (8e9 bytes)', () => expect(formatBytes(8e9)).toBe('7.5 GB'));
});

describe('formatMbps', () => {
  it('null/undefined -> em dash', () => {
    expect(formatMbps(null)).toBe('—');
    expect(formatMbps(undefined)).toBe('—');
  });
  it('zero/negative -> em dash (unknown probe)', () => {
    expect(formatMbps(0)).toBe('—');
    expect(formatMbps(-1)).toBe('—');
  });
  it('formats Mbps (46e6 bps)', () => expect(formatMbps(46e6)).toBe('46.0 Mbps'));
  it('formats Mbps (10e6 bps)', () => expect(formatMbps(10e6)).toBe('10.0 Mbps'));
});

describe('specRows', () => {
  const existing = spec({
    resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'fel', size_bytes: 40e9,
    video_codec: 'HEVC', audio: 'TrueHD 7.1', bitrate: 46e6, duration_min: 120, path: '/d/y',
  });
  const incoming = spec({
    resolution: '1080p', hdr: null, dv_layer: null, size_bytes: 8e9,
    video_codec: 'H.264', audio: 'EAC3 5.1', bitrate: 10e6, duration_min: 120, path: '/x',
  });

  it('returns rows in the documented order', () => {
    const rows = specRows(existing, incoming);
    expect(rows.map((r) => r.label)).toEqual([
      'Resolution', 'HDR / DV', 'Video', 'Audio', 'Bitrate', 'Size', 'Duration',
    ]);
  });

  it('Resolution row: values + existing (2160p) ranks better than incoming (1080p)', () => {
    const [row] = specRows(existing, incoming);
    expect(row.existing).toBe('2160p');
    expect(row.incoming).toBe('1080p');
    expect(row.better).toBe('existing');
  });

  it('HDR / DV row: shows the DV layer when known, ranks Dolby Vision over SDR', () => {
    const row = specRows(existing, incoming)[1];
    expect(row.existing).toBe('Dolby Vision (FEL)');
    expect(row.incoming).toBe('SDR');
    expect(row.better).toBe('existing');
  });

  it('HDR / DV row: ranks HLG over SDR (not treated as SDR)', () => {
    const hlg = spec({ resolution: '2160p', hdr: 'HLG' });
    const sdr = spec({ resolution: '2160p', hdr: null });
    const row = specRows(hlg, sdr)[1];
    expect(row.existing).toBe('HLG');
    expect(row.incoming).toBe('SDR');
    expect(row.better).toBe('existing');
  });

  it('HDR / DV row: ranks Dolby Vision and HDR10 over HLG', () => {
    const dv = spec({ hdr: 'Dolby Vision', dv_layer: 'p8' });
    const hdr10 = spec({ hdr: 'HDR10' });
    const hlg = spec({ hdr: 'HLG' });
    expect(specRows(dv, hlg)[1].better).toBe('existing');
    expect(specRows(hdr10, hlg)[1].better).toBe('existing');
  });

  it('Video / Audio rows carry raw values with no better-side verdict', () => {
    const rows = specRows(existing, incoming);
    const video = rows.find((r) => r.label === 'Video')!;
    const audio = rows.find((r) => r.label === 'Audio')!;
    expect(video).toMatchObject({ existing: 'HEVC', incoming: 'H.264', better: null });
    expect(audio).toMatchObject({ existing: 'TrueHD 7.1', incoming: 'EAC3 5.1', better: null });
  });

  it('Bitrate row: formats Mbps and ranks the higher bitrate better', () => {
    const row = specRows(existing, incoming).find((r) => r.label === 'Bitrate')!;
    expect(row.existing).toBe('46.0 Mbps');
    expect(row.incoming).toBe('10.0 Mbps');
    expect(row.better).toBe('existing');
  });

  it('Size row: formats bytes and ranks the larger file better', () => {
    const row = specRows(existing, incoming).find((r) => r.label === 'Size')!;
    expect(row.existing).toBe('37.3 GB');
    expect(row.incoming).toBe('7.5 GB');
    expect(row.better).toBe('existing');
  });

  it('Duration row: raw minutes with no better-side verdict', () => {
    const row = specRows(existing, incoming).find((r) => r.label === 'Duration')!;
    expect(row.existing).toBe('120 min');
    expect(row.incoming).toBe('120 min');
    expect(row.better).toBe(null);
  });

  it('destination-free (existing.present === false) shows em dashes and favors incoming', () => {
    const free = spec({ present: false, path: '/d/y', resolution: null });
    const rows = specRows(free, incoming);
    const res = rows[0];
    expect(res.existing).toBe('—');
    expect(res.incoming).toBe('1080p');
    expect(res.better).toBe('incoming');
  });

  it('null existing/incoming specs render em dashes without throwing', () => {
    const rows = specRows(null, null);
    expect(rows.every((r) => r.existing === '—' && r.incoming === '—')).toBe(true);
  });
});

describe('needsDvScan', () => {
  it('true when hdr is Dolby Vision and dv_layer is unknown', () => {
    expect(needsDvScan(spec({ hdr: 'Dolby Vision', dv_layer: null }))).toBe(true);
  });
  it('false when the DV layer is already known', () => {
    expect(needsDvScan(spec({ hdr: 'Dolby Vision', dv_layer: 'fel' }))).toBe(false);
  });
  it('false when hdr is not Dolby Vision', () => {
    expect(needsDvScan(spec({ hdr: 'HDR10', dv_layer: null }))).toBe(false);
    expect(needsDvScan(spec({ hdr: null, dv_layer: null }))).toBe(false);
  });
  it('false for a null/undefined spec', () => {
    expect(needsDvScan(null)).toBe(false);
    expect(needsDvScan(undefined)).toBe(false);
  });
});
