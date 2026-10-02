import { h } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dl, kpis, note, caveat, para, ul, details, auto, badge } from '../components.js';
import { chart } from '../charts.js';
import { num, pct } from '../fmt.js';

const split = (s) => ({ n: s.n, W: s.W, D: s.D, L: s.L, ppg: s.ppg, 'goals for/match': s.gf_pm, 'goals against/match': s.ga_pm, 'mean opp Elo': s.mean_opp_elo, 'Elo-expected ppg': s.elo_expected_ppg });

function playerCard(p, extraBadge) {
  return h('article', { class: 'card' },
    h('div', { class: 'cand-head' }, h('h3', null, p.name), h('span', null, p.low_confidence ? badge(t('salah.low_conf'), 'red') : null, extraBadge || null)),
    p.low_confidence ? caveat(t('salah.low_conf_note'), 'warn') : null,
    p.fit == null ? h('p', { class: 'note' }, t('salah.not_found_detail')) : [
      h('p', null, `${p.club} (${p.league}) · ${p.position} · ${t('salah.age')} ${p.age}`),
      kpis([[t('salah.fit'), num(p.fit)], [t('salah.output'), num(p.output_index)], [t('salah.minutes'), p.minutes_12m], ['Goals / assists (12m)', `${p.goals_12m} / ${p.assists_12m}`]]),
      p.reasons ? h('details', null, h('summary', null, 'Why'), ul(p.reasons)) : null]);
}

