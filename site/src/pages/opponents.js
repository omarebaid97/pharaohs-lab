import { h, link } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dl, kpis, note, caveat, para, ul, details, auto, badge, oddsBar, cell } from '../components.js';
import { chart } from '../charts.js';
import { date, num, pct, wdl, ci } from '../fmt.js';

function fixtureBlock(o) {
  const items = (o.fixtures && o.fixtures.items) || [];
  return items.map((f) => h('div', null,
    h('h4', null, `${f.home} v ${f.away} `, badge(f.status === 'upcoming' ? t('opp.upcoming') : t('opp.played'), f.status === 'upcoming' ? 'red' : '')),
    h('p', null, `${date(f.date)} · ${f.competition}${f.round ? ` (round ${f.round})` : ''} · ${f.venue || '?'}${f.city ? `, ${f.city}` : ''}${f.score ? ` · ${f.score}` : ''}`),
    f.elo ? [oddsBar(f.elo.wdl), h('p', { class: 'note' }, `Win ${pct(f.elo.wdl.win, 1)} · draw ${pct(f.elo.wdl.draw, 1)} · loss ${pct(f.elo.wdl.loss, 1)}. Elo ${f.elo.egypt} (#${f.elo.egypt_rank}) vs ${f.elo.opponent} (#${f.elo.opponent_rank}); home advantage applied ${f.elo.home_advantage_applied}. Elo as of ${date(f.elo.as_of)}.`)] : null,
    h('p', { class: 'meta' }, t('common.source'), ': ', ...(f.source_urls || []).flatMap((u, i) => (i ? [', ', link(u, `source ${i + 1}`)] : [link(u, 'source 1')])))));
}

