import { h, link, safeUrl } from './dom.js';
import { t } from './i18n.js';
import { label, num, isCI, ci, date } from './fmt.js';

export const SRC = {
  fotmob: { name: 'FotMob', url: 'https://www.fotmob.com/' },
  elo: { name: 'eloratings.net', url: 'https://www.eloratings.net/' },
  wikipedia: { name: 'Wikipedia', url: 'https://en.wikipedia.org/' },
  wikidata: { name: 'Wikidata', url: 'https://www.wikidata.org/' },
  statsbomb: { name: 'StatsBomb open data', url: 'https://github.com/statsbomb/open-data' },
  transfermarkt: { name: 'Transfermarkt', url: 'https://www.transfermarkt.com/' },
  fifa: { name: 'FIFA', url: 'https://www.fifa.com/' },
};

export function sources(keys, extraUrls = []) {
  const out = [];
  for (const k of keys || []) if (SRC[k]) out.push(link(SRC[k].url, SRC[k].name));
  extraUrls.filter(Boolean).forEach((u, i) => out.push(link(u, t('common.source_n', { n: i + 1 }))));
  const nodes = [];
  out.forEach((o, i) => { if (i) nodes.push(', '); nodes.push(o); });
  return nodes;
}

// A page section. opts: {n, src:[keys], urls:[...], caveat, intro, id, level}
export function section(title, opts = {}, ...body) {
  const meta = [];
  if (opts.n != null) meta.push(h('span', { class: 'n' }, t('common.n'), ' = ', String(opts.n)));
  if ((opts.src && opts.src.length) || (opts.urls && opts.urls.length)) {
    meta.push(h('span', { class: 'src' }, t('common.source'), ': ', sources(opts.src, opts.urls)));
  }
  return h('section', { class: 'sec', id: opts.id },
    h(opts.level || 'h2', null, title),
    opts.intro ? h('p', { class: 'intro' }, opts.intro) : null,
    opts.caveat ? caveat(opts.caveat) : null,
    body,
    meta.length ? h('p', { class: 'meta' }, meta.flatMap((m, i) => (i ? [' · ', m] : [m]))) : null);
}

export function caveat(text, kind = 'caveat') {
  return h('p', { class: kind, role: 'note' }, h('strong', null, kind === 'warn' ? t('common.warning') : t('common.caveat'), ': '), text);
}
export function note(text) { return text ? h('p', { class: 'note' }, text) : null; }
export function para(text) { return text ? h('p', null, text) : null; }

export function kpis(items) {
  return h('div', { class: 'kpis' }, items.map(([k, v, sub]) => h('div', { class: 'kpi' },
    h('div', { class: 'kpi-v' }, v), h('div', { class: 'kpi-k' }, k), sub ? h('div', { class: 'kpi-s' }, sub) : null)));
}

export function badge(text, kind = '') { return h('span', { class: `badge ${kind}` }, text); }

export function ul(items) { return h('ul', null, items.map((i) => h('li', null, i))); }

export function details(summary, ...body) { return h('details', null, h('summary', null, summary), body); }

// cell content for a value (text only, links only for http(s))
export function cell(v, key = '') {
  if (v == null) return '—';
  if (typeof v === 'number') return num(v);
  if (typeof v === 'boolean') return v ? 'yes' : 'no';
  if (typeof v === 'string') {
    if (/(^|_)(source_url|url|link)$/.test(key) && safeUrl(v)) return link(v, t('common.source_short'));
    return v;
  }
  if (isCI(v)) return ci(v);
  if (Array.isArray(v)) {
    if (v.length && v.every((x) => typeof x === 'string' && safeUrl(x))) {
      return h('span', null, v.slice(0, 4).flatMap((u, i) => (i ? [' ', link(u, `[${i + 1}]`)] : [link(u, '[1]')])));
    }
    return v.map((x) => (typeof x === 'object' && x ? compact(x) : String(x))).join(', ');
  }
  return compact(v);
}
export function compact(o) {
  if (o == null) return '—';
  if (isCI(o)) return ci(o);
  if (Array.isArray(o)) return o.map(compact).join(', ');
  if (typeof o === 'object') return Object.entries(o).map(([k, v]) => `${label(k)}: ${compact(v)}`).join('; ');
  return typeof o === 'number' ? num(o) : String(o);
}