export async function render() {
  const d = await load('salah_succession');
  const dep = d.dependency; const ov = dep.overall;
  const root = h('div', null, h('h1', null, t('salah.title')), h('p', { class: 'lead' }, t('salah.lead')));

  // dependency
  const wi = ov.with_salah_in_xi; const wo = ov.without_salah_in_xi;
  root.append(section(t('salah.dependency'), { n: dep.n_matches_used, src: ['fotmob', 'elo'], caveat: dep.scope },
    kpis([[t('salah.with'), `${num(wi.ppg)} ppg`, `n = ${wi.n}, ${wi.W}W ${wi.D}D ${wi.L}L`], [t('salah.without'), `${num(wo.ppg)} ppg`, `n = ${wo.n}, ${wo.W}W ${wo.D}D ${wo.L}L`],
      [t('salah.diff'), `+${num(ov.ppg_diff_with_minus_without)}`, `${t('salah.bootstrap')}: ${num(ov.ppg_diff_bootstrap95[0])} to ${num(ov.ppg_diff_bootstrap95[1])}`]]),
    h('p', null, `The interval includes zero, so the data cannot separate "Egypt are better with Salah" from noise. Egypt's opponents were also similar in strength (${wi.mean_opp_elo} vs ${wo.mean_opp_elo} mean Elo), but see the confounders.`),
    table([{ group: t('salah.with'), ...split(wi) }, { group: t('salah.without'), ...split(wo) }], null, { rowHeader: true }),
    h('h3', null, t('salah.confounders')), ul(dep.confounders),
    h('h3', null, t('salah.by_band')),
    table(Object.entries(dep.by_opp_elo_band).flatMap(([k, v]) => [{ band: k, group: 'with', ...split(v.with_salah) }, { band: k, group: 'without', ...split(v.without_salah) }]), null, { collapseOver: 0 }),
    h('h3', null, t('salah.by_comp')),
    table(Object.entries(dep.by_competition_type).flatMap(([k, v]) => [{ type: k, group: 'with', ...split(v.with_salah) }, ...(v.without_salah ? [{ type: k, group: 'without', ...split(v.without_salah) }] : [])]), null, { collapseOver: 0 }),
    h('h3', null, t('salah.share')),
    table([['In matches he started', dep.salah_share_when_in_xi], ['All matches', dep.salah_share_all_matches]].map(([k, v]) => ({ scope: k, 'matches with events': v.matches_with_events, 'Egypt goals': v.egypt_goals_in_events, 'Salah goals': v.salah_goals, 'goal share': pct(v.goal_share), 'assists (recorded)': v.salah_assists, 'goal involvement share': pct(v.salah_goal_involvement_share) })), null, { rowHeader: true }),
    h('h3', null, t('salah.marmoush')), note(dep.marmoush_era_since_2024_02_06.note),
    table(Object.entries(dep.marmoush_era_since_2024_02_06.grid).map(([k, v]) => ({ cell: k.replace(/__/g, ' / ').replace(/_/g, ' '), ...split(v) })), null, { rowHeader: true, caption: `n = ${dep.marmoush_era_since_2024_02_06.n}` })));

  // roles
  const roleKeys = Object.keys(d.similarity);
  const holder = h('div');
  const tabs = h('div', { class: 'tabs', role: 'group', 'aria-label': t('salah.role') });
  const show = (k) => {
    holder.replaceChildren();
    const r = d.similarity[k];
    holder.append(note(`${t('common.n')} scored = ${r.n_scored}`),
      table(r.top15.map((p) => ({ rank: p.rank, player: p.name, similarity: p.sim, age: p.age, club: p.club, league: p.league, position: p.position, 'minutes (12m)': p.minutes_12m, 'closest on': (p.top_agreeing_features || []).slice(0, 2).map((f) => f.feature).join(', '), 'largest gap': (p.largest_gaps || []).slice(0, 1).map((f) => `${f.feature} ${f.candidate_minus_target_pct > 0 ? '+' : ''}${f.candidate_minus_target_pct}`).join(', ') })), null, { caption: t(`salah.role_${k}`) }));
    for (const b of tabs.children) b.setAttribute('aria-pressed', String(b.dataset.k === k));
  };
  roleKeys.forEach((k) => { const b = h('button', { type: 'button', 'data-k': k, 'aria-pressed': 'false' }, t(`salah.role_${k}`)); b.addEventListener('click', () => show(k)); tabs.append(b); });
  root.append(section(t('salah.roles'), { n: d.coverage.eligible, src: ['fotmob'], intro: t('salah.roles_note') }, tabs, holder,
    details('Role definitions', auto(d.role_definitions)), caveat(d.league_strength_multiplier)));
  show(roleKeys[0]);

  // board
  const tiers = d.board.tiers;
  const sens = d.board.rules.league_tier_table.sensitivity_if_egypt_league_tier_2;
  const byName = {};
  for (const g of ['ready_now', 'one_two_years', 'long_term', 'watch']) for (const p of tiers[g] || []) byName[p.name] = p;
  const boardHolder = h('div', { 'aria-live': 'polite' });
  const toggle = h('div', { class: 'tabs', role: 'group', 'aria-label': t('salah.tier_toggle') });
  const draw = (mode) => {
    boardHolder.replaceChildren();
    if (mode === 'tier2') boardHolder.append(caveat(t('salah.tier2_note')));
    for (const g of ['ready_now', 'one_two_years', 'long_term']) {
      const list = mode === 'tier2' ? (sens[g] || []).map((nm) => byName[nm] || { name: nm }) : tiers[g] || [];
      boardHolder.append(h('h3', null, `${t(`salah.${g}`)} (${list.length})`));
      if (!list.length) boardHolder.append(note(t('salah.none_in_group')));
      list.forEach((p) => boardHolder.append(playerCard(p, mode === 'tier2' && !(tiers[g] || []).some((x) => x.name === p.name) ? badge('moves here at tier 2', 'gold') : null)));
    }
    if (mode === 'tier3') {
      boardHolder.append(h('h3', null, `${t('salah.watch')} (${(tiers.watch || []).length})`),
        table((tiers.watch || []).map((p) => ({ player: p.name, fit: p.fit, 'output index': p.output_index, age: p.age, club: p.club, 'minutes (12m)': p.minutes_12m, 'low confidence': p.low_confidence })), null, { collapseOver: 8 }));
    }
    for (const b of toggle.children) b.setAttribute('aria-pressed', String(b.dataset.m === mode));
  };
  [['tier3', t('salah.tier3')], ['tier2', t('salah.tier2')]].forEach(([m, l]) => { const b = h('button', { type: 'button', 'data-m': m, 'aria-pressed': 'false' }, l); b.addEventListener('click', () => draw(m)); toggle.append(b); });
  root.append(section(t('salah.board'), { n: d.coverage.eligible, src: ['fotmob'], intro: t('salah.board_note') },
    h('p', null, h('strong', null, t('salah.tier_toggle'), ': ')), toggle, boardHolder,
    details(t('salah.rules'), dl({ ...d.board.rules, league_tier_table: undefined })),
    details(t('salah.coverage'), auto(d.coverage))));
  draw('tier3');
  return root;
}
