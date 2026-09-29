"""FotMob via its public web pages (server-rendered __NEXT_DATA__ JSON).

The /api/* endpoints require signed headers; we do NOT try to replicate that. The HTML pages
are ordinary public pages and are fetched through the polite client like everything else.
"""
from __future__ import annotations

import json
import re
from datetime import datetime

from .. import http
from ..db import now, upsert, season_of, norm

BASE = "https://www.fotmob.com"
EGYPT_ID = 10255
EGYPT_PL_ID = 519  # Egyptian Premier League

SITUATION = {
    "RegularPlay": "open_play", "FastBreak": "open_play", "FromCorner": "corner",
    "FreeKick": "free_kick", "Penalty": "penalty", "ThrowInSetPiece": "throw_in",
    "SetPiece": "unknown",
}
OUTCOME = {"Goal": "goal", "AttemptSaved": "saved", "Blocked": "blocked", "Miss": "off_target", "Post": "post"}

STAGES = {"1/32": "Round of 64", "1/16": "Round of 32", "1/8": "Round of 16", "1/4": "Quarter-final", "1/2": "Semi-final",
          "final": "Final", "3rd place": "Third place", "3rd_place": "Third place"}

_NEXT = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def next_data(url: str, ttl=None) -> dict:
    html = http.get(url, ttl=ttl)
    m = _NEXT.search(html)
    if not m:
        raise http.Blocked(f"no __NEXT_DATA__ at {url} (possible challenge page)")
    return json.loads(m.group(1))


def _clean(pageurl: str) -> str:
    """Match page URLs (/matches/<slug>/<pair-hash>#<id>) resolve to the pairing's latest fixture when
    fetched server-side (the id is only a URL fragment), so fetch /match/<id> instead."""
    mid = pageurl.partition("#")[2]
    return f"{BASE}/match/{mid}" if mid else BASE + pageurl


def _mid(pageurl: str) -> str:
    return pageurl.split("#")[-1]


# ---------------------------------------------------------------- discovery
def team_fixtures(team_id=EGYPT_ID, slug="egypt", ttl=6 * 3600):
    d = next_data(f"{BASE}/teams/{team_id}/overview/{slug}", ttl=ttl)
    t = d["props"]["pageProps"]["fallback"][f"team-{team_id}"]
    return t["fixtures"]["allFixtures"]["fixtures"], t


def discover_egypt(since="2018-01-01", log=print) -> dict[str, dict]:
    """Return {match_id: {url, date}} of Egypt matches >= since.

    Team page lists the recent ~30 fixtures; older ones are found by walking each match
    page's 'teamForm' (Egypt's previous 5 matches) backwards until we cross `since`.
    """
    found: dict[str, dict] = {}
    fx, _ = team_fixtures()
    for f in fx:
        st = f["status"]
        if st.get("finished") and not st.get("cancelled") and st["utcTime"][:10] >= since:
            found[_mid(f["pageUrl"])] = dict(url=_clean(f["pageUrl"]), date=st["utcTime"][:10], alt=BASE + f["pageUrl"].split("#")[0])
    seen: set[str] = set()
    while True:
        todo = sorted((v["date"], k) for k, v in found.items() if k not in seen)
        if not todo:
            break
        date, mid = todo[0]  # earliest unexpanded first
        seen.add(mid)
        if date < since:
            continue
        try:
            d = next_data(found[mid]["url"])
        except http.NotFound:
            continue
        forms = d["props"]["pageProps"]["content"]["matchFacts"].get("teamForm") or []
        for lst in forms:
            if not any((e.get("home", {}).get("id") == str(EGYPT_ID) and e["home"].get("isOurTeam"))
                       or (e.get("away", {}).get("id") == str(EGYPT_ID) and e["away"].get("isOurTeam")) for e in lst):
                continue
            for e in lst:
                dt = e["date"]["utcTime"][:10]
                mid2 = _mid(e["linkToMatch"])
                alt2 = BASE + e["linkToMatch"].split("#")[0]
                dup = any(v["date"] == dt and v["alt"] == alt2 for v in found.values())  # FotMob alias ids
                if dt >= since and mid2 not in found and not dup:
                    found[mid2] = dict(url=_clean(e["linkToMatch"]), date=dt, alt=BASE + e["linkToMatch"].split("#")[0])
    return found


