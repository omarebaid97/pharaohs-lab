"""Idempotent ETL entrypoint.

    python -m etl.run                 # all steps
    python -m etl.run --only fotmob_egypt
    python -m etl.run --limit 20      # cap matches per step (smoke test)
"""
from __future__ import annotations

import argparse
import json
import traceback

from . import db, http
from .sources import elo, fotmob, statsbomb, wikidata, wikipedia

SINCE = "2018-01-01"
STEPS = ["wikipedia", "fotmob_egypt", "coaches", "elo", "fotmob_league", "fotmob_players", "statsbomb", "wikidata", "validate"]
HASSAN_START_FALLBACK = "2024-02-06"


def log(*a):
    print(*a, flush=True)


# ------------------------------------------------------------------ steps
def step_wikipedia(con, run, args):
    ts = db.now()
    fx = wikipedia.fetch_fixtures(SINCE)
    for f in fx:
        db.upsert(con, "wiki_fixtures", ["date", "home", "away"], dict(**f, fetched_at=ts))
    run.rows = len(fx)
    rec = wikipedia.hassan_record()
    db.upsert(con, "meta", ["key"], dict(key="wikipedia_hassan_record", value=json.dumps(rec, ensure_ascii=False),
                                         source_url=rec["source_url"], fetched_at=ts))
    if not rec.get("found"):
        run.note("Hossam Hassan managerial-record table not parsed", "partial")
    run.note(f"{len(fx)} Egypt fixtures {SINCE}..; Hassan record: {rec.get('played')} played from {rec.get('from')}")


def step_fotmob_egypt(con, run, args):
    found = fotmob.discover_egypt(SINCE, log)
    log(f"  discovered {len(found)} Egypt matches on FotMob")
    items = sorted(found.items(), key=lambda kv: kv[1]["date"])
    if args.limit:
        items = items[-args.limit:]
    fail = 0
    for i, (mid, info) in enumerate(items, 1):
        try:
            nd, used = fotmob.get_match(mid, info["url"], info.get("alt"), ttl=30 * 24 * 3600 if info["date"] < db.now()[:10] else None)
            fotmob.ingest_match(con, nd, "national", used)
            con.commit()
            run.rows += 1
        except http.Blocked:
            raise
        except Exception as e:
            fail += 1
            run.note(f"match {mid} {info['url']}: {type(e).__name__}: {e}")
            traceback.print_exc()
        if i % 20 == 0:
            log(f"  {i}/{len(items)}")
    con.execute("DELETE FROM players WHERE source='fotmob-legacy' AND id NOT IN (SELECT player_id FROM lineups) "
                "AND id NOT IN (SELECT player_id FROM events_lite WHERE player_id IS NOT NULL) "
                "AND id NOT IN (SELECT related_player_id FROM events_lite WHERE related_player_id IS NOT NULL)")
    if fail:
        run.note(f"{fail} match pages failed to parse", "partial")
    run.note(f"{len(found)} matches discovered, {run.rows} ingested")


