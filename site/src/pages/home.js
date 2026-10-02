import { h } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, oddsBar, table, caveat, kpis, badge, sources } from '../components.js';
import { date, dateTime, num, pct, ci } from '../fmt.js';

export function nextFixture(opp) {
  const all = [];
  for (const o of opp.opponents || []) {
    for (const f of (o.fixtures && o.fixtures.items) || []) if (f.status === 'upcoming') all.push({ o, f });
  }
  all.sort((a, b) => (a.f.kickoff_utc || a.f.date).localeCompare(b.f.kickoff_utc || b.f.date));
  return all[0] || null;
}

function card(link, title, text, extra) {
  return h('article', { class: 'card' }, h('h3', null, title), h('p', null, text), extra || null,
    h('p', { class: 'meta' }, h('a', { href: link }, t('home.read_more'))));
}

export async function render() {
  const [opp, coach, salah, dia, sp, ld, meta] = await Promise.all(
    ['opponent_dossiers', 'coach_hassan', 'salah_succession', 'diaspora_candidates', 'set_pieces', 'load_tracker', 'meta'].map(load));
  const root = h('div', null, h('h1', null, t('home.title')), h('p', { class: 'lead' }, t('home.lead')));

  // ---- next match card
  const nx = nextFixture(opp);
  if (!nx) {
    root.append(h('div', { class: 'card next' }, h('h2', null, t('home.next_match')), h('p', null, t('home.no_next'))));
  } else {
    const { o, f } = nx;
    const side = f.egypt_side;
    const venueTxt = side === 'home' ? t('home.home') : side === 'away' ? t('home.away') : t('home.neutral');
    const card1 = h('div', { class: 'card next' },
      h('p', { class: 'meta' }, t('home.next_match'), ' · ', f.competition),
      h('h2', { style: 'border:0;margin-top:.2rem' }, `Egypt ${t('home.vs')} ${o.opponent}`, ' ', badge(venueTxt, 'dark')),
      h('p', null, date(f.date), f.kickoff_utc && !f.date_tbd_flag ? ` · ${t('home.kickoff')} ${dateTime(f.kickoff_utc)}` : '', ` · ${f.venue || ''}${f.city ? `, ${f.city}` : ''}`));
    if (f.elo) {
      card1.append(h('h3', null, t('home.elo_odds')), oddsBar(f.elo.wdl),
        h('p', { class: 'note' }, `Elo: Egypt ${f.elo.egypt} (#${f.elo.egypt_rank}), ${o.opponent} ${f.elo.opponent} (#${f.elo.opponent_rank}). `, t('home.elo_note')));
    }
    // formation: scenario model + persistence baseline
    const a = o.egypt_angle || {};
    const fm = a.formation_model, base = a.formation_persistence_baseline, bt = a.backtest;
    card1.append(h('h3', null, t('home.predicted_formation')));
    if (fm && fm.top) {
      card1.append(h('p', { class: 'note' }, `${fm.scenario} (n = ${fm.n_matches_in_cell} matching matches). ${t('home.formation_note')}`),
        table(fm.top.map((x) => ({ formation: x.formation, probability: pct(x.prob), 'interval (90%)': `${pct(x.ci90[0])}–${pct(x.ci90[1])}` })), ['formation', 'probability', 'interval (90%)']));
    }
    if (base) {
      card1.append(h('p', null, h('strong', null, t('home.baseline'), ': '), base.formation, ` (${base.from_match})`));
    }
    if (bt) {
      card1.append(caveat(t('home.baseline_honest', { base: pct(bt.persistence_exact_hit_rate), model: pct(bt.model_exact_hit_rate), n: bt.n_test_matches }), 'warn'));
    }
    const xi = coach.predictive.xi_next_match;
    const byName = Object.fromEntries(xi.players.map((p) => [p.player, p]));
    card1.append(h('h3', null, t('home.predicted_xi')), h('p', { class: 'note' }, t('home.xi_note')),
      h('ol', { class: 'xi' }, xi.predicted_xi_top_gk_plus_10.map((n) => {
        const p = byName[n];
        return h('li', null, n, p ? ` ${pct(p.p_start)} [${pct(p.p_start_ci80[0])}–${pct(p.p_start_ci80[1])}]` : '');
      })),
      h('p', { class: 'meta' }, `${t('home.xi_n', { n: coach.predictive.xi_backtest.n_test_matches })} · `, t('common.source'), ': ',
        sources(['fotmob', 'elo']), ' · ', h('a', { href: '/coach#predict' }, t('home.read_more'))));
    root.append(card1);
  }

  // ---- headline findings
  const rr = coach.record_results.overall_shootouts_as_draws;
  const fb = coach.predictive.formation_backtest;
  const ov = salah.dependency.overall;
  const cw = ld.current_window;
  const flagged = cw.board.filter((p) => p.score > 0).length;
  const bn = ld.backtest.n_rows;
  const firstSp = (sp.key_findings || [])[0];
  root.append(h('h2', null, t('home.findings')), h('div', { class: 'grid two' },
    card('/coach', t('home.f_coach'), t('home.f_coach_text', { p: rr.P, from: coach.meta.first_match, w: rr.W, d: rr.D, l: rr.L, ppg: rr.points_per_game, model: pct(fb.exact_hit_rate.mean), base: pct(fb.baseline_same_as_previous_match.mean), n: fb.n_test_matches })),
    card('/salah', t('home.f_salah'), t('home.f_salah_text', { with: num(ov.with_salah_in_xi.ppg), nw: ov.with_salah_in_xi.n, without: num(ov.without_salah_in_xi.ppg), nwo: ov.without_salah_in_xi.n, diff: num(ov.ppg_diff_with_minus_without), lo: num(ov.ppg_diff_bootstrap95[0]), hi: num(ov.ppg_diff_bootstrap95[1]) })),
    card('/diaspora', t('home.f_diaspora'), t('home.f_diaspora_text', { n: dia.count }), h('p', { class: 'note' }, dia.disclaimer)),
    card('/opponents', t('home.f_opp'), t('home.f_opp_text', { n: opp.opponents.length })),
    card('/set-pieces', t('home.f_sp'), firstSp ? `${firstSp.text} (${firstSp.sample})` : ''),
    card('/load', t('home.f_load'), t('home.f_load_text', { flagged, total: cw.board.length, n: bn }))));

  root.append(h('p', { class: 'meta' }, `${t('site.updated')}: ${dateTime(meta.last_updated)}`));
  return root;
}