def league_fixtures(league_id: int, season: str, slug="premier-league"):
    """season like '2025/2026'. Returns list of finished fixture dicts."""
    q = season.replace("/", "%2F")
    d = next_data(f"{BASE}/leagues/{league_id}/fixtures/{slug}?season={q}", ttl=6 * 3600)
    pp = d["props"]["pageProps"]
    out = []
    for m in pp["fixtures"]["allMatches"]:
        st = m["status"]
        if st.get("finished") and not st.get("cancelled"):
            out.append(dict(id=str(m["id"]), url=_clean(m["pageUrl"]), date=st["utcTime"][:10]))
    return out, pp["details"]["name"]


# ---------------------------------------------------------------- parsing helpers
def _side_of(team_id, home_id, away_id):
    return "home" if team_id == home_id else "away" if team_id == away_id else None


def _dur_minutes(halfs: dict) -> float | None:
    def p(s):
        try:
            return datetime.strptime(s, "%d.%m.%Y %H:%M:%S")
        except Exception:
            return None
    tot, ok = 0.0, False
    for a, b in (("firstHalfStarted", "firstHalfEnded"), ("secondHalfStarted", "secondHalfEnded"),
                 ("firstExtraHalfStarted", "secondExtraHalfEnded")):
        s, e = p(halfs.get(a, "")), p(halfs.get(b, ""))
        if s and e:
            tot += (e - s).total_seconds() / 60
            ok = True
    return round(tot) if ok and tot > 60 else None


def ensure_team(con, fm_id, name, kind, country=None, url=None):
    return upsert(con, "teams", ["fotmob_id"], dict(fotmob_id=int(fm_id), name=name, kind=kind, country=country,
                                                    source="fotmob", source_url=url, fetched_at=now()), keep_existing=("kind",))


def ensure_club(con, fm_id, name, country=None, url=None):
    tid = ensure_team(con, fm_id, name, "club", country, url)
    return upsert(con, "clubs", ["fotmob_id"], dict(fotmob_id=int(fm_id), name=name, country=country, team_id=tid,
                                                    source="fotmob", source_url=url, fetched_at=now()))


def ensure_player(con, fm_id, name, country=None, code=None, club_id=None, url=None):
    return upsert(con, "players", ["fotmob_id"], dict(
        fotmob_id=int(fm_id), name=name, nationality=country, nationality_code=code, current_club_id=club_id,
        source="fotmob", source_url=url, fetched_at=now()))


def ensure_coach(con, c, team, url):
    if not c or not c.get("name"):
        return None
    return upsert(con, "coaches", ["name"], dict(name=c["name"], fotmob_id=c.get("id"), nationality=c.get("countryName"),
                                                 team=team, source="fotmob", source_url=url, fetched_at=now()))


# ---------------------------------------------------------------- match ingest
def get_match(mid: str, url: str, alt: str | None = None, ttl=None) -> tuple[dict, str]:
    """Fetch a match page by id; fall back to the slug URL for ids /match/<id> can't resolve."""
    try:
        return next_data(url, ttl=ttl), url
    except http.NotFound:
        if not alt:
            raise
        nd = next_data(alt, ttl=ttl)
        if str(nd["props"]["pageProps"]["general"]["matchId"]) != str(mid):
            raise
        return nd, alt