def step_coaches(con, run, args):
    rec = con.execute("SELECT value FROM meta WHERE key='wikipedia_hassan_record'").fetchone()
    hstart = (json.loads(rec[0]).get("from") if rec else None) or HASSAN_START_FALLBACK
    # 1. reset earlier inferred tags (so reruns are deterministic), then recompute tenures from source-tagged matches
    con.execute("UPDATE matches SET egypt_coach_id=NULL, coach_source=NULL WHERE coach_source IN ('gap_fill_bracketed','wikipedia_year','wikipedia_tenure')")
    for c in con.execute("SELECT c.id, c.name, MIN(m.date) a, MAX(m.date) b FROM coaches c JOIN matches m ON m.egypt_coach_id=c.id "
                         "WHERE c.team='Egypt' GROUP BY c.id").fetchall():
        start, end, src = c["a"], c["b"], "derived from source-tagged matches (first/last match date)"
        if db.norm(c["name"]) == "hossam hassan":
            start, end, src = hstart, None, "Wikipedia (Hossam Hassan article, managerial record)"
        con.execute("UPDATE coaches SET tenure_start=?, tenure_end=?, source_url=COALESCE(source_url,?) WHERE id=?", (start, end, src, c["id"]))
        run.rows += 1
    # 2. untagged matches bracketed by two tagged matches with the same coach
    ms = con.execute("SELECT id, date, egypt_coach_id c FROM matches WHERE is_egypt=1 ORDER BY date, id").fetchall()
    tagged = [(m["date"], m["c"]) for m in ms if m["c"]]
    filled = 0
    for m in ms:
        if m["c"]:
            continue
        prev = [c for d, c in tagged if d < m["date"]]
        nxt = [c for d, c in tagged if d > m["date"]]
        if prev and nxt and prev[-1] == nxt[0]:
            con.execute("UPDATE matches SET egypt_coach_id=?, coach_source='gap_fill_bracketed' WHERE id=?", (prev[-1], m["id"]))
            filled += 1
    # 2b. Hassan's tenure is open-ended from hstart (Wikipedia managerial record): untagged matches from then on are his
    hid_ = con.execute("SELECT id FROM coaches WHERE team='Egypt' AND lower(name) LIKE 'hossam hassan%'").fetchone()
    if hid_:
        n2 = con.execute("UPDATE matches SET egypt_coach_id=?, coach_source='wikipedia_tenure' WHERE is_egypt=1 AND egypt_coach_id IS NULL AND date>=?",
                         (hid_[0], hstart)).rowcount
        filled += n2
    # 3. Wikipedia coaching-history year ranges (only if exactly one coach covers the year)
    hist = wikipedia.coaching_history()
    name2id = {db.norm(r["name"]): r["id"] for r in con.execute("SELECT id, name FROM coaches WHERE team='Egypt'")}
    ranges = [(name2id[db.norm(h["name"])], h["from_year"], h["to_year"]) for h in hist if db.norm(h["name"]) in name2id]
    for m in con.execute("SELECT id, date FROM matches WHERE is_egypt=1 AND egypt_coach_id IS NULL").fetchall():
        y = int(m["date"][:4])
        cand = {cid for cid, a, b in ranges if a <= y <= b}
        if len(cand) == 1:
            con.execute("UPDATE matches SET egypt_coach_id=?, coach_source='wikipedia_year' WHERE id=?", (cand.pop(), m["id"]))
            filled += 1
    left = con.execute("SELECT COUNT(*) FROM matches WHERE is_egypt=1 AND egypt_coach_id IS NULL").fetchone()[0]
    run.note(f"{filled} untagged Egypt matches assigned via bracketing/Wikipedia years; {left} still without a coach (ambiguous)", "partial" if left else None)
    con.execute("DELETE FROM coaches WHERE id NOT IN (SELECT egypt_coach_id FROM matches WHERE egypt_coach_id IS NOT NULL)")
    con.commit()


def step_elo(con, run, args):
    data, url = elo.fetch(SINCE)
    bad = 0
    for m in con.execute("SELECT id, date, home_score, away_score, egypt_side FROM matches WHERE is_egypt=1 AND source='fotmob'").fetchall():
        e = None
        egy = m["home_score"] if m["egypt_side"] == "home" else m["away_score"]
        opp = m["away_score"] if m["egypt_side"] == "home" else m["home_score"]
        from datetime import date as _d, timedelta as _td
        d0 = _d.fromisoformat(m["date"])
        # FotMob dates are UTC, Elo dates are local: try same day, then +-1 day, requiring the score to agree
        for off in (0, -1, 1):
            c = data.get((d0 + _td(days=off)).isoformat())
            if c and (c["egy_score"], c["opp_score"]) == (egy, opp):
                e = c
                break
        if not e:
            if any(data.get((d0 + _td(days=o)).isoformat()) for o in (0, -1, 1)):
                bad += 1
                run.note(f"elo: no score-consistent entry near {m['date']} (ours {egy}-{opp})")
            continue
        con.execute("UPDATE matches SET opponent_elo=?, egypt_elo=? WHERE id=?", (e["opp_elo"], e["egy_elo"], m["id"]))
        run.rows += 1
    if bad:
        run.status = "partial"
    con.commit()
    # StatsBomb copies of Egypt matches
    for m in con.execute("SELECT id, date FROM matches WHERE is_egypt=1 AND source='statsbomb'").fetchall():
        e = data.get(m["date"])
        if e:
            con.execute("UPDATE matches SET opponent_elo=?, egypt_elo=? WHERE id=?", (e["opp_elo"], e["egy_elo"], m["id"]))
    con.commit()


def step_fotmob_league(con, run, args):
    cur_season = db.season_of(db.now()[:10])
    y = int(cur_season[:4])
    seasons = [f"{y}/{y+1}", f"{y-1}/{y}"]
    fail = 0
    for season in seasons:
        fx, name = fotmob.league_fixtures(fotmob.EGYPT_PL_ID, season)
        items = fx[-args.limit:] if args.limit else fx
        log(f"  {name} {season}: {len(items)} finished fixtures")
        for i, f in enumerate(items, 1):
            try:
                ttl = 90 * 24 * 3600
                nd = fotmob.next_data(f["url"], ttl=ttl)
                fotmob.ingest_match(con, nd, "egyptian_league", f["url"])
                con.commit()
                run.rows += 1
            except http.Blocked:
                raise
            except Exception as e:
                fail += 1
                run.note(f"{f['url']}: {type(e).__name__}: {e}")
            if i % 50 == 0:
                log(f"  {season} {i}/{len(items)}")
    if fail:
        run.note(f"{fail} league match pages failed", "partial")


