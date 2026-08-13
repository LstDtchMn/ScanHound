/** Return `url` only when it is a plain http(s) link, else ''.
 *
 *  Every url rendered by the downloads views was scraped from a third-party
 *  release page and stored verbatim. Binding an attacker-controlled string
 *  straight into an href is how a stored 'javascript:...' value becomes script
 *  execution on click, so the scheme is checked at the point of rendering
 *  rather than trusted from the database. Callers render no anchor at all when
 *  this returns ''. */
export function safeHttpUrl(url: string | null | undefined): string {
  if (!url) return '';
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? url : '';
  } catch {
    return '';   // relative/malformed — nothing safe to link to
  }
}