def ingest_match(con, nd: dict, scope: str, url: str) -> dict:
    """Parse one FotMob match page and upsert match/lineups/events/club-minutes. Returns stats."""
    pp = nd["props"]["pageProps"]
    g, hdr, c = pp["general"], pp["header"], pp["content"]
    want = re.search(r"/match/(\d+)", url)
    if want and str(g["matchId"]) != want.group(1):
        raise ValueError(f"FotMob returned match {g['matchId']} for requested {want.group(1)}")
    st = hdr["status"]
    ts = fetched = now()
    home, away = g["homeTeam"], g["awayTeam"]
    hid, aid = int(home["id"]), int(away["id"])
    kind = "national" if scope == "national" else "club"
    if kind == "national":
        h_t, a_t = ensure_team(con, hid, home["name"], kind, url=url), ensure_team(con, aid, away["name"], kind, url=url)
    else:
        h_t = ensure_club(con, hid, home["name"], "EGY", url) and con.execute("SELECT id FROM teams WHERE fotmob_id=?", (hid,)).fetchone()[0]
        a_t = ensure_club(con, aid, away["name"], "EGY", url) and con.execute("SELECT id FROM teams WHERE fotmob_id=?", (aid,)).fetchone()[0]
    team_db = {hid: h_t, aid: a_t}

    teams = hdr["teams"]
    hs, as_ = teams[0].get("score"), teams[1].get("score")
    reason = (st.get("reason") or {}).get("short", "")
    pens = (st.get("reason") or {}).get("penalties")
    decided = "aet" if reason == "AET" else "pens" if reason in ("Pen", "PEN") else None
    info = c.get("matchFacts", {}).get("infoBox") or {}
    stad = info.get("Stadium") or {}
    tourn = info.get("Tournament") or {}
    is_egypt = EGYPT_ID in (hid, aid)
    egypt_side = ("home" if hid == EGYPT_ID else "away") if is_egypt else None

    neutral = None
    if scope == "national" and stad.get("country"):
        sc = stad["country"].lower()
        host = None
        if sc in home["name"].lower() or home["name"].lower() in sc:
            host = "home"
        elif sc in away["name"].lower() or away["name"].lower() in sc:
            host = "away"
        neutral = 0 if host else 1
    elif scope == "egyptian_league":
        neutral = 0

    lu = c.get("lineup") or {}
    lh, la = lu.get("homeTeam") or {}, lu.get("awayTeam") or {}
    fh, fa = lh.get("formation"), la.get("formation")
    coach_id = None
    if is_egypt:
        ec = (lh if egypt_side == "home" else la).get("coach")
        coach_id = ensure_coach(con, ec, "Egypt", url)
    date = st["utcTime"][:10]

    row = dict(
        source="fotmob", source_match_id=str(g["matchId"]), scope=scope, date=date, kickoff_utc=st["utcTime"],
        competition=g.get("leagueName"), stage=STAGES.get(str(g.get("leagueRoundName") or tourn.get("roundName") or "").lower(), g.get("leagueRoundName") or tourn.get("roundName")), season=season_of(date),
        home_team_id=h_t, away_team_id=a_t, home_score=hs, away_score=as_, decided_by=decided, pens_home=pens[0] if pens else None, pens_away=pens[1] if pens else None,
        venue=stad.get("name"), venue_city=stad.get("city"), venue_country=stad.get("country"), neutral=neutral,
        is_egypt=1 if is_egypt else 0, egypt_side=egypt_side, egypt_coach_id=coach_id, coach_source="fotmob" if coach_id else None,
        formation_home=fh, formation_away=fa,
        formation_egypt=(fh if egypt_side == "home" else fa) if is_egypt else None,
        formation_opp=(fa if egypt_side == "home" else fh) if is_egypt else None,
        source_url=url, fetched_at=fetched,
    )
    match_id = upsert(con, "matches", ["source", "source_match_id"], row)
    # children are replaced wholesale per match: player-id resolution for legacy lineups can improve between
    # runs, so upserting alone could leave stale rows behind. Delete + reinsert keeps reruns idempotent.
    con.execute("DELETE FROM lineups WHERE match_id=?", (match_id,))
    con.execute("DELETE FROM events_lite WHERE match_id=?", (match_id,))
    if scope != "national":
        con.execute("DELETE FROM player_club_minutes WHERE source_match_id=? AND source='fotmob'", (str(g["matchId"]),))

    # duration
    dur = 120 if decided in ("aet", "pens") else 90  # nominal; stoppage time not modelled

    # events (raw)
    evs = (c.get("matchFacts", {}).get("events") or {}).get("events") or []
    reds = {}  # player_id -> minute
    for e in evs:
        if e.get("type") == "Card" and e.get("card") in ("Red", "YellowRed") and e.get("playerId"):
            reds[int(e["playerId"])] = e["time"]

    # lineups. Older ("simple") FotMob lineups have names only (no ids/formation/coach), so ids are recovered
    # from the events feed (goals/cards/subs carry ids) or by unique normalised-name match in our players table.
    stats = dict(lineups=0, events=0)
    name_ids: dict[str, int] = {}
    for e in evs:
        pl = e.get("player") or {}
        if pl.get("id") and pl.get("name"):
            name_ids[norm(pl["name"])] = int(pl["id"])
        for sw in e.get("swap") or []:
            if sw.get("id") and sw.get("name"):
                name_ids[norm(sw["name"])] = int(sw["id"])

    def resolve(p, tid):
        """Return (fotmob_id or None, player db id)."""
        club_id = None
        if scope == "national" and p.get("primaryTeamId"):
            club_id = ensure_club(con, p["primaryTeamId"], p.get("primaryTeamName") or "?", None, url)
        elif scope != "national":
            club_id = con.execute("SELECT id FROM clubs WHERE fotmob_id=?", (tid,)).fetchone()[0]
        nm = p.get("name") or f"{p.get('firstName','')} {p.get('lastName','')}".strip()
        fid = p.get("id") or name_ids.get(norm(nm))
        if fid:
            return int(fid), ensure_player(con, fid, nm, p.get("countryName"), p.get("countryCode"),
                                           club_id if scope == "national" else None, url)
        rows = con.execute("SELECT id FROM players WHERE fotmob_id IS NOT NULL AND lower(name)=lower(?)", (nm,)).fetchall()
        if len(rows) != 1:
            rows = [r for r in con.execute("SELECT id, name FROM players WHERE fotmob_id IS NOT NULL") if norm(r["name"]) == norm(nm)]
        if len(rows) == 1:
            return con.execute("SELECT fotmob_id FROM players WHERE id=?", (rows[0][0],)).fetchone()[0], rows[0][0]
        r = con.execute("SELECT id FROM players WHERE fotmob_id IS NULL AND statsbomb_id IS NULL AND source='fotmob-legacy' AND name=?", (nm,)).fetchone()
        if r:
            return None, r[0]
        cur = con.execute("INSERT INTO players(name, nationality, source, source_url, fetched_at) VALUES (?,?,?,?,?)",
                          (nm, p.get("countryName"), "fotmob-legacy", url, fetched))
        return None, cur.lastrowid

    rosters = {}
    for side, L, tid in (("home", lh, hid), ("away", la, aid)):
        rosters[side] = []
        for grp, started in (("starters", 1), ("subs", 0)):
            for p in L.get(grp) or []:
                fid, dbp = resolve(p, tid)
                rosters[side].append(dict(p=p, fid=fid, dbp=dbp, started=started, side=side, tid=tid, key=norm(p.get("name") or "")))
    pl_db: dict[int, int] = {r["fid"]: r["dbp"] for v in rosters.values() for r in v if r["fid"]}
    entered: dict[int, int] = {}

    # sub in/out per player from the events feed (works for both lineup styles)
    ev_on: dict[str, int] = {}
    ev_off: dict[str, int] = {}
    for side in ("home", "away"):
        pitch = {r["key"] for r in rosters[side] if r["started"]}
        idkey = {str(r["fid"]): r["key"] for r in rosters[side] if r["fid"]}
        for e in sorted((x for x in evs if x.get("type") == "Substitution" and x.get("swap") and len(x["swap"]) == 2
                         and bool(x.get("isHome")) == (side == "home")), key=lambda x: x["time"]):
            k = [idkey.get(str(sw.get("id"))) or norm(sw.get("name")) for sw in e["swap"]]
            if k[0] in pitch:
                out, inn = k[0], k[1]
            elif k[1] in pitch:
                out, inn = k[1], k[0]
            else:
                inn, out = k[0], k[1]
            pitch.discard(out); pitch.add(inn)
            ev_on[inn] = e["time"]; ev_off[out] = e["time"]

    sub_info = {}
    for side in ("home", "away"):
        has_perf = any((r["p"].get("performance") or {}).get("substitutionEvents") for r in rosters[side])
        has_bench = any(not r["started"] for r in rosters[side])
        sub_info[side] = has_perf or bool(ev_on) or bool(ev_off) or not has_bench
    for side in ("home", "away"):
        for r in rosters[side]:
            p, started, fid, dbp, tid = r["p"], r["started"], r["fid"], r["dbp"], r["tid"]
            se = (p.get("performance") or {}).get("substitutionEvents") or []
            on = next((x["time"] for x in se if x["type"] == "subIn"), None)
            off = next((x["time"] for x in se if x["type"] == "subOut"), None)
            if on is None and off is None:
                on, off = ev_on.get(r["key"]), ev_off.get(r["key"])
            if off is None and fid and fid in reds:
                off = reds[fid]
            if started and on is not None:
                on = None  # a starter cannot come on
            if on is not None and fid:
                entered[fid] = on
            mins = (off if off is not None else dur) if started else ((off if off is not None else dur) - on if on is not None else 0)
            if not sub_info[side]:
                mins, on, off = None, None, None  # feed has a bench but no substitution data: minutes unknown
            upsert(con, "lineups", ["match_id", "player_id"], dict(
                match_id=match_id, player_id=dbp, team_id=team_db[tid], team_side=side, started=started,
                position=str(p.get("positionId") if p.get("positionId") not in (None, -1) else (p.get("usualPlayingPositionId") if p.get("usualPlayingPositionId") not in (None, -1) else "")),
                slot=str(p.get("positionId")) if p.get("positionId") not in (None, -1) else None,
                shirt=p.get("shirtNumber"), minutes_played=None if mins is None else max(mins, 0), sub_on_minute=on, sub_off_minute=off,
                captain=1 if p.get("isCaptain") else 0, rating=(p.get("performance") or {}).get("rating"),
                source="fotmob", source_url=url, fetched_at=fetched))
            stats["lineups"] += 1
            if scope != "national":
                club_id = con.execute("SELECT id FROM clubs WHERE fotmob_id=?", (tid,)).fetchone()[0]
                upsert(con, "player_club_minutes", ["player_id", "source_match_id"], dict(
                    player_id=dbp, club_id=club_id, season=season_of(date), competition=g.get("leagueName"),
                    competition_id=g.get("leagueId"), date=date, source_match_id=str(g["matchId"]),
                    opponent=(away if side == "home" else home)["name"], minutes=None if mins is None else int(round(max(mins, 0))),
                    started=started, source="fotmob", source_url=url, fetched_at=fetched))

    def side_team(is_home):
        return team_db[hid if is_home else aid]

    # shots keyed for goal join
    shots = (c.get("shotmap") or {}).get("shots") or []
    shot_by_pm = {}
    for s in shots:
        if s.get("eventType") == "Goal" and not s.get("isOwnGoal"):
            shot_by_pm[(s.get("playerId"), s.get("min"))] = s

    entered_or_started = {r["fid"] for v in rosters.values() for r in v if r["started"] and r["fid"]}

    def pdb(pid):
        return pl_db.get(int(pid)) if pid else None

    def ev(key, **kw):
        upsert(con, "events_lite", ["match_id", "event_key"], dict(match_id=match_id, event_key=key, source="fotmob",
                                                                    source_url=url, fetched_at=fetched, **kw))
        stats["events"] += 1

    for e in evs:
        t = e.get("type")
        if t == "Goal":
            if e.get("isPenaltyShootoutEvent"):
                continue
            pid = e.get("playerId") or (e.get("player") or {}).get("id")
            shot = shot_by_pm.get((pid, e.get("time")))
            desc = (e.get("goalDescriptionKey") or "").lower() + (e.get("goalDescription") or "").lower()
            if e.get("ownGoal"):
                typ, team = "own_goal", side_team(not e.get("isHome"))
            else:
                typ = "pen_goal" if ("pen" in desc or (shot and shot.get("situation") == "Penalty")) else "goal"
                team = side_team(e.get("isHome"))
            sit = "penalty" if typ == "pen_goal" else SITUATION.get(shot.get("situation"), "unknown") if shot else "unknown"
            ev(f"goal:{e.get('eventId') or e['time']}:{pid}", minute=e["time"], added_minute=e.get("overloadTime") or None,
               type=typ, team_id=team, player_id=pdb(pid), related_player_id=pdb(e.get("assistPlayerId")) if e.get("assistPlayerId") else None,
               situation=sit, outcome="goal", xg=shot.get("expectedGoals") if shot else None)
        elif t == "Card":
            card = e.get("card")
            typ = "yellow" if card == "Yellow" else "red" if card in ("Red", "YellowRed") else None
            if typ:
                ev(f"card:{e.get('eventId') or e['time']}:{e.get('playerId')}:{card}", minute=e["time"],
                   added_minute=e.get("overloadTime") or None, type=typ, team_id=side_team(e.get("isHome")),
                   player_id=pdb(e.get("playerId")), related_player_id=None, situation=None, outcome=card, xg=None)
        elif t == "Substitution" and e.get("swap") and len(e["swap"]) == 2:
            a, b = int(e["swap"][0]["id"]), int(e["swap"][1]["id"])
            if entered.get(a) == e["time"]:
                pin, pout = a, b
            elif entered.get(b) == e["time"]:
                pin, pout = b, a
            elif a in entered_or_started or a in entered:
                pin, pout = b, a
            else:
                pin, pout = a, b
            tm = side_team(e.get("isHome"))
            ev(f"subon:{e['time']}:{pin}", minute=e["time"], type="sub_on", team_id=tm, player_id=pdb(pin),
               related_player_id=pdb(pout), situation=None, outcome=None, xg=None, added_minute=None)
            ev(f"suboff:{e['time']}:{pout}", minute=e["time"], type="sub_off", team_id=tm, player_id=pdb(pout),
               related_player_id=pdb(pin), situation=None, outcome=None, xg=None, added_minute=None)

    for s in shots:
        if s.get("isOwnGoal"):
            continue
        pid = s.get("playerId")
        if pid and int(pid) not in pl_db:
            pl_db[int(pid)] = ensure_player(con, pid, s.get("playerName") or "?", url=url)
        ev(f"shot:{s['id']}", minute=s.get("min"), added_minute=s.get("minAdded"), type="shot",
           team_id=team_db.get(s.get("teamId")), player_id=pdb(pid), related_player_id=None,
           situation=SITUATION.get(s.get("situation"), "unknown"), outcome=OUTCOME.get(s.get("eventType"), s.get("eventType")),
           xg=s.get("expectedGoals"))

    con.execute("UPDATE matches SET has_lineup=?, has_events=? WHERE id=?",
                (1 if stats["lineups"] else 0, 1 if (evs or shots) else 0, match_id))
    stats["match_id"] = match_id
    stats["shots"] = len(shots)
    return stats


