import { h, link } from '../dom.js';
import { t, tl } from '../i18n.js';
import { load } from '../data.js';
import { section, table, ul, para, caveat, details, auto, badge } from '../components.js';
import { dateTime } from '../fmt.js';
import { REPO } from '../main.js';

const licFor = (name) => { const k = `meth.licenses.${name.toLowerCase().split(/[ .]/)[0]}`; const v = t(k); return v === k ? '' : v; };

export async function render() {
  const [meta, coach, sp, ld, dia] = await Promise.all(['meta', 'coach_hassan', 'set_pieces', 'load_tracker', 'diaspora_candidates'].map(load));
  const root = h('div', null, h('h1', null, t('meth.title')), h('p', { class: 'lead' }, t('meth.lead')));

  root.append(section(t('meth.sources'), {},
    table(meta.sources.map((s) => ({ source: s.name, url: s.url, used_for: s.use, [t('meth.license')]: s.license || licFor(s.name) })), ['source', 'url', 'used_for', t('meth.license')].map((k) => ({ key: k, label: k === 'url' ? 'URL' : k, fmt: (r) => (k === 'url' ? link(r.url, r.url.replace(/^https?:\/\//, '')) : r[k]) })), { rowHeader: true })));
  root.append(section(t('meth.policy'), {}, ul(tl('meth.policy_items'))));
  root.append(section(t('meth.stats'), {}, para(t('meth.stats_text'))));

  root.append(section(t('meth.module_status'), { intro: `${t('site.updated')}: ${dateTime(meta.last_updated)}` },
    table(Object.entries(meta.modules).map(([k, v]) => ({ module: k, status: v.status, 'data as of': v.as_of || '—', exported: v.exported_at ? dateTime(v.exported_at) : '—' })), null, { rowHeader: true })));

  root.append(section(t('meth.gaps'), {},
    h('h3', null, t('nav.coach')), ul(coach.methodology.gaps),
    h('h3', null, t('nav.setpieces')), ul(sp.methodology.coverage_caveats),
    h('h3', null, t('nav.load')), ul(ld.backtest.caveats),
    h('h3', null, t('nav.diaspora')), para(dia.disclaimer)));

  root.append(section(t('meth.error'), {}, para(t('meth.error_text')),
    h('p', null, link(`${REPO}/issues/new`, t('site.report_error')), ' · ', link(`${REPO}/issues`, t('meth.open_issues')), ' · ', link(REPO, 'Source code'))));
  return root;
}
