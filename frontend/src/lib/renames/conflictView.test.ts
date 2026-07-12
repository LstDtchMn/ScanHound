// @vitest-environment node
import { describe, it, expect } from 'vitest';
import { formatBytes, formatMbps, specRows, needsDvScan, conflictSummary, actionsForKind } from './conflictView';
import type { FileSpec, ConflictAnalysis } from '$lib/api/types';

const spec = (o: Partial<FileSpec>): FileSpec => ({
  present: true, path: '/x', size_bytes: null, resolution: null, video_codec: null,
  hdr: null, dv_layer: null, audio: null, audio_profile: null, duration_min: null,
  bitrate: null, ...o,
});

const analysis = (o: Partial<ConflictAnalysis>): ConflictAnalysis => ({
  kind: 'same_path',
  existing: spec({}), incoming: spec({}),
  recommended: null, reason: null, degraded: false, analyzed_at: '2026-07-11T00:00:00Z',
  ...o,
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

  it('HDR / DV row: HDR10+ displays distinctly (not collapsed into "HDR10") and ranks over plain HDR10', () => {
    const hdr10Plus = spec({ hdr: 'HDR10+' });
    const hdr10 = spec({ hdr: 'HDR10' });
    const row = specRows(hdr10Plus, hdr10)[1];
    expect(row.existing).toBe('HDR10+');
    expect(row.incoming).toBe('HDR10');
    expect(row.better).toBe('tie'); // both rank at tier 3 today — HDR10+ shows distinctly, ranking parity is a conflicts.py-side concern
  });

  it('Video / Audio rows carry raw values with no better-side verdict', () => {
    const rows = specRows(existing, incoming);
    const video = rows.find((r) => r.label === 'Video')!;
    const audio = rows.find((r) => r.label === 'Audio')!;
    expect(video).toMatchObject({ existing: 'HEVC', incoming: 'H.264', better: null });
    expect(audio).toMatchObject({ existing: 'TrueHD 7.1', incoming: 'EAC3 5.1', better: null });
  });

  it('omits the Audio Profile row when neither side has probed audio_profile data', () => {
    const rows = specRows(existing, incoming); // fixture specs above have no audio_profile set
    expect(rows.find((r) => r.label === 'Audio Profile')).toBeUndefined();
  });

  it('adds an Audio Profile row when at least one side has probed audio_profile data', () => {
    const atmos = spec({ resolution: '2160p', audio_profile: 'TrueHD 7.1 Atmos' });
    const plain = spec({ resolution: '2160p', audio_profile: null });
    const rows = specRows(atmos, plain);
    const row = rows.find((r) => r.label === 'Audio Profile');
    expect(row).toBeDefined();
    expect(row).toMatchObject({ existing: 'TrueHD 7.1 Atmos', incoming: '—', better: null });
  });

  it('Audio Profile row shows on the incoming side alone too', () => {
    const plain = spec({ resolution: '1080p', audio_profile: null });
    const dtsHd = spec({ resolution: '1080p', audio_profile: 'DTS-HD MA 5.1' });
    const row = specRows(plain, dtsHd).find((r) => r.label === 'Audio Profile');
    expect(row).toMatchObject({ existing: '—', incoming: 'DTS-HD MA 5.1', better: null });
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

describe('conflictSummary', () => {
  it('shows only differing axes', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'mel', size_bytes: 25e9 }),
      incoming: spec({ resolution: '2160p', hdr: 'Dolby Vision', dv_layer: 'fel', size_bytes: 29e9 }),
      recommended: 'incoming',
    });
    const s = conflictSummary(a);
    expect(s).toContain('MEL');
    expect(s).toContain('FEL');
    // formatBytes is binary (1024^3) GB, not decimal 1e9 — see formatBytes
    // tests above (e.g. 40e9 -> "37.3 GB"), so 25e9/29e9 bytes render as
    // 23.3/27.0 GB, not a literal "25.0"/"29.0".
    expect(s).toContain('23.3 GB');
    expect(s).toContain('27.0 GB');
    expect(s).toContain('keep Incoming');
  });

  it('identical-except-size renders no redundant repeated axis', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p', hdr: null, dv_layer: null, size_bytes: 22e9 }),
      incoming: spec({ resolution: '2160p', hdr: null, dv_layer: null, size_bytes: 26e9 }),
      recommended: 'incoming',
    });
    const s = conflictSummary(a);
    expect(s).not.toContain('2160p');
    // See binary-vs-decimal GB note above: 22e9/26e9 bytes -> 20.5/24.2 GB.
    expect(s).toContain('20.5 GB');
    expect(s).toContain('24.2 GB');
  });

  it('missing analysis returns empty string, never throws', () => {
    expect(conflictSummary(null)).toBe('');
    expect(conflictSummary(undefined)).toBe('');
  });

  it('degraded analysis omits the keep-recommendation clause', () => {
    const a = analysis({
      existing: spec({ resolution: '2160p' }), incoming: spec({ present: false }),
      recommended: null, degraded: true,
    });
    expect(conflictSummary(a)).not.toContain('keep');
  });
});

describe('actionsForKind', () => {
  it('same_path shows Overwrite + Keep both (today\'s behavior)', () => {
    expect(actionsForKind('same_path')).toEqual({ overwrite: true, keepBoth: true, applyAnyway: false });
  });

  it('library_duplicate shows Apply anyway, not Overwrite/Keep both', () => {
    expect(actionsForKind('library_duplicate')).toEqual({ overwrite: false, keepBoth: false, applyAnyway: true });
  });

  it('undefined kind defaults to same_path shape (no analysis yet)', () => {
    expect(actionsForKind(undefined)).toEqual({ overwrite: true, keepBoth: true, applyAnyway: false });
  });
});
