"""Build data/validation_report.md from the DB. Honest about gaps."""
from __future__ import annotations

import json
import random
from pathlib import Path

from . import db

REPORT = db.ROOT / "data" / "validation_report.md"

# Facts learned by hand-probing during the Phase 1 build (each request made once via etl.http rules).
PROBE_NOTES = [
    ("Sofascore (api.sofascore.com / www.sofascore.com/api)", "blocked", "HTTP 403 on first request. Not retried, no evasion. FotMob is the substitute; no Sofascore module built."),
    ("FotMob /api/* JSON endpoints", "unusable", "404 without signed request headers. We do not replicate the signature. Public HTML pages (server-rendered __NEXT_DATA__) are used instead."),
    ("pub.fotmob.com fixture-by-date", "unusable", "HTTP 400; older Egypt fixtures are discovered via each match page's 'teamForm' instead."),
    ("Transfermarkt", "not used", "Homepage reachable, but FotMob player pages already provide match-level club minutes, so no scraper was built (less load on a site that is hostile to scrapers)."),
    ("FBref", "not used", "Not needed for Phase 1 (no module built)."),
]


def _tbl(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else str(v).replace("|", "/") for v in r) + " |")
    return "\n".join(out)


def _match_block(con, m) -> str:
    t = lambda i: con.execute("SELECT name FROM teams WHERE id=?", (i,)).fetchone()[0]
    h, a = t(m["home_team_id"]), t(m["away_team_id"])
    coach = con.execute("SELECT name FROM coaches WHERE id=?", (m["egypt_coach_id"],)).fetchone()
    lines = [f"#### {m['date']}  {h} {m['home_score']}-{m['away_score']} {a}",
             f"- competition: {m['competition']} / {m['stage']} ; venue: {m['venue']} ({m['venue_city']}, {m['venue_country']}) ; neutral={m['neutral']} ; decided_by={m['decided_by']}",
             f"- Egypt coach: {coach[0] if coach else None} ; formations: {h} {m['formation_home']} vs {a} {m['formation_away']} ; opponent Elo (pre-match): {m['opponent_elo']}",
             f"- source: {m['source_url']} (fetched {m['fetched_at']})"]
    for side, nm in (("home", h), ("away", a)):
        rows = con.execute("SELECT p.name, l.started, l.shirt, l.minutes_played, l.sub_on_minute, l.sub_off_minute, l.captain FROM lineups l JOIN players p ON p.id=l.player_id "
                           "WHERE l.match_id=? AND l.team_side=? ORDER BY l.started DESC, l.sub_on_minute IS NULL, l.sub_on_minute, l.shirt+0", (m["id"], side)).fetchall()
        xi = [f"{r['name']} ({'?' if r['minutes_played'] is None else int(r['minutes_played'])}')" + (" (C)" if r["captain"] else "") for r in rows if r["started"]]
        subs = [f"{r['name']} on {r['sub_on_minute']}' ({'?' if r['minutes_played'] is None else int(r['minutes_played'])}')" for r in rows if not r["started"] and r["sub_on_minute"] is not None]
        unused = [r["name"] for r in rows if not r["started"] and r["sub_on_minute"] is None]
        lines.append(f"- **{nm} XI**: " + "; ".join(xi))
        lines.append(f"  - subs used: " + ("; ".join(subs) or "none"))
        lines.append(f"  - unused bench: " + (", ".join(unused) or "none"))
    ev = con.execute("SELECT e.minute, e.type, p.name, e.situation, e.xg FROM events_lite e LEFT JOIN players p ON p.id=e.player_id WHERE e.match_id=? "
                     "AND e.type IN ('goal','own_goal','pen_goal','yellow','red') ORDER BY e.minute", (m["id"],)).fetchall()
    lines.append("- goals/cards: " + ("; ".join(f"{r['minute']}' {r['type']} {r['name']}" + (f" [{r['situation']}, xG {r['xg']:.2f}]" if r['xg'] is not None else "") for r in ev) or "none"))
    sh = con.execute("SELECT COUNT(*), ROUND(SUM(xg),2) FROM events_lite WHERE match_id=? AND type='shot'", (m["id"],)).fetchone()
    lines.append(f"- shots recorded: {sh[0]} (total xG {sh[1]})")
    return "\n".join(lines)


