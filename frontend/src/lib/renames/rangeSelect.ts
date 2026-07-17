/** Inclusive id range between an anchor and a target within an ordered id list
 *  (the flattened visual order of the Renames list). Direction-agnostic. If
 *  either id isn't present (e.g. the anchor scrolled out of the current
 *  filtered/sorted view), falls back to just the target — the caller then
 *  treats it as a plain single selection. Pure + dependency-free for testing. */
export function computeRange(orderedIds: number[], anchorId: number, targetId: number): number[] {
  const a = orderedIds.indexOf(anchorId);
  const b = orderedIds.indexOf(targetId);
  if (a === -1 || b === -1) return [targetId];
  const [lo, hi] = a <= b ? [a, b] : [b, a];
  return orderedIds.slice(lo, hi + 1);
}