def step_fotmob_players(con, run, args):
    rows = con.execute(
        "SELECT DISTINCT p.id, p.fotmob_id FROM lineups l JOIN matches m ON m.id=l.match_id JOIN players p ON p.id=l.player_id "
        "JOIN teams t ON t.id=l.team_id WHERE m.is_egypt=1 AND m.source='fotmob' AND m.date>='2024-01-01' AND t.fotmob_id=? "
        "AND p.fotmob_id IS NOT NULL", (fotmob.EGYPT_ID,)).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    log(f"  {len(rows)} Egypt-capped players since 2024")
    fail = 0
    for i, r in enumerate(rows, 1):
        try:
            run.rows += fotmob.ingest_player_page(con, r["fotmob_id"], r["id"])
            con.commit()
        except http.Blocked:
            raise
        except Exception as e:
            fail += 1
            run.note(f"player {r['fotmob_id']}: {type(e).__name__}: {e}")
        if i % 20 == 0:
            log(f"  {i}/{len(rows)}")
    con.execute("DELETE FROM player_club_minutes WHERE date < '2025-07-01'")  # keep last + current season only
    if fail:
        run.note(f"{fail} player pages failed", "partial")


def step_statsbomb(con, run, args):
    run.rows = statsbomb.ingest(con, run, limit=args.limit)


def step_wikidata(con, run, args):
    rows = wikidata.fetch()
    by_name: dict[str, list] = {}
    for r in rows:
        for nm in {db.norm(r["en"])}:
            by_name.setdefault(nm, []).append(r)
    matched = 0
    for p in con.execute("SELECT id, name, dob FROM players").fetchall():
        cands = by_name.get(db.norm(p["name"]), [])
        if p["dob"]:
            cands = [c for c in cands if c["dob"] == p["dob"]]
        if len(cands) != 1:
            continue
        c = cands[0]
        con.execute("UPDATE players SET wikidata_qid=?, name_ar=COALESCE(?,name_ar), transfermarkt_id=COALESCE(?,transfermarkt_id),"
                    " other_nationalities=COALESCE(?,other_nationalities), dob=COALESCE(dob,?) WHERE id=?",
                    (c["qid"], c["ar"], c["tm"], c["citizenships"], c["dob"], p["id"]))
        matched += 1
    con.commit()
    run.rows = matched
    run.note(f"Wikidata returned {len(rows)} Egypt-sport footballers; matched {matched} players (name + DOB when known)")


def step_validate(con, run, args):
    from . import validate
    validate.write_report(con)


FUNCS = {"wikipedia": step_wikipedia, "fotmob_egypt": step_fotmob_egypt, "coaches": step_coaches, "elo": step_elo,
         "fotmob_league": step_fotmob_league, "fotmob_players": step_fotmob_players, "statsbomb": step_statsbomb,
         "wikidata": step_wikidata, "validate": step_validate}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=STEPS, action="append", help="run only this step (repeatable)")
    ap.add_argument("--limit", type=int, default=0, help="cap items per step (smoke test)")
    args = ap.parse_args()
    con = db.connect()
    db.init(con)
    before = db.counts(con)
    steps = args.only or STEPS
    for step in [x for x in steps if x != "validate"]:
        log(f"== {step}")
        try:
            with db.Run(con, step) as run:
                FUNCS[step](con, run, args)
                con.commit()
        except http.Blocked as e:
            con.execute("UPDATE ingest_runs SET status='blocked' WHERE id=(SELECT MAX(id) FROM ingest_runs WHERE step=?)", (step,))
            con.commit()
            log(f"  BLOCKED: {e}")
        except Exception as e:  # keep going; recorded in ingest_runs
            log(f"  FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()
    after = db.counts(con)
    snap = db.ROOT / "data" / "row_counts.json"
    snap.write_text(json.dumps(dict(at=db.now(), before=before, after=after), indent=1))
    log("row counts before -> after:")
    for t in after:
        log(f"  {t:24s} {before.get(t,0):7d} -> {after[t]:7d}")
    if "validate" in steps:
        log("== validate")
        try:
            with db.Run(con, "validate") as run:
                step_validate(con, run, args)
        except Exception as e:
            log(f"  FAILED: {type(e).__name__}: {e}")
            traceback.print_exc()


if __name__ == "__main__":
    main()
