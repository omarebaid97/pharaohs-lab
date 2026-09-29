"""FotMob helpers for opponent dossiers (module D).

Public web pages only (`__NEXT_DATA__`), fetched through the shared polite client. Nothing is written to the DB
here: functions return plain dicts and the model persists them in its own `opp_*` tables.
"""
from __future__ import annotations

from datetime import date, datetime

from .. import http
from . import fotmob

BASE = fotmob.BASE
LONG_TTL = 7 * 86400   # finished matches do not change
SHORT_TTL = 3600       # upcoming fixtures can move


def _mid(link: str) -> str:
    return link.split("#")[-1]


def group_of(pos_id) -> str:
    """FotMob pitch slot -> coarse group. 11 = GK, 3x = back line, 5x-9x = midfield rows, 10x = front row."""
    try:
        p = int(pos_id)
    except (TypeError, ValueError):
        return "?"
    if p < 20:
        return "GK"
    if p < 50:
        return "DEF"
    if p < 100:
        return "MID"
    return "FWD"


def squad_info(team_page: dict) -> dict:
    """Coach + player position descriptors from the team page squad tab."""
    out = {"coach": None, "players": {}}
    for grp in (team_page.get("squad") or {}).get("squad") or []:
        for m in grp.get("members") or []:
            if grp.get("title") == "coach":
                out["coach"] = dict(name=m.get("name"), nationality=m.get("cname"), age=m.get("age"))
            else:
                out["players"][int(m["id"])] = dict(name=m.get("name"), pos=m.get("positionIdsDesc"), club=m.get("cname"),
                                                     age=m.get("age"), shirt=m.get("shirtNumber"))
    return out


def finished_from_team_page(team_id: int, slug: str):
    """(finished fixtures [{mid,date,url}], team page dict)."""
    fx, t = fotmob.team_fixtures(team_id, slug, ttl=6 * 3600)
    out = []
    for f in fx:
        st = f["status"]
        if st.get("finished") and not st.get("cancelled"):
            out.append(dict(mid=_mid(f["pageUrl"]), date=st["utcTime"][:10], url=f"{BASE}/match/{_mid(f['pageUrl'])}"))
    return out, t


def walk_back(team_id: int, slug: str, since: str, min_n: int = 10, cap: int = 16):
    """Collect the team's latest finished matches: team-page list, then each earliest match page's teamForm (5 previous)."""
    fin, page = finished_from_team_page(team_id, slug)
    found = {m["mid"]: m for m in fin}
    expanded: set[str] = set()
    while True:
        cur = sorted(found.values(), key=lambda m: m["date"])
        need = len(cur) < min_n or cur[0]["date"] >= since
        if not need or len(cur) >= cap:
            break
        todo = [m for m in cur if m["mid"] not in expanded]
        if not todo:
            break
        m = todo[0]
        expanded.add(m["mid"])
        try:
            nd = fotmob.next_data(m["url"], ttl=LONG_TTL)
        except (http.NotFound, http.Blocked):
            continue
        forms = nd["props"]["pageProps"]["content"]["matchFacts"].get("teamForm") or []
        for lst in forms:
            if not any((e.get("home", {}).get("isOurTeam") and e["home"].get("id") == str(team_id))
                       or (e.get("away", {}).get("isOurTeam") and e["away"].get("id") == str(team_id)) for e in lst):
                continue
            for e in lst:
                dt = e["date"]["utcTime"][:10]
                mid = _mid(e["linkToMatch"])
                if mid in found or any(v["date"] == dt for v in found.values()):
                    continue
                found[mid] = dict(mid=mid, date=dt, url=f"{BASE}/match/{mid}")
    return sorted(found.values(), key=lambda m: m["date"], reverse=True), page