# ---------------------------------------------------------------- player pages
def ingest_player_page(con, fm_id: int, player_db_id: int, since_season_start="2025-07-01", log=print):
    """Enrich player (dob/position/nationality) and store club match-level minutes (club matches only)."""
    url = f"{BASE}/players/{fm_id}/x"
    d = next_data(url, ttl=24 * 3600)
    dd = d["props"]["pageProps"]["data"]
    ts = now()
    pos = (dd.get("positionDescription") or {}).get("primaryPosition") or {}
    info = {x["title"]: x["value"].get("fallback") for x in dd.get("playerInformation") or [] if isinstance(x.get("value"), dict)}
    dob = ((dd.get("birthDate") or {}).get("utcTime") or "")[:10] or None
    pt = dd.get("primaryTeam") or {}
    club_id = ensure_club(con, pt["teamId"], pt.get("teamName") or "?", None, url) if pt.get("teamId") else None
    con.execute("UPDATE players SET name=?, dob=COALESCE(?,dob), position=COALESCE(?,position), nationality=COALESCE(?,nationality),"
                " current_club_id=COALESCE(?,current_club_id), source_url=?, fetched_at=? WHERE id=?",
                (dd["name"], dob, pos.get("label"), info.get("Country"), club_id, url, ts, player_db_id))
    nat_ids = {e["teamId"] for e in ((dd.get("careerHistory") or {}).get("careerItems") or {}).get("national team", {}).get("teamEntries", [])}
    n = 0
    for m in dd.get("recentMatches") or []:
        if m["teamId"] in nat_ids or m["teamId"] == EGYPT_ID:
            continue  # national-team appearances live in matches/lineups
        date = m["matchDate"]["utcTime"][:10]
        if date < since_season_start:
            continue
        cid = ensure_club(con, m["teamId"], m["teamName"], None, url)
        upsert(con, "player_club_minutes", ["player_id", "source_match_id"], dict(
            player_id=player_db_id, club_id=cid, season=season_of(date), competition=m.get("leagueName"),
            competition_id=m.get("leagueId"), date=date, source_match_id=str(m["id"]), opponent=m.get("opponentTeamName"),
            minutes=m.get("minutesPlayed") or 0, started=None,
            goals=m.get("goals"), assists=m.get("assists"), yellow=m.get("yellowCards"), red=m.get("redCards"),
            source="fotmob", source_url=url, fetched_at=ts))
        n += 1
    return n
