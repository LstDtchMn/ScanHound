/** Parse a numeric settings-input value, keeping a legitimately-typed `0`
 *  instead of falling back to the default the way `parseInt(x) || default`
 *  does. Only an unparseable/empty value falls back. */
export function numOrDefault(raw: string, fallback: number): number {
  const v = parseInt(raw, 10);
  return Number.isNaN(v) ? fallback : v;
}
