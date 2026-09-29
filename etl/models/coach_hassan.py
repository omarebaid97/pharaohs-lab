"""Module A - Hossam Hassan coach profile.

    python -m etl.models.coach_hassan

Reads data/pharaohs.db (+ Wikipedia through the shared polite client, cached) and writes
  data/export/coach_hassan.json
  data/export/coach_hassan_summary.md
Own tables (rebuilt every run): coach_hassan_matches, coach_hassan_backtest_xi, coach_hassan_wiki_jobs.
Models are frequencies / Beta-binomial / Dirichlet-multinomial with explicit priors. No ML.
"""
from __future__ import annotations

import csv
import json
import math
import os
import random
import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .. import db as dbm

ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = ROOT / "data" / "export"
MANUAL_DIR = ROOT / "data" / "manual"
NOTES_CSV = MANUAL_DIR / "coach_notes.csv"
OUT_JSON = EXPORT_DIR / "coach_hassan.json"
OUT_MD = EXPORT_DIR / "coach_hassan_summary.md"

COACH = "Hossam Hassan"
WIKI_URL = "https://en.wikipedia.org/wiki/Hossam_Hassan"
WIKI_RESULTS_URL = "https://en.wikipedia.org/wiki/Egypt_national_football_team_results_(2020%E2%80%93present)"
WIKI_UNOFF_URL = "https://en.wikipedia.org/wiki/Egypt_national_football_team_results_(unofficial_matches)"
ELO_URL = "https://www.eloratings.net/Egypt.tsv"
AS_OF = os.environ.get("PHARAOHS_AS_OF") or date.today().isoformat()

# ---- model parameters (documented in methodology) -------------------------------------------
CI_LEVEL = 0.90
HALF_LIFE_MATCHES = 8.0        # recency half-life for the XI model, in Hassan matches (a priori, not tuned)
KAPPA_START, PI_START = 2.0, 0.44   # prior for P(start | in matchday squad): Beta(0.88, 1.12)
KAPPA_SQUAD, PI_SQUAD = 2.0, 0.10   # prior for P(in matchday squad)
CLUB_WINDOW_DAYS = 35          # recent club minutes window
CLUB_COVER_DAYS = 90           # need >=1 club appearance in this window to call a player "covered"
CLUB_FULL_MINUTES = 180.0      # 2 full matches in the window = "fully active"
CLUB_BETA = 1.0                # odds multiplier exp(BETA*(z-0.5)), z = min(1, recent/180)
FORMATION_ALPHA = 3.0          # Dirichlet shrinkage strength between hierarchy levels
MIN_TRAIN = 10                 # backtest starts at the 11th Hassan match


# ================================================================================================
# small stats helpers
# ================================================================================================
def beta_ci(k, n, a=1.0, b=1.0, level=CI_LEVEL, draws=4000):
    """Beta-binomial posterior for a proportion. Deterministic Monte-Carlo interval."""
    k, n = float(k), float(n)
    mean = (a + k) / (a + b + n)
    rng = random.Random(1_000_003 * int(round(k * 100)) + int(round(n * 100)) + 17)
    xs = sorted(rng.betavariate(a + k, b + max(n - k, 0.0)) for _ in range(draws))
    lo = xs[int((1 - level) / 2 * draws)]
    hi = xs[min(draws - 1, int((1 + level) / 2 * draws))]
    return {"k": round(k, 3) if k != int(k) else int(k), "n": round(n, 3) if n != int(n) else int(n),
            "mean": round(mean, 3), "lo": round(lo, 3), "hi": round(hi, 3)}


