"""Module F: international-window load tracker (club load + travel going into each Egypt window).

Run:  .venv/bin/python -m etl.models.load_tracker [--no-fetch]

Writes data/export/load_tracker.json and data/export/load_tracker_summary.md.
Own tables (prefix load_) are created here and rebuilt on every run; nothing else in the ETL is modified.
HTTP goes through etl.http (FotMob player pages via etl.sources.fotmob.next_data, Wikipedia API).
Descriptive only: the back-test makes no causal claims.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .. import db, http
from ..sources import fotmob
from ..sources.geo_wikidata import haversine_km

ROOT = db.ROOT
EXPORT = ROOT / "data" / "export"
MANUAL = ROOT / "data" / "manual"
AS_OF = date(2026, 9, 28)
CURRENT_FIRST = "2026-09-25"           # first match of the current window
REFRESH_TTL = 3 * 86400                # refresh FotMob player pages older than 3 days (current-window players only)
WINDOW_GAP_DAYS = 10                   # matches more than this many days apart start a new window
COVERAGE_FLOOR = "2025-08-08"          # earliest date of the club-minutes data set
WIKI_SQUAD_URL = "https://en.wikipedia.org/wiki/Egypt_national_football_team#Current_squad"

THRESHOLDS = {
    "high_load": "club minutes in the 14 days before the first window match >= 270, OR >= 3 club matches played in those 14 days",
    "short_rest": "days between the last club match played and the first window match < 3 (calendar-day difference, kickoff times ignored)",
    "long_haul": "any leg of the window route >= 5000 km great-circle, OR any leg with |time-zone shift| >= 3 h "
                 "(legs: club city -> first venue, then venue -> venue in match order)",
    "low_rhythm": "club minutes in the 30 days before the first window match < 90",
}
SCORE_POINTS = {"high_load": 2.0, "short_rest": 2.0, "long_haul": 1.5, "low_rhythm": 1.5}
SCORE_TEXT = ("combined_score = 2.0*high_load + 2.0*short_rest + 1.5*long_haul + 1.5*low_rhythm (0 to 7). Flags that cannot be "
              "evaluated (coverage too short) count 0. Ties are broken by club minutes in the last 14 days. It is a ranking aid, "
              "not a fitted model.")

SCHEMA = """
CREATE TABLE IF NOT EXISTS load_windows (
  window_id TEXT PRIMARY KEY, start_date TEXT, end_date TEXT, kind TEXT, n_matches INTEGER, first_match_date TEXT,
  status TEXT, squad_source TEXT, route_json TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS load_club_matches (
  player_id INTEGER, source_match_id TEXT, date TEXT, club_fotmob_id INTEGER, club_name TEXT, competition TEXT,
  minutes INTEGER, source TEXT, fetched_at TEXT,
  PRIMARY KEY (player_id, source_match_id)
);
CREATE TABLE IF NOT EXISTS load_player_horizon (
  player_id INTEGER PRIMARY KEY, horizon_date TEXT, n_matches_listed INTEGER, source TEXT, page_fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS load_player_window (
  window_id TEXT, player_key TEXT, player_id INTEGER, name TEXT, position TEXT, in_matchday_squad INTEGER,
  in_published_squad INTEGER, club TEXT, club_source TEXT, club_city TEXT, first_match_date TEXT,
  coverage TEXT, cov_days INTEGER, min7 INTEGER, min14 INTEGER, min30 INTEGER, matches14 INTEGER,
  last_club_match TEXT, rest_days INTEGER, route_km REAL, max_leg_km REAL, max_tz_shift REAL,
  high_load INTEGER, short_rest INTEGER, long_haul INTEGER, low_rhythm INTEGER, score REAL,
  intl_minutes INTEGER, intl_starts INTEGER, intl_matches_known INTEGER, intl_matches INTEGER,
  PRIMARY KEY (window_id, player_key)
);
"""


# ------------------------------------------------------------------ static lookups
def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class Geo:
    def __init__(self):
        self.clubs_by_fm, self.clubs_by_name = {}, {}
        for r in read_csv(MANUAL / "club_cities.csv"):
            if r.get("fotmob_id"):
                self.clubs_by_fm[str(r["fotmob_id"])] = r
            self.clubs_by_name[db.norm(r["club"])] = r
        self.venues = {}
        for r in read_csv(MANUAL / "venues.csv"):
            r["city"] = re.sub(r".*\((.*)\)", r"\1", r["venue_city"]).strip()   # 'al-Qahirah (Cairo)' -> 'Cairo'
            self.venues[(db.norm(r["venue_city"]), db.norm(r["venue_country"]))] = r

    def club(self, fm_id, name):
        return self.clubs_by_fm.get(str(fm_id)) or self.clubs_by_name.get(db.norm(name))

    def venue(self, city, country):
        return self.venues.get((db.norm(city), db.norm(country)))


def utc_offset_h(tz: str, d: str) -> float:
    dt = datetime.fromisoformat(d + "T12:00:00").replace(tzinfo=ZoneInfo(tz))
    return dt.utcoffset().total_seconds() / 3600


# ------------------------------------------------------------------ windows
def kind_of(comp: str) -> str:
    l = (comp or "").lower()
    if "qualif" in l or "friend" in l:
        return "fifa_window"
    if "africa cup of nations" in l or "world cup" in l or "arab cup" in l:
        return "tournament"
    return "fifa_window"


def hassan_start(conn) -> str:
    r = conn.execute("SELECT tenure_start FROM coaches WHERE name='Hossam Hassan'").fetchone()
    return r[0] if r and r[0] else "2024-02-06"


def build_windows(conn) -> list[dict]:
    start = hassan_start(conn)
    ms = []
    for r in conn.execute("SELECT id,date,competition,venue,venue_city,venue_country FROM matches WHERE is_egypt=1 AND date>=? ORDER BY date,id", (start,)):
        ms.append(dict(match_id=r["id"], date=r["date"], competition=r["competition"], venue=r["venue"], city=r["venue_city"],
                       country=r["venue_country"], status="played" if r["date"] <= AS_OF.isoformat() else "scheduled", src="matches"))
    last = max(m["date"] for m in ms)
    have = {m["date"] for m in ms}
    for r in conn.execute("SELECT * FROM opp_fixtures WHERE date>? AND COALESCE(finished,0)=0 ORDER BY date", (last,)):
        if r["date"] in have or r["date"] > (date.fromisoformat(last) + timedelta(days=WINDOW_GAP_DAYS)).isoformat():
            continue
        ms.append(dict(match_id=None, date=r["date"], competition=r["competition"], venue=r["venue"], city=r["city"],
                       country=r["country"], status="scheduled", src="opp_fixtures"))
        last = r["date"]
    ms.sort(key=lambda m: m["date"])
    wins, cur = [], []
    for m in ms:
        if cur and ((date.fromisoformat(m["date"]) - date.fromisoformat(cur[-1]["date"])).days > WINDOW_GAP_DAYS
                    or kind_of(m["competition"]) != kind_of(cur[-1]["competition"])):
            wins.append(cur)
            cur = []
        cur.append(m)
    if cur:
        wins.append(cur)
    out = []
    for w in wins:
        out.append(dict(window_id="W" + w[0]["date"], start=w[0]["date"], end=w[-1]["date"], kind=kind_of(w[0]["competition"]),
                        first=w[0]["date"], matches=w,
                        status="current" if w[0]["date"] == CURRENT_FIRST else ("past" if w[-1]["date"] < CURRENT_FIRST else "upcoming")))
    return out


# ------------------------------------------------------------------ squads
def wiki_squad() -> tuple[list[dict], str | None]:
    """Parse '=== Current squad ===' of the Wikipedia Egypt national team article (called-up list)."""
    try:
        t = http.get("https://en.wikipedia.org/w/api.php", params={"action": "parse", "page": "Egypt national football team", "prop": "wikitext",
                                                                     "format": "json", "formatversion": 2, "redirects": 1}, ttl=6 * 3600)
        w = json.loads(t)["parse"]["wikitext"]
        a, b = w.index("===Current squad==="), w.index("===Recent call-ups===")
    except Exception as e:  # noqa: BLE001
        return [], f"wikipedia squad unavailable: {e}"
    sec = re.sub(r"<ref.*?</ref>", "", w[a:b], flags=re.S)
    head = re.search(r"called up for the (.*?)\.\n", sec)
    out = []
    for line in sec.split("\n"):
        if not line.startswith("{{nat fs g player"):
            continue
        nm = re.search(r"name=(?:\[\[([^\]]*)\]\]|([^|}]*))", line)
        n = (nm.group(1) or nm.group(2)).split("|")[-1].strip()
        pos = re.search(r"pos=(\w+)", line)
        dob = re.search(r"birth date and age\|df=yes\|(\d+)\|(\d+)\|(\d+)", line)
        cl = re.search(r"club=(?:\[\[([^\]]*)\]\]|([^|}]*))", line)
        out.append(dict(name=n, pos=pos.group(1) if pos else None,
                        dob="%s-%02d-%02d" % tuple(map(int, dob.groups())) if dob else None,
                        club=(cl.group(1) or cl.group(2)).split("|")[-1].strip() if cl else None))
    txt = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", head.group(1)) if head else None
    return out, txt


ALIASES = {"el mahdy soliman": "al mahdi soliman"}


def match_player(conn, name: str, dob: str | None, prefer_ids: set[int]):
    """Wikipedia name -> players row (has fotmob_id, not diaspora_scout). Prefer players in Egypt matchday lineups."""
    n = ALIASES.get(db.norm(name), db.norm(name))
    cands = [r for r in conn.execute("SELECT id,name,dob,fotmob_id,position,current_club_id FROM players WHERE COALESCE(source,'')!='diaspora_scout'")
             if db.norm(r["name"]) == n]
    if not cands:
        return None
    cands.sort(key=lambda r: (r["id"] not in prefer_ids, r["fotmob_id"] is None, r["dob"] != dob, r["id"]))
    return cands[0]


def egypt_lineup(conn, match_ids):
    q = ",".join("?" * len(match_ids))
    return conn.execute(
        f"SELECT l.match_id,l.player_id,l.started,l.minutes_played,l.sub_on_minute,l.sub_off_minute,p.name,p.position,p.fotmob_id,p.current_club_id,m.date "
        f"FROM lineups l JOIN matches m ON m.id=l.match_id JOIN players p ON p.id=l.player_id "
        f"WHERE l.match_id IN ({q}) AND l.team_side=m.egypt_side AND COALESCE(p.source,'')!='diaspora_scout'", match_ids).fetchall()


# ------------------------------------------------------------------ club matches (FotMob player pages)
def fetch_player_matches(conn, p, refresh: bool, stats: dict):
    """Parse recentMatches of a player page into load_club_matches. Cached page of any age is used unless `refresh`
    (current-window players), in which case it's refetched when older than REFRESH_TTL. Falls back to player_club_minutes."""
    pid, fm = p["id"], p["fotmob_id"]
    ts = db.now()
    rows, horizon, n_listed, src, pf = [], None, None, None, None
    if fm:
        url = f"{fotmob.BASE}/players/{fm}/x"
        cp = http._cache_path(url)
        existed = cp.exists()
        stale = existed and (datetime.now().timestamp() - cp.stat().st_mtime) > REFRESH_TTL
        if refresh or existed:
            try:
                n0 = http.STATS["network"]
                dd = fotmob.next_data(url, ttl=REFRESH_TTL if refresh else 10 ** 10)["props"]["pageProps"]["data"]
                stats["network"] += http.STATS["network"] - n0
                if refresh:
                    stats["refresh_checked"] += 1
                    stats["refreshed" if (stale or not existed) else "cache_fresh"] += 1
                pf = datetime.fromtimestamp(cp.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                nat = {e["teamId"] for e in ((dd.get("careerHistory") or {}).get("careerItems") or {}).get("national team", {}).get("teamEntries", [])}
                rm = dd.get("recentMatches") or []
                n_listed = len(rm)
                dates = [m["matchDate"]["utcTime"][:10] for m in rm]
                horizon = min(dates) if dates else None
                for m in rm:
                    if m["teamId"] in nat or m["teamId"] == fotmob.EGYPT_ID:
                        continue
                    mins = m.get("minutesPlayed") or 0
                    if not (m.get("playedInMatch") or mins > 0):
                        continue
                    rows.append((pid, str(m["id"]), m["matchDate"]["utcTime"][:10], m["teamId"], m["teamName"], m.get("leagueName"), mins, "fotmob_page", pf or ts))
                src = "fotmob_page"
            except (http.Blocked, http.NotFound, RuntimeError, KeyError) as e:
                stats["errors"].append(f"{p['name']} ({fm}): {type(e).__name__}: {e}")
    if src is None:
        r = conn.execute("SELECT date,source_match_id,club_id,minutes,competition FROM player_club_minutes WHERE player_id=?", (pid,)).fetchall()
        if r:
            names = {c["id"]: (c["fotmob_id"], c["name"]) for c in conn.execute("SELECT id,fotmob_id,name FROM clubs")}
            for x in r:
                fid, nm = names.get(x["club_id"], (None, None))
                rows.append((pid, str(x["source_match_id"]), x["date"], fid, nm, x["competition"], x["minutes"] or 0, "db_player_club_minutes", ts))
            horizon = min(x["date"] for x in r)   # conservative: nothing earlier is known
            src = "db_player_club_minutes"
            stats["db_fallback"] += 1
    conn.execute("DELETE FROM load_club_matches WHERE player_id=?", (pid,))
    conn.executemany("INSERT OR REPLACE INTO load_club_matches VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.execute("INSERT OR REPLACE INTO load_player_horizon VALUES (?,?,?,?,?)", (pid, horizon, n_listed, src, pf))
    return len(rows)


# ------------------------------------------------------------------ per-player metrics
def coverage_of(horizon: str | None, d: str):
    if not horizon:
        return "none", 0
    cd = (date.fromisoformat(d) - date.fromisoformat(horizon)).days
    return ("full" if cd >= 30 else "partial" if cd >= 14 else "none"), cd


def load_metrics(cm: list[tuple], horizon, d: str):
    """cm = [(date, minutes, club_fm, club_name)] of club matches played. Window = matches strictly before d."""
    cov, cd = coverage_of(horizon, d)
    dd = date.fromisoformat(d)
    res = dict(coverage=cov, cov_days=cd, min7=None, min14=None, min30=None, matches14=None, last_club_match=None, rest_days=None)
    if cov == "none":
        return res
    prior = [x for x in cm if x[0] < d and x[1] > 0]
    S = lambda n: sum(x[1] for x in prior if (dd - date.fromisoformat(x[0])).days <= n)
    res["min7"], res["min14"] = S(7), S(14)
    res["matches14"] = sum(1 for x in prior if (dd - date.fromisoformat(x[0])).days <= 14)
    if cov == "full":
        res["min30"] = S(30)
    last = max((x[0] for x in prior), default=None)
    if last and (dd - date.fromisoformat(last)).days <= cd:
        res["last_club_match"], res["rest_days"] = last, (dd - date.fromisoformat(last)).days
    return res


def route_for(club_row, venues: list[dict], geo: Geo, dates: list[str]):
    """Legs: club -> v1 -> v2 ... (consecutive identical venues collapse). Returns dict or None if a coordinate is missing."""
    pts = []
    if club_row:
        pts.append(("club:" + club_row["city"], float(club_row["lat"]), float(club_row["lon"]), club_row["tz"], dates[0]))
    seq = []
    for v, d in zip(venues, dates):
        if v and (not seq or seq[-1][0] != v["city"]):
            seq.append((v["city"], float(v["lat"]), float(v["lon"]), v["tz"], d))
    if not club_row or not seq:
        return None
    pts += [(c, la, lo, tz, d) for c, la, lo, tz, d in seq]
    legs = []
    for a, b in zip(pts, pts[1:]):
        km = haversine_km(a[1], a[2], b[1], b[2])
        shift = utc_offset_h(b[3], b[4]) - utc_offset_h(a[3], b[4])
        legs.append(dict(frm=a[0].replace("club:", ""), to=b[0], km=round(km), tz_shift_h=round(shift, 1), date=b[4]))
    return dict(legs=legs, total_km=sum(l["km"] for l in legs), max_leg_km=max(l["km"] for l in legs) if legs else 0,
                max_tz_shift=max((abs(l["tz_shift_h"]) for l in legs), default=0))


def flags_of(m: dict, r: dict | None):
    f = dict(high_load=None, short_rest=None, long_haul=None, low_rhythm=None)
    if m["coverage"] != "none":
        f["high_load"] = int(m["min14"] >= 270 or m["matches14"] >= 3)
        f["short_rest"] = int(m["rest_days"] is not None and m["rest_days"] < 3)
    if m["coverage"] == "full":
        f["low_rhythm"] = int(m["min30"] < 90)
    if r:
        f["long_haul"] = int(r["max_leg_km"] >= 5000 or r["max_tz_shift"] >= 3)
    score = sum(SCORE_POINTS[k] for k, v in f.items() if v)
    return f, round(score, 1)


# ------------------------------------------------------------------ main
def run(conn=None, fetch: bool = True):
    own = conn is None
    if own:
        conn = db.connect()
    conn.execute("PRAGMA busy_timeout=120000")
    conn.row_factory = __import__("sqlite3").Row
    conn.executescript(SCHEMA)
    geo = Geo()
    gaps: list[str] = []
    stats = dict(network=0, refresh_checked=0, refreshed=0, cache_fresh=0, db_fallback=0, errors=[])
    wins = build_windows(conn)
    cur = next(w for w in wins if w["window_id"] == "W" + CURRENT_FIRST)

    # ---- squads per window
    played_ids_by_win = {w["window_id"]: [m["match_id"] for m in w["matches"] if m["match_id"]] for w in wins}
    lu_by_win, prefer = {}, set()
    for w in wins:
        ids = played_ids_by_win[w["window_id"]]
        lu_by_win[w["window_id"]] = egypt_lineup(conn, ids) if ids else []
        prefer |= {r["player_id"] for r in lu_by_win[w["window_id"]]}
    squads: dict[str, dict[str, dict]] = {}
    for w in wins:
        s = {}
        for r in lu_by_win[w["window_id"]]:
            s.setdefault(str(r["player_id"]), dict(player_id=r["player_id"], name=r["name"], position=r["position"], matchday=True, published=False, wiki_pos=None))
        squads[w["window_id"]] = s
        w["squad_source"] = "lineups (matchday squad: starters + bench, union over window matches played)"
    wsq, wnote = wiki_squad()
    unmatched = []
    if wsq:
        cs = squads[cur["window_id"]]
        for x in wsq:
            p = match_player(conn, x["name"], x["dob"], prefer)
            if p is None or p["fotmob_id"] is None:
                unmatched.append(x["name"] + (" (no FotMob id in players table)" if p else " (no players row)"))
            if p is None:
                cs["wiki:" + x["name"]] = dict(player_id=None, name=x["name"], position=None, matchday=False, published=True, wiki_pos=x["pos"], wiki_club=x["club"], wiki_dob=x["dob"])
                continue
            e = cs.setdefault(str(p["id"]), dict(player_id=p["id"], name=p["name"], position=p["position"], matchday=False, published=True, wiki_pos=x["pos"]))
            e["published"], e["wiki_pos"], e["wiki_club"] = True, x["pos"], x["club"]
            if not e.get("position"):
                e["position"] = p["position"]
        cur["squad_source"] = (f"Wikipedia 'Current squad' ({WIKI_SQUAD_URL}, read {AS_OF.isoformat()}; called up for: {wnote}) "
                               "unioned with the 2026-09-25 Angola matchday squad from lineups")
        if unmatched:
            gaps.append("Published-squad players without usable FotMob data (no load computed): " + "; ".join(unmatched))
    else:
        gaps.append(f"Wikipedia squad not available ({wnote}); current window uses the 2026-09-25 matchday squad only")
        cur["squad_source"] = "lineups: Egypt matchday squad of the 2026-09-25 Angola match (proxy for the call-up list)"

    # ---- club matches
    players = {r["id"]: r for r in conn.execute("SELECT id,name,fotmob_id,position,current_club_id FROM players")}
    cur_ids = {v["player_id"] for v in squads[cur["window_id"]].values() if v["player_id"]}
    all_ids = set()
    for w in wins:
        if w["end"] >= "2025-08-08":     # only windows that can touch the club-minutes coverage
            all_ids |= {v["player_id"] for v in squads[w["window_id"]].values() if v["player_id"]}
    if not fetch:
        stats["note"] = "--no-fetch: cached pages only"
    for pid in sorted(all_ids):
        p = players[pid]
        fetch_player_matches(conn, p, refresh=fetch and pid in cur_ids and bool(p["fotmob_id"]), stats=stats)
    conn.commit()
    horizon = {r["player_id"]: r["horizon_date"] for r in conn.execute("SELECT player_id,horizon_date FROM load_player_horizon")}
    cms = defaultdict(list)
    for r in conn.execute("SELECT player_id,date,minutes,club_fotmob_id,club_name FROM load_club_matches"):
        cms[r["player_id"]].append((r["date"], r["minutes"], r["club_fotmob_id"], r["club_name"]))
    clubs_tab = {r["id"]: r for r in conn.execute("SELECT id,fotmob_id,name FROM clubs")}

    # ---- per player-window
    conn.execute("DELETE FROM load_windows")
    conn.execute("DELETE FROM load_player_window")
    missing_clubs: dict[str, str] = {}
    missing_venues: set[str] = set()
    prow: list[dict] = []
    win_out = []
    for w in wins:
        wid = w["window_id"]
        vs = [geo.venue(m["city"], m["country"]) for m in w["matches"]]
        for m, v in zip(w["matches"], vs):
            if v is None:
                missing_venues.add(f"{m['city']}|{m['country']}")
        dates = [m["date"] for m in w["matches"]]
        # window minutes availability: matches with known minutes
        ids = played_ids_by_win[wid]
        known_ids = {r["match_id"] for r in lu_by_win[wid] if r["minutes_played"] is not None}
        lu = defaultdict(list)
        for r in lu_by_win[wid]:
            lu[r["player_id"]].append(r)
        w["n_played"] = len(ids)
        w["n_minutes_known"] = len(known_ids)
        route_desc = " -> ".join(v["city"] for v in vs if v) if all(vs) else None
        for key, s in squads[wid].items():
            pid = s["player_id"]
            p = players.get(pid) if pid else None
            m = load_metrics(cms.get(pid, []), horizon.get(pid), w["first"]) if (pid and w["end"] >= COVERAGE_FLOOR) else \
                dict(coverage="none", cov_days=0, min7=None, min14=None, min30=None, matches14=None, last_club_match=None, rest_days=None)
            # club at window time = club of last club match before the first window match, else current club (labelled)
            club_fm, club_name, club_src = None, None, None
            prior = sorted((x for x in cms.get(pid, []) if x[0] < w["first"] and x[1] > 0), reverse=True)
            if prior:
                club_fm, club_name, club_src = prior[0][2], prior[0][3], f"last club match {prior[0][0]}"
            elif w["status"] == "current" and p and p["current_club_id"] in clubs_tab:
                c = clubs_tab[p["current_club_id"]]
                club_fm, club_name, club_src = c["fotmob_id"], c["name"], "players.current_club_id (no club match before window in coverage)"
            elif w["status"] == "current" and s.get("wiki_club"):
                club_name, club_src = s["wiki_club"], "Wikipedia squad table"
            crow = geo.club(club_fm, club_name) if club_name else None
            if club_name and not crow and w["end"] >= COVERAGE_FLOOR:
                missing_clubs[str(club_fm)] = club_name
            rt = route_for(crow, vs, geo, dates) if (w["end"] >= COVERAGE_FLOOR) else None
            fl, score = flags_of(m, rt)
            rec = lu.get(pid, [])
            mins = sum(x["minutes_played"] for x in rec if x["minutes_played"] is not None)
            row = dict(window_id=wid, player_key=key, player_id=pid, name=s["name"], position=s.get("position") or s.get("wiki_pos"),
                       in_matchday_squad=int(s["matchday"]), in_published_squad=int(s["published"]),
                       club=club_name, club_source=club_src, club_city=(crow["city"] + ", " + crow["country"]) if crow else None,
                       first_match_date=w["first"], **m,
                       route_km=rt["total_km"] if rt else None, max_leg_km=rt["max_leg_km"] if rt else None,
                       max_tz_shift=rt["max_tz_shift"] if rt else None, **fl, score=score,
                       intl_minutes=mins if known_ids else None, intl_starts=sum(x["started"] or 0 for x in rec),
                       intl_matches_known=len(known_ids), intl_matches=len(ids))
            row["_legs"] = rt["legs"] if rt else None
            row["_appearances"] = [dict(date=x["date"], started=bool(x["started"]), minutes=x["minutes_played"]) for x in rec]
            prow.append(row)
        win_out.append(w)
    cols = [c for c in next(iter(prow)).keys() if not c.startswith("_")]
    conn.executemany(f"INSERT OR REPLACE INTO load_player_window ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [[r[c] for c in cols] for r in prow])
    ts = db.now()
    for w in win_out:
        conn.execute("INSERT OR REPLACE INTO load_windows VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (w["window_id"], w["start"], w["end"], w["kind"], len(w["matches"]), w["first"], w["status"], w["squad_source"],
                      json.dumps([dict(date=m["date"], city=m["city"], country=m["country"]) for m in w["matches"]]), ts))
    conn.commit()

    if missing_clubs:
        gaps.append(f"club_cities.csv missing {len(missing_clubs)} clubs (no travel computed for those player-windows): " +
                    "; ".join(f"{n} [{i}]" for i, n in sorted(missing_clubs.items(), key=lambda x: x[1])))
    if missing_venues:
        gaps.append("venues.csv missing: " + "; ".join(sorted(missing_venues)))
    if stats["errors"]:
        gaps.append("FotMob page errors: " + "; ".join(stats["errors"]))

    result = assemble(conn, wins, prow, squads, stats, gaps, geo)
    EXPORT.mkdir(parents=True, exist_ok=True)
    (EXPORT / "load_tracker.json").write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
    (EXPORT / "load_tracker_summary.md").write_text(summary_md(result))
    if own:
        conn.close()
    return result


# ------------------------------------------------------------------ back-test
def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def backtest(prow, wins):
    win = {w["window_id"]: w for w in wins}
    rows = []
    for r in prow:
        w = win[r["window_id"]]
        if w["status"] != "past" or not r["in_matchday_squad"]:
            continue
        if r["intl_matches_known"] == 0 or r["coverage"] == "none":
            continue
        r = dict(r)
        r["min_share"] = min(1.0, (r["intl_minutes"] or 0) / (r["intl_matches_known"] * 90))
        r["start_share"] = r["intl_starts"] / r["intl_matches"] if r["intl_matches"] else None
        r["gk"] = (r["position"] or "").lower() in ("keeper", "gk", "goalkeeper")
        r["kind"] = w["kind"]
        rows.append(r)

    def cmp(rs, flag):
        a = [x for x in rs if x[flag] == 1]
        b = [x for x in rs if x[flag] == 0]
        f = lambda g: dict(n=len(g), n_windows=len({x["window_id"] for x in g}), mean_minutes_share=mean([x["min_share"] for x in g]),
                           median_minutes_share=med([x["min_share"] for x in g]), mean_starts_share=mean([x["start_share"] for x in g]),
                           share_played_zero=mean([int((x["intl_minutes"] or 0) == 0) for x in g]))
        return dict(flagged=f(a), not_flagged=f(b))

    out = dict(
        definition=("Rows = players in a past window's matchday squad (lineups, starters + bench) whose pre-window club load could be computed "
                    "(coverage != none) and where FotMob recorded minutes for at least one window match. Outcome = Egypt minutes in the window "
                    "divided by 90 x matches with known minutes (capped at 1), and share of window matches started. Descriptive only."),
        caveats=[
            "Selection bias: only players who made the matchday squad are visible; players a coach left out or who withdrew injured are invisible.",
            "Matchday-squad status of unused bench players (0 minutes) is included, so 'minutes share' mixes coach choice with squad-depth roles.",
            "Club-minutes coverage starts 2025-08-08 and each FotMob page lists only its last ~50 matches, so earlier windows have few or no rows.",
            "Flags are not independent (short rest and high load overlap); no controls for position, quality, injuries or opponent. No causal reading.",
            "Windows are treated as independent although the same players recur; n counts player-windows, not players.",
        ],
        n_rows=len(rows), n_windows=len({r["window_id"] for r in rows}),
        windows=sorted({r["window_id"] for r in rows}),
        n_players=len({r["player_id"] for r in rows}),
        exclusions=dict(
            no_minutes_recorded_windows=[w["window_id"] for w in wins if w["status"] == "past" and w["n_played"] and not w["n_minutes_known"]],
            no_coverage_player_windows=sum(1 for r in prow if win[r["window_id"]]["status"] == "past" and r["in_matchday_squad"] and r["coverage"] == "none"),
        ),
    )
    sets = {"all_players": rows, "outfield_only": [r for r in rows if not r["gk"]],
            "fifa_windows_outfield": [r for r in rows if not r["gk"] and r["kind"] == "fifa_window"],
            "tournaments_outfield": [r for r in rows if not r["gk"] and r["kind"] == "tournament"]}
    out["by_flag"] = {}
    for sname, rs in sets.items():
        out["by_flag"][sname] = {}
        for f in ("high_load", "short_rest", "long_haul", "low_rhythm"):
            valid = [r for r in rs if r[f] in (0, 1)]
            out["by_flag"][sname][f] = cmp(valid, f)
    bands = [("0 min", 0, 0), ("1-179", 1, 179), ("180-269", 180, 269), ("270+", 270, 10 ** 6)]
    out["by_min14_band_outfield"] = {}
    for lab, lo, hi in bands:
        g = [r for r in sets["outfield_only"] if r["min14"] is not None and lo <= r["min14"] <= hi]
        out["by_min14_band_outfield"][lab] = dict(n=len(g), mean_minutes_share=mean([x["min_share"] for x in g]), mean_starts_share=mean([x["start_share"] for x in g]))
    sc = defaultdict(list)
    for r in sets["outfield_only"]:
        sc["0" if r["score"] == 0 else "0.1-2" if r["score"] <= 2 else ">2"].append(r)
    out["by_score_band_outfield"] = {k: dict(n=len(v), mean_minutes_share=mean([x["min_share"] for x in v]), mean_starts_share=mean([x["start_share"] for x in v])) for k, v in sorted(sc.items())}
    out["per_window_n"] = {wid: dict(n=sum(1 for r in rows if r["window_id"] == wid), n_high_load=sum(1 for r in rows if r["window_id"] == wid and r["high_load"] == 1),
                                     n_short_rest=sum(1 for r in rows if r["window_id"] == wid and r["short_rest"] == 1)) for wid in out["windows"]}
    return out


# ------------------------------------------------------------------ assemble / write
def assemble(conn, wins, prow, squads, stats, gaps, geo):
    win_meta = []
    for w in wins:
        rs = [r for r in prow if r["window_id"] == w["window_id"]]
        cov = defaultdict(int)
        for r in rs:
            cov[r["coverage"]] += 1
        win_meta.append(dict(
            window_id=w["window_id"], start=w["start"], end=w["end"], kind=w["kind"], status=w["status"], first_match_date=w["first"],
            matches=[dict(date=m["date"], competition=m["competition"], venue=m["venue"], city=m["city"], country=m["country"], status=m["status"]) for m in w["matches"]],
            squad_source=w["squad_source"], squad_size=len(rs), n_matches_played=w["n_played"], n_matches_with_minutes=w["n_minutes_known"],
            club_load_coverage=dict(cov), club_load_note=(
                "no club-minutes data before 2025-08-08" if w["end"] < COVERAGE_FLOOR else
                "no player has >=14 days of listed club history before this window (FotMob pages list only ~50 latest matches)" if not (cov.get("full") or cov.get("partial")) else
                "full = >=30 days of listed club history before first match; partial = 14-29 days (30-day flag not evaluated); none = <14 days")))
    cw = next(w for w in wins if w["status"] == "current")
    board = []
    for r in prow:
        if r["window_id"] != cw["window_id"]:
            continue
        b = {k: v for k, v in r.items() if not k.startswith("_") and k not in ("window_id", "player_key", "first_match_date")}
        b["route_legs"] = r["_legs"]
        b["angola_2026_09_25"] = r["_appearances"][0] if r["_appearances"] else None
        b["flags_triggered"] = [k for k in ("high_load", "short_rest", "long_haul", "low_rhythm") if r[k] == 1]
        b["flags_not_evaluated"] = [k for k in ("high_load", "short_rest", "long_haul", "low_rhythm") if r[k] is None]
        board.append(b)
    board.sort(key=lambda b: (-(b["score"] or 0), -(b["min14"] or -1), b["name"]))
    bt = backtest(prow, wins)
    hz = conn.execute("SELECT COUNT(*),MIN(page_fetched_at),MAX(page_fetched_at) FROM load_player_horizon WHERE source='fotmob_page'").fetchone()
    return dict(
        module="F load_tracker", as_of=AS_OF.isoformat(), generated_at=db.now(),
        thresholds=THRESHOLDS, score=dict(points=SCORE_POINTS, definition=SCORE_TEXT),
        method_notes=[
            "Club minutes come from FotMob player-page recentMatches (club matches only, minutes > 0). National-team matches are excluded from club load.",
            "Windows are Egypt matches under Hossam Hassan (from 2024-02-06) grouped when <=10 days apart and of the same kind (fifa_window vs tournament).",
            "Load is measured before the FIRST match of the window, using club matches strictly before that date.",
            "Distances are great-circle (haversine) between city-level coordinates (venues except Cairo/Juba stadiums are city centres); time-zone shift uses IANA offsets on the match date (DST-aware).",
            "Club at window time = club of the last club match before the window; for the current window falls back to players.current_club_id / Wikipedia squad table.",
        ],
        sources=dict(
            club_minutes="FotMob player pages (https://www.fotmob.com/players/<id>/x, recentMatches); coverage floor 2025-08-08",
            squads="lineups table (FotMob match pages); current window also Wikipedia " + WIKI_SQUAD_URL,
            fixtures="matches table; opp_fixtures (FotMob) for 2026-09-29 Juba and 2026-10-04 Cairo",
            coordinates="Wikidata P625 (data/manual/club_cities.csv, data/manual/venues.csv carry per-row source_url)"),
        coverage=dict(club_minutes_start=COVERAGE_FLOOR, fotmob_pages_used=hz[0], page_fetch_dates=[hz[1], hz[2]],
                      refresh=dict(stats, errors=stats["errors"]), player_pages_list_last_matches="~50 matches per page, so heavily-used players lose their oldest matches"),
        windows=win_meta,
        current_window=dict(window_id=cw["window_id"], as_of=AS_OF.isoformat(), first_match_date=cw["first"],
                            matches=[dict(date=m["date"], venue=m["venue"], city=m["city"], country=m["country"], status=m["status"]) for m in cw["matches"]],
                            load_measured_before=cw["first"], squad_source=cw["squad_source"], board=board),
        backtest=bt, gaps=gaps)


def summary_md(res) -> str:
    cw = res["current_window"]
    L = [f"# Load tracker - international windows (module F)", "",
         f"As of {res['as_of']}. Descriptive only. Club minutes: FotMob recentMatches, coverage starts {res['coverage']['club_minutes_start']} "
         "(earlier windows have no load data; windows shortly after are partial).", "",
         "## Flag thresholds", ""]
    for k, v in res["thresholds"].items():
        L.append(f"- **{k}**: {v}")
    L += ["", f"Score: {res['score']['definition']}", "", f"## Current window ({cw['window_id'][1:]}, load measured before {cw['load_measured_before']})", ""]
    L.append("Matches: " + "; ".join(f"{m['date']} {m['city']} ({m['status']})" for m in cw["matches"]))
    L.append(f"Squad: {cw['squad_source']}.")
    L.append("")
    fl = [b for b in cw["board"] if b["flags_triggered"]]
    L.append(f"### Flagged players ({len(fl)} of {len(cw['board'])})")
    L.append("")
    L.append("| Player | Club | 7/14/30d min | Club matches 14d | Last club match | Rest days | Max leg km / tz | Flags | Score |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for b in fl:
        L.append(f"| {b['name']} | {b['club'] or '?'} | {b['min7']}/{b['min14']}/{b['min30'] if b['min30'] is not None else 'n/a'} | {b['matches14']} | "
                 f"{b['last_club_match'] or 'n/a'} | {b['rest_days'] if b['rest_days'] is not None else 'n/a'} | {b['max_leg_km']} / {b['max_tz_shift']} | {', '.join(b['flags_triggered'])} | {b['score']} |")
    L += ["", "### Full board (sorted by score, then club minutes in last 14 days)", "",
          "| Player | Pos | Club (city) | Matchday 25 Sep | 7/14/30d | M14 | Last club match | Rest d | Route km | Max leg km | Max tz h | Flags | Score | Coverage |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for b in cw["board"]:
        a = b["angola_2026_09_25"]
        ang = "not in matchday squad" if not b["in_matchday_squad"] else ("start %s'" % (int(a["minutes"]) if a["minutes"] is not None else "?") if a["started"] else "bench %s'" % (int(a["minutes"]) if a["minutes"] is not None else "?"))
        L.append(f"| {b['name']} | {b['position'] or ''} | {b['club'] or '?'} ({b['club_city'] or 'n/a'}) | {ang} | "
                 f"{b['min7']}/{b['min14']}/{b['min30'] if b['min30'] is not None else 'n/a'} | {b['matches14']} | {b['last_club_match'] or 'n/a'} | "
                 f"{b['rest_days'] if b['rest_days'] is not None else 'n/a'} | {b['route_km']} | {b['max_leg_km']} | {b['max_tz_shift']} | "
                 f"{', '.join(b['flags_triggered']) or '-'}{' (n/e: ' + ','.join(b['flags_not_evaluated']) + ')' if b['flags_not_evaluated'] else ''} | {b['score']} | {b['coverage']} ({b['cov_days']}d) |")
    bt = res["backtest"]
    ob = bt["by_flag"]["outfield_only"]
    obs = []
    for f, v in ob.items():
        a, b = v["flagged"], v["not_flagged"]
        if a["n"] == 0:
            obs.append(f"{f}: no flagged player-windows in the evaluable sample (n_not_flagged={b['n']}), so nothing can be said.")
        else:
            obs.append(f"{f}: flagged n={a['n']} ({a['n_windows']} windows) mean minutes share {a['mean_minutes_share']} vs not flagged n={b['n']} ({b['n_windows']} windows) {b['mean_minutes_share']}"
                       + ("; flagged n < 10, anecdotal only." if a["n"] < 10 else "."))
    L += ["", "## Back-test (Hassan-era past windows, descriptive)", "", "Key observations (outfield players, matchday squad only; no causal reading):", ""] + [f"- {o}" for o in obs] + ["", bt["definition"], "",
          f"Rows: n={bt['n_rows']} player-windows, {bt['n_players']} players, {bt['n_windows']} windows ({', '.join(w[1:] for w in bt['windows'])}).",
          f"Windows excluded for missing FotMob minutes: {', '.join(w[1:] for w in bt['exclusions']['no_minutes_recorded_windows']) or 'none'}; "
          f"squad player-windows without club-load coverage: {bt['exclusions']['no_coverage_player_windows']}.", ""]
    for sname in ("outfield_only", "fifa_windows_outfield", "tournaments_outfield", "all_players"):
        L += [f"### {sname}", "", "| Flag | Flagged n (windows) | Mean min share | Starts share | Not flagged n (windows) | Mean min share | Starts share |", "|---|---|---|---|---|---|---|"]
        for f, v in bt["by_flag"][sname].items():
            a, b = v["flagged"], v["not_flagged"]
            L.append(f"| {f} | {a['n']} ({a['n_windows']}) | {a['mean_minutes_share']} | {a['mean_starts_share']} | {b['n']} ({b['n_windows']}) | {b['mean_minutes_share']} | {b['mean_starts_share']} |")
        L.append("")
    L += ["### Minutes share by club minutes in the 14 days before (outfield)", "", "| Band | n | Mean min share | Starts share |", "|---|---|---|---|"]
    for k, v in bt["by_min14_band_outfield"].items():
        L.append(f"| {k} | {v['n']} | {v['mean_minutes_share']} | {v['mean_starts_share']} |")
    L += ["", "### By combined score band (outfield)", "", "| Score | n | Mean min share | Starts share |", "|---|---|---|---|"]
    for k, v in bt["by_score_band_outfield"].items():
        L.append(f"| {k} | {v['n']} | {v['mean_minutes_share']} | {v['mean_starts_share']} |")
    L += ["", "Per-window n (flagged high_load / short_rest counts): " + "; ".join(f"{k[1:]}: n={v['n']}, HL={v['n_high_load']}, SR={v['n_short_rest']}" for k, v in bt["per_window_n"].items()), "",
          "Caveats:"] + [f"- {c}" for c in bt["caveats"]]
    L += ["", "## Windows", "", "| Window | Kind | Matches | Squad n | Club-load coverage | Note |", "|---|---|---|---|---|---|"]
    for w in res["windows"]:
        L.append(f"| {w['start']}..{w['end']} ({w['status']}) | {w['kind']} | {len(w['matches'])} ({w['n_matches_with_minutes']} with minutes) | {w['squad_size']} | {', '.join(f'{k}={v}' for k, v in sorted(w['club_load_coverage'].items()))} | {w['club_load_note']} |")
    c = res["coverage"]
    L += ["", "## Coverage and gaps", "",
          f"- FotMob player pages parsed: {c['fotmob_pages_used']} (fetch dates {c['page_fetch_dates'][0]} to {c['page_fetch_dates'][1]}); refresh stats: {c['refresh']}.",
          f"- Pages list only the last ~50 matches, so long-season players lose early matches; coverage is judged per player from the earliest listed match."]
    L += [f"- {g}" for g in res["gaps"]]
    return "\n".join(L) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="never touch the network (cached pages only)")
    a = ap.parse_args(argv)
    conn = db.connect()
    conn.execute("PRAGMA busy_timeout=120000")
    res = run(conn, fetch=not a.no_fetch)
    cw = res["current_window"]
    print(f"load_tracker: {len(res['windows'])} windows, current board {len(cw['board'])} players, "
          f"flagged {sum(1 for b in cw['board'] if b['flags_triggered'])}, backtest n={res['backtest']['n_rows']}; gaps={len(res['gaps'])}")
    for g in res["gaps"]:
        print("GAP:", g[:300])
    return 0


if __name__ == "__main__":
    sys.exit(main())
