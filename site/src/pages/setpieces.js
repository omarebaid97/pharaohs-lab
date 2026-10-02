import { h, link } from '../dom.js';
import { t } from '../i18n.js';
import { load } from '../data.js';
import { section, table, dl, kpis, note, caveat, para, ul, details, auto, cell } from '../components.js';
import { chart, PALETTE } from '../charts.js';
import { pct, num, ci } from '../fmt.js';

const situ = (obj) => Object.entries(obj).map(([k, v]) => ({ situation: k.replace(/_/g, ' '), goals: v }));

export async function render() {
  const d = await load('set_pieces');
  const g = d.goals_by_situation; const sh = d.set_piece_shots_xg; const pe = d.penalties; const tt = d.takers_targets_fotmob; const sb = d.statsbomb_afcon2023;
  const root = h('div', null, h('h1', null, t('sp.title')), h('p', { class: 'lead' }, t('sp.lead')), caveat(t('sp.coverage_global'), 'warn'));

  root.append(section(t('sp.findings'), { n: d.meta.matches_with_shotmap, src: ['fotmob'], caveat: g.note },
    ...d.key_findings.map((f) => h('div', { class: 'card' }, h('h3', null, f.topic), h('p', null, f.text), h('p', { class: 'meta' }, `${t('common.n')} = ${f.n}; ${f.sample}`)))));

  // goals by situation
  const cats = g.categories;
  const mk = (blk) => ({ labels: ['Egypt scored', 'Egypt conceded'], datasets: cats.map((c, i) => ({ label: c.replace(/_/g, ' '), backgroundColor: c === 'unknown' ? '#d1d5db' : PALETTE[i % PALETTE.length], data: [blk.for.by_situation[c], blk.against.by_situation[c]] })) });
  const block = (title, blk) => h('div', null, h('h3', null, title),
    caveat(`Situation known for ${blk.for.situation_known} of ${blk.for.goals} goals scored (${pct(blk.for.coverage)}) and ${blk.against.situation_known} of ${blk.against.goals} conceded (${pct(blk.against.coverage)}); "unknown" is a large share. Matches: ${blk.matches}, with shot map: ${blk.matches_with_shotmap}.`),
    chart('bar', mk(blk), { stacked: true, height: 280, yTitle: 'goals', alt: `${title}: goals by situation. Scored ${blk.for.goals}, of which ${blk.for.situation_known} have a known situation.` }),
    table([...cats.map((c) => ({ situation: c.replace(/_/g, ' '), scored: blk.for.by_situation[c], conceded: blk.against.by_situation[c] })), { situation: 'total', scored: blk.for.goals, conceded: blk.against.goals }], null, { rowHeader: true }),
    kpis([['Set-piece goals scored, lower bound (excl. pens)', blk.for.set_piece_goals_lower_bound_excl_pens], ['Conceded, lower bound', blk.against.set_piece_goals_lower_bound_excl_pens]]),
    blk.for.shotmap_matches_subset && blk.for.shotmap_matches_subset.set_piece_share_excl_pens
      ? para(`Where a shot map exists (n = ${blk.for.shotmap_matches_subset.set_piece_share_excl_pens.n} non-penalty goals scored), set pieces gave ${ci({ mean: blk.for.shotmap_matches_subset.set_piece_share_excl_pens.p, ...blk.for.shotmap_matches_subset.set_piece_share_excl_pens })} of goals; conceded: ${blk.against.shotmap_matches_subset.set_piece_share_excl_pens.k}/${blk.against.shotmap_matches_subset.set_piece_share_excl_pens.n}.`) : null);
  root.append(section(t('sp.goals'), { n: g.overall.matches, src: ['fotmob'] },
    block(`${t('sp.all_era')} (${g.overall.matches} matches)`, g.overall), block(`${t('sp.hassan')} (${g.hassan_era.matches} matches)`, g.hassan_era),
    details('By year', auto(g.by_year)), details('By coach', auto(g.by_coach)), details('Pre-Hassan shot-map sample', auto(g.pre_hassan_shotmap_sample))));

  // shots
  const sitRows = (blk) => Object.entries(blk.by_situation).map(([k, v]) => ({ situation: k.replace(/_/g, ' '), shots: v.shots, 'per match': v.per_match, goals: v.goals, 'conversion [90%]': v.conversion && v.conversion.p != null ? ci(v.conversion) : '—', xG: v.xg, 'xG/shot': v.xg_per_shot }));
  root.append(section(t('sp.shots'), { n: sh.hassan_era.matches, src: ['fotmob'], caveat: `${sh.note} Shot maps exist for only ${sh.hassan_era.matches} Hassan-era matches.` },
    h('h3', null, `${t('sp.goals_for')}`), table(sitRows(sh.hassan_era.for), null, { rowHeader: true, caption: `Hassan era, shot-map matches only (n = ${sh.hassan_era.matches})` }),
    h('h3', null, `${t('sp.goals_against')}`), table(sitRows(sh.hassan_era.against), null, { rowHeader: true }),
    details('All shot-map matches', auto(sh.all_shotmap_matches)), details('AFCON 2023 (FotMob)', auto(sh.pre_hassan_afcon2023_fotmob)), details(t('sp.coverage'), auto(sh.coverage))));

  // penalties
  const so = pe.shootouts;
  root.append(section(t('sp.pens'), { n: so.matches_flagged_decided_by_pens, src: ['fotmob'], caveat: `${pe.note} Kick-by-kick data exists for ${so.matches_with_kick_data} of ${so.matches_flagged_decided_by_pens} shootouts.` },
    kpis([['Shootout record', `${so.record.won}W-${so.record.lost}L`, `n = ${so.matches_flagged_decided_by_pens}`], ['Egypt kicks scored', ci(so.egypt_kicks)], ['Opponent kicks scored', ci(so.opponent_kicks)]]),
    h('h3', null, t('sp.shootouts')), table(so.matches.map((m) => ({ date: m.date, opponent: m.opponent, competition: m.competition, shootout: m.shootout, result: m.result, 'Egypt kicks': m.egypt_kicks, 'opponent kicks': m.opponent_kicks })), null, { collapseOver: 10 }),
    h('h3', null, 'Egypt shootout takers'), table(so.egypt_takers, null, { collapseOver: 10 }), note(so.note),
    h('h3', null, 'In-play penalties'),
    table([{ type: 'Won by Egypt', ...pe.in_play_won_by_egypt }, { type: 'Conceded', ...pe.in_play_conceded }], null, { rowHeader: true }),
    details('In-play takers', auto(pe.egypt_takers_in_play)), details('By year', auto(pe.by_year))));

  // takers
  root.append(section(t('sp.takers'), { src: ['fotmob'], caveat: `${tt.note} ${tt.coverage ? compactCov(tt.coverage) : ''}` },
    h('h3', null, 'Set-piece goal scorers'), table(tt.egypt_set_piece_scorers, null, { collapseOver: 10 }),
    h('h3', null, 'Set-piece goal assisters'), table(tt.egypt_set_piece_goal_assisters, null, { collapseOver: 10 }),
    note(`${tt.egypt_set_piece_goals_missing_assist} set-piece goals have no assist recorded.`),
    details('Set-piece goals scored', auto(tt.egypt_set_piece_goals)), details('Top set-piece shooters (shot maps)', auto(tt.top_set_piece_shooters_shotmap)), details('Conceded from set pieces', auto(tt.goals_conceded_from_set_pieces))));

  // StatsBomb corners
  const ca = sb.corners_egypt_attacking; const cd = sb.corners_egypt_defending; const cf = sb.corners_field_benchmark;
  const cornerRows = (c) => [['Corners', c.corners], ['Short', c.short], ['Delivered', c.delivered], ['Completed', c.completed ? ci(c.completed) : '—'], ['Shots after corner', c.shots_after_corner], ['Shots per corner', c.shots_per_corner], ['Goals', c.goals], ['xG', c.xg], ['xG per corner', c.xg_per_corner]].map(([k, v]) => ({ metric: k, value: v }));
  root.append(section(t('sp.sb'), { n: sb.egypt_matches, src: ['statsbomb'], caveat: `${t('sp.sb_outdated')} ${sb.note}` },
    h('p', { class: 'attrib' }, t('site.statsbomb'), ' · ', sb.attribution),
    h('div', { class: 'grid two' }, h('div', null, h('h3', null, t('sp.corners_att')), table(cornerRows(ca), null, { rowHeader: true, caption: `n = ${ca.corners} corners` })),
      h('div', null, h('h3', null, t('sp.corners_def')), table(cornerRows(cd), null, { rowHeader: true, caption: `n = ${cd.corners} corners` }))),
    h('h3', null, t('sp.corners_field')), table(cornerRows(cf), null, { rowHeader: true, caption: `All teams, n = ${cf.corners} corners` }),
    details('Delivery technique and end zones (Egypt attacking)', auto({ techniques: ca.techniques, end_zones: ca.end_zones, first_contact: ca.first_contact })),
    h('h3', null, 'Corner takers'), table(sb.corner_takers.map((p) => ({ player: p.player, corners: p.corners, delivery: p.delivery })), null, { collapseOver: 8 }),
    details('First recipients', auto(sb.corner_targets_first_recipient)), details('Free-kick takers', auto(sb.free_kick_takers)), details('Set-piece shooters', auto(sb.set_piece_shooters_egypt)),
    details('Goals and penalties', auto(sb.egypt_set_piece_goals_and_penalties)), details('Conceded from set pieces', auto(sb.goals_conceded_from_set_pieces)),
    details(t('sp.cross'), auto(d.cross_check_fotmob_vs_statsbomb)), details(t('sp.coverage'), auto(sb.coverage)),
    h('p', { class: 'attrib' }, t('site.statsbomb'))));

  root.append(section(t('sp.caveats'), {}, ul(d.methodology.coverage_caveats), note(d.methodology.attribution)));
  return root;
}
const flat = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v == null || typeof v !== 'object'));
const compactCov = (c) => Object.entries(c).map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('; ');