// table(rows, cols, opts) -- cols: [key | {key, label, fmt(row)->Node|string}]
export function table(rows, cols, opts = {}) {
  const cs = (cols || Object.keys(rows[0] || {})).map((c) => (typeof c === 'string' ? { key: c, label: label(c) } : { label: label(c.key), ...c }));
  const body = rows.map((r) => h('tr', null, cs.map((c, i) => {
    const v = c.fmt ? c.fmt(r) : cell(r[c.key], c.key);
    return h(i === 0 && opts.rowHeader ? 'th' : 'td', i === 0 && opts.rowHeader ? { scope: 'row' } : { class: typeof r[c.key] === 'number' ? 'num' : '' }, v);
  })));
  const tbl = h('table', null,
    opts.caption ? h('caption', null, opts.caption) : null,
    h('thead', null, h('tr', null, cs.map((c) => h('th', { scope: 'col' }, c.label)))),
    h('tbody', null, body));
  const wrap = h('div', { class: 'tablewrap', role: 'region', tabindex: '0', 'aria-label': opts.caption || t('common.table') }, tbl);
  if (opts.collapseOver && rows.length > opts.collapseOver) {
    return details(t('common.show_rows', { n: rows.length }), wrap);
  }
  return wrap;
}

// dict-of-dicts -> table with key column
export function dictTable(obj, opts = {}) {
  const keys = Object.keys(obj);
  const rows = keys.map((k) => ({ [opts.keyLabel || 'name']: k, ...(typeof obj[k] === 'object' && obj[k] && !isCI(obj[k]) ? obj[k] : { value: obj[k] }) }));
  const cols = opts.cols || [...new Set(rows.flatMap((r) => Object.keys(r)))];
  return table(rows, cols, { rowHeader: true, ...opts });
}

// key/value list
export function dl(obj, opts = {}) {
  const entries = Object.entries(obj).filter(([, v]) => v != null && (!opts.skip || !opts.skip.includes(v)));
  return h('dl', { class: 'kv' }, entries.flatMap(([k, v]) => [h('dt', null, label(k)), h('dd', null, cell(v, k))]));
}

// Generic fallback renderer for nested data: used for the long tail so nothing is hidden.
export function auto(v, opts = {}, depth = 0) {
  if (v == null || typeof v !== 'object' || isCI(v)) return h('span', null, cell(v));
  if (Array.isArray(v)) {
    if (!v.length) return h('p', { class: 'note' }, t('common.none'));
    if (v.every((x) => x && typeof x === 'object' && !Array.isArray(x) && !isCI(x))) {
      const keys = [...new Set(v.flatMap((r) => Object.keys(r)))];
      return table(v, keys, { collapseOver: opts.collapseOver ?? 15 });
    }
    return ul(v.map((x) => cell(x)));
  }
  const vals = Object.values(v);
  if (vals.length && vals.every((x) => x && typeof x === 'object' && !Array.isArray(x) && !isCI(x))) {
    return dictTable(v, { collapseOver: opts.collapseOver ?? 20, keyLabel: opts.keyLabel });
  }
  // mixed dict: scalars in a dl, nested in sub-blocks
  const scalars = {}; const nested = [];
  for (const [k, x] of Object.entries(v)) {
    if (x && typeof x === 'object' && !isCI(x)) nested.push([k, x]); else scalars[k] = x;
  }
  return h('div', { class: 'auto' },
    Object.keys(scalars).length ? dl(scalars) : null,
    nested.map(([k, x]) => (depth >= 2 ? h('div', null, h('strong', null, label(k)), ': ', compact(x)) : details(label(k), auto(x, opts, depth + 1)))));
}

export function oddsBar(w) {
  const seg = (k, cls) => h('span', { class: `seg ${cls}`, style: `width:${Math.max(w[k] * 100, 0)}%` }, w[k] >= 0.07 ? `${Math.round(w[k] * 100)}%` : '');
  return h('div', { class: 'odds', role: 'img', 'aria-label': `Win ${Math.round(w.win * 100)}%, draw ${Math.round(w.draw * 100)}%, loss ${Math.round(w.loss * 100)}%` },
    seg('win', 'w'), seg('draw', 'd'), seg('loss', 'l'));
}
export const dateCell = (s) => date(s);
