import { h, link } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dl, kpis, note, caveat, para, ul, details, badge } from '../components.js';
import { num } from '../fmt.js';

export async function render() {
  const d = await load('diaspora_candidates');
  const root = h('div', null, h('h1', null, t('diaspora.title')),
    h('div', { class: 'disclaimer', role: 'note' }, h('strong', null, t('diaspora.disclaimer_title'), ': '), d.disclaimer),
    h('p', { class: 'lead' }, t('diaspora.lead')));

  const needs = Object.entries(d.position_need);
  root.append(section(t('diaspora.position_need'), { src: ['fotmob'], intro: t('diaspora.position_need_note') },
    table(needs.map(([k, v]) => ({ position: k, 'need score': v.need, 'regulars': v.n_regulars, 'minutes (12m)': v.minutes_12m, 'avg age of top 2': v.top2_avg_age, 'top 2': (v.top2 || []).join(', ') })), null, { rowHeader: true })));

  const cards = [...d.candidates].sort((a, b) => a.rank - b.rank).map((c) => {
    const checked = c.verification === 'checked';
    return h('article', { class: 'card' },
      h('div', { class: 'cand-head' }, h('h3', null, `#${c.rank} ${c.name}`), h('span', null, badge(checked ? t('diaspora.v_checked') : t('diaspora.v_partial'), checked ? 'gold' : ''), ' ', badge(`${t('diaspora.score')} ${num(c.score, 1)}`, 'dark'))),
      h('p', null, [c.position, c.club && `${c.club}${c.league ? ` (${c.league})` : ''}`, c.age != null && `age ${num(c.age, 1)}`].filter(Boolean).join(' · ')),
      h('p', null, badge(c.club_verified ? t('diaspora.club_verified') : t('diaspora.club_unverified')), badge(c.caps_verified ? t('diaspora.caps_verified') : t('diaspora.caps_unverified')),
        c.birth_country ? badge(`born: ${c.birth_country}`) : null, ...(c.citizenships || []).map((x) => badge(x))),
      h('p', { class: 'note' }, c.why_abroad, '. ', c.cap_check),
      c.other_nation_youth_appearances && c.other_nation_youth_appearances.length ? caveat(`${t('diaspora.youth_other')}: ${c.other_nation_youth_appearances.map((x) => (typeof x === 'string' ? x : JSON.stringify(x))).join('; ')}`) : null,
      c.name_collision_note ? caveat(c.name_collision_note) : null,
      h('h4', null, t('diaspora.evidence')),
      h('ul', null, c.evidence.map((e) => h('li', null, h('strong', null, `${e.claim} (${e.confidence}): `), e.evidence_text, ' ', link(e.source_url, `[${t('common.source_short')}]`)))),
      h('p', { class: 'meta' }, t('common.source'), ': ', ...(c.source_links || []).flatMap((u, i) => (i ? [', ', link(u, `link ${i + 1}`)] : [link(u, 'link 1')]))),
      details(t('diaspora.score'), dl({ ...c.score_components }), note(`${t('diaspora.weights')}: ${Object.entries(c.score_weights_used).map(([k, v]) => `${k} ${num(v)}`).join(', ')}`)));
  });
  root.append(section(t('diaspora.cards'), { n: d.count, src: ['wikipedia', 'wikidata', 'transfermarkt'], intro: t('diaspora.no_private') }, ...cards));

  root.append(section(t('diaspora.rule'), { src: ['fifa'], intro: t('diaspora.rule_note') },
    ...d.fifa_rule.split(/(?<=\.) (?=Art\. |How this module)/).map((p) => h('p', null, p)),
    h('h3', null, t('diaspora.fifa_docs')),
    h('ul', null, Object.entries(d.fifa_sources).map(([k, u]) => h('li', null, link(u, k))))));

  root.append(section(t('diaspora.method'), { n: d.n_documented_total }, para(t('diaspora.method_text'))));
  return root;
}
