import { h, link } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dictTable, dl, kpis, note, caveat, para, ul, details, auto, badge, compact } from '../components.js';
import { chart, PALETTE } from '../charts.js';
import { pct, num, ci, date, wdl } from '../fmt.js';

const recRow = (name, r) => ({ name, P: r.P, W: r.W, D: r.D, L: r.L, 'points per game': r.points_per_game });

export async function render() {
  const d = await load('coach_hassan');
  const { record_results: rr, tendencies: tn, predictive: pr, bio_career: bio } = d;
  const n = d.meta.matches;
  const root = h('div', null, h('h1', null, t('coach.title')), h('p', { class: 'lead' }, t('coach.lead')));

  // ---- career
  root.append(section(t('coach.career'), { src: ['wikipedia'], urls: [bio.source_url] },
    dl(bio.personal),
    h('h3', null, t('coach.playing')),
    kpis([['Senior apps', bio.senior_total.apps], ['Senior goals', bio.senior_total.goals], ['Egypt caps', bio.international_playing.caps], ['Egypt goals', bio.international_playing.goals]]),
    table(bio.senior_career, ['years', 'team', 'apps', 'goals'], { caption: t('coach.playing') }),
    h('h3', null, t('coach.managerial')),
    table(bio.managerial_jobs, ['team', 'from', 'to', 'P', 'W', 'D', 'L', 'win_pct'], { caption: t('coach.managerial'), collapseOver: 8 }),
    note(bio.club_era_detail)));

  // ---- record
  const o = rr.overall_shootouts_as_draws;
  const bands = Object.entries(rr.by_opponent_elo_band);
  root.append(section(t('coach.record'), { n, src: ['fotmob', 'elo'], urls: [] , caveat: t('coach.record_note') },
    kpis([[t('common.played'), o.P], [t('common.won'), o.W], [t('common.drawn'), o.D], [t('common.lost'), o.L], ['GF-GA', `${o.goals_for}-${o.goals_against}`], [t('common.ppg'), num(o.points_per_game)]]),
    h('h3', null, t('coach.by_band')),
    chart('bar', {
      labels: bands.map(([k, v]) => `${k} (n=${v.P})`),
      datasets: [['W', '#CE1126', 'W'], ['D', '#9ca3af', 'D'], ['L', '#111111', 'L']].map(([k, c, l]) => ({ label: l, data: bands.map(([, v]) => v[k]), backgroundColor: c })),
    }, { stacked: true, alt: `Results by opponent Elo band: ${bands.map(([k, v]) => `${k} ${wdl(v)}`).join('; ')}`, yTitle: 'matches' }),
    table(bands.map(([k, v]) => recRow(k, v)), null, { rowHeader: true, caption: t('coach.by_band') }),
    h('div', { class: 'grid two' },
      h('div', null, h('h3', null, t('coach.by_comp')), table(Object.entries(rr.by_competition_type).map(([k, v]) => recRow(k, v)), null, { rowHeader: true })),
      h('div', null, h('h3', null, t('coach.by_venue')), table(Object.entries(rr.by_venue).map(([k, v]) => recRow(k, v)), null, { rowHeader: true }))),
    h('h3', null, t('coach.elo_exp')), note(rr.elo_expectation.note),
    kpis([['Expected score', num(rr.elo_expectation.expected_mean)], ['Actual score', num(rr.elo_expectation.actual_mean)], ['Per match', `+${num(rr.elo_expectation.actual_minus_expected_per_match)}`]]),
    details('Match list', table(rr.matches, null, { caption: 'Hassan-era matches', collapseOver: 0 }))));

  // ---- formations
  const phases = Object.keys(tn.formations.by_phase);
  const fnames = Object.keys(tn.formations.overall);
  root.append(section(t('coach.formations'), { n, src: ['fotmob'], intro: t('coach.formations_note') },
    chart('bar', {
      labels: phases.map((p) => `${p} (n=${tn.formations.by_phase[p].n})`),
      datasets: fnames.map((f, i) => ({ label: f, backgroundColor: PALETTE[i % PALETTE.length], data: phases.map((p) => { const x = tn.formations.by_phase[p].formations[f]; return x ? Math.round((x.count / tn.formations.by_phase[p].n) * 100) : 0; }) })),
    }, { stacked: true, height: 340, yTitle: '% of matches', max: 100, alt: `Formation share by phase of Hassan's tenure. 4-2-3-1 is the most used overall at ${pct(tn.formations.overall['4-2-3-1'].mean)}.` }),
    h('h3', null, t('coach.formation_share')),
    table(fnames.map((f) => ({ formation: f, count: tn.formations.overall[f].count, 'share [90% interval]': ci(tn.formations.overall[f]) })), null, { rowHeader: true }),
    table(Object.entries(tn.formations.overall_family).map(([k, v]) => ({ family: k, count: v.count, 'share [90% interval]': ci(v) })), null, { rowHeader: true }),
    details(t('coach.formation_band'), auto(Object.fromEntries(Object.entries(tn.formations.by_opponent_elo_band).map(([k, v]) => [k, { n: v.n, ...Object.fromEntries(Object.entries(v.formations).map(([f, x]) => [f, `${x.count} (${pct(x.mean)})`])) }])))),
    details(t('coach.formation_venue'), auto(Object.fromEntries(Object.entries(tn.formations.by_venue).map(([k, v]) => [k, { n: v.n, ...Object.fromEntries(Object.entries(v.formations).map(([f, x]) => [f, `${x.count} (${pct(x.mean)})`])) }])))),
    caveat(tn.formations.shot_stats_by_family.coverage)));

  // ---- continuity
  const xc = tn.xi_continuity;
  root.append(section(t('coach.continuity'), { n: xc.n_transitions, src: ['fotmob'], intro: t('coach.continuity_note') },
    kpis([['Mean changes', num(xc.mean_changes)], ['Median', xc.median_changes], ['Max', `${xc.max_changes.changes} (${xc.max_changes.opponent}, ${date(xc.max_changes.date)})`], ['If ≤4 days apart', num(xc.within_match_window_le_4_days_mean)]]),
    chart('line', { labels: xc.series.map((s) => s.date), datasets: [{ label: 'starters changed', data: xc.series.map((s) => s.changes), borderColor: '#CE1126', backgroundColor: '#CE1126', tension: 0.15, pointRadius: 3 }] },
      { alt: `Changes to the starting XI per match, mean ${xc.mean_changes}, max ${xc.max_changes.changes}`, yTitle: 'starters changed' }),
    table(Object.entries(xc.by_competition_type).map(([k, v]) => ({ type: k, n: v.n, 'mean changes': v.mean_changes })), null, { rowHeader: true }),
    note(xc.note)));

  // ---- squad core
  const sc = tn.loyalty; // most-started table lives under `loyalty`
  const loy = tn.squad_core; // club-share table lives under `squad_core` (key names are swapped in the export)
  root.append(section(t('coach.core'), { n: sc.matches, src: ['fotmob'], intro: t('coach.core_note') },
    kpis([['Players used as starter', sc.players_used_as_starter], ['With at least half the starts', sc.players_with_ge_half_starts]]),
    table(sc.most_started.map((p) => ({ player: p.player, starts: p.starts, 'start share [90%]': ci(p.start_share), 'nominal minutes': p.nominal_minutes })), null, { collapseOver: 12 }),
    h('h3', null, 'Where starters play (by current club)'),
    table(Object.entries(loy.current_club.shares).map(([k, v]) => ({ group: k, 'share of starter slots': ci(v) })), null, { rowHeader: true, caption: `Starter slots n = ${loy.current_club.starter_slots}` }),
    note(loy.note), note(sc.note)));

  // ---- goalkeeper
  const gk = tn.goalkeepers;
  root.append(section(t('coach.gk'), { n, src: ['fotmob'] },
    para(`Mohamed El Shenawy started ${gk.el_shenawy_starts_before_2026_01_17} of the first ${n - gk.matches_after_2026_01_17} matches and none of the ${gk.matches_after_2026_01_17} since 17 Jan 2026 (last start ${date(gk.el_shenawy_last_start)}); Mostafa Shobeir has started ${gk.shobeir_starts_after_2026_01_17}. In the latest matchday squad El Shenawy is: ${gk.el_shenawy_in_latest_matchday_squad}.`),
    table(gk.starts.map((s) => ({ goalkeeper: s.gk, starts: s.starts, 'share [90%]': ci(s.share) })), null, { rowHeader: true }),
    note(`${gk.gk_changes_between_consecutive_matches} goalkeeper changes in ${gk.transitions} transitions.`), note(gk.note)));

  // ---- subs
  const sb = tn.substitutions;
  const ord = Object.entries(sb.by_ordinal);
  root.append(section(t('coach.subs'), { n: sb.n_sub_events, src: ['fotmob'], intro: t('coach.subs_note') },
    kpis([['Matches', sb.n_matches], ['Substitutions', sb.n_sub_events], ['Per match', num(sb.subs_per_match.mean)]]),
    table(ord.map(([k, v]) => ({ substitution: k, n: v.minutes.n, 'median minute': v.minutes.median, 'IQR': `${v.minutes.q1}–${v.minutes.q3}`, 'p10–p90': `${num(v.minutes.p10, 0)}–${v.minutes.p90}` })), null, { rowHeader: true }),
    h('h3', null, 'By game state'),
    table(Object.entries(sb.by_game_state).map(([k, v]) => ({ state: k, n: v.n_subs, 'median minute': v.minutes.median, IQR: `${v.minutes.q1}–${v.minutes.q3}` })), null, { rowHeader: true }),
    h('h3', null, 'Type of change'),
    table(Object.entries(sb.type_overall).map(([k, v]) => ({ type: k, 'share [90%]': ci(v) })), null, { rowHeader: true }),
    note(sb.note), details('Substitution events', auto(sb.events.slice(0, 200), { collapseOver: 10 }))));

  // ---- Salah role
  const sl = tn.salah;
  root.append(section(t('coach.salah'), { n, src: ['fotmob'] },
    note(sl.note),
    table(Object.entries(sl.by_coach).map(([k, v]) => ({ coach: k, matches: v.egypt_matches_tagged, 'Salah in squad': v.salah_in_matchday_squad, starts: v.starts, 'start share [90%]': v.start_share_of_tagged_matches ? ci(v.start_share_of_tagged_matches) : '—' })), null, { rowHeader: true }),
    details('Positions: Hassan vs previous coaches', auto(sl.hassan_vs_previous_positions))));

  // ---- conceding first
  const cf = tn.after_conceding_first;
  root.append(section(t('coach.conceding'), { n: cf.of, src: ['fotmob'] },
    para(`Egypt conceded first in ${cf.matches_conceded_first} of ${cf.of} matches and did not lose ${ci(cf.not_lost)}; when scoring first (n = ${cf.comparison_when_scoring_first.P}) they did not lose ${ci(cf.comparison_when_scoring_first.not_lost)}.`),
    note(cf.note), details('Matches', auto(cf.matches))));

  // ---- tactical notes
  root.append(section(t('coach.tactical'), { n: d.tactical_notes.n, intro: t('coach.tactical_note') },
    h('ul', null, d.tactical_notes.notes.map((x) => h('li', null, h('strong', null, `${x.match_date} ${x.opponent}: `), x.note, ' ', x.source_url ? link(x.source_url, `[${t('common.source_short')}]`) : null)))));

  // ---- prediction
  const fb = pr.formation_backtest;
  const bars = [[t('coach.model'), fb.exact_hit_rate], [t('coach.baseline_prev'), fb.baseline_same_as_previous_match], [t('coach.baseline_freq'), fb.baseline_most_frequent_so_far]];
  const xb = pr.xi_backtest;
  const xm = Object.entries(xb.models);
  const players = pr.xi_next_match.players;
  const scen = pr.formation_next_match_scenarios;
  const sec = section(t('coach.predict'), { n: fb.n_test_matches, id: 'predict', src: ['fotmob', 'elo'], caveat: pr.parameters ? 'Each backtest match is predicted from earlier matches only; 26 test matches is a small sample.' : null },
    h('div', { class: 'honest' }, h('h3', null, t('coach.honesty_title')),
      h('p', null, t('coach.honesty', { base: pct(fb.baseline_same_as_previous_match.mean), model: pct(fb.exact_hit_rate.mean), n: fb.n_test_matches }))),
    h('h3', null, t('coach.bt_formation')),
    chart('bar', { labels: bars.map(([k]) => k), datasets: [{ label: 'exact hit rate', data: bars.map(([, v]) => Math.round(v.mean * 100)), backgroundColor: ['#CE1126', '#111111', '#C8A24A'] }] },
      { yTitle: '% of matches predicted exactly', max: 100, alt: `Exact formation hit rate: model ${pct(fb.exact_hit_rate.mean)}, same-as-previous ${pct(fb.baseline_same_as_previous_match.mean)}, most frequent ${pct(fb.baseline_most_frequent_so_far.mean)}` }),
    table([...bars.map(([k, v]) => ({ method: k, hits: `${v.k}/${v.n}`, 'rate [90% interval]': ci(v) })), { method: t('coach.bt_family'), hits: `${fb.family_hit_rate.k}/${fb.family_hit_rate.n}`, 'rate [90% interval]': ci(fb.family_hit_rate) }], null, { rowHeader: true }),
    note(`Mean probability the model gave to the formation actually used: ${pct(fb.mean_probability_assigned_to_actual_formation)}.`), note(fb.note),
    details(t('coach.bt_rows'), table(fb.rows, ['date', 'opponent', 'band', 'venue', 'predicted', 'predicted_prob', 'actual', 'hit', 'family_hit', 'n_train_cell'], { caption: t('coach.bt_rows') })),
    h('h3', null, t('coach.scenarios')),
    table(Object.entries(scen.scenarios).map(([k, v]) => ({ scenario: k, 'matches in cell': v.n_matches_in_cell, 'top formations (90% interval)': v.top.slice(0, 3).map((x) => `${x.formation} ${pct(x.prob)} [${pct(x.ci90[0])}–${pct(x.ci90[1])}]`).join('; ') })), null, { rowHeader: true }),
    note(scen.note),
    h('h3', null, t('coach.xi_pred')),
    note(pr.xi_next_match.note),
    table(players.slice(0, 20).map((p) => ({ player: p.player, 'P(start)': pct(p.p_start), 'interval (80%)': `${pct(p.p_start_ci80[0])}–${pct(p.p_start_ci80[1])}`, 'without club adjustment': pct(p.p_start_without_club_adjustment), 'starts last 5': p.starts_last_5_hassan_matches, 'starts total': p.starts_total })), null, { collapseOver: 14 }),
    h('h3', null, t('coach.xi_bt')), note(t('coach.xi_bt_note')),
    chart('bar', { labels: xm.map(([k]) => k), datasets: [{ label: 'mean correct of 11', data: xm.map(([, v]) => v.mean_correct_of_11), backgroundColor: xm.map(([k]) => (k.startsWith('baseline') ? '#111111' : '#CE1126')) }] },
      { horizontal: true, height: 300, max: 11, alt: `Mean starters correct of 11: ${xm.map(([k, v]) => `${k} ${v.mean_correct_of_11}`).join('; ')}` }),
    table(xm.map(([k, v]) => ({ model: k, 'mean correct': v.mean_correct_of_11, min: v.min, max: v.max, n: v.n_test_matches, 'goalkeeper hit': v.gk_hit_rate ? ci(v.gk_hit_rate) : '—' })), null, { rowHeader: true }),
    note(xb.note),
    details(t('coach.calibration'), table(xb.calibration_primary_model.map((c) => ({ bin: c.bin, 'player-matches': c.n_player_matches, 'mean predicted': c.mean_predicted, 'observed start rate': ci(c.observed_start_rate) })), null, { rowHeader: true })),
    details('Model parameters', dl(pr.parameters)));
  root.append(sec);

  // ---- reconciliation
  const rc = d.reconciliation;
  root.append(section(t('coach.reconcile'), { src: ['wikipedia'], urls: [rc.article.source_url] },
    para(rc.correction_to_brief), para(rc.unexplained),
    table(rc.our_record_by_convention.map((r) => ({ scope: r.scope, convention: r.convention, P: r.P, W: r.W, D: r.D, L: r.L, 'gap vs article': r.abs_gap })), null, { collapseOver: 6 }),
    details('Explained differences', ul(rc.explained)),
    details('Checks made', ul(rc.checks_made)),
    note(rc.article_table_internal_consistency.note)));

  // ---- method
  root.append(section(t('coach.method'), { src: [], urls: [] },
    h('h3', null, 'Sources'), table(d.methodology.sources, ['name', 'url', 'use']),
    h('h3', null, 'Coverage'), auto(d.methodology.coverage),
    h('h3', null, 'Gaps'), ul(d.methodology.gaps),
    h('h3', null, 'Model assumptions'), ul(d.methodology.model_assumptions)));
  return root;
}