def parse_match(nd: dict, team_id: int, url: str) -> dict | None:
    """One finished match from `team_id`'s point of view. None if not finished."""
    pp = nd["props"]["pageProps"]
    g, hdr, c = pp["general"], pp["header"], pp["content"]
    st = hdr["status"]
    if not st.get("finished") or st.get("cancelled"):
        return None
    home, away = g["homeTeam"], g["awayTeam"]
    hid, aid = int(home["id"]), int(away["id"])
    side = "home" if hid == team_id else "away" if aid == team_id else None
    if side is None:
        return None
    hs, as_ = hdr["teams"][0].get("score"), hdr["teams"][1].get("score")
    gf, ga = (hs, as_) if side == "home" else (as_, hs)
    reason = (st.get("reason") or {}).get("short", "")
    pens = (st.get("reason") or {}).get("penalties")
    decided = "aet" if reason == "AET" else "pens" if reason in ("Pen", "PEN") else None
    res = "W" if gf > ga else "L" if gf < ga else "D"
    shoot = None
    if decided == "pens" and pens:
        pf, pa = (pens[0], pens[1]) if side == "home" else (pens[1], pens[0])
        shoot = f"{pf}-{pa}"
    info = (c.get("matchFacts") or {}).get("infoBox") or {}
    stad = info.get("Stadium") or {}
    lu = c.get("lineup") or {}
    lt = (lu.get("homeTeam") if side == "home" else lu.get("awayTeam")) or {}
    lo = (lu.get("awayTeam") if side == "home" else lu.get("homeTeam")) or {}
    dur = 120 if decided in ("aet", "pens") else 90
    evs = ((c.get("matchFacts") or {}).get("events") or {}).get("events") or []
    is_home_side = side == "home"

    # ---- lineup with minutes
    starters = lt.get("starters") or []
    subs = lt.get("subs") or []
    pitch = {int(p["id"]) for p in starters if p.get("id")}
    ev_on: dict[int, int] = {}
    ev_off: dict[int, int] = {}
    for e in sorted((x for x in evs if x.get("type") == "Substitution" and x.get("swap") and len(x["swap"]) == 2
                     and bool(x.get("isHome")) == is_home_side), key=lambda x: x["time"]):
        a, b = int(e["swap"][0]["id"]), int(e["swap"][1]["id"])
        if a in pitch:
            out, inn = a, b
        elif b in pitch:
            out, inn = b, a
        else:
            inn, out = a, b
        pitch.discard(out)
        pitch.add(inn)
        ev_on[inn] = e["time"]
        ev_off[out] = e["time"]
    reds = {int(e["playerId"]): e["time"] for e in evs if e.get("type") == "Card" and e.get("card") in ("Red", "YellowRed") and e.get("playerId")}
    has_lineup = bool(starters)
    players = []
    for started, lst in ((1, starters), (0, subs)):
        for p in lst:
            if not p.get("id"):
                continue
            pid = int(p["id"])
            se = (p.get("performance") or {}).get("substitutionEvents") or []
            on = next((x["time"] for x in se if x["type"] == "subIn"), None)
            off = next((x["time"] for x in se if x["type"] == "subOut"), None)
            if on is None and off is None:
                on, off = ev_on.get(pid), ev_off.get(pid)
            if off is None and pid in reds:
                off = reds[pid]
            if started:
                on = None
                mins = off if off is not None else dur
            else:
                mins = ((off if off is not None else dur) - on) if on is not None else 0
            players.append(dict(fm_id=pid, name=p.get("name"), pos_id=p.get("positionId"), pos_group=group_of(p.get("positionId")),
                                usual_pos=p.get("usualPlayingPositionId"), club=p.get("primaryTeamName"), started=started,
                                minutes=max(int(mins), 0), rating=(p.get("performance") or {}).get("rating"),
                                captain=1 if p.get("isCaptain") else 0))

    # ---- goals with situation (needs shotmap)
    shots = (c.get("shotmap") or {}).get("shots") or []
    has_shotmap = bool(shots)
    shot_by = {(s.get("playerId"), s.get("min")): s for s in shots if s.get("eventType") == "Goal" and not s.get("isOwnGoal")}
    goals = []
    for e in evs:
        if e.get("type") != "Goal" or e.get("isPenaltyShootoutEvent"):
            continue
        own = bool(e.get("ownGoal"))
        scorer_home = bool(e.get("isHome"))
        benef_home = (not scorer_home) if own else scorer_home
        pid = e.get("playerId") or (e.get("player") or {}).get("id")
        sh = shot_by.get((pid, e.get("time")))
        desc = ((e.get("goalDescriptionKey") or "") + (e.get("goalDescription") or "")).lower()
        if own:
            sit = "own_goal"
        elif "pen" in desc or (sh and sh.get("situation") == "Penalty"):
            sit = "penalty"
        elif sh:
            sit = fotmob.SITUATION.get(sh.get("situation"), "unknown")
        else:
            sit = "unknown"
        goals.append(dict(minute=e["time"], added=e.get("overloadTime") or None, for_team=1 if benef_home == is_home_side else 0,
                          situation=sit, player=(e.get("player") or {}).get("name") or e.get("nameStr"), has_shotmap=has_shotmap,
                          xg=sh.get("expectedGoals") if sh else None))

    coach = lt.get("coach") or {}
    opp = away if side == "home" else home
    return dict(
        mid=str(g["matchId"]), url=url, date=st["utcTime"][:10], utc=st["utcTime"], competition=g.get("leagueName"),
        side=side, opponent=opp["name"], opponent_id=int(opp["id"]), gf=gf, ga=ga, result=res, decided_by=decided, shootout=shoot,
        neutral_hint=None, venue=stad.get("name"), venue_country=stad.get("country"),
        formation=lt.get("formation"), opp_formation=lo.get("formation"),
        coach=coach.get("name"), coach_id=coach.get("id"), has_lineup=has_lineup, has_shotmap=has_shotmap,
        players=players, goals=goals,
    )


def fetch_team_matches(team_id: int, slug: str, since: str, min_n: int = 10, cap: int = 16, log=print):
    """Return (matches newest-first, squad_info, team_page)."""
    found, page = walk_back(team_id, slug, since, min_n, cap)
    out = []
    for m in found:
        try:
            nd = fotmob.next_data(m["url"], ttl=LONG_TTL)
            pm = parse_match(nd, team_id, m["url"])
        except (http.NotFound, http.Blocked, KeyError) as e:
            log(f"    skip {m['url']}: {e!r}")
            continue
        if pm:
            out.append(pm)
    out.sort(key=lambda x: x["date"], reverse=True)
    return out, squad_info(page), page


def fixture_detail(mid: str) -> dict:
    """Venue / date flags of a (possibly upcoming) match page."""
    nd = fotmob.next_data(f"{BASE}/match/{mid}", ttl=SHORT_TTL)
    c = nd["props"]["pageProps"]["content"]
    info = (c.get("matchFacts") or {}).get("infoBox") or {}
    stad = info.get("Stadium") or {}
    md = info.get("Match Date") or {}
    tour = info.get("Tournament") or {}
    return dict(venue=stad.get("name"), city=stad.get("city"), country=stad.get("country"),
                date_tbd=bool(md.get("matchDateTbd")), round=tour.get("roundName"),
                h2h_summary=(c.get("h2h") or {}).get("summary"),
                h2h_matches=[dict(date=x["time"]["utcTime"][:10], comp=(x.get("league") or {}).get("name"),
                                  home=x["home"]["name"], away=x["away"]["name"], score=(x.get("status") or {}).get("scoreStr"))
                             for x in (c.get("h2h") or {}).get("matches") or []])