def quantile(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    pos = (len(xs) - 1) * q
    lo, hi = int(math.floor(pos)), int(math.ceil(pos))
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def dist_stats(xs, seed=7):
    xs = [x for x in xs if x is not None]
    if not xs:
        return {"n": 0}
    out = {"n": len(xs), "mean": round(statistics.mean(xs), 1), "median": quantile(xs, .5),
           "p10": quantile(xs, .1), "q1": quantile(xs, .25), "q3": quantile(xs, .75), "p90": quantile(xs, .9),
           "min": min(xs), "max": max(xs)}
    if len(xs) >= 5:  # bootstrap 90% interval for the median
        rng = random.Random(seed + len(xs))
        meds = sorted(statistics.median([rng.choice(xs) for _ in xs]) for _ in range(1000))
        out["median_ci90"] = [meds[50], meds[949]]
    return out


def pct(x, nd=3):
    return None if x is None else round(x, nd)


def wdl_of(rows):
    c = Counter(r["result"] for r in rows)
    return {"P": len(rows), "W": c["W"], "D": c["D"], "L": c["L"]}


def points_share(rows):
    """Points per game with W=3 D=1 L=0 (shootouts = draws)."""
    if not rows:
        return None
    return round(sum(3 if r["result"] == "W" else 1 if r["result"] == "D" else 0 for r in rows) / len(rows), 2)


# ================================================================================================
# classification helpers
# ================================================================================================
def elo_band(elo):
    if elo is None:
        return "unknown"
    return "low(<1450)" if elo < 1450 else "mid(1450-1699)" if elo < 1700 else "high(>=1700)"


def comp_type(comp, stage=None):
    c = (comp or "").lower()
    if "qualification" in c:
        return "qualifier"
    if "friend" in c:
        return "friendly"
    return "tournament"


def formation_family(f):
    if not f:
        return None
    try:
        n = int(f.split("-")[0])
    except ValueError:
        return None
    return {3: "back-3", 4: "back-4", 5: "back-5"}.get(n, f"back-{n}")


def venue_of(row):
    if row["neutral"] == 1:
        return "neutral"
    return row["egypt_side"]  # home | away


def pid_parts(pid):
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return None
    return (p // 10, p % 10)


def group_of_pid(pid):
    """Coarse group of a FotMob pitch slot id: 0 GK, 1 DEF, 2 MID, 3 FWD (None if unknown).
    Slot ids encode row (tens) and lateral position (last digit, 1 = right ... 9 = left)."""
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return None
    if p == 11:
        return 0
    row, lat = p // 10, p % 10
    if row in (3, 5):
        return 1
    if row == 6:
        return 1 if (lat <= 2 or lat >= 8) else 2     # wide slots of the 6-row are wing-backs
    if row in (7, 8):
        return 2
    if row in (10, 11):
        return 3
    return None


def role_family(pid):
    """Very coarse attacking role for the Salah section."""
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if p <= 3:
        return "group only (no pitch slot)"
    row, lat = p // 10, p % 10
    if row in (10, 11):
        return "right-sided forward (front-three wing)" if lat <= 3 else "left-sided forward (front-three wing)" if lat >= 7 else "central forward / striker pair"
    if row == 8:
        return "right attacking midfield / wing" if lat <= 4 else "central attacking midfield" if lat == 5 else "left attacking midfield / wing"
    if row == 7:
        return "central/wide midfield row"
    return "other"


def role_label(pid):
    try:
        p = int(pid)
    except (TypeError, ValueError):
        return "unknown"
    if p <= 3:
        return f"group only: {GROUP_NAME[p]} (older lineup, no pitch slot)"
    if p == 11:
        return "GK"
    row, lat = p // 10, p % 10
    side = "R" if lat <= 2 else "R-centre" if lat <= 4 else "centre" if lat == 5 else "L-centre" if lat <= 7 else "L"
    rows = {3: "back line", 5: "wing-back/high back line", 6: "deep midfield row", 7: "central midfield row",
            8: "attacking-midfield row", 10: "forward row", 11: "forward row"}
    return f"{side} {rows.get(row, 'row ' + str(row))} (slot {p})"


GROUP_NAME = {0: "GK", 1: "DEF", 2: "MID", 3: "FWD"}


def sub_type(off_grp, on_grp):
    if off_grp is None or on_grp is None:
        return "unknown"
    if off_grp == 0 or on_grp == 0:
        return "goalkeeper"
    if on_grp > off_grp:
        return "attacking"
    if on_grp < off_grp:
        return "defensive"
    return "like-for-like"


def minute_bucket(m):
    if m is None:
        return None
    if m <= 46:
        return "HT (<=46)"
    if m <= 60:
        return "47-60"
    if m <= 75:
        return "61-75"
    if m <= 90:
        return "76-90"
    return "extra time (91+)"


BUCKETS = ["HT (<=46)", "47-60", "61-75", "76-90", "extra time (91+)"]


# ================================================================================================
# load data
# ================================================================================================
def load_matches(conn):
    conn.row_factory = __import__("sqlite3").Row
    egypt_id = conn.execute("SELECT id FROM teams WHERE name='Egypt' AND kind='national'").fetchone()[0]
    hid = conn.execute("SELECT id FROM coaches WHERE name=?", (COACH,)).fetchone()[0]
    q = """
    SELECT m.*, th.name AS home_name, ta.name AS away_name, co.name AS coach_name
    FROM matches m
    JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
    LEFT JOIN coaches co ON co.id=m.egypt_coach_id
    WHERE m.is_egypt=1 AND m.source='fotmob' AND m.date<=? ORDER BY m.date, m.id"""
    allm = [dict(r) for r in conn.execute(q, (AS_OF,))]
    for r in allm:
        eh = r["egypt_side"] == "home"
        r["opp_name"] = r["away_name"] if eh else r["home_name"]
        r["opp_id"] = r["away_team_id"] if eh else r["home_team_id"]
        r["gf"] = r["home_score"] if eh else r["away_score"]
        r["ga"] = r["away_score"] if eh else r["home_score"]
        if r["decided_by"] == "pens":
            ep, op = (r["pens_home"], r["pens_away"]) if eh else (r["pens_away"], r["pens_home"])
            r["pens_for"], r["pens_against"] = ep, op
        else:
            r["pens_for"] = r["pens_against"] = None
        r["result"] = "W" if r["gf"] > r["ga"] else "L" if r["gf"] < r["ga"] else "D"
        r["venue_kind"] = venue_of(r)
        r["ctype"] = comp_type(r["competition"], r["stage"])
        r["band"] = elo_band(r["opponent_elo"])
        r["family"] = formation_family(r["formation_egypt"])
    return egypt_id, hid, allm


def load_lineups(conn, match_ids, egypt_side_by_match):
    out = defaultdict(list)
    q = """SELECT l.match_id, l.player_id, l.started, l.position, l.slot, l.minutes_played, l.sub_on_minute,
                  l.sub_off_minute, l.captain, p.name, p.current_club_id
           FROM lineups l JOIN players p ON p.id=l.player_id
           WHERE l.match_id IN (%s) AND l.team_side=(SELECT egypt_side FROM matches WHERE id=l.match_id)""" % ",".join(
        str(i) for i in match_ids)
    for r in conn.execute(q):
        out[r["match_id"]].append(dict(r))
    return out


def load_events(conn, match_ids):
    out = defaultdict(list)
    q = """SELECT match_id, minute, added_minute, type, team_id, player_id, related_player_id, xg, outcome, id
           FROM events_lite WHERE match_id IN (%s) ORDER BY match_id, COALESCE(minute,0), COALESCE(added_minute,0), id""" % ",".join(
        str(i) for i in match_ids)
    for r in conn.execute(q):
        out[r["match_id"]].append(dict(r))
    return out


def goal_timeline(events, egypt_id):
    """[(minute, added, +1 Egypt / -1 opponent)] from goal events. Own goals credit the other side."""
    tl = []
    for e in events:
        if e["type"] not in ("goal", "pen_goal", "own_goal") or e["minute"] is None:
            continue
        egypt_team = e["team_id"] == egypt_id
        if e["type"] == "own_goal":
            egypt_team = not egypt_team
        tl.append((e["minute"], e["added_minute"] or 0, 1 if egypt_team else -1))
    tl.sort()
    return tl


def state_at(tl, minute):
    diff = sum(s for (m, a, s) in tl if m <= minute)
    return "leading" if diff > 0 else "trailing" if diff < 0 else "level"


# ================================================================================================
# club at match time
# ================================================================================================
class ClubIndex:
    def __init__(self, conn):
        self.ahly = conn.execute("SELECT id FROM clubs WHERE name='Al Ahly SC'").fetchone()[0]
        self.egy = {r[0] for r in conn.execute("SELECT id FROM clubs WHERE country='EGY'")}
        self.egy |= {r[0] for r in conn.execute(
            """SELECT c.id FROM clubs c WHERE c.team_id IN
               (SELECT home_team_id FROM matches WHERE scope='egyptian_league'
                UNION SELECT away_team_id FROM matches WHERE scope='egyptian_league')""")}
        self.rows = defaultdict(list)
        for r in conn.execute("SELECT player_id, club_id, date, minutes FROM player_club_minutes WHERE date IS NOT NULL ORDER BY date"):
            self.rows[r[0]].append((r[2], r[1], r[3] or 0))
        self.cover_min = conn.execute("SELECT MIN(date) FROM player_club_minutes").fetchone()[0]
        self.cover_max = conn.execute("SELECT MAX(date) FROM player_club_minutes").fetchone()[0]

    def klass(self, club_id):
        if club_id is None:
            return "unknown"
        return "Al Ahly" if club_id == self.ahly else "other Egyptian" if club_id in self.egy else "abroad"

    def at_match(self, pid, current_club, mdate, window=60):
        """(class, basis). basis = 'match-time' if a club appearance within `window` days before the match exists."""
        d0 = (date.fromisoformat(mdate) - timedelta(days=window)).isoformat()
        best = None
        for (d, c, _m) in self.rows.get(pid, ()):
            if d0 <= d <= mdate:
                best = c
        if best is not None:
            return self.klass(best), "match-time"
        return self.klass(current_club), "current-club-fallback"

    def recent(self, pid, mdate):
        """(recent_minutes, covered) using appearances strictly before mdate."""
        d = date.fromisoformat(mdate)
        c0 = (d - timedelta(days=CLUB_COVER_DAYS)).isoformat()
        w0 = (d - timedelta(days=CLUB_WINDOW_DAYS)).isoformat()
        covered, mins = False, 0
        for (dd, _c, m) in self.rows.get(pid, ()):
            if c0 <= dd < mdate:
                covered = True
                if dd >= w0:
                    mins += m
        return mins, covered


# ================================================================================================
# Wikipedia bio / career (through the shared cached client)
# ================================================================================================
def _txt(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def wiki_bio():
    from bs4 import BeautifulSoup
    from ..sources import wikipedia as w
    html = w._html("Hossam Hassan", ttl=7 * 86400)
    soup = BeautifulSoup(html, "lxml")
    out = {"source_url": WIKI_URL}
    ib = soup.select_one("table.infobox")
    section, personal, senior, managerial, intl, youth, medals = None, {}, [], [], None, [], []
    medal_comp = None
    for tr in ib.find_all("tr"):
        cells = [_txt(c) for c in tr.find_all(["th", "td"])]
        if not cells:
            continue
        if cells[0].startswith("Medal record"):
            section = None
            continue
        if len(cells) == 1:
            if tr.find("th") and cells[0] in ("Personal information", "Team information", "Youth career",
                                               "Senior career*", "International career", "Managerial career"):
                section = cells[0]
            elif cells[0].startswith("Medal record"):
                section = None
            continue
        if section == "Personal information":
            personal[cells[0]] = cells[1]
        elif section == "Youth career":
            youth.append({"years": cells[0], "team": cells[1]})
        elif section == "Senior career*" and cells[0] != "Years":
            if cells[0] == "Total":
                out["senior_total"] = {"apps": cells[2], "goals": cells[3].strip("() ")}
            else:
                senior.append({"years": cells[0], "team": cells[1], "apps": int(cells[2]),
                               "goals": int(re.sub(r"\D", "", cells[3]))})
        elif section == "International career":
            intl = {"years": cells[0], "team": cells[1], "caps": int(cells[2]), "goals": int(re.sub(r"\D", "", cells[3]))}
        elif section == "Managerial career":
            managerial.append({"years": cells[0], "team": cells[1]})
    # medal record (nested table inside the infobox): rows after the 'Representing Egypt' header
    in_medals = False
    for tr in ib.find_all("tr"):
        cells = [c for c in (_txt(x) for x in tr.find_all(["th", "td"])) if c]
        if not cells:
            continue
        if cells == ["Representing Egypt"]:
            in_medals = True
            continue
        if not in_medals or cells[0].startswith("Medal record") or cells[0] == "Men's football":
            continue
        if len(cells) == 1 and re.match(r"\d{4}", cells[0]):
            medals.append({"competition": medal_comp, "place": "medal (type shown as an icon, not text)", "edition": cells[0]})
        elif len(cells) == 1:
            medal_comp = cells[0]
        elif len(cells) == 2 and medal_comp and re.match(r"\d{4}", cells[1]):
            medals.append({"competition": medal_comp, "place": cells[0], "edition": cells[1]})
    medals = [dict(t) for t in {tuple(sorted(m.items())) for m in medals}]
    medals.sort(key=lambda m: m["edition"])
    out["personal"] = personal
    out["youth"] = youth
    out["senior_career"] = senior
    out["international_playing"] = intl
    out["managerial_career_years"] = managerial
    out["playing_medals"] = medals

    # managerial record table
    jobs = []
    for tb in soup.select("table.wikitable"):
        cap = _txt(tb.find("caption"))
        if "Managerial record" not in cap:
            continue
        for tr in tb.find_all("tr"):
            cells = [_txt(c) for c in tr.find_all(["th", "td"])]
            if not cells:
                continue
            if cells[0].startswith("Career Total"):
                nums = [c for c in cells[1:] if re.fullmatch(r"[\d.]+", c)]
                out["managerial_total"] = {"P": int(nums[0]), "W": int(nums[1]), "D": int(nums[2]), "L": int(nums[3])}
                continue
            di = [k for k, c in enumerate(cells) if re.fullmatch(r"\d{1,2} \w+ \d{4}|Present", c)]
            if len(di) == 2:
                nums = re.findall(r"\d+(?:\.\d+)?", " ".join(cells[di[1] + 1:]))
                if len(nums) >= 4:
                    team = re.sub(r"\s*\[.*?\]", "", cells[0]).strip()
                    jobs.append({"team": team, "from": cells[di[0]], "to": cells[di[1]], "P": int(nums[0]), "W": int(nums[1]),
                                 "D": int(nums[2]), "L": int(nums[3]), "win_pct": float(nums[-1])})
    out["managerial_jobs"] = jobs
    txt = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
    m = re.search(r"Managerial statistics \[ edit \] As of match played (\d{1,2} \w+ \d{4})", txt)
    out["managerial_table_as_of"] = m.group(1) if m else None
    out["milestones_from_article"] = [
        "Appointed Egypt head coach 6 February 2024 (article cites KingFut, 6 Feb 2024).",
        "First major tournament with Egypt: AFCON 2025 in Morocco, semi-finals (article; Egypt lost the bronze match on penalties per our data).",
        "Led Egypt to the knockout stage of the 2026 FIFA World Cup for the first time in the nation's history; eliminated in the Round of 16 by Argentina (2-3).",
        "Renewed contract with the national team until 2030 (article text, cites Ahram Online 9 July 2026; the Ahram page returned HTTP 403 to us and was not fetched).",
    ]
    return out


# ================================================================================================
# main computation
# ================================================================================================
def run(conn):
    egypt_id, hid, allm = load_matches(conn)
    H = [m for m in allm if m["egypt_coach_id"] == hid]
    T = [m for m in allm if m["coach_name"] == "Helmi Toulan"]
    assert H, "no Hassan matches"
    ids = [m["id"] for m in allm if m["date"] >= "2018-01-01"]
    lineups = load_lineups(conn, ids, None)
    events = load_events(conn, [m["id"] for m in H + T])
    clubs = ClubIndex(conn)

    # ---- per match structures -------------------------------------------------------------
    for m in H + T:
        lu = lineups.get(m["id"], [])
        m["xi"] = [l for l in lu if l["started"]]
        m["bench"] = [l for l in lu if not l["started"]]
        m["xi_ids"] = {l["player_id"] for l in m["xi"]}
        m["squad_ids"] = {l["player_id"] for l in lu}
        gk = [l for l in m["xi"] if l["position"] == "11"]
        m["gk"] = gk[0] if gk else None
        ev = events.get(m["id"], [])
        m["events"] = ev
        m["tl"] = goal_timeline(ev, egypt_id)
        gf = sum(1 for t in m["tl"] if t[2] == 1)
        ga = sum(1 for t in m["tl"] if t[2] == -1)
        m["events_score_ok"] = (gf, ga) == (m["gf"], m["ga"])
        shots = [e for e in ev if e["type"] == "shot"]
        m["has_shots"] = bool(shots)
        m["shots_for"] = sum(1 for e in shots if e["team_id"] == egypt_id)
        m["shots_against"] = sum(1 for e in shots if e["team_id"] != egypt_id)
        m["xg_for"] = round(sum(e["xg"] or 0 for e in shots if e["team_id"] == egypt_id), 2) if shots else None
        m["xg_against"] = round(sum(e["xg"] or 0 for e in shots if e["team_id"] != egypt_id), 2) if shots else None
        m["n_events"] = len(ev)
    n_score_bad = [m["date"] for m in H if not m["events_score_ok"]]

    # ---- own tables --------------------------------------------------------------------------
    conn.executescript("""
    DROP TABLE IF EXISTS coach_hassan_matches;
    DROP TABLE IF EXISTS coach_hassan_wiki_jobs;
    DROP TABLE IF EXISTS coach_hassan_backtest_xi;
    CREATE TABLE coach_hassan_matches (
      match_id INTEGER PRIMARY KEY, seq INTEGER, date TEXT, opponent TEXT, venue TEXT, competition TEXT, comp_type TEXT,
      gf INTEGER, ga INTEGER, result TEXT, decided_by TEXT, formation TEXT, formation_family TEXT, opp_formation TEXT,
      egypt_elo INTEGER, opp_elo INTEGER, opp_band TEXT, has_shots INTEGER, xg_for REAL, xg_against REAL, source_url TEXT);
    CREATE TABLE coach_hassan_backtest_xi (
      seq INTEGER, date TEXT, model TEXT, correct INTEGER, gk_correct INTEGER, PRIMARY KEY (seq, model));
    CREATE TABLE IF NOT EXISTS coach_hassan_wiki_jobs (
      team TEXT, from_date TEXT, to_date TEXT, p INTEGER, w INTEGER, d INTEGER, l INTEGER, win_pct REAL);
    """)

    # =========================== 2. RECORD & RESULTS ==============================================
    match_rows = []
    for i, m in enumerate(H, 1):
        we = 1 / (1 + 10 ** (-((m["egypt_elo"] or 0) - (m["opponent_elo"] or 0)) / 400)) if m["egypt_elo"] and m["opponent_elo"] else None
        m["seq"] = i
        m["elo_expected"] = we
        row = {
            "seq": i, "date": m["date"], "opponent": m["opp_name"], "venue": m["venue_kind"],
            "venue_name": m["venue"], "venue_city": m["venue_city"], "competition": m["competition"], "stage": m["stage"],
            "competition_type": m["ctype"], "score": f"{m['gf']}-{m['ga']}", "gf": m["gf"], "ga": m["ga"],
            "result": m["result"], "decided_by": m["decided_by"],
            "shootout": (f"{m['pens_for']}-{m['pens_against']}" if m["decided_by"] == "pens" else None),
            "shootout_result": (("W" if m["pens_for"] > m["pens_against"] else "L") if m["decided_by"] == "pens" else None),
            "formation": m["formation_egypt"], "formation_family": m["family"], "opp_formation": m["formation_opp"],
            "egypt_elo": m["egypt_elo"], "opponent_elo": m["opponent_elo"],
            "elo_diff": (m["egypt_elo"] - m["opponent_elo"]) if m["egypt_elo"] and m["opponent_elo"] else None,
            "opp_elo_band": m["band"], "elo_expected_score": pct(we, 3),
            "coach_tag_source": m["coach_source"],
            "shots": ({"for": m["shots_for"], "against": m["shots_against"], "xg_for": m["xg_for"], "xg_against": m["xg_against"]}
                      if m["has_shots"] else None),
            "source_url": m["source_url"],
        }
        match_rows.append(row)
        conn.execute("INSERT INTO coach_hassan_matches VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (m["id"], i, m["date"], m["opp_name"], m["venue_kind"], m["competition"], m["ctype"], m["gf"], m["ga"],
                      m["result"], m["decided_by"], m["formation_egypt"], m["family"], m["formation_opp"], m["egypt_elo"],
                      m["opponent_elo"], m["band"], int(m["has_shots"]), m["xg_for"], m["xg_against"], m["source_url"]))

    def rec_by(key):
        g = defaultdict(list)
        for m in H:
            g[m[key]].append(m)
        return {k: {**wdl_of(v), "points_per_game": points_share(v)} for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

    shoot = [m for m in H if m["decided_by"] == "pens"]
    exp_pts = [m["elo_expected"] for m in H if m["elo_expected"] is not None]
    act_pts = [(1.0 if m["result"] == "W" else 0.5 if m["result"] == "D" else 0.0) for m in H if m["elo_expected"] is not None]
    gf, ga = sum(m["gf"] for m in H), sum(m["ga"] for m in H)
    shots_m = [m for m in H if m["has_shots"]]
    record = {
        "scope": f"{len(H)} matches tagged {COACH} in FotMob ({H[0]['date']} to {H[-1]['date']}); the 3 Arab Cup matches of Dec 2025 (Helmi Toulan) are excluded.",
        "overall_shootouts_as_draws": {**wdl_of(H), "goals_for": gf, "goals_against": ga, "points_per_game": points_share(H)},
        "shootout_matches": [{"date": m["date"], "opponent": m["opp_name"], "score": f"{m['gf']}-{m['ga']}",
                              "shootout": f"{m['pens_for']}-{m['pens_against']}"} for m in shoot],
        "by_competition_type": rec_by("ctype"),
        "by_venue": rec_by("venue_kind"),
        "by_opponent_elo_band": rec_by("band"),
        "elo_expectation": {
            "note": "Expected score = 1/(1+10^(-dElo/400)) with pre-match Elo of both sides, no home advantage; actual score W=1 D=0.5 L=0 (shootouts = draws).",
            "matches": len(exp_pts), "expected_mean": round(statistics.mean(exp_pts), 3), "actual_mean": round(statistics.mean(act_pts), 3),
            "actual_minus_expected_per_match": round(statistics.mean(act_pts) - statistics.mean(exp_pts), 3),
        },
        "egypt_elo_first_last": [H[0]["egypt_elo"], H[-1]["egypt_elo"]],
        "toulan_arab_cup_excluded": [{"date": m["date"], "opponent": m["opp_name"], "score": f"{m['gf']}-{m['ga']}",
                                      "competition": m["competition"], "coach": m["coach_name"]} for m in T],
        "coach_tag_provenance": dict(Counter(m["coach_source"] for m in H)),
        "shot_data_coverage": {"matches_with_shots": len(shots_m), "of": len(H),
                               "dates": [m["date"] for m in shots_m]},
        "matches": match_rows,
    }

    # =========================== reconciliation ===========================================
    recon = reconcile(conn, H, T)

    # =========================== 3. TENDENCIES ===========================================
    tend = {}
    # ---- formations
    def fcount(rows, key="formation_egypt"):
        c = Counter(r[key] for r in rows if r[key])
        n = sum(c.values())
        return {f: {"count": k, **{kk: vv for kk, vv in beta_ci(k, n).items() if kk in ("mean", "lo", "hi")}, "n": n}
                for f, k in c.most_common()}

    def group_form(keyfn):
        g = defaultdict(list)
        for m in H:
            g[keyfn(m)].append(m)
        return {k: {"n": len(v), "formations": fcount(v), "families": fcount(v, "family")} for k, v in sorted(g.items(), key=lambda kv: str(kv[0]))}

    def phase(m):
        d = m["date"]
        return ("1 2024 (friendlies + qualifiers)" if d <= "2024-12-31" else
                "2 2025 WCQ + friendlies" if d < "2025-12-20" else
                "3 AFCON 2025" if d <= "2026-01-20" else
                "4 2026 pre-World-Cup friendlies" if d < "2026-06-10" else
                "5 World Cup 2026" if d <= "2026-07-10" else "6 post-World-Cup")

    for m in H:
        m["phase"] = phase(m)
    tend["formations"] = {
        "note": "Start formation as recorded by FotMob. In-match formation changes are not in the data. 'mean/lo/hi' = Beta(1,1) posterior share with 90% credible interval; n = matches in the group.",
        "overall": fcount(H),
        "overall_family": fcount(H, "family"),
        "by_opponent_elo_band": group_form(lambda m: m["band"]),
        "by_venue": group_form(lambda m: m["venue_kind"]),
        "by_competition_type": group_form(lambda m: m["ctype"]),
        "by_phase": group_form(lambda m: m["phase"]),
        "sequence": [{"date": m["date"], "opponent": m["opp_name"], "formation": m["formation_egypt"], "family": m["family"],
                      "phase": m["phase"]} for m in H],
        "opponent_formation_overall": fcount(H, "formation_opp"),
    }
    # shot based by family (coverage stated)
    fam_shots = defaultdict(list)
    for m in shots_m:
        fam_shots[m["family"]].append(m)
    tend["formations"]["shot_stats_by_family"] = {
        "coverage": f"shot/xG data exists for {len(shots_m)} of {len(H)} Hassan matches (AFCON 2025 + World Cup 2026 only); no qualifiers, no friendlies.",
        "by_family": {k: {"n_matches_with_shots": len(v), "mean_xg_for": round(statistics.mean(x["xg_for"] for x in v), 2),
                          "mean_xg_against": round(statistics.mean(x["xg_against"] for x in v), 2),
                          "mean_shots_for": round(statistics.mean(x["shots_for"] for x in v), 1),
                          "mean_shots_against": round(statistics.mean(x["shots_against"] for x in v), 1)}
                      for k, v in sorted(fam_shots.items())}}

    # ---- XI continuity
    cont = []
    for a, b in zip(H, H[1:]):
        if len(a["xi_ids"]) == 11 and len(b["xi_ids"]) == 11:
            same = len(a["xi_ids"] & b["xi_ids"])
            cont.append({"date": b["date"], "opponent": b["opp_name"], "competition_type": b["ctype"], "changes": 11 - same,
                         "gk_changed": bool(a["gk"] and b["gk"] and a["gk"]["player_id"] != b["gk"]["player_id"]),
                         "days_since_previous": (date.fromisoformat(b["date"]) - date.fromisoformat(a["date"])).days})
    ch = [c["changes"] for c in cont]
    tend["xi_continuity"] = {
        "note": "changes = starters not in the previous Hassan match's XI (consecutive Hassan matches; the 3 Arab Cup games sit between 2025-11-17 and 2025-12-16 and are ignored).",
        "n_transitions": len(cont), "mean_changes": round(statistics.mean(ch), 2), "median_changes": statistics.median(ch),
        "distribution": dict(sorted(Counter(ch).items())),
        "by_competition_type": {k: {"n": len(v), "mean_changes": round(statistics.mean(v), 2)}
                                for k, v in sorted({t: [c["changes"] for c in cont if c["competition_type"] == t]
                                                    for t in {c["competition_type"] for c in cont}}.items())},
        "max_changes": max(cont, key=lambda c: c["changes"]),
        "within_match_window_le_4_days_mean": round(statistics.mean([c["changes"] for c in cont if c["days_since_previous"] <= 4] or [0]), 2),
        "series": cont,
    }

    # ---- squad core (club class of starters)
    def core_for(basis):
        per_match, tot = [], Counter()
        basis_used = Counter()
        for m in H:
            c = Counter()
            for l in m["xi"]:
                if basis == "current":
                    k, b = clubs.klass(l["current_club_id"]), "current-club"
                else:
                    k, b = clubs.at_match(l["player_id"], l["current_club_id"], m["date"])
                c[k] += 1
                basis_used[b] += 1
            tot.update(c)
            n = sum(c.values())
            per_match.append({"date": m["date"], "opponent": m["opp_name"], "n_starters": n, **{k: c.get(k, 0) for k in ("Al Ahly", "other Egyptian", "abroad", "unknown")}})
        n = sum(tot.values())
        return {"starter_slots": n, "basis_counts": dict(basis_used),
                "shares": {k: beta_ci(tot.get(k, 0), n) for k in ("Al Ahly", "other Egyptian", "abroad", "unknown")},
                "per_match": per_match}
    core_cur = core_for("current")
    core_mt = core_for("match-time")
    tend["squad_core"] = {
        "note": (f"'current_club' = players.current_club_id (today's club) applied to every match. 'match_time_where_covered' = club of the latest "
                 f"player_club_minutes appearance within 60 days before the match (coverage {clubs.cover_min} to {clubs.cover_max}, partial), else the current "
                 "club; the basis of every slot is counted. Matches before 2025-08-08 can only use the current club. 'Egyptian' = club in the Egyptian Premier League "
                 "(2025/26 or 2026/27 FotMob data); an Egyptian second-division club would be labelled 'abroad'. 'unknown' = no club known."),
        "current_club": core_cur, "match_time_where_covered": core_mt,
    }
    # per phase, current-club basis
    ph = defaultdict(Counter)
    for m, pm in zip(H, core_cur["per_match"]):
        for k in ("Al Ahly", "other Egyptian", "abroad", "unknown"):
            ph[m["phase"]][k] += pm[k]
    tend["squad_core"]["current_club_by_phase"] = {p: {"starter_slots": sum(c.values()),
        **{k: beta_ci(c.get(k, 0), sum(c.values()))["mean"] for k in ("Al Ahly", "other Egyptian", "abroad", "unknown")}} for p, c in sorted(ph.items())}

    # ---- loyalty
    starts = Counter()
    names, mins = {}, defaultdict(float)
    for m in H:
        for l in m["xi"]:
            starts[l["player_id"]] += 1
            names[l["player_id"]] = l["name"]
            mins[l["player_id"]] += l["minutes_played"] or 0
    n_full = sum(1 for m in H if len(m["xi_ids"]) == 11)
    seen, first_time = set(), []
    for k, m in enumerate(H):
        new = [l["name"] for l in m["xi"] if l["player_id"] not in seen]
        first_time.append({"date": m["date"], "opponent": m["opp_name"], "new_starters": len(new) if k else None,
                           "names": new if k else [], "cumulative_unique_starters": len(seen | m["xi_ids"])})
        seen |= m["xi_ids"]
    tend["loyalty"] = {
        "note": "starts = matches in the Hassan-tagged XI (n matches); minutes are nominal. first-time = first start under Hassan (first match is the baseline, so counts start at match 2).",
        "matches": len(H),
        "most_started": [{"player": names[p], "starts": s, "start_share": beta_ci(s, len(H)), "nominal_minutes": int(mins[p])}
                         for p, s in starts.most_common(20)],
        "players_used_as_starter": len(starts),
        "players_with_ge_half_starts": sum(1 for s in starts.values() if s >= len(H) / 2),
        "first_time_starters_by_match": first_time,
        "first_time_starters_total_after_match_1": sum(f["new_starters"] for f in first_time[1:]),
        "cumulative_unique_starters_last": first_time[-1]["cumulative_unique_starters"],
    }

    # ---- goalkeepers
    gk_starts = Counter()
    gk_seq = []
    shen_ids = {r[0] for r in conn.execute("SELECT id FROM players WHERE name='Mohamed El Shenawy'")}
    for m in H:
        g = m["gk"]
        gk_starts[g["name"] if g else None] += 1
        status = ("started" if any(l["player_id"] in shen_ids for l in m["xi"]) else
                  "bench" if any(l["player_id"] in shen_ids for l in m["bench"]) else "not in matchday squad")
        gk_seq.append({"date": m["date"], "opponent": m["opp_name"], "competition_type": m["ctype"],
                       "gk": g["name"] if g else None, "el_shenawy_status": status})
    changes = sum(1 for a, b in zip(gk_seq, gk_seq[1:]) if a["gk"] != b["gk"])
    last_shen = max((g["date"] for g in gk_seq if g["el_shenawy_status"] == "started"), default=None)
    tend["goalkeepers"] = {
        "note": "GK = the starter in FotMob slot 11. El-Shenawy status uses matchday squad membership in FotMob lineups (bench included).",
        "starts": [{"gk": g, "starts": k, "share": beta_ci(k, len(H))} for g, k in gk_starts.most_common()],
        "gk_changes_between_consecutive_matches": changes, "transitions": len(H) - 1,
        "el_shenawy_last_start": last_shen,
        "el_shenawy_starts_before_2026_01_17": sum(1 for g in gk_seq if g["gk"] == "Mohamed El Shenawy" and g["date"] <= "2026-01-17"),
        "el_shenawy_starts_after_2026_01_17": sum(1 for g in gk_seq if g["gk"] == "Mohamed El Shenawy" and g["date"] > "2026-01-17"),
        "shobeir_starts_after_2026_01_17": sum(1 for g in gk_seq if g["gk"] == "Mostafa Shobeir" and g["date"] > "2026-01-17"),
        "matches_after_2026_01_17": sum(1 for g in gk_seq if g["date"] > "2026-01-17"),
        "el_shenawy_in_latest_matchday_squad": gk_seq[-1]["el_shenawy_status"],
        "sequence": gk_seq,
    }

    # ---- substitutions
    subs = []
    for m in H:
        tl = m["tl"]
        lu_by = {l["player_id"]: l for l in m["xi"] + m["bench"]}
        ev = [e for e in m["events"] if e["type"] == "sub_off" and e["team_id"] == egypt_id and e["minute"] is not None]
        ev.sort(key=lambda e: (e["minute"], e["added_minute"] or 0, e["id"]))
        minutes_sorted = sorted({e["minute"] for e in ev})
        for k, e in enumerate(ev, 1):
            off, on = lu_by.get(e["player_id"]), lu_by.get(e["related_player_id"])
            off_g = group_of_pid(off["position"]) if off and off["started"] else (int(off["position"]) if off and off["position"] in ("0", "1", "2", "3") else None)
            on_g = int(on["position"]) if on and not on["started"] and on["position"] in ("0", "1", "2", "3") else None
            subs.append({"match": m["date"], "match_id": m["id"], "minute": e["minute"], "ordinal": k, "wave": minutes_sorted.index(e["minute"]) + 1,
                         "state": state_at(tl, e["minute"]), "type": sub_type(off_g, on_g), "match_state_ok": m["events_score_ok"],
                         "off": off["name"] if off else None, "on": on["name"] if on else None,
                         "off_group": GROUP_NAME.get(off_g), "on_group": GROUP_NAME.get(on_g), "ctype": m["ctype"], "aet": m["decided_by"] in ("aet", "pens")})

    def dstats(rows):
        return dist_stats([r["minute"] for r in rows])

    def bucket_shares(rows):
        n = len(rows)
        c = Counter(minute_bucket(r["minute"]) for r in rows)
        return {b: beta_ci(c.get(b, 0), n) for b in BUCKETS}

    ord_key = lambda r: "1st" if r["ordinal"] == 1 else "2nd" if r["ordinal"] == 2 else "3rd+"
    wave_key = lambda r: "wave 1" if r["wave"] == 1 else "wave 2" if r["wave"] == 2 else "wave 3+"
    subs_ok = [s for s in subs if s["match_state_ok"]]
    n_no_state = len(subs) - len(subs_ok)
    per_match_subs = Counter(s["match_id"] for s in subs)
    by_ord = {k: {"minutes": dstats(v), "bucket_shares": bucket_shares(v)} for k, v in sorted(_grp(subs, ord_key).items())}
    by_wave = {k: {"minutes": dstats(v), "bucket_shares": bucket_shares(v)} for k, v in sorted(_grp(subs, wave_key).items())}
    by_state = {}
    for st, rows in sorted(_grp(subs_ok, lambda r: r["state"]).items()):
        by_state[st] = {"n_subs": len(rows), "minutes": dstats(rows), "bucket_shares": bucket_shares(rows),
                        "by_ordinal": {k: dstats(v) for k, v in sorted(_grp(rows, ord_key).items())},
                        "type_shares": {t: beta_ci(sum(1 for r in rows if r["type"] == t), len(rows)) for t in ("attacking", "defensive", "like-for-like", "goalkeeper", "unknown")}}
    tl_types = Counter(s["type"] for s in subs)
    tend["substitutions"] = {
        "note": ("Sub events come from FotMob sub_off/sub_on events. 1st/2nd/3rd+ = order of the sub event within the match (a batch of subs in one minute "
                 "occupies consecutive ordinals); 'wave' = distinct sub minutes. State = Egypt goal difference at the sub minute from goal events (goals in the same "
                 "minute count); subs in the 1 match whose goal events do not sum to the final score are excluded from state tables. Type: outgoing group from the "
                 "pitch slot he occupied (GK/DEF/MID/FWD; wide slots of the 6-row = wing-backs = DEF; the 7 and 8 rows = MID; 10/11 rows = FWD), incoming group = FotMob "
                 "usual-position group of the substitute. Attacking = incoming group above outgoing, defensive = below, like-for-like = same. This is coarse: e.g. a winger "
                 "replacing a wide attacking midfielder counts as attacking. Extra-time subs (minute > 90) are included."),
        "n_matches": len(H), "n_sub_events": len(subs),
        "matches_without_sub_events": [{"date": m["date"], "opponent": m["opp_name"]} for m in H if not any(sb["match_id"] == m["id"] for sb in subs)], "n_sub_events_without_valid_state": n_no_state,
        "subs_per_match": {"mean": round(len(subs) / len(H), 2), "distribution": dict(sorted(Counter(per_match_subs.get(m["id"], 0) for m in H).items()))},
        "by_ordinal": by_ord, "by_wave": by_wave, "by_game_state": by_state,
        "type_overall": {t: beta_ci(v, len(subs)) for t, v in tl_types.items()},
        "type_by_ordinal": {k: {t: beta_ci(sum(1 for r in rows if r["type"] == t), len(rows))["mean"] for t in ("attacking", "defensive", "like-for-like", "goalkeeper", "unknown")}
                            | {"n": len(rows)} for k, rows in sorted(_grp(subs, ord_key).items())},
        "events": subs,
    }

    # ---- after conceding first
    conceded_first = []
    for m in H:
        if not m["events_score_ok"] or not m["tl"]:
            continue
        first = m["tl"][0]
        if first[2] == -1:
            after = [s for s in subs if s["match_id"] == m["id"] and s["minute"] >= first[0]]
            conceded_first.append({"date": m["date"], "opponent": m["opp_name"], "competition_type": m["ctype"], "conceded_minute": first[0],
                                   "result": m["result"], "score": f"{m['gf']}-{m['ga']}", "formation": m["formation_egypt"],
                                   "formation_family": m["family"], "first_sub_minute": min([s["minute"] for s in after], default=None),
                                   "subs_within_20_min_of_conceding": [{"minute": s["minute"], "type": s["type"]} for s in after if s["minute"] - first[0] <= 20]})
    scored_first = [m for m in H if m["events_score_ok"] and m["tl"] and m["tl"][0][2] == 1]
    n_cf = len(conceded_first)
    c_cf = Counter(x["result"] for x in conceded_first)
    tend["after_conceding_first"] = {
        "note": ("Matches where the opponent scored the first goal (own goals credited correctly). Formation is the STARTING formation: FotMob has no in-match formation "
                 "changes, so the response is proxied by the subs made within 20 minutes of the goal. Shootouts count as draws."),
        "matches_conceded_first": n_cf, "of": len(H),
        "result_distribution": {r: beta_ci(c_cf.get(r, 0), n_cf) for r in ("W", "D", "L")} if n_cf else {},
        "not_lost": beta_ci(c_cf.get("W", 0) + c_cf.get("D", 0), n_cf) if n_cf else None,
        "comparison_when_scoring_first": {**wdl_of(scored_first), "not_lost": beta_ci(sum(1 for m in scored_first if m["result"] != "L"), len(scored_first))},
        "family_when_conceding_first": dict(Counter(x["formation_family"] for x in conceded_first)),
        "family_all_matches": dict(Counter(m["family"] for m in H)),
        "matches": conceded_first,
    }

    # ---- Salah
    tend["salah"] = salah_section(conn, allm, hid)

    # =========================== 4. PREDICTIVE ==============================================
    pred = predictive(conn, H, clubs, names)

    # =========================== 5. TACTICAL NOTES ===========================================
    notes = read_notes()

    # =========================== 1. BIO ======================================================
    try:
        bio = wiki_bio()
        for j in bio["managerial_jobs"]:
            conn.execute("INSERT INTO coach_hassan_wiki_jobs VALUES (?,?,?,?,?,?,?,?)",
                         (j["team"], j["from"], j["to"], j["P"], j["W"], j["D"], j["L"], j["win_pct"]))
        bio["bio_fetch_error"] = None
    except Exception as e:  # network/parse problem must not kill the export
        bio = {"source_url": WIKI_URL, "bio_fetch_error": f"{type(e).__name__}: {e}"}
    if bio.get("managerial_jobs"):
        jobs = bio["managerial_jobs"]
        s = {k: sum(j[k] for j in jobs) for k in "PWDL"}
        bio["managerial_jobs_sum_vs_reported_total"] = {"sum_of_rows": s, "reported_total": bio.get("managerial_total"),
                                                       "matches": s == bio.get("managerial_total")}
        bio["club_era_detail"] = ("Per-job P/W/D/L is taken from Wikipedia's managerial statistics table. Match-level detail for the club jobs (2008-2023) was not "
                                  "built: it would need Egyptian league match archives that are not in the FotMob window ingested in Phase 1.")
    if bio.get("managerial_jobs") and bio.get("managerial_total"):
        eg = [j for j in bio["managerial_jobs"] if j["team"] == "Egypt"]
        rs = bio["managerial_jobs_sum_vs_reported_total"]
        tot = bio["managerial_total"]
        if eg:
            e = eg[0]
            rest = {k: rs["sum_of_rows"][k] - e[k] for k in "PWDL"}
            recon["article_table_internal_consistency"] = {
                "note": ("The article's 'Career Total' row does not equal the sum of its own rows, so the table is not maintained arithmetically. "
                         "Implied Egypt figures if the Total row were right = Total minus the other jobs: they disagree with the Egypt row on P, D and L; the implied W (21) coincides with our best-fit W."),
                "sum_of_rows": rs["sum_of_rows"], "reported_total": tot,
                "total_minus_other_jobs_implied_egypt": {k: tot[k] - rest[k] for k in "PWDL"},
                "egypt_row": {k: e[k] for k in "PWDL"},
                "total_W_equals_row_sum_if_egypt_W_is": tot["W"] - rest["W"]}
    bio["egypt_tenure_our_data"] = {**record["overall_shootouts_as_draws"], "from": H[0]["date"], "to": H[-1]["date"]}

    methodology = build_methodology(conn, H, T, recon, clubs, n_score_bad, bio, notes)

    conn.commit()
    export = {
        "meta": {"module": "coach_hassan", "as_of": AS_OF, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "coach": COACH, "matches": len(H), "first_match": H[0]["date"], "last_match": H[-1]["date"]},
        "bio_career": bio,
        "record_results": record,
        "reconciliation": recon,
        "tendencies": tend,
        "predictive": pred,
        "tactical_notes": notes,
        "methodology": methodology,
    }
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(export, indent=1, ensure_ascii=False, default=str, allow_nan=False))
    OUT_MD.write_text(summary_md(export))
    return export


def _grp(rows, keyfn):
    g = defaultdict(list)
    for r in rows:
        g[keyfn(r)].append(r)
    return g


# ================================================================================================
# reconciliation
# ================================================================================================
def reconcile(conn, H, T):
    row = conn.execute("SELECT value FROM meta WHERE key='wikipedia_hassan_record'").fetchone()
    meta = json.loads(row[0]) if row else {}
    cells = meta.get("row") or []
    nums = [c for c in cells[1:] if re.fullmatch(r"\d+", c)]
    art = {"P": int(nums[0]), "W": int(nums[1]), "D": int(nums[2]), "L": int(nums[3])} if len(nums) >= 4 else None

    def res(m, conv):
        r = m["result"]
        if m["decided_by"] == "pens":
            w = m["pens_for"] > m["pens_against"]
            if conv == "shootout=draw":
                return "D"
            if conv == "shootout W/L":
                return "W" if w else "L"
            if conv == "shootout winner=W, loser=D":
                return "W" if w else "D"
            if conv == "every shootout=W":
                return "W"
        return r

    scopes = {
        "Hassan-tagged incl. 2026-09-25 Angola (36)": H,
        "Hassan-tagged excl. Angola (35)": [m for m in H if m["date"] < "2026-09-25"],
        "Hassan-tagged incl. Angola + 3 Arab Cup games (39)": H + T,
    }
    grid = []
    for sn, ms in scopes.items():
        for conv in ("shootout=draw", "shootout W/L", "shootout winner=W, loser=D", "every shootout=W"):
            c = Counter(res(m, conv) for m in ms)
            rec = {"P": len(ms), "W": c["W"], "D": c["D"], "L": c["L"]}
            gap = {k: (art[k] - rec[k]) if art else None for k in "PWDL"}
            grid.append({"scope": sn, "convention": conv, **rec, "article_minus_ours": gap,
                         "abs_gap": sum(abs(v) for v in gap.values()) if art else None})
    best = sorted(grid, key=lambda g: g["abs_gap"])[0]
    max_w = max(g["W"] for g in grid)
    return {
        "article": {"source_url": WIKI_URL, "record": art, "table_as_of": "match played 25 September 2026 (header of the table on the article, read 2026-09-28)",
                    "stored_in_meta": meta.get("row")},
        "correction_to_brief": ("The article's managerial table is headed 'As of match played 25 September 2026', so its P37 already INCLUDES the 0-0 vs Angola. "
                                "Comparing with 35 matches (excluding Angola) was the wrong basis; the like-for-like count is 36."),
        "our_record_by_convention": grid,
        "closest": best,
        "max_wins_reachable_under_any_convention_or_scope": max_w,
        "explained": [
            "Nigeria 0-0 (2-4 p) AFCON bronze match is a draw in our regulation/ET record; the article's L count of 5 agrees with ours only if that shootout defeat is NOT counted as a loss.",
            "(As of the 36-match dataset.) Wins: our 36 matches have 19 regulation/ET wins. The article's 22 W needs +3: the two Hassan shootout wins (Cape Verde 2025-11-17, Australia 2026-07-03) give 21, which is the best any scope reaches when shootout losses are not counted as losses.",
            "Losses: 5 in ours (New Zealand-Croatia ACUD final 2-4, Uzbekistan 0-2, Senegal 0-1, Brazil 1-2, Argentina 2-3) = the article's 5.",
            "The 3 Arab Cup games (D, D, L; Helmi Toulan) cannot be the missing matches: adding any of them cannot produce the missing win, and adding the loss breaks L=5.",
        ],
        "unexplained": ("One match remains. Under the best-fitting convention (shootout winner counted W, shootout loser counted D) our 36 matches read W21 D10 L5 against the article's "
                        "W22 D10 L5 (P37): the article has one more played match and one more win than any combination of our data produces. Under 'every shootout = W' the gap "
                        "is instead one draw. The cause cannot be identified from public data: the Wikipedia results list (2020-present) has exactly the 38 fixtures we hold for the tenure "
                        "window (35 Hassan + 3 Arab Cup) plus no Angola row yet, FotMob has no additional Egypt match, and the 'unofficial matches' results page has no 2024-2026 entry. "
                        "Most likely a hand-edited/miscounted cell in the article (its Win % 59.46 is just 22/37, internally consistent). Not forced: our export keeps the 36-match record."),
        "checks_made": [f"Wikipedia results page (2020-present) fixtures in tenure window vs FotMob: identical sets (validation_report section 1).",
                        f"Unofficial matches page {WIKI_UNOFF_URL}: no 2024-2026 entries (checked 2026-09-28).",
                        "Article reference list scanned for the tenure record: the Egypt row of the table carries no reference."],
    }


# ================================================================================================
# Salah
# ================================================================================================
def salah_section(conn, allm, hid):
    coach_of = {m["id"]: (m["coach_name"] or "untagged") for m in allm}
    date_of = {m["id"]: m["date"] for m in allm}
    q = """SELECT l.match_id, l.started, l.position, l.minutes_played, l.captain
           FROM lineups l JOIN players p ON p.id=l.player_id JOIN matches m ON m.id=l.match_id
           WHERE p.name='Mohamed Salah' AND m.source='fotmob' AND m.is_egypt=1
             AND l.team_side=m.egypt_side"""
    rows = [dict(r) for r in conn.execute(q) if r["match_id"] in coach_of]
    goals = Counter()
    for r in conn.execute("""SELECT e.match_id FROM events_lite e JOIN players p ON p.id=e.player_id
                             WHERE p.name='Mohamed Salah' AND e.type IN ('goal','pen_goal')"""):
        if r[0] in coach_of:
            goals[coach_of[r[0]]] += 1
    by = defaultdict(list)
    for r in rows:
        by[coach_of[r["match_id"]]].append(r)
    total_by_coach = Counter(coach_of.values())
    out = {}
    for coach, rs in sorted(by.items()):
        st = [r for r in rs if r["started"]]
        with_pos = [r for r in st if r["position"]]
        pc = Counter(r["position"] for r in with_pos)
        fam = Counter(role_family(r["position"]) for r in with_pos)
        out[coach] = {
            "egypt_matches_tagged": total_by_coach[coach], "salah_in_matchday_squad": len(rs), "starts": len(st),
            "start_share_of_tagged_matches": beta_ci(len(st), total_by_coach[coach]),
            "mean_nominal_minutes_when_started": round(statistics.mean([r["minutes_played"] for r in st if r["minutes_played"] is not None] or [0]), 1),
            "goals_in_our_event_data": goals.get(coach, 0),
            "starts_with_position_data": len(with_pos),
            "positions": [{"slot": p, "role": role_label(p), "starts": k, "share_of_starts_with_position": beta_ci(k, len(with_pos))["mean"]}
                          for p, k in pc.most_common()],
            "role_families": [{"family": f, "starts": k, "share": beta_ci(k, len(with_pos))} for f, k in fam.most_common()],
            "first_last_match": [min(date_of[r["match_id"]] for r in rs), max(date_of[r["match_id"]] for r in rs)],
        }
    prev = [c for c in out if c != COACH]
    return {
        "note": ("Slot id = FotMob pitch position (row = tens, lateral = last digit, 1 = right ... 9 = left). Older matches carry only a coarse group code (0-3) instead of a pitch slot "
                 "('group only'); the pitch-slot comparison therefore rests on the few earlier starts that have one - see starts_with_position_data and role_families. Minutes are nominal. "
                 "Goals are those in events_lite (all Egypt matches ingested since 2018)."),
        "by_coach": out,
        "hassan_vs_previous_positions": {
            "hassan": {"starts_with_position_data": out.get(COACH, {}).get("starts_with_position_data"),
                       "positions": out.get(COACH, {}).get("positions"), "role_families": out.get(COACH, {}).get("role_families")},
            "previous_coaches_combined": _combine_positions([out[c] for c in prev]),
        },
    }


def _combine_positions(cs):
    c, f = Counter(), Counter()
    for x in cs:
        for p in x["positions"]:
            c[p["slot"]] += p["starts"]
        for r in x["role_families"]:
            f[r["family"]] += r["starts"]
    n = sum(c.values())
    return {"starts_with_position_data": n,
            "positions": [{"slot": p, "role": role_label(p), "starts": k, "share": beta_ci(k, n)["mean"]} for p, k in c.most_common()],
            "role_families": [{"family": q, "starts": k, "share": beta_ci(k, n)} for q, k in f.most_common()]}


# ================================================================================================
# predictive
# ================================================================================================
def predict_formation(train, band, venue):
    """Hierarchical Dirichlet-multinomial: overall -> band -> band x venue. Returns {formation: prob}."""
    forms = sorted({m["formation_egypt"] for m in train if m["formation_egypt"]})
    if not forms:
        return {}
    N = len(train)
    c0 = Counter(m["formation_egypt"] for m in train)
    p0 = {f: (c0[f] + 0.5) / (N + 0.5 * len(forms)) for f in forms}      # level 0: Jeffreys-style smoothing
    b = [m for m in train if m["band"] == band]
    cb = Counter(m["formation_egypt"] for m in b)
    p1 = {f: (cb[f] + FORMATION_ALPHA * p0[f]) / (len(b) + FORMATION_ALPHA) for f in forms}
    bv = [m for m in b if m["venue_kind"] == venue]
    cbv = Counter(m["formation_egypt"] for m in bv)
    p2 = {f: (cbv[f] + FORMATION_ALPHA * p1[f]) / (len(bv) + FORMATION_ALPHA) for f in forms}
    return {"p": p2, "n_band": len(b), "n_cell": len(bv), "p_overall": p0, "p_band": p1}


def start_probs(hist, i, date_i, clubs, half_life=HALF_LIFE_MATCHES, club_adj=True, players=None):
    """P(start) for each player from matches hist[:i] (each hist item has xi_ids/squad_ids), decayed by matches."""
    W = 0.0
    s, a = defaultdict(float), defaultdict(float)
    for j in range(i):
        w = 1.0 if half_life is None else 0.5 ** ((i - 1 - j) / half_life)
        W += w
        for p in hist[j]["squad_ids"]:
            a[p] += w
        for p in hist[j]["xi_ids"]:
            s[p] += w
    out = {}
    for p in a:
        p_sq = (a[p] + KAPPA_SQUAD * PI_SQUAD) / (W + KAPPA_SQUAD)
        p_st = (s[p] + KAPPA_START * PI_START) / (a[p] + KAPPA_START)
        pr = p_sq * p_st
        cov = None
        if club_adj and date_i:
            mins, covered = clubs.recent(p, date_i)
            if covered:
                z = min(1.0, mins / CLUB_FULL_MINUTES)
                odds = pr / (1 - pr) * math.exp(CLUB_BETA * (z - 0.5))
                pr, cov = odds / (1 + odds), round(z, 2)
        out[p] = (pr, cov)
    return out


def pick_xi(probs, gk_ids):
    gks = sorted(((p, v[0]) for p, v in probs.items() if p in gk_ids), key=lambda x: -x[1])
    out_ = sorted(((p, v[0]) for p, v in probs.items() if p not in gk_ids), key=lambda x: -x[1])
    xi = ([gks[0][0]] if gks else []) + [p for p, _ in out_[:10 if gks else 11]]
    return xi


def predictive(conn, H, clubs, names):
    # ---- formation backtest -------------------------------------------------------------
    n = len(H)
    fb, base_mode, base_prev, prob_actual, prob_base = [], [], [], [], []
    rows = []
    for i in range(MIN_TRAIN, n):
        train, m = H[:i], H[i]
        pf = predict_formation(train, m["band"], m["venue_kind"])
        p = pf["p"]
        top = max(p, key=p.get)
        mode = Counter(t["formation_egypt"] for t in train).most_common(1)[0][0]
        prev = train[-1]["formation_egypt"]
        act = m["formation_egypt"]
        fb.append(top == act)
        base_mode.append(mode == act)
        base_prev.append(prev == act)
        prob_actual.append(p.get(act, 0.0))
        rows.append({"seq": i + 1, "date": m["date"], "opponent": m["opp_name"], "band": m["band"], "venue": m["venue_kind"],
                     "predicted": top, "predicted_prob": round(p[top], 3), "actual": act, "hit": top == act,
                     "prob_assigned_to_actual": round(p.get(act, 0.0), 3), "family_hit": formation_family(top) == m["family"],
                     "n_train_cell": pf["n_cell"], "n_train_band": pf["n_band"]})
    fam_hit = [r["family_hit"] for r in rows]
    nb = len(rows)
    formation_bt = {
        "n_test_matches": nb, "first_test_match": rows[0]["date"],
        "exact_hit_rate": beta_ci(sum(fb), nb), "family_hit_rate": beta_ci(sum(fam_hit), nb),
        "baseline_most_frequent_so_far": beta_ci(sum(base_mode), nb),
        "baseline_same_as_previous_match": beta_ci(sum(base_prev), nb),
        "mean_probability_assigned_to_actual_formation": round(statistics.mean(prob_actual), 3),
        "note": ("Each test match is predicted from earlier Hassan matches only. Prediction = argmax of the hierarchical Dirichlet-multinomial "
                 f"(alpha={FORMATION_ALPHA} between levels overall -> Elo band -> band x venue). With ~5 formations used and 26 tests the hit-rate interval is wide."),
        "rows": rows,
    }
    # scenario grid for the next match, all 36 matches as training
    scen = {}
    for band in ("low(<1450)", "mid(1450-1699)", "high(>=1700)"):
        for v in ("home", "away", "neutral"):
            pf = predict_formation(H, band, v)
            if not pf:
                continue
            p = pf["p"]
            items = sorted(p.items(), key=lambda kv: -kv[1])[:4]
            eff = pf["n_cell"] + FORMATION_ALPHA
            scen[f"{band} | {v}"] = {"n_matches_in_cell": pf["n_cell"], "n_matches_in_band": pf["n_band"],
                                     "top": [{"formation": f, "prob": round(pv, 3),
                                              "ci90": [beta_ci(pv * eff, eff, a=1e-6, b=1e-6)["lo"], beta_ci(pv * eff, eff, a=1e-6, b=1e-6)["hi"]]}
                                             for f, pv in items]}
    formation_next = {
        "note": ("Probability of each starting formation for a hypothetical next match, trained on all Hassan matches. The 90% interval treats each probability as a "
                 f"Beta with effective sample size n_cell + alpha ({FORMATION_ALPHA}); it is indicative, not exact for a Dirichlet mixture. n_matches_in_cell shows how thin the data is."),
        "scenarios": scen,
    }

    # ---- XI backtest ---------------------------------------------------------------------
    hist = [{"xi_ids": m["xi_ids"], "squad_ids": m["squad_ids"]} for m in H]
    gk_ids = {l["player_id"] for m in H for l in m["xi"] if l["position"] == "11"}
    gk_ids |= {l["player_id"] for m in H for l in m["bench"] if l["position"] == "0"}
    variants = {
        "primary: half-life 8 + club-minutes adjustment": dict(half_life=HALF_LIFE_MATCHES, club_adj=True),
        "recency only (half-life 8, no club adjustment)": dict(half_life=HALF_LIFE_MATCHES, club_adj=False),
        "no recency (all matches equal), no adjustment": dict(half_life=None, club_adj=False),
        "sensitivity: half-life 4, no adjustment": dict(half_life=4.0, club_adj=False),
        "sensitivity: half-life 16, no adjustment": dict(half_life=16.0, club_adj=False),
    }
    res = {k: [] for k in variants}
    res["baseline: previous match's XI"] = []
    gk_hit = {k: [] for k in variants}
    covered_flags, cov_dates = [], []
    calib = []
    for i in range(MIN_TRAIN, n):
        m = H[i]
        actual = m["xi_ids"]
        gk_actual = m["gk"]["player_id"] if m["gk"] else None
        # coverage of club minutes for the actual squad
        cov_n = sum(1 for p in m["squad_ids"] if clubs.recent(p, m["date"])[1])
        covered_flags.append(cov_n / max(1, len(m["squad_ids"])))
        for name, kw in variants.items():
            probs = start_probs(hist, i, m["date"], clubs, **kw)
            xi = pick_xi(probs, gk_ids)
            c = len(set(xi) & actual)
            res[name].append(c)
            gkp = xi[0] if xi else None
            gk_hit[name].append(gkp == gk_actual)
            conn.execute("INSERT OR REPLACE INTO coach_hassan_backtest_xi VALUES (?,?,?,?,?)", (i + 1, m["date"], name, c, int(gkp == gk_actual)))
            if name.startswith("primary"):
                for p, (pr, _c) in probs.items():
                    calib.append((pr, 1 if p in actual else 0))
        prev = H[i - 1]["xi_ids"]
        res["baseline: previous match's XI"].append(len(prev & actual))
        # players in the actual XI that were never in any earlier Hassan squad
    unseen = [len(H[i]["xi_ids"] - set().union(*[H[j]["squad_ids"] for j in range(i)])) for i in range(MIN_TRAIN, n)]
    # coverage-restricted comparison: matches where >=50% of the squad has club-minute coverage
    idx_cov = [k for k, f in enumerate(covered_flags) if f >= 0.5]

    def summ(xs):
        return {"mean_correct_of_11": round(statistics.mean(xs), 2), "min": min(xs), "max": max(xs), "n_test_matches": len(xs)}

    xi_bt = {
        "n_test_matches": len(res["baseline: previous match's XI"]),
        "note": ("For the k-th Hassan match (k >= 11) the XI is predicted from matches 1..k-1 only: top-1 goalkeeper + top-10 outfield players by P(start). "
                 "'Correct' = predicted players who really started (any position, out of 11). The club-minutes adjustment can only act when a player has club data "
                 "in the 90 days before the match (coverage starts 2025-08-08), so it is inert for earlier matches."),
        "models": {k: {**summ(v), "gk_hit_rate": beta_ci(sum(gk_hit[k]), len(gk_hit[k])) if k in gk_hit else None} for k, v in res.items()},
        "models_on_club_covered_matches": {
            "definition": "test matches where >=50% of the matchday squad has a club appearance in the prior 90 days",
            "n_test_matches": len(idx_cov),
            "results": {k: summ([v[j] for j in idx_cov]) for k, v in res.items()} if idx_cov else {}},
        "avg_actual_starters_never_seen_in_prior_squads": round(statistics.mean(unseen), 2),
        "by_match": [{"seq": i + 1, "date": H[i]["date"], "opponent": H[i]["opp_name"],
                      **{k: res[k][i - MIN_TRAIN] for k in res}} for i in range(MIN_TRAIN, n)],
    }
    bins = [(0, .1), (.1, .3), (.3, .5), (.5, .7), (.7, .9), (.9, 1.01)]
    cal = []
    for lo, hi in bins:
        sel = [(p, y) for p, y in calib if lo <= p < hi]
        if sel:
            cal.append({"bin": f"{lo:.1f}-{min(hi, 1):.1f}", "n_player_matches": len(sel), "mean_predicted": round(statistics.mean(p for p, _ in sel), 3),
                        "observed_start_rate": beta_ci(sum(y for _, y in sel), len(sel))})
    xi_bt["calibration_primary_model"] = cal

    # ---- current-form XI probabilities ----------------------------------------------------
    probs = start_probs(hist, n, AS_OF, clubs, **variants["primary: half-life 8 + club-minutes adjustment"])
    probs_noadj = start_probs(hist, n, AS_OF, clubs, half_life=HALF_LIFE_MATCHES, club_adj=False)
    W = sum(0.5 ** ((n - 1 - j) / HALF_LIFE_MATCHES) for j in range(n))
    a, s = defaultdict(float), defaultdict(float)
    for j in range(n):
        w = 0.5 ** ((n - 1 - j) / HALF_LIFE_MATCHES)
        for p in H[j]["squad_ids"]:
            a[p] += w
        for p in H[j]["xi_ids"]:
            s[p] += w
    rng = random.Random(4242)
    plist = []
    for p, (pr, z) in probs.items():
        if pr < 0.03:
            continue
        # 80% credible interval: sample both Beta components (P(squad), P(start|squad)), then apply the club odds multiplier
        draws = []
        for _ in range(1500):
            ps = rng.betavariate(a[p] + KAPPA_SQUAD * PI_SQUAD, max(W - a[p], 0) + KAPPA_SQUAD * (1 - PI_SQUAD))
            pt = rng.betavariate(s[p] + KAPPA_START * PI_START, max(a[p] - s[p], 0) + KAPPA_START * (1 - PI_START))
            x = ps * pt
            if z is not None:
                o = x / (1 - x) * math.exp(CLUB_BETA * (z - 0.5))
                x = o / (1 + o)
            draws.append(x)
        draws.sort()
        plist.append({"player": names.get(p) or conn.execute("SELECT name FROM players WHERE id=?", (p,)).fetchone()[0],
                      "is_goalkeeper": p in gk_ids, "p_start": round(pr, 3), "p_start_ci80": [round(draws[150], 3), round(draws[1349], 3)],
                      "p_start_without_club_adjustment": round(probs_noadj[p][0], 3), "club_activity_z": z,
                      "weighted_starts": round(s[p], 2), "weighted_squad_apps": round(a[p], 2), "effective_matches_weight": round(W, 2),
                      "starts_last_5_hassan_matches": sum(1 for m in H[-5:] if p in m["xi_ids"]),
                      "starts_total": sum(1 for m in H if p in m["xi_ids"])})
    plist.sort(key=lambda x: -x["p_start"])
    xi_next = pick_xi(probs, gk_ids)
    xi_info = {
        "as_of": AS_OF,
        "note": ("Start probability for a next Hassan match (opponent unknown). P(start) = P(in matchday squad) x P(start | in squad), each a Beta posterior on recency-weighted counts. "
                 "The club adjustment (only for players with a club appearance in the prior 90 days) multiplies the odds by exp(1.0 x (z-0.5)), z = min(1, club minutes in the last 35 days / 180). "
                 "Interval = 80% credible interval from sampling both Betas. A player absent from the last squad or not covered shows no adjustment (club_activity_z null). "
                 "Squad selection reasons (injury, suspension, form, a fresh call-up list) are not modelled - the list for the real squad should override these numbers."),
        "predicted_xi_top_gk_plus_10": [names.get(p) or conn.execute("SELECT name FROM players WHERE id=?", (p,)).fetchone()[0] for p in xi_next],
        "players": plist[:35],
    }
    subs_note = ("Expected substitution windows by game state are in tendencies.substitutions.by_game_state (median, IQR, bootstrap CI of the median, and Beta-binomial bucket shares with 90% CI; n shown).")
    return {"formation_backtest": formation_bt, "formation_next_match_scenarios": formation_next, "xi_backtest": xi_bt,
            "xi_next_match": xi_info, "substitution_windows_pointer": subs_note,
            "parameters": {"half_life_matches": HALF_LIFE_MATCHES, "kappa_start": KAPPA_START, "pi_start": PI_START, "kappa_squad": KAPPA_SQUAD,
                           "pi_squad": PI_SQUAD, "club_window_days": CLUB_WINDOW_DAYS, "club_cover_days": CLUB_COVER_DAYS,
                           "club_full_minutes": CLUB_FULL_MINUTES, "club_beta": CLUB_BETA, "formation_alpha": FORMATION_ALPHA,
                           "ci_level": CI_LEVEL, "min_train_matches": MIN_TRAIN}}


# ================================================================================================
# notes, methodology, summary
# ================================================================================================
def read_notes():
    if not NOTES_CSV.exists():
        return {"file": str(NOTES_CSV.relative_to(ROOT)), "notes": [], "n": 0}
    with NOTES_CSV.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return {"file": str(NOTES_CSV.relative_to(ROOT)), "n": len(rows),
            "note": "Paraphrased from public reports we fetched through etl/http.py; match_date may be a range for tournament-level notes. Not a complete tactical record.",
            "notes": rows}


def build_methodology(conn, H, T, recon, clubs, n_score_bad, bio, notes):
    return {
        "as_of": AS_OF,
        "sources": [
            {"name": "FotMob match pages (lineups, formations, events, shots/xG, coach tag)", "url": "https://www.fotmob.com/", "use": "per-match source_url in record_results.matches"},
            {"name": "Wikipedia - Hossam Hassan", "url": WIKI_URL, "use": "bio, playing/managerial career, per-job P/W/D/L, Egypt record used in the reconciliation"},
            {"name": "Wikipedia - Egypt national football team results (2020-present)", "url": WIKI_RESULTS_URL, "use": "fixture cross-check (wiki_fixtures)"},
            {"name": "Wikipedia - Egypt results (unofficial matches)", "url": WIKI_UNOFF_URL, "use": "checked for unlisted matches in the tenure window"},
            {"name": "eloratings.net Egypt.tsv", "url": ELO_URL, "use": "pre-match Elo of Egypt and opponent"},
            {"name": "FotMob player pages (club match minutes)", "url": "https://www.fotmob.com/", "use": "player_club_minutes for club-at-match-time and recent-minutes adjustment"},
        ] + [{"name": "tactical note source", "url": u, "use": "data/manual/coach_notes.csv"} for u in sorted({n["source_url"] for n in notes["notes"]})],
        "coverage": {
            "hassan_matches": len(H), "period": [H[0]["date"], H[-1]["date"]],
            "matches_with_formation": sum(1 for m in H if m["formation_egypt"]),
            "matches_with_full_xi": sum(1 for m in H if len(m["xi_ids"]) == 11),
            "matches_with_bench_and_sub_events": sum(1 for m in H if m["bench"] and any(e["type"] == "sub_off" for e in m["events"])),
            "matches_with_events": sum(1 for m in H if m["n_events"]),
            "matches_with_shots_xg": sum(1 for m in H if m["has_shots"]),
            "matches_where_goal_events_do_not_sum_to_score": n_score_bad,
            "player_club_minutes_range": [clubs.cover_min, clubs.cover_max],
            "coach_tag_provenance": dict(Counter(m["coach_source"] for m in H)),
        },
        "gaps": [
            "Shots/xG exist only for 12 of 36 matches (AFCON 2025 from the group stage on, and the World Cup); every shot-based figure carries that coverage.",
            "Player minutes are nominal (sub minute vs 90/120); stoppage time is not modelled.",
            "Formations are the starting shape from FotMob; in-match changes are not recorded. FotMob's label can differ from press descriptions (e.g. Iran 2026-06-27: FotMob 4-2-3-1 with Ziko as lone striker, Daily News Egypt describes Trezeguet and Salah as an attacking duo).",
            "Club at match time only exists from 2025-08-08 and only within FotMob's bounded recent-matches window per player; otherwise the CURRENT club is used, which flatters clubs players joined later (flagged with the basis counts in tendencies.squad_core).",
            "Neutral flag is a heuristic (AFCON 2025 in Morocco is tagged neutral for Egypt; friendlies in Egypt are home).",
            f"{sum(1 for m in H if m['coach_source'] != 'fotmob')} of {len(H)} matches are tagged Hassan by inference (Wikipedia tenure / bracketing) rather than by FotMob itself; all fall inside his tenure window.",
            "Club-era (2008-2023) match-level data not built; Wikipedia's per-job record is used.",
            f"Tactical notes: {notes['n']} notes from press coverage; no fetched source described set-pieces or pressing in enough detail to paraphrase honestly, so those categories are empty.",
            "Ahram Online (contract renewal) returned HTTP 403 and was not fetched or evaded.",
            "Squad availability (injury, suspension) is not modelled in the XI probabilities.",
        ],
        "reconciliation_summary": recon["unexplained"],
        "model_assumptions": [
            f"Interval convention: {int(CI_LEVEL*100)}% equal-tailed credible interval of a Beta(1,1)-prior binomial, sampled deterministically (seeded).",
            "Formation prediction: hierarchical Dirichlet-multinomial, level 0 = smoothed overall frequency (+0.5 per formation), level 1 = opponent Elo band, level 2 = band x venue; each level shrinks toward the one above with strength alpha=3. Elo bands: <1450 / 1450-1699 / >=1700 (absolute opponent Elo).",
            "Baselines in the backtest: most frequent formation so far, and the previous match's formation.",
            f"XI model: P(start) = P(in matchday squad) x P(start | squad), Beta posteriors on counts weighted by 0.5^(matches ago / {HALF_LIFE_MATCHES:g}) (half-life {HALF_LIFE_MATCHES:g} Hassan matches, fixed a priori; sensitivity to 4 and 16 shown, not used for selection). Priors: P(start|squad) ~ Beta({KAPPA_START*PI_START:.2f},{KAPPA_START*(1-PI_START):.2f}); P(squad) ~ Beta({KAPPA_SQUAD*PI_SQUAD:.2f},{KAPPA_SQUAD*(1-PI_SQUAD):.2f}).",
            f"Club adjustment: odds x exp({CLUB_BETA:g} x (z-0.5)) where z = min(1, club minutes in prior {CLUB_WINDOW_DAYS} days / {CLUB_FULL_MINUTES:g}); applied only when the player has a club appearance in the prior {CLUB_COVER_DAYS} days.",
            "Predicted XI = top goalkeeper (anyone who has started in slot 11) + top 10 outfield players; position balance is not enforced.",
            "Substitution windows are empirical (n stated); game state uses goal events at the sub minute.",
            "Results treat shootouts as draws (regulation/extra-time score); shootout results are listed separately.",
            "Hassan-era coverage is small (36 matches, ~26 backtest matches): every rate has a wide interval and none of the models should be read as more than descriptive.",
        ],
    }


def summary_md(ex):
    r = ex["record_results"]["overall_shootouts_as_draws"]
    bt = ex["predictive"]["xi_backtest"]
    fb = ex["predictive"]["formation_backtest"]
    rec = ex["reconciliation"]
    t = ex["tendencies"]
    L = []
    L.append(f"# Hossam Hassan - coach profile (module A) - summary\n\nAs of {ex['meta']['as_of']}. Full data: `data/export/coach_hassan.json`. Built only from public data (FotMob, Wikipedia, eloratings.net).\n")
    L.append(f"## Record\n\n{ex['record_results']['scope']}\n\n"
             f"- P{r['P']} W{r['W']} D{r['D']} L{r['L']}, goals {r['goals_for']}-{r['goals_against']}, {r['points_per_game']} points per game (shootouts = draws). "
             f"Shootouts: " + "; ".join(f"{s['opponent']} {s['score']} ({s['shootout']} p)" for s in ex['record_results']['shootout_matches']) + ".\n"
             f"- Elo expectation: actual minus expected = {ex['record_results']['elo_expectation']['actual_minus_expected_per_match']:+.3f} points per match (n={ex['record_results']['elo_expectation']['matches']}).\n")
    L.append("## Reconciliation with Wikipedia (P37 W22 D10 L5)\n")
    L.append(f"- {rec['correction_to_brief']}\n- Best fit: {rec['closest']['scope']} / {rec['closest']['convention']}: W{rec['closest']['W']} D{rec['closest']['D']} L{rec['closest']['L']} (P{rec['closest']['P']}).\n- {rec['unexplained']}\n")
    L.append("## Backtest (matches 11 to 36, each predicted from earlier matches only)\n")
    L.append(f"- Formation, exact hit rate: {fb['exact_hit_rate']['k']}/{fb['exact_hit_rate']['n']} = {fb['exact_hit_rate']['mean']:.2f} (90% CI {fb['exact_hit_rate']['lo']:.2f}-{fb['exact_hit_rate']['hi']:.2f}); "
             f"back-3/4/5 family: {fb['family_hit_rate']['mean']:.2f} ({fb['family_hit_rate']['lo']:.2f}-{fb['family_hit_rate']['hi']:.2f}); "
             f"baselines: most-frequent-so-far {fb['baseline_most_frequent_so_far']['mean']:.2f}, previous-match formation {fb['baseline_same_as_previous_match']['mean']:.2f}.")
    for k, v in bt["models"].items():
        L.append(f"- XI, {k}: {v['mean_correct_of_11']} of 11 correct on average (range {v['min']}-{v['max']}, n={v['n_test_matches']}).")
    cov = bt["models_on_club_covered_matches"]
    if cov.get("results"):
        L.append(f"- On the {cov['n_test_matches']} matches with club-minutes coverage: " + "; ".join(f"{k.split(':')[0]} {v['mean_correct_of_11']}" for k, v in cov["results"].items()) + ".")
    L.append("")
    L.append("## Key findings\n")
    for f in key_findings(ex):
        L.append(f"- {f}")
    L.append("\n## Caveats\n")
    for g in ex["methodology"]["gaps"]:
        L.append(f"- {g}")
    L.append("- Small sample: 36 matches, 26 backtest matches. Intervals are wide by design; none of the models is more than descriptive.")
    L.append("- The 3 Arab Cup games of Dec 2025 (Helmi Toulan: D, D, L) are excluded from all Hassan stats.")
    return "\n".join(L) + "\n"


def key_findings(ex):
    t, out = ex["tendencies"], []
    f = t["formations"]["overall"]
    fam = t["formations"]["overall_family"]
    top3 = ", ".join(f"{k} {v['count']}" for k, v in list(f.items())[:4])
    out.append(f"Formation is fluid: {len(f)} different starting shapes in {t['formations']['overall'][next(iter(f))]['n']} matches ({top3}); families " +
               ", ".join(f"{k} {v['count']}" for k, v in fam.items()) + ".")
    eb = ex["record_results"]["by_opponent_elo_band"]
    out.append("Record by opponent Elo band (pre-match): " + "; ".join(
        f"{k} W{v['W']} D{v['D']} L{v['L']} ({v['points_per_game']} ppg, n={v['P']})" for k, v in eb.items()) +
        ". All 5 defeats came against opponents rated 1700 or higher.")
    ph = t["formations"]["by_phase"]
    a = ph.get("3 AFCON 2025")
    if a:
        out.append("Back-3/5 phase: AFCON 2025 families " + ", ".join(f"{k} {v['count']}" for k, v in a["families"].items()) +
                   "; World Cup 2026 families " + ", ".join(f"{k} {v['count']}" for k, v in ph["5 World Cup 2026"]["families"].items()) + ".")
    xc = t["xi_continuity"]
    out.append(f"XI churn: {xc['mean_changes']} changes per match on average (median {xc['median_changes']}, max {xc['max_changes']['changes']} on {xc['max_changes']['date']}).")
    g = t["goalkeepers"]
    out.append(f"Goalkeeper: El-Shenawy started {g['el_shenawy_starts_before_2026_01_17']} of the first {len(g['sequence']) - g['matches_after_2026_01_17']} matches up to the AFCON bronze game; since then he started {g['el_shenawy_starts_after_2026_01_17']} "
               f"and Shobeir {g['shobeir_starts_after_2026_01_17']} of {g['matches_after_2026_01_17']}. El-Shenawy in the 2026-09-25 matchday squad: {g['el_shenawy_in_latest_matchday_squad']}.")
    s = t["squad_core"]["current_club"]["shares"]
    mt = t["squad_core"]["match_time_where_covered"]["shares"]
    out.append(f"Squad core (starter slots): Al Ahly {s['Al Ahly']['mean']:.0%}, other Egyptian {s['other Egyptian']['mean']:.0%}, abroad {s['abroad']['mean']:.0%} on the current-club basis; "
               f"match-time-where-covered basis: {mt['Al Ahly']['mean']:.0%} / {mt['other Egyptian']['mean']:.0%} / {mt['abroad']['mean']:.0%} (mixed basis).")
    sub = t["substitutions"]["by_ordinal"]
    if "1st" in sub:
        out.append(f"Substitutions: median 1st sub at minute {sub['1st']['minutes']['median']} (IQR {sub['1st']['minutes']['q1']}-{sub['1st']['minutes']['q3']}, n={sub['1st']['minutes']['n']}), "
                   f"2nd at {sub.get('2nd', {}).get('minutes', {}).get('median')}, 3rd+ at {sub.get('3rd+', {}).get('minutes', {}).get('median')}.")
    ac = t["after_conceding_first"]
    if ac["matches_conceded_first"]:
        out.append(f"After conceding first (n={ac['matches_conceded_first']}): W{sum(1 for m in ac['matches'] if m['result']=='W')} D{sum(1 for m in ac['matches'] if m['result']=='D')} L{sum(1 for m in ac['matches'] if m['result']=='L')}; "
                   f"not-lost rate {ac['not_lost']['mean']:.2f} (90% CI {ac['not_lost']['lo']:.2f}-{ac['not_lost']['hi']:.2f}).")
    sal = t["salah"]["by_coach"].get(COACH)
    if sal:
        out.append("Salah under Hassan: " + ", ".join(f"{p['role']} x{p['starts']}" for p in sal["positions"][:3]) + f" ({sal['starts']} starts, {sal['starts_with_position_data']} with position data).")
    return out


if __name__ == "__main__":
    con = dbm.connect()
    con.execute("PRAGMA busy_timeout=30000")
    res = run(con)
    print(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size} bytes) and {OUT_MD}")