function oppCard(o, d) {
  const hh = o.head_to_head; const rf = o.recent_form; const tx = o.typical_xi_and_key_players; const st = o.style_indicators; const fm = st.fotmob; const sb = st.afcon_2023_statsbomb;
  const per = fm.goals_by_period ? Object.entries(fm.goals_by_period) : [];
  const card = h('article', { class: 'card', id: o.opponent.toLowerCase().replace(/\W+/g, '-') },
    h('h2', { style: 'margin-top:0' }, o.opponent), h('p', { class: 'note' }, o.competition_context),
    h('h3', null, t('opp.fixture')), fixtureBlock(o),
    h('h3', null, t('opp.h2h')),
    kpis([[t('opp.since2018'), wdl(hh.since_2018_db), `n = ${hh.since_2018_db.P}`], [t('opp.all_time'), wdl(hh.all_time), `n = ${hh.all_time.P}`]]),
    hh.since_2018_db.matches.length ? table(hh.since_2018_db.matches, ['date', 'competition', 'venue', 'score', 'result', 'source_url'], { caption: t('opp.since2018') }) : null,
    note(hh.all_time.note),
    h('h3', null, t('opp.form')),
    h('p', null, `${t('opp.last10')}: ${wdl(rf.record)}, goals ${rf.goals_for}-${rf.goals_against} (n = ${rf.n}, ${date(rf.span.first)} to ${date(rf.span.last)}). ${t('opp.coach')}: ${rf.current_coach.name}.`),
    details(t('opp.last10'), table(rf.matches, ['date', 'competition', 'venue_side', 'opponent', 'score', 'result', 'formation', 'coach', 'source_url'], { caption: t('opp.last10') })),
    h('h3', null, t('opp.xi')),
    note(`${t('opp.matches_lineup')}: ${tx.matches_with_lineup} of ${tx.matches_in_window} (${tx.window}). ${tx.typical_xi_note}`),
    table(tx.typical_xi, ['player', 'club', 'position', 'starts', 'minutes']),
    h('h3', null, t('opp.style')),
    h('p', null, `${t('opp.formations')} (n = ${fm.matches_used}, ${fm.window}): ${Object.entries(fm.formation_counts).map(([k, v]) => `${k} ×${v}`).join(', ')}.`),
    per.length ? chart('bar', { labels: per.map(([k]) => k), datasets: [{ label: 'scored', data: per.map(([, v]) => v.scored), backgroundColor: '#CE1126' }, { label: 'conceded', data: per.map(([, v]) => v.conceded), backgroundColor: '#111111' }] },
      { height: 220, yTitle: 'goals', alt: `${o.opponent} goals scored and conceded by match period, n = ${fm.matches_used} matches` }) : null,
    caveat(fm.set_piece_goals.coverage + (fm.set_piece_goals.note ? `. ${fm.set_piece_goals.note}` : '')),
    h('h3', null, t('opp.angle')),
    para(`Egypt vs the ${o.egypt_angle.elo_band} Elo band under Hossam Hassan: ${wdl(o.egypt_angle.band_record)}, ${num(o.egypt_angle.band_record.points_per_game)} points per game (n = ${o.egypt_angle.band_record.P}).`),
    note(o.egypt_angle.formation_persistence_baseline ? `Formation: model backtest ${pct(o.egypt_angle.backtest.model_exact_hit_rate)} vs persistence baseline ${pct(o.egypt_angle.backtest.persistence_exact_hit_rate)} (n = ${o.egypt_angle.backtest.n_test_matches}); the baseline wins.` : ''),
    details('Same-band matches', table(o.egypt_angle.same_band_matches, null, { caption: 'Same-band matches' })),
    h('h3', null, t('opp.sb')),
    caveat(t('opp.sb_outdated'), 'warn'));
  if (sb.available === false) {
    card.append(para(`${t('opp.sb_na')}: ${sb.reason}`));
  } else {
    card.append(
      kpis([['Matches', sb.n_matches], ['PPDA', num(sb.ppda.value, 1)], ['Possession', pct(sb.possession_share.value)], ['Shots / match', num(sb.shots.per_match, 1)], ['Set-piece shot share', pct(sb.shots.set_piece_shot_share)]]),
      note(`${sb.label}. Shots n = ${sb.shots.n}. PPDA: ${sb.ppda.note}`),
      dl({ 'forward progress ratio': sb.directness.forward_progress_ratio, 'long pass share': sb.directness.long_pass_share, 'mean shot distance (m)': sb.shots.mean_distance, 'mean xG per shot': sb.shots.mean_xg_per_shot }),
      details('Most involved passers', table(sb.pass_network_top_players, null)), note(sb.pass_network_note),
      h('p', { class: 'attrib' }, t('site.statsbomb'), ' · ', link('https://github.com/statsbomb/open-data', 'StatsBomb open data')),
      h('p', { class: 'meta' }, t('common.source'), ': ', ...sb.source_urls.slice(0, 3).flatMap((u, i) => (i ? [', ', link(u, `source ${i + 1}`)] : [link(u, 'source 1')]))));
  }
  return card;
}

export async function render() {
  const d = await load('opponent_dossiers');
  const root = h('div', null, h('h1', null, t('opp.title')), h('p', { class: 'lead' }, t('opp.lead')));
  const opps = [...d.opponents].sort((a, b) => (a.next_fixture_date || '9').localeCompare(b.next_fixture_date || '9'));
  const g = d.qualification_group;
  root.append(section(t('opp.group'), { src: ['wikipedia', 'fotmob'], urls: g.source_urls.slice(0, 1) }, h('p', { class: 'note' }, g.name),
    table(g.standings.table.map((r) => ({ team: r.team, P: r.P, W: r.W, D: r.D, L: r.L, 'GF-GA': r.GF_GA, pts: r.pts })), null, { rowHeader: true, caption: g.standings.name })));
  opps.forEach((o) => root.append(oppCard(o, d)));
  root.append(section(t('meth.stats'), {}, note(d.attribution), ul(d.methodology_notes),
    details('Elo method', auto(opps[0].fixtures.elo_method)),
    h('p', { class: 'attrib' }, t('site.statsbomb'))));
  return root;
}
