"""StatsBomb open data (github.com/statsbomb/open-data). Free for local use with attribution.

Raw JSON is kept under data/statsbomb/{matches,lineups,events}/ for Phase 2 opponent analysis.
Public-site copy must carry: "Data provided by StatsBomb" attribution and must not republish raw events.
"""
from __future__ import annotations

import json
from pathlib import Path

from .. import http
from ..db import now, upsert, norm

RAW = "https://raw.githubusercontent.com/statsbomb/open-data/master/data"
GH = "https://github.com/statsbomb/open-data"
OUT = Path(__file__).resolve().parents[2] / "data" / "statsbomb"
ALIASES = {"cote d ivoire": "ivory coast", "cabo verde": "cape verde", "congo dr": "dr congo",
           "democratic republic of congo": "dr congo", "equatorial guinea": "equatorial guinea"}

SB_OUTCOME = {"Goal": "goal", "Saved": "saved", "Saved Off Target": "saved", "Saved to Post": "saved", "Blocked": "blocked",
              "Off T": "off_target", "Wayward": "off_target", "Post": "post"}


def _fetch(kind: str, rel: str):
    """Return parsed JSON, using on-disk raw copy when present (no double caching)."""
    f = OUT / kind / Path(rel).name
    if f.exists():
        return json.loads(f.read_text())
    txt = http.get(f"{RAW}/{rel}", store=False, ttl=0)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(txt)
    return json.loads(txt)


def _clock(s):
    if s is None:
        return None
    m, sec = s.split(":")[:2]
    return int(m) + int(sec) / 60


def find_afcon2023():
    comps = json.loads(http.get(f"{RAW}/competitions.json", ttl=24 * 3600))
    for c in comps:
        if "african cup" in c["competition_name"].lower() and str(c["season_name"]).startswith("2023"):
            return c
    return None


def team_id(con, sb_id, name):
    n = norm(name)
    n = ALIASES.get(n, n)
    r = con.execute("SELECT id FROM teams WHERE statsbomb_id=?", (sb_id,)).fetchone()
    if r:
        return r[0]
    for r in con.execute("SELECT id, name FROM teams WHERE kind='national'"):
        if norm(r["name"]) == n or ALIASES.get(norm(r["name"])) == n:
            con.execute("UPDATE teams SET statsbomb_id=? WHERE id=?", (sb_id, r["id"]))
            return r["id"]
    return upsert(con, "teams", ["statsbomb_id"], dict(statsbomb_id=sb_id, name=name, kind="national", source="statsbomb",
                                                       source_url=GH, fetched_at=now()))


def fmt(f):
    s = str(f) if f else ""
    return "-".join(s) if s and s.isdigit() else (s or None)


