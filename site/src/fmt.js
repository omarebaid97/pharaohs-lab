export const pct = (x, d = 0) => (x == null || Number.isNaN(x) ? '—' : `${(x * 100).toFixed(d)}%`);
export const num = (x, d = 2) => {
  if (x == null || Number.isNaN(x)) return '—';
  if (typeof x !== 'number') return String(x);
  return Number.isInteger(x) ? String(x) : x.toFixed(d).replace(/0+$/, '').replace(/\.$/, '');
};
export function date(s) {
  if (!s) return '—';
  const d = new Date(s.length === 10 ? `${s}T00:00:00Z` : s);
  if (Number.isNaN(d.getTime())) return String(s);
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' }).format(d);
}
export function dateTime(s) {
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return String(s || '—');
  return new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'UTC', timeZoneName: 'short' }).format(d);
}
export const label = (k) => String(k).replace(/_/g, ' ').replace(/\s+/g, ' ');
// Beta/binomial interval object: {mean|p, lo, hi, k?, n?}
export const isCI = (o) => o && typeof o === 'object' && !Array.isArray(o) && ('lo' in o) && ('hi' in o) && ('mean' in o || 'p' in o);
export function ci(o) {
  const m = o.mean ?? o.p;
  const kn = o.k != null && o.n != null ? ` (${o.k}/${o.n})` : o.n != null ? ` (n=${o.n})` : '';
  return `${pct(m)} [${pct(o.lo)}–${pct(o.hi)}]${kn}`;
}
export const wdl = (r) => (r ? `${r.W ?? 0}-${r.D ?? 0}-${r.L ?? 0}` : '—');
