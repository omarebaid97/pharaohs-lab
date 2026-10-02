import { h } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dl, kpis, note, caveat, para, ul, details, auto, badge } from '../components.js';
import { chart } from '../charts.js';
import { date, num, pct } from '../fmt.js';

const FLAGS = [['high_load', 'h_load'], ['short_rest', 's_rest'], ['long_haul', 'l_haul'], ['low_rhythm', 'l_rhythm']];

export async function render() {
  const d = await load('load_tracker');
  const cw = d.current_window; const bt = d.backtest;
  const board = [...cw.board].sort((a, b) => b.score - a.score || b.min14 - a.min14);
  const root = h('div', null, h('h1', null, t('load.title')), h('p', { class: 'lead' }, t('load.lead')));

  root.append(section(t('load.window'), { n: board.length, src: ['fotmob', 'wikipedia'], caveat: `${t('load.squad_source')}: ${cw.squad_source}` },
    h('p', null, `${cw.window_id}, load measured before ${date(cw.load_measured_before)}.`),
    table(cw.matches.map((m) => ({ date: m.date, venue: m.venue, city: m.city, country: m.country, status: m.status })), null, { caption: t('load.matches') })));

  const flagBadges = (p) => h('span', null, FLAGS.filter(([k]) => p[k]).map(([k, lk]) => badge(t(`load.${lk}`), 'red')), FLAGS.every(([k]) => !p[k]) ? t('load.none') : null);
  const cols = [
    { key: 'name', label: t('load.player') }, { key: 'club', label: t('load.club') },
    { key: 'score', label: t('load.score') },
    { key: 'flags', label: t('load.flags'), fmt: flagBadges },
    { key: 'min14', label: t('load.min14') }, { key: 'min30', label: t('load.min30') }, { key: 'matches14', label: 'Matches 14d' },
    { key: 'rest_days', label: t('load.rest') }, { key: 'route_km', label: t('load.route') }, { key: 'coverage', label: 'Coverage' },
  ];
  root.append(section(t('load.board'), { n: board.length, src: ['fotmob'], intro: t('load.board_note') },
    chart('bar', { labels: board.map((p) => p.name), datasets: [{ label: t('load.score'), data: board.map((p) => p.score), backgroundColor: board.map((p) => (p.score >= 3.5 ? '#CE1126' : p.score > 0 ? '#C8A24A' : '#9ca3af')) }] },
      { height: Math.max(260, board.length * 22), horizontal: true, max: 7, alt: `Combined load score for ${board.length} squad players; highest is ${board[0].name} at ${board[0].score}.` }),
    table(board, cols, { rowHeader: true, caption: `${t('load.board')} (n = ${board.length})` }),
    details(t('load.thresholds'), ul(Object.values(d.thresholds))),
    details(t('load.score_def'), para(d.score.definition), dl(d.score.points))));

  const legs = board.filter((p) => p.route_legs && p.route_legs.length);
  root.append(section(t('load.travel'), { n: legs.length, src: ['wikidata'], intro: t('load.travel_note') },
    chart('bar', { labels: board.map((p) => p.name), datasets: [{ label: 'route km', data: board.map((p) => p.route_km), backgroundColor: '#111111' }] },
      { height: Math.max(260, board.length * 22), horizontal: true, alt: 'Total route length in km per player across the window' }),
    details('Route legs per player', table(legs.flatMap((p) => p.route_legs.map((l) => ({ player: p.name, from: l.frm, to: l.to, km: l.km, 'time-zone shift (h)': l.tz_shift_h, date: l.date }))), null, { caption: 'Route legs' }))));

  const scopes = [['all_players', t('load.scope_all')], ['outfield_only', t('load.scope_outfield')]];
  const flagRows = (scope) => FLAGS.flatMap(([k, lk]) => {
    const b = bt.by_flag[scope][k];
    return [['flagged', b.flagged], ['not_flagged', b.not_flagged]].map(([g, v]) => ({ flag: t(`load.${lk}`), group: t(`load.${g}`), 'n (player-windows)': v.n, windows: v.n_windows, [t('load.mean_share')]: v.mean_minutes_share == null ? '—' : pct(v.mean_minutes_share), [t('load.played_zero')]: v.share_played_zero == null ? '—' : pct(v.share_played_zero) }));
  });
  root.append(section(t('load.backtest'), { n: bt.n_rows, src: ['fotmob'], intro: t('load.backtest_note'), caveat: `${bt.n_windows} windows, ${bt.n_players} players. Flags with n = 0 could not be evaluated.` },
    para(bt.definition),
    ...scopes.flatMap(([s, l]) => [h('h3', null, l), chart('bar', { labels: FLAGS.map(([, lk]) => t(`load.${lk}`)), datasets: [{ label: t('load.flagged'), data: FLAGS.map(([k]) => Math.round((bt.by_flag[s][k].flagged.mean_minutes_share || 0) * 100)), backgroundColor: '#CE1126' }, { label: t('load.not_flagged'), data: FLAGS.map(([k]) => Math.round((bt.by_flag[s][k].not_flagged.mean_minutes_share || 0) * 100)), backgroundColor: '#9ca3af' }] },
      { height: 240, max: 100, yTitle: '% of window minutes played', alt: `${l}: mean share of window minutes played, flagged vs not flagged, by flag` }), table(flagRows(s), null, { collapseOver: 0 })]),
    h('h3', null, t('load.caveats')), ul(bt.caveats),
    details('By previous-14-day minutes band (outfield)', auto(bt.by_min14_band_outfield)), details('By score band (outfield)', auto(bt.by_score_band_outfield)), details('Per window n', auto(bt.per_window_n)), details('Exclusions', auto(bt.exclusions))));

  root.append(section(t('load.windows'), { n: d.windows.length, src: ['fotmob'] },
    table(d.windows.map((w) => ({ window: w.window_id, start: w.start, end: w.end, kind: w.kind, status: w.status, squad: w.squad_size, 'matches played': w.n_matches_played, 'matches with minutes': w.n_matches_with_minutes, 'club-load coverage': w.club_load_coverage })), null, { collapseOver: 8 }),
    ul(d.method_notes), details('Coverage', auto(d.coverage)), d.gaps.length ? ul(d.gaps) : null));
  return root;
}