def ingest(con, run, limit=None):
    comp = find_afcon2023()
    if not comp:
        run.note("AFCON 2023 not found in StatsBomb competitions.json", "failed")
        return 0
    cid, sid = comp["competition_id"], comp["season_id"]
    murl = f"{RAW}/matches/{cid}/{sid}.json"
    matches = _fetch("matches", f"matches/{cid}/{sid}.json")
    run.note(f"competition {cid}/{sid} '{comp['competition_name']} {comp['season_name']}' -> {len(matches)} matches")
    ts = now()
    n = 0
    for m in sorted(matches, key=lambda x: x["match_date"])[: (limit or None)]:
        mid = m["match_id"]
        ht, at = m["home_team"], m["away_team"]
        h_t = team_id(con, ht["home_team_id"], ht["home_team_name"])
        a_t = team_id(con, at["away_team_id"], at["away_team_name"])
        egy = "Egypt" in (ht["home_team_name"], at["away_team_name"])
        side = ("home" if ht["home_team_name"] == "Egypt" else "away") if egy else None
        coach_id = None
        if egy:
            mg = (ht if side == "home" else at).get("managers") or []
            if mg:
                nm = mg[0].get("nickname") or mg[0]["name"]
                # reuse FotMob-coach row if surname matches
                cand = {norm(nm), norm(mg[0]["name"])}
                def _same(a, b):
                    ta, tb = norm(a).split(), norm(b).split()
                    return bool(ta and tb and ta[0] == tb[0] and ta[-1] == tb[-1])
                row = next((r for r in con.execute("SELECT id, name FROM coaches WHERE team='Egypt'")
                            if norm(r["name"]) in cand or _same(r["name"], mg[0]["name"]) or _same(r["name"], nm)), None)
                coach_id = row[0] if row else upsert(con, "coaches", ["name"], dict(
                    name=mg[0]["name"], nationality=(mg[0].get("country") or {}).get("name"), team="Egypt",
                    source="statsbomb", source_url=GH, fetched_at=ts))
        ev_url = f"{GH}/blob/master/data/events/{mid}.json"
        db_mid = upsert(con, "matches", ["source", "source_match_id"], dict(
            source="statsbomb", source_match_id=str(mid), scope="national", date=m["match_date"],
            kickoff_utc=None, competition="Africa Cup of Nations", stage=(m.get("competition_stage") or {}).get("name"),
            season="2023", home_team_id=h_t, away_team_id=a_t, home_score=m["home_score"], away_score=m["away_score"],
            venue=(m.get("stadium") or {}).get("name"), venue_country=(m.get("stadium") or {}).get("country", {}).get("name"),
            neutral=1 if not any(x in ("Ivory Coast", "Côte d'Ivoire") for x in (ht["home_team_name"], at["away_team_name"])) else 0,
            is_egypt=1 if egy else 0, egypt_side=side, egypt_coach_id=coach_id, coach_source="statsbomb" if coach_id else None, source_url=ev_url, fetched_at=ts))
        # events
        events = _fetch("events", f"events/{mid}.json")
        lineups = _fetch("lineups", f"lineups/{mid}.json")
        end = 90.0
        for e in events:
            if e.get("period", 1) <= 4:
                end = max(end, e["minute"] + e.get("second", 0) / 60)
        end = round(end)
        players: dict[int, int] = {}
        team_map = {ht["home_team_id"]: (h_t, "home"), at["away_team_id"]: (a_t, "away")}
        formations = {}
        for e in events:
            if e["type"]["name"] == "Starting XI":
                formations[e["team"]["id"]] = fmt((e.get("tactics") or {}).get("formation"))
        for t in lineups:
            tid, side_t = team_map[t["team_id"]]
            for p in t["lineup"]:
                nm = p.get("player_nickname") or p["player_name"]
                pid = upsert(con, "players", ["statsbomb_id"], dict(
                    statsbomb_id=p["player_id"], name=nm, nationality=(p.get("country") or {}).get("name"),
                    source="statsbomb", source_url=GH, fetched_at=ts))
                players[p["player_id"]] = pid
                pos = p.get("positions") or []
                started = 1 if pos and pos[0].get("start_reason") == "Starting XI" else 0
                mins = 0.0
                for q in pos:
                    a = _clock(q["from"])
                    b = _clock(q["to"]) if q.get("to") else end
                    mins += max(b - a, 0)
                lastto = pos[-1].get("to") if pos else None
                upsert(con, "lineups", ["match_id", "player_id"], dict(
                    match_id=db_mid, player_id=pid, team_id=tid, team_side=side_t, started=started,
                    position=pos[0]["position"] if pos else None, slot=str(pos[0]["position_id"]) if pos else None,
                    shirt=str(p.get("jersey_number")), minutes_played=round(mins, 1),
                    sub_on_minute=None if started or not pos else int(_clock(pos[0]["from"])),
                    sub_off_minute=int(_clock(lastto)) if lastto else None, captain=None, rating=None,
                    source="statsbomb", source_url=ev_url, fetched_at=ts))
                n += 1

        def pid_of(x):
            return players.get(x["id"]) if x else None

        def ev(key, e, typ, player=None, related=None, sit=None, outcome=None, xg=None):
            nonlocal n
            upsert(con, "events_lite", ["match_id", "event_key"], dict(
                match_id=db_mid, event_key=key, minute=e["minute"], added_minute=None, type=typ,
                team_id=team_map[e["team"]["id"]][0], player_id=player, related_player_id=related, situation=sit,
                outcome=outcome, xg=xg, source="statsbomb", source_url=ev_url, fetched_at=ts))
            n += 1

        for e in events:
            t = e["type"]["name"]
            if e.get("period", 1) == 5:
                continue  # shootout
            if t == "Shot":
                s = e["shot"]
                st, pp = s["type"]["name"], (e.get("play_pattern") or {}).get("name", "")
                sit = ("penalty" if st == "Penalty" else "corner" if st == "Corner" or pp == "From Corner" else
                       "free_kick" if st == "Free Kick" or pp == "From Free Kick" else
                       "throw_in" if pp == "From Throw In" else "open_play")
                oc = SB_OUTCOME.get(s["outcome"]["name"], s["outcome"]["name"])
                ev(f"{e['id']}:shot", e, "shot", pid_of(e.get("player")), None, sit, oc, s.get("statsbomb_xg"))
                if oc == "goal":
                    ev(f"{e['id']}:goal", e, "pen_goal" if st == "Penalty" else "goal", pid_of(e.get("player")), None,
                       "penalty" if st == "Penalty" else sit, "goal", s.get("statsbomb_xg"))
            elif t == "Own Goal Against":
                ev(f"{e['id']}:og", e, "own_goal", pid_of(e.get("player")), None, "unknown", "goal")
            elif t in ("Foul Committed", "Bad Behaviour"):
                card = (e.get("foul_committed") or e.get("bad_behaviour") or {}).get("card", {}).get("name")
                if card:
                    ev(f"{e['id']}:card", e, "yellow" if card == "Yellow Card" else "red", pid_of(e.get("player")), None, None, card)
            elif t == "Substitution":
                rep = e["substitution"]["replacement"]
                # replacement may not be in lineup map if missing; skip safely
                ev(f"{e['id']}:off", e, "sub_off", pid_of(e.get("player")), players.get(rep["id"]))
                ev(f"{e['id']}:on", e, "sub_on", players.get(rep["id"]), pid_of(e.get("player")))
        fh = formations.get(ht["home_team_id"]); fa = formations.get(at["away_team_id"])
        con.execute("UPDATE matches SET formation_home=?, formation_away=?, formation_egypt=?, formation_opp=?, has_lineup=1, has_events=1 WHERE id=?",
                    (fh, fa, (fh if side == "home" else fa) if egy else None, (fa if side == "home" else fh) if egy else None, db_mid))
        con.commit()
    return n