def write_report(con) -> Path:
    L: list[str] = []
    add = L.append
    add("# Pharaohs Lab - Phase 1 validation report")
    add(f"\nGenerated {db.now()} from `data/pharaohs.db`. Sources: FotMob public pages, Wikipedia (MediaWiki API), eloratings.net, StatsBomb open data, Wikidata.\n")

    # ---- Hassan era
    rec = con.execute("SELECT value FROM meta WHERE key='wikipedia_hassan_record'").fetchone()
    rec = json.loads(rec[0]) if rec else {}
    start = rec.get("from") or "2024-02-06"
    add("## 1. Hassan-era match count vs Wikipedia\n")
    fm = con.execute("SELECT id, date, home_score, away_score, home_team_id, away_team_id FROM matches WHERE is_egypt=1 AND source='fotmob' AND date>=?", (start,)).fetchall()
    wk = con.execute("SELECT * FROM wiki_fixtures WHERE date>=? ORDER BY date", (start,)).fetchall()
    tagged = con.execute("SELECT COUNT(*) FROM matches m JOIN coaches c ON c.id=m.egypt_coach_id WHERE m.is_egypt=1 AND m.source='fotmob' AND c.name LIKE 'Hossam Hassan%'").fetchone()[0]
    add(_tbl(["measure", "count"], [
        (f"FotMob-sourced Egypt matches on/after {start} (Hassan tenure start per Wikipedia)", len(fm)),
        ("FotMob matches tagged with coach Hossam Hassan", tagged),
        (f"Wikipedia 'Egypt national football team results' fixtures on/after {start}", len(wk)),
        ("Wikipedia 'Hossam Hassan' managerial record, Egypt row, P (as of page)", rec.get("played")),
    ]))
    add(f"\nWikipedia managerial-record row: `{rec.get('row')}` ({rec.get('source_url')})\n")
    from datetime import date as _d, timedelta as _td

    def near(d, pool):
        d0 = _d.fromisoformat(d)
        for o in (0, -1, 1):
            k = (d0 + _td(days=o)).isoformat()
            if k in pool:
                return k
        return None

    fm_by_date = {r["date"]: r for r in fm}
    wk_by_date = {r["date"]: r for r in wk}
    mism = []
    used = set()
    for d, w in wk_by_date.items():
        k = near(d, fm_by_date)
        if not k:
            mism.append((d, "in Wikipedia results, missing from FotMob ingest", f"{w['home']} {w['score_raw']} {w['away']}"))
            continue
        used.add(k)
        f = fm_by_date[k]
        if (f["home_score"], f["away_score"]) != (w["home_score"], w["away_score"]) and (f["home_score"], f["away_score"]) != (w["away_score"], w["home_score"]):
            mism.append((d, "score differs", f"FotMob {f['home_score']}-{f['away_score']} vs Wikipedia {w['score_raw']} ({w['home']} v {w['away']})"))
    for d, f in fm_by_date.items():
        if d not in used:
            th = con.execute("SELECT name FROM teams WHERE id=?", (f["home_team_id"],)).fetchone()[0]
            ta = con.execute("SELECT name FROM teams WHERE id=?", (f["away_team_id"],)).fetchone()[0]
            mism.append((d, "in FotMob, not in Wikipedia results list (Wikipedia page lag?)", f"{th} {f['home_score']}-{f['away_score']} {ta}"))
    add("Dates are compared with a +-1 day tolerance (FotMob stores UTC kickoff dates, Wikipedia local dates).\n")
    by_coach = con.execute("SELECT COALESCE(c.name,'(none)') n, COUNT(*) FROM matches m LEFT JOIN coaches c ON c.id=m.egypt_coach_id WHERE m.is_egypt=1 AND m.source='fotmob' AND m.date>=? GROUP BY n", (start,)).fetchall()
    add("FotMob matches since Hassan's start, by coach tag: " + ", ".join(f"{r[0]}: {r[1]}" for r in by_coach) + "\n")
    add("### Mismatches (Hassan era)\n")
    add(_tbl(["date", "issue", "detail"], sorted(mism)) if mism else "None.")

    # ---- whole window cross-check
    add("\n### Cross-check, whole window 2018-01-01 to today\n")
    allf = con.execute("SELECT date, home_score, away_score FROM matches WHERE is_egypt=1 AND source='fotmob'").fetchall()
    allw = con.execute("SELECT date, home_score, away_score, home, away, score_raw FROM wiki_fixtures").fetchall()
    fd = {r["date"]: r for r in allf}
    wd = {r["date"]: r for r in allw}
    only_w, bad, used2 = [], [], set()
    for d in sorted(wd):
        k = near(d, fd)
        if not k:
            only_w.append(d)
            continue
        used2.add(k)
        f, w = fd[k], wd[d]
        if (f["home_score"], f["away_score"]) not in ((w["home_score"], w["away_score"]), (w["away_score"], w["home_score"])):
            bad.append(d)
    only_f = sorted(d for d in fd if d not in used2)
    add(f"FotMob Egypt matches: {len(fd)} ; Wikipedia fixtures: {len(wd)} ; only in Wikipedia: {len(only_w)} ; only in FotMob: {len(only_f)} ; score conflicts: {len(bad)}")
    if only_w:
        add("\nOnly in Wikipedia: " + ", ".join(f"{d} ({wd[d]['home']} {wd[d]['score_raw']} {wd[d]['away']})" for d in only_w))
    if only_f:
        add("\nOnly in FotMob: " + ", ".join(only_f))
    if bad:
        add("\nScore conflicts: " + ", ".join(bad))
    sb = con.execute("SELECT s.date, s.home_score, s.away_score, f.home_score fh, f.away_score fa FROM matches s JOIN matches f ON f.date=s.date AND f.is_egypt=1 AND f.source='fotmob' WHERE s.source='statsbomb' AND s.is_egypt=1").fetchall()
    add(f"\nStatsBomb vs FotMob, Egypt's AFCON 2023 matches present in both: {len(sb)}; score disagreements: {sum(1 for r in sb if (r['home_score'], r['away_score']) != (r['fh'], r['fa']))}")

    # ---- missing data
    add("\n## 2. Matches lacking formation / lineup / events\n")
    rows = con.execute("SELECT m.id, m.source, m.date, m.competition, m.formation_egypt, m.has_lineup, m.has_events, "
                       "(SELECT COUNT(*) FROM events_lite e WHERE e.match_id=m.id AND e.type='shot') shots, "
                       "(SELECT COUNT(*) FROM lineups l WHERE l.match_id=m.id AND l.started=1) xi "
                       "FROM matches m WHERE m.is_egypt=1 ORDER BY m.date").fetchall()
    miss = []
    for r in rows:
        prob = []
        if not r["formation_egypt"]:
            prob.append("no formation")
        if r["xi"] < 22 or not r["has_lineup"]:
            prob.append(f"lineup incomplete (starters={r['xi']})")
        if not r["has_events"]:
            prob.append("no events")
        if prob:
            miss.append((r["date"], r["source"], r["competition"], ", ".join(prob)))
    add("Gaps here mean: no formation, incomplete starting XI (<22 starters recorded), or no events. Shot/xG coverage is reported separately below.\n")
    add(f"Egypt national-team matches with any gap: {len(miss)} of {len(rows)}.\n")
    add(_tbl(["date", "source", "competition", "gaps"], miss) if miss else "None.")
    shots_by_year = con.execute("SELECT substr(m.date,1,4) y, COUNT(*) n, SUM(CASE WHEN EXISTS(SELECT 1 FROM events_lite e WHERE e.match_id=m.id AND e.type='shot') THEN 1 ELSE 0 END) w FROM matches m WHERE m.is_egypt=1 GROUP BY y ORDER BY y").fetchall()
    add("\n### Shot / xG coverage of Egypt matches, by year\n")
    add(_tbl(["year", "Egypt matches", "with shot data"], [tuple(r) for r in shots_by_year]))
    unm = con.execute("SELECT date, competition, source FROM matches WHERE is_egypt=1 AND egypt_coach_id IS NULL ORDER BY date").fetchall()
    add(f"\n### Egypt matches with no coach tag ({len(unm)}): source gave none and tenure was ambiguous\n")
    add(", ".join(f"{r['date']} ({r['competition']})" for r in unm) or "None.")
    nomin = q0 = con.execute("SELECT COUNT(DISTINCT match_id) FROM lineups WHERE minutes_played IS NULL").fetchone()[0]
    add(f"\n### Matches whose feed has a bench but no substitution data (player minutes NULL): {nomin}\n")
    lm = con.execute("SELECT m.date, m.competition, m.source_match_id, "
                     "(SELECT COUNT(*) FROM lineups l WHERE l.match_id=m.id AND l.started=1) xi, m.formation_home, m.formation_away, m.has_events "
                     "FROM matches m WHERE m.scope='egyptian_league' ORDER BY m.date").fetchall()
    lbad = [(r["date"], r["source_match_id"], f"starters={r['xi']}", "no formation" if not (r["formation_home"] and r["formation_away"]) else "", "no events" if not r["has_events"] else "") for r in lm
            if r["xi"] < 22 or not (r["formation_home"] and r["formation_away"]) or not r["has_events"]]
    add(f"\nEgyptian Premier League matches ingested: {len(lm)}; with any gap (incomplete XI / no formation / no events): {len(lbad)}. (FotMob coverage for the league has no shot maps, so xG is absent league-wide.)\n")
    if lbad:
        add(_tbl(["date", "fotmob match id", "xi", "formation", "events"], lbad[:150]))
        if len(lbad) > 150:
            add(f"\n... {len(lbad)-150} more not shown.")

    # ---- counts
    add("\n## 3. Row counts\n")
    c = db.counts(con)
    add(_tbl(["table", "rows"], sorted(c.items())))
    add("\n### Rows per source\n")
    per = []
    for t in ["matches", "lineups", "events_lite", "players", "player_club_minutes", "teams", "clubs", "coaches"]:
        for r in con.execute(f"SELECT COALESCE(source,'(none)') s, COUNT(*) n FROM {t} GROUP BY s"):
            per.append((t, r["s"], r["n"]))
    add(_tbl(["table", "source", "rows"], per))
    add("\nMatches by scope/source:\n")
    add(_tbl(["scope", "source", "season", "matches"], [tuple(r) for r in con.execute("SELECT scope, source, season, COUNT(*) FROM matches GROUP BY 1,2,3 ORDER BY 1,2,3")]))
    snap = db.ROOT / "data" / "row_counts.json"
    if snap.exists():
        s = json.loads(snap.read_text())
        add(f"\n### Idempotence: last `etl.run` ({s['at']}) row counts before vs after\n")
        add(_tbl(["table", "before", "after", "delta"], [(t, s["before"].get(t, 0), n, n - s["before"].get(t, 0)) for t, n in s["after"].items()]))

    # ---- coverage
    add("\n## 4. Field coverage\n")
    q = lambda sql: con.execute(sql).fetchone()[0]
    cov = [
        ("Egypt matches with coach tagged", f"{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1 AND egypt_coach_id IS NOT NULL')}/{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1')}"),
        ("Egypt matches with opponent Elo", f"{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1 AND opponent_elo IS NOT NULL')}/{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1')}"),
        ("Players with DOB", f"{q('SELECT COUNT(*) FROM players WHERE dob IS NOT NULL')}/{q('SELECT COUNT(*) FROM players')}"),
        ("Players with Arabic name (Wikidata)", q("SELECT COUNT(*) FROM players WHERE name_ar IS NOT NULL")),
        ("Players with Wikidata QID", q("SELECT COUNT(*) FROM players WHERE wikidata_qid IS NOT NULL")),
        ("Players with Transfermarkt id (via Wikidata)", q("SELECT COUNT(*) FROM players WHERE transfermarkt_id IS NOT NULL")),
        ("Captain flag present in lineups (any =1) FotMob", q("SELECT COUNT(*) FROM lineups WHERE source='fotmob' AND captain=1")),
        ("Matches (Egypt) with neutral flag set", f"{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1 AND neutral IS NOT NULL')}/{q('SELECT COUNT(*) FROM matches WHERE is_egypt=1')}"),
    ]
    add(_tbl(["field", "value"], cov))
    cm = con.execute("SELECT COUNT(DISTINCT player_id), COUNT(*), MIN(date), MAX(date) FROM player_club_minutes").fetchone()
    early = con.execute("SELECT COUNT(*) FROM (SELECT player_id, MIN(date) d FROM player_club_minutes GROUP BY player_id HAVING d <= '2025-09-01')").fetchone()[0]
    add(f"\nplayer_club_minutes: {cm[1]} match-level rows for {cm[0]} players, {cm[2]} to {cm[3]}. Players whose rows reach back to 2025-09-01 or earlier: {early} (FotMob's per-player recent-matches list is a bounded window, so last-season coverage for abroad players is partial).")

    # ---- sources
    add("\n## 5. Sources: failed / blocked and fallbacks\n")
    add(_tbl(["source", "status", "note"], PROBE_NOTES))
    add("\n### Latest run per step (`ingest_runs`)\n")
    runs = con.execute("SELECT r.step, r.status, r.rows_written, r.http_network, r.http_cache_hits, r.finished_at, r.notes FROM ingest_runs r "
                       "WHERE r.id IN (SELECT MAX(id) FROM ingest_runs GROUP BY step) ORDER BY r.id").fetchall()
    add(_tbl(["step", "status", "rows", "http", "cache hits", "finished", "notes (first 300 chars)"],
             [(r["step"], r["status"], r["rows_written"], r["http_network"], r["http_cache_hits"], r["finished_at"], (r["notes"] or "").replace("\n", " / ")[:300]) for r in runs]))

    # ---- spot-check
    add("\n## 6. Spot-check: 3 random Hassan-era matches\n")
    add("Compare against Wikipedia's match pages / results list. Minutes are nominal (sub minute vs 90/120; stoppage time not modelled); NULL means the feed had no substitution data.\n")
    pool = con.execute("SELECT * FROM matches WHERE is_egypt=1 AND source='fotmob' AND date>=? AND has_lineup=1 ORDER BY date", (start,)).fetchall()
    for m in random.SystemRandom().sample(pool, min(3, len(pool))):
        add(_match_block(con, m) + "\n")

    add("\n## 7. Known gaps (by design or by data)\n")
    for g in [
        "Sofascore blocked (403); no fallback needed since FotMob covers lineups/shots.",
        "Player minutes in national-team and league matches are derived from substitution/red-card minutes against a nominal 90 (120 after extra time) minutes; stoppage time is not modelled and they are not official minute counts. They are NULL where the feed lists a bench but no substitution data.",
        "Older FotMob matches (roughly pre-2023) have name-only lineups: no formation, no coach; players are resolved by events/name (source 'fotmob-legacy' for unresolved).",
        "Club-level minutes for Egypt-capped players come from FotMob player pages' recent-matches list (bounded window); older club matches may be missing.",
        "Shot maps/xG exist only for FotMob matches with shotmap coverage (most senior internationals; not the Egyptian league). StatsBomb xG for AFCON 2023 uses StatsBomb's own model, not comparable to FotMob's.",
        "StatsBomb players are separate rows from FotMob players (no cross-source identity merge yet; Phase 2).",
        "Captain flag depends on FotMob providing it; see coverage table.",
        "Neutral-venue flag is a heuristic (stadium country vs. team names).",
        "eligibility_evidence is schema only (Phase 2).",
    ]:
        add(f"- {g}")
    REPORT.write_text("\n".join(L) + "\n")
    return REPORT
