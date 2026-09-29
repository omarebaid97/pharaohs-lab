"""Module D - opponent dossiers for Egypt's next competitive opponents.

    python -m etl.models.opponent_dossiers

Reads data/pharaohs.db + data/export/coach_hassan.json + data/statsbomb/ (raw AFCON 2023 open data) and fetches
FotMob team/match pages, eloratings.net TSVs and Wikipedia through the shared polite client (etl/http.py, cached).
Writes
  data/export/opponent_dossiers.json
  data/export/opponent_dossiers_summary.md
Own tables (rebuilt every run, prefix opp_): opp_fixtures, opp_form, opp_lineups, opp_goals, opp_sb_metrics.
Frequencies and one documented Elo formula; no ML. Nothing is invented: unannounced things are reported as such.
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from bs4 import BeautifulSoup

from .. import db as dbm
from .. import http
from ..sources import fotmob, opp_fotmob

ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = ROOT / "data" / "export"
SB_DIR = ROOT / "data" / "statsbomb"
OUT_JSON = EXPORT_DIR / "opponent_dossiers.json"
OUT_MD = EXPORT_DIR / "opponent_dossiers_summary.md"
HASSAN_JSON = EXPORT_DIR / "coach_hassan.json"
AS_OF = os.environ.get("PHARAOHS_AS_OF") or date.today().isoformat()
SINCE_12M = (date.fromisoformat(AS_OF) - timedelta(days=365)).isoformat()

ELO_WORLD = "https://www.eloratings.net/World.tsv"
ELO_TEAMS = "https://www.eloratings.net/en.teams.tsv"
ELO_EGYPT = "https://www.eloratings.net/Egypt.tsv"
ELO_ABOUT = "https://www.eloratings.net/about"
WIKI_QUAL = "https://en.wikipedia.org/wiki/2027_Africa_Cup_of_Nations_qualification"
WIKI_WCQ = "https://en.wikipedia.org/wiki/2030_FIFA_World_Cup_qualification_(CAF)"
WIKI_API = "https://en.wikipedia.org/w/api.php"
SB_ATTR = "Data provided by StatsBomb (open data, https://github.com/statsbomb/open-data)"
SB_LABEL = "AFCON 2023 (StatsBomb), may be outdated"
HOME_ADV = 100  # Elo points added to the home side (eloratings.net convention)

# name in FotMob, FotMob id, slug, eloratings code + Egypt-opponent page name, StatsBomb team name
OPPONENTS = [
    dict(name="South Sudan", fm_id=408231, slug="south-sudan", elo_code="SS", sb_name=None),
    dict(name="South Africa", fm_id=6316, slug="south-africa", elo_code="ZA", sb_name="South Africa"),
    dict(name="Malawi", fm_id=6020, slug="malawi", elo_code="MW", sb_name=None),
    dict(name="Angola", fm_id=6712, slug="angola", elo_code="AO", sb_name="Angola"),
]
BINS = [("0-15", 0, 15), ("16-30", 16, 30), ("31-45+", 31, 45), ("46-60", 46, 60), ("61-75", 61, 75), ("76-90+", 76, 90)]

DDL = """
CREATE TABLE IF NOT EXISTS opp_fixtures (fm_match_id TEXT PRIMARY KEY, opponent TEXT, date TEXT, kickoff_utc TEXT, competition TEXT, round TEXT,
  home TEXT, away TEXT, egypt_side TEXT, venue TEXT, city TEXT, country TEXT, date_tbd INTEGER, finished INTEGER, score TEXT, source_url TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS opp_form (fm_match_id TEXT, opponent TEXT, date TEXT, competition TEXT, side TEXT, vs TEXT, gf INTEGER, ga INTEGER,
  result TEXT, decided_by TEXT, shootout TEXT, formation TEXT, vs_formation TEXT, coach TEXT, venue TEXT, source_url TEXT, fetched_at TEXT,
  PRIMARY KEY (fm_match_id, opponent));
CREATE TABLE IF NOT EXISTS opp_lineups (fm_match_id TEXT, opponent TEXT, date TEXT, fm_player_id INTEGER, player TEXT, club TEXT, pos_id INTEGER,
  pos_group TEXT, started INTEGER, minutes INTEGER, rating REAL, source_url TEXT, PRIMARY KEY (fm_match_id, opponent, fm_player_id));
CREATE TABLE IF NOT EXISTS opp_goals (fm_match_id TEXT, opponent TEXT, date TEXT, minute INTEGER, added INTEGER, for_team INTEGER, situation TEXT,
  player TEXT, has_shotmap INTEGER, xg REAL, source_url TEXT);
CREATE TABLE IF NOT EXISTS opp_sb_metrics (opponent TEXT, metric TEXT, value_json TEXT, source TEXT, PRIMARY KEY (opponent, metric));
"""


def log(*a):
    print(*a, flush=True)


def connect():
    con = dbm.connect()
    con.execute("PRAGMA busy_timeout=120000")
    return con


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =========================================================================== Elo
def _i(x):
    """Parse eloratings.net signed ints; the minus sign is U+2212 (sometimes mojibake'd to 'â' + control chars)."""
    neg = any(c in x for c in ("\u2212", "\u00e2", "-"))
    digits = re.sub(r"\D", "", x)
    return -int(digits) if neg else int(digits or 0)


def load_elo():
    world = {}
    for line in http.get(ELO_WORLD, ttl=6 * 3600).splitlines():
        f = line.split("\t")
        if len(f) > 4:
            world[f[2]] = dict(rank=int(f[0]), rating=int(f[3]))
    names = {}
    for line in http.get(ELO_TEAMS, ttl=7 * 86400).splitlines():
        f = line.split("\t")
        if len(f) >= 2:
            names[f[0]] = f[1]
    return world, names


def egypt_history():
    """All Egypt rows from eloratings.net with pre-match ratings + venue flag. Pre-match = post - change."""
    rows = []
    for line in http.get(ELO_EGYPT, ttl=6 * 3600).splitlines():
        f = line.split("\t")
        if len(f) < 13:
            continue
        try:
            d = f"{int(f[0]):04d}-{int(f[1]):02d}-{int(f[2]):02d}"
            t1, t2, s1, s2 = f[3], f[4], int(f[5]), int(f[6])
            chg, r1, r2 = _i(f[9]), int(f[10]), int(f[11])
        except (ValueError, IndexError):
            continue
        venue = f[8]
        if t1 == "EG":
            egy_home = venue in ("", "EG")
            neutral = venue not in ("", "EG", t1) and venue != ""
            rows.append(dict(date=d, opp=t2, gf=s1, ga=s2, egy_pre=r1 - chg, opp_pre=r2 + chg, comp=f[7],
                             egy_home=1 if (venue in ("", "EG")) else 0, opp_home=1 if venue == t2 else 0))
        elif t2 == "EG":
            rows.append(dict(date=d, opp=t1, gf=s2, ga=s1, egy_pre=r2 + chg, opp_pre=r1 - chg, comp=f[7],
                             egy_home=1 if venue == "EG" else 0, opp_home=1 if venue in ("", t1) else 0))
    return rows


def elo_expect(egy, opp, egy_home=False, opp_home=False):
    dr = egy - opp + (HOME_ADV if egy_home else 0) - (HOME_ADV if opp_home else 0)
    return dr, 1.0 / (1.0 + 10 ** (-dr / 400.0))


def fit_draw_scale(hist, since="1990-01-01"):
    """pD = d0 * 4E(1-E): d0 fitted so the mean model draw rate equals Egypt's empirical draw rate in Elo history."""
    xs = []
    for r in hist:
        if r["date"] < since:
            continue
        _, e = elo_expect(r["egy_pre"], r["opp_pre"], r["egy_home"], r["opp_home"])
        xs.append((4 * e * (1 - e), r["gf"] == r["ga"]))
    if not xs:
        return 0.25, 0
    draw_rate = sum(d for _, d in xs) / len(xs)
    d0 = draw_rate / (sum(k for k, _ in xs) / len(xs))
    return d0, len(xs)


def wdl(e, d0):
    """Split Elo expected score E into W/D/L keeping E exactly: pW + pD/2 = E."""
    pd = min(d0 * 4 * e * (1 - e), 2 * min(e, 1 - e))
    return dict(win=round(e - pd / 2, 3), draw=round(pd, 3), loss=round(1 - e - pd / 2, 3))


def all_time_h2h(hist, code):
    rows = [r for r in hist if r["opp"] == code]
    w = sum(r["gf"] > r["ga"] for r in rows)
    d = sum(r["gf"] == r["ga"] for r in rows)
    l = sum(r["gf"] < r["ga"] for r in rows)
    since2018 = [r for r in rows if r["date"] >= "2018-01-01"]
    return dict(P=len(rows), W=w, D=d, L=l, GF=sum(r["gf"] for r in rows), GA=sum(r["ga"] for r in rows),
                first=rows[0]["date"] if rows else None, last=rows[-1]["date"] if rows else None,
                last5=[dict(date=r["date"], score=f"{r['gf']}-{r['ga']}", comp=r["comp"]) for r in rows[-5:]][::-1],
                note="eloratings.net match history for Egypt (matches that site counts; shootouts scored as the on-pitch result); "
                     "includes friendlies. No Wikipedia all-time head-to-head page exists for Egypt (checked), so this is the all-time source.",
                source_url=ELO_EGYPT)


# ===================================================================== fixtures
def egypt_fixtures(opps):
    """Egypt's remaining/played fixtures vs each opponent, from FotMob's Egypt team page + match pages."""
    fx, tp = fotmob.team_fixtures(ttl=3600)
    by_id = {o["fm_id"]: o for o in opps}
    res = defaultdict(list)
    other_upcoming = []
    for f in fx:
        st = f["status"]
        oid = f["home"]["id"] if f["away"]["id"] == fotmob.EGYPT_ID else f["away"]["id"]
        mid = f["pageUrl"].split("#")[-1]
        upcoming = not st.get("finished") and not st.get("cancelled")
        comp = f["tournament"]["name"]
        if oid in by_id and (upcoming or (st["utcTime"][:10] >= "2026-09-01" and "Qualification" in comp)):
            det = opp_fotmob.fixture_detail(mid)
            res[oid].append(dict(
                fm_match_id=mid, date=st["utcTime"][:10], kickoff_utc=st["utcTime"], competition=comp, round=det["round"],
                home=f["home"]["name"], away=f["away"]["name"], egypt_side="home" if f["home"]["id"] == fotmob.EGYPT_ID else "away",
                venue=det["venue"], city=det["city"], country=det["country"], date_tbd=det["date_tbd"],
                finished=bool(st.get("finished")), score=st.get("scoreStr") if st.get("finished") else None,
                fotmob_h2h_summary=det["h2h_summary"], fotmob_h2h_matches=det["h2h_matches"],
                source_url=f"https://www.fotmob.com/match/{mid}"))
        elif upcoming:
            other_upcoming.append(dict(date=st["utcTime"][:10], opponent=f["home"]["name"] if f["away"]["id"] == fotmob.EGYPT_ID else f["away"]["name"],
                                       competition=comp))
    group = None
    for tb in (tp.get("table") or [{}])[0].get("data", {}).get("tables", []):
        rows = tb["table"]["all"]
        if any(r["id"] == fotmob.EGYPT_ID for r in rows):
            group = dict(name=tb["leagueName"], table=[dict(team=r["name"], P=r["played"], W=r["wins"], D=r["draws"], L=r["losses"],
                                                            GF_GA=r["scoresStr"], pts=r["pts"]) for r in rows])
    return res, other_upcoming, group


def wiki_crosscheck(fix_by_opp):
    """Confirm Group B membership + Egypt fixture dates against Wikipedia's qualification article."""
    r = http.get_json(WIKI_API, params={"action": "parse", "page": "2027 Africa Cup of Nations qualification", "prop": "text",
                                        "format": "json", "formatversion": 2, "redirects": 1}, ttl=6 * 3600)
    txt = re.sub(r"\s+", " ", BeautifulSoup(r["parse"]["text"], "lxml").get_text(" ", strip=True))
    i = txt.find("Group B [ edit ]")
    j = txt.find("Group C [ edit ]")
    sec = txt[i:j] if i >= 0 else ""
    out = {}
    for opp, fxs in fix_by_opp.items():
        chk = []
        for f in fxs:
            m = re.search(r"\(\s*%s\s*\)[^()]{0,90}?%s v %s|\(\s*%s\s*\)[^()]{0,90}?%s [0-9]+\S[0-9]+ %s|\(\s*%s\s*\)[^()]{0,90}?%s [0-9] . [0-9] %s" % (
                (f["date"], f["home"], f["away"]) * 3), sec)
            chk.append(dict(date=f["date"], fixture=f"{f['home']} v {f['away']}", listed_on_wikipedia=bool(m)))
        out[opp] = chk
    return dict(group_b_teams_on_wikipedia=[t for t in ("Egypt", "Angola", "Malawi", "South Sudan") if t in sec], checks=out,
                url=WIKI_QUAL)


def wcq_status():
    """2030 WC qualifying (CAF): report what Wikipedia says; do not invent a draw."""
    r = http.get_json(WIKI_API, params={"action": "parse", "page": "2030 FIFA World Cup qualification (CAF)", "prop": "text",
                                        "format": "json", "formatversion": 2, "redirects": 1}, ttl=6 * 3600)
    txt = re.sub(r"\s+", " ", BeautifulSoup(r["parse"]["text"], "lxml").get_text(" ", strip=True))
    caf_fmt = bool(re.search(r"CAF\s*\[ edit \]", txt))
    egypt_hits = len(re.findall(r"Egypt", txt))
    return dict(
        status="not drawn / not announced",
        detail="As of the Wikipedia article on 2030 FIFA World Cup qualification, only CONCACAF and UEFA have published their qualifying format; "
               "there is no CAF format section, no draw and no Egypt fixtures. No 2030 World Cup qualifying opponent is included in these dossiers.",
        caf_format_section_present=caf_fmt, egypt_mentions_on_page=egypt_hits, source_url=WIKI_WCQ, checked=AS_OF)


# ===================================================================== analysis helpers
def h2h_db(conn, name):
    rows = conn.execute(
        """SELECT m.date, m.competition, m.stage, m.home_score, m.away_score, m.egypt_side, m.decided_by, m.pens_home, m.pens_away,
                  m.venue, m.venue_city, m.neutral, m.egypt_elo, m.opponent_elo, m.source_url, m.formation_egypt, m.formation_opp
           FROM matches m JOIN teams th ON th.id=m.home_team_id JOIN teams ta ON ta.id=m.away_team_id
           WHERE m.is_egypt=1 AND m.source='fotmob' AND m.date>='2018-01-01'
             AND ((m.egypt_side='home' AND ta.name=?) OR (m.egypt_side='away' AND th.name=?))
           ORDER BY m.date DESC""", (name, name)).fetchall()
    out, W, D, L, GF, GA = [], 0, 0, 0, 0, 0
    for r in rows:
        gf, ga = (r["home_score"], r["away_score"]) if r["egypt_side"] == "home" else (r["away_score"], r["home_score"])
        if gf is None:
            continue
        res = "W" if gf > ga else "L" if gf < ga else "D"
        W += res == "W"; D += res == "D"; L += res == "L"; GF += gf; GA += ga
        out.append(dict(date=r["date"], competition=r["competition"], venue=r["venue"], egypt_side=r["egypt_side"], score=f"{gf}-{ga}",
                        result=res, decided_by=r["decided_by"], egypt_formation=r["formation_egypt"], opp_formation=r["formation_opp"],
                        egypt_elo=r["egypt_elo"], opp_elo=r["opponent_elo"], source_url=r["source_url"]))
    return dict(scope="Egypt matches in our DB since 2018-01-01 (FotMob source)", P=len(out), W=W, D=D, L=L, GF=GF, GA=GA, matches=out)


def form_block(matches, squad):
    last10 = matches[:10]
    coach = next((m["coach"] for m in matches if m["coach"]), None)
    coach_src = "match lineup (most recent with a coach listed)"
    if squad.get("coach") and squad["coach"].get("name"):
        coach, coach_src = squad["coach"]["name"], "FotMob team page squad tab"
    coaches = Counter(m["coach"] for m in last10 if m["coach"])
    rec = Counter(m["result"] for m in last10)
    fcount = Counter(m["formation"] for m in last10 if m["formation"])
    return dict(
        n=len(last10), span=dict(first=last10[-1]["date"] if last10 else None, last=last10[0]["date"] if last10 else None),
        record=dict(W=rec["W"], D=rec["D"], L=rec["L"]),
        goals_for=sum(m["gf"] for m in last10), goals_against=sum(m["ga"] for m in last10),
        current_coach=dict(name=coach, source=coach_src, coaches_in_last10=dict(coaches)),
        formations_last10=dict(fcount),
        matches=[dict(date=m["date"], competition=m["competition"], venue_side=m["side"], opponent=m["opponent"], score=f"{m['gf']}-{m['ga']}",
                      result=m["result"], decided_by=m["decided_by"], shootout=m["shootout"], formation=m["formation"],
                      opponent_formation=m["opp_formation"], coach=m["coach"], source_url=m["url"]) for m in last10],
        source_urls=[m["url"] for m in last10])


def key_players(matches, squad):
    win = [m for m in matches if m["date"] >= SINCE_12M and m["has_lineup"]]
    agg: dict[int, dict] = {}
    for m in sorted(win, key=lambda x: x["date"]):
        for p in m["players"]:
            a = agg.setdefault(p["fm_id"], dict(fm_id=p["fm_id"], name=p["name"], starts=0, apps=0, minutes=0, ratings=[], groups=Counter(),
                                                 slots=Counter(), clubs=[]))
            if p["started"] or p["minutes"] > 0:
                a["apps"] += 1
            a["starts"] += p["started"]
            a["minutes"] += p["minutes"]
            if p["rating"]:
                a["ratings"].append(p["rating"])
            if p["started"]:
                a["groups"][p["pos_group"]] += 1
                a["slots"][p["pos_id"]] += 1
            if p["club"]:
                a["clubs"].append(p["club"])
    rows = []
    for a in agg.values():
        if a["apps"] == 0:
            continue
        sq = squad["players"].get(a["fm_id"], {})
        rows.append(dict(player=a["name"], fotmob_id=a["fm_id"], club=a["clubs"][-1] if a["clubs"] else sq.get("club"),
                         position=sq.get("pos") or (a["groups"].most_common(1)[0][0] if a["groups"] else None),
                         position_group=a["groups"].most_common(1)[0][0] if a["groups"] else None,
                         starts=a["starts"], appearances=a["apps"], minutes=a["minutes"],
                         avg_rating=round(sum(a["ratings"]) / len(a["ratings"]), 2) if a["ratings"] else None))
    rows.sort(key=lambda r: (-r["starts"], -r["minutes"]))
    # typical XI: group caps from the modal formation's actual lineups
    xi, note = [], None
    fcount = Counter(m["formation"] for m in win if m["formation"])
    if fcount and rows:
        modal = fcount.most_common(1)[0][0]
        counts = defaultdict(Counter)
        for m in win:
            if m["formation"] == modal:
                c = Counter(p["pos_group"] for p in m["players"] if p["started"])
                for g in ("GK", "DEF", "MID", "FWD"):
                    counts[g][c.get(g, 0)] += 1
        caps = {g: counts[g].most_common(1)[0][0] for g in counts}
        if sum(caps.values()) != 11:
            caps = None
        left = dict(caps) if caps else None
        for r in rows:
            g = r["position_group"]
            if left is None or (g in left and left[g] > 0):
                xi.append(r)
                if left:
                    left[g] -= 1
            if len(xi) == 11:
                break
        if len(xi) < 11:
            for r in rows:
                if r not in xi:
                    xi.append(r)
                if len(xi) == 11:
                    break
        note = f"Filled greedily by starts within the position-group counts of the modal formation ({modal}); groups from FotMob pitch slots."
    return dict(window=f"{SINCE_12M} to {AS_OF}", matches_with_lineup=len(win), matches_in_window=len([m for m in matches if m["date"] >= SINCE_12M]),
                typical_xi=[dict(player=r["player"], club=r["club"], position=r["position"], starts=r["starts"], minutes=r["minutes"]) for r in xi],
                typical_xi_note=note, most_started=rows[:15], source_urls=[m["url"] for m in win])


def style_block(matches):
    win = matches  # style over all fetched matches (<= 16, ~12 months+)
    win12 = [m for m in win if m["date"] >= SINCE_12M]
    use = win12 if len(win12) >= 5 else win[:10]
    fam = Counter(m["formation"] for m in use if m["formation"])
    by_side = {s: dict(Counter(m["formation"] for m in use if m["formation"] and m["side"] == s)) for s in ("home", "away")}
    per = {b[0]: dict(scored=0, conceded=0) for b in BINS}
    per["extra time"] = dict(scored=0, conceded=0)
    n_goal_matches = 0
    sits = {"scored": Counter(), "conceded": Counter()}
    shotmap_matches = 0
    for m in use:
        if m["has_shotmap"]:
            shotmap_matches += 1
        for g in m["goals"]:
            key = "scored" if g["for_team"] else "conceded"
            t = g["minute"]
            lab = "extra time" if t > 90 else next(b[0] for b in BINS if b[1] <= t <= b[2] or (b[0] == "0-15" and t < 1))
            per[lab][key] += 1
            if m["has_shotmap"]:
                sits[key][g["situation"]] += 1

    def sp_share(c):
        tot = sum(c.values())
        if not tot:
            return None
        sp = c["corner"] + c["free_kick"] + c["throw_in"]
        return dict(goals=tot, set_piece_no_pens=sp, set_piece_share_no_pens=round(sp / tot, 3),
                    penalties=c["penalty"], set_piece_share_incl_pens=round((sp + c["penalty"]) / tot, 3), breakdown=dict(c))
    return dict(
        matches_used=len(use), window="last 12 months" if len(win12) >= 5 else "last 10 matches (fewer than 5 matches in 12 months)",
        formation_counts=dict(fam), formation_by_venue_side=by_side,
        goals_by_period=per, goals_total=dict(scored=sum(v["scored"] for v in per.values()), conceded=sum(v["conceded"] for v in per.values())),
        set_piece_goals=dict(coverage=f"{shotmap_matches} of {len(use)} matches have a FotMob shotmap (situation data)",
                             scored=sp_share(sits["scored"]), conceded=sp_share(sits["conceded"]),
                             note=None if shotmap_matches else "No FotMob shotmap for these matches; set-piece share cannot be computed."),
        source_urls=[m["url"] for m in use])


# ===================================================================== StatsBomb
def _load_sb():
    ms = []
    for f in (SB_DIR / "matches").glob("*.json"):
        x = json.loads(f.read_text())
        ms += x if isinstance(x, list) else [x]
    return ms


def sb_metrics(team_name, matches):
    """Team-level indicators from raw AFCON 2023 events (aggregated, no raw republishing)."""
    mine = [m for m in matches if team_name in (m["home_team"]["home_team_name"], m["away_team"]["away_team_name"])]
    if not mine:
        return None
    opp_pass_def = 0
    def_actions = 0
    dur_me = dur_all = 0.0
    fwd = tot_len = 0.0
    passes = long_passes = 0
    shots = []
    net: dict[tuple, int] = defaultdict(int)
    made: Counter = Counter()
    played_matches = []
    for m in mine:
        ev = json.loads((SB_DIR / "events" / f"{m['match_id']}.json").read_text())
        played_matches.append(dict(match_id=m["match_id"], date=m["match_date"], home=m["home_team"]["home_team_name"], away=m["away_team"]["away_team_name"]))
        for e in ev:
            t = e["type"]["name"]
            tm = e.get("team", {}).get("name")
            loc = e.get("location")
            dur = e.get("duration") or 0
            pt = e.get("possession_team", {}).get("name")
            if pt and dur and e["type"]["name"] not in ("Half Start", "Half End"):
                dur_all += dur
                if pt == team_name:
                    dur_me += dur
            if t == "Pass":
                p = e["pass"]
                if tm == team_name:
                    passes += 1
                    L = p.get("length") or 0
                    end = p.get("end_location")
                    if end and loc:
                        fwd += (end[0] - loc[0])
                    tot_len += L
                    if L >= 32:
                        long_passes += 1
                    if "outcome" not in p and p.get("recipient"):   # completed pass
                        a, b = e["player"]["name"], p["recipient"]["name"]
                        net[(a, b)] += 1
                        made[a] += 1
                elif loc and loc[0] <= 72 and e.get("pass", {}).get("type", {}).get("name") not in ("Throw-in",):
                    # opponent pass in its own 60% of the pitch = the zone our team presses
                    opp_pass_def += 1
            if tm == team_name and loc and loc[0] >= 48:
                if (t == "Duel" and e.get("duel", {}).get("type", {}).get("name") == "Tackle") or t in ("Interception", "Foul Committed") \
                        or t == "Dribbled Past" or (t == "50/50" and False):
                    def_actions += 1
            if t == "Shot" and tm == team_name:
                s = e["shot"]
                shots.append(dict(x=loc[0], y=loc[1], xg=s.get("statsbomb_xg") or 0, type=s["type"]["name"], pattern=e["play_pattern"]["name"],
                                  outcome=s["outcome"]["name"], player=e["player"]["name"]))
    # PPDA is own defensive actions vs opponent passes in that zone
    ppda = round(opp_pass_def / def_actions, 2) if def_actions else None
    poss = round(dur_me / dur_all, 3) if dur_all else None

    def zone(s):
        x, y = s["x"], s["y"]
        if s["type"] == "Penalty":
            return "penalty"
        if x >= 114 and 30 <= y <= 50:
            return "six_yard_zone"
        if x >= 102 and 18 <= y <= 62:
            return "penalty_area"
        return "outside_box"
    zc = Counter(zone(s) for s in shots)
    n = len(shots)
    dist = [math.hypot(120 - s["x"], 40 - s["y"]) for s in shots]
    sp = sum(1 for s in shots if s["pattern"] in ("From Corner", "From Free Kick", "From Throw In") or s["type"] in ("Free Kick", "Corner", "Penalty"))
    # pass network: PageRank on passer->recipient, plus share of completed passes involving the player
    names = sorted({n_ for k in net for n_ in k})
    idx = {n_: i for i, n_ in enumerate(names)}
    top = []
    if names:
        A = np.zeros((len(names), len(names)))
        for (a, b), w in net.items():
            A[idx[a], idx[b]] += w
        out = A.sum(1, keepdims=True)
        P = np.divide(A, out, out=np.full_like(A, 1.0 / len(names)), where=out > 0)
        r = np.full(len(names), 1.0 / len(names))
        for _ in range(200):
            r = 0.15 / len(names) + 0.85 * (r @ P)
        tot = A.sum()
        inv = A.sum(0)
        outw = A.sum(1)
        for n_, i in idx.items():
            top.append(dict(player=n_, pagerank=round(float(r[i]), 4), pass_involvement_share=round(float((inv[i] + outw[i]) / (2 * tot)), 4),
                            completed_passes_made=int(outw[i])))
        top.sort(key=lambda x: -x["pagerank"])
    return dict(
        label=SB_LABEL, attribution=SB_ATTR, matches=played_matches, n_matches=len(mine),
        ppda=dict(value=ppda, note="Opponent passes in their own 60% of the pitch (x<=72, throw-ins excluded) / this team's tackles, interceptions, fouls and 'dribbled past' events in the opposition 60% (x>=48). Lower = more intense pressing."),
        possession_share=dict(value=poss, note="Share of event duration by possession team (StatsBomb possession chains)."),
        directness=dict(forward_progress_ratio=round(fwd / tot_len, 3) if tot_len else None, long_pass_share=round(long_passes / passes, 3) if passes else None,
                        note="forward_progress_ratio = sum(pass end x - start x) / sum(pass length), 0=sideways, 1=all forward. long_pass_share = passes >=32 m (of 120x80 pitch units)."),
        shots=dict(n=n, per_match=round(n / len(mine), 1), mean_distance=round(sum(dist) / n, 1) if n else None,
                   zone_share={k: round(v / n, 3) for k, v in zc.items()} if n else {}, mean_xg_per_shot=round(sum(s["xg"] for s in shots) / n, 3) if n else None,
                   set_piece_shot_share=round(sp / n, 3) if n else None,
                   set_piece_definition="play pattern From Corner / From Free Kick / From Throw In, or shot type Free Kick / Corner / Penalty"),
        pass_network_top_players=top[:6],
        pass_network_note="Directed network of completed passes (passer->recipient) pooled over the team's matches; PageRank d=0.85. Includes all players who appeared.",
        source_urls=["https://github.com/statsbomb/open-data"] + [f"https://raw.githubusercontent.com/statsbomb/open-data/master/data/events/{m['match_id']}.json" for m in mine])


# ===================================================================== Egypt angle
def egypt_angle(hassan, opp_elo_now, egy_side_venue, last_match):
    rr = hassan["record_results"]
    bands = rr["by_opponent_elo_band"]
    band = "low(<1450)" if opp_elo_now < 1450 else "high(>=1700)" if opp_elo_now >= 1700 else "mid(1450-1699)"
    same = [dict(date=m["date"], opponent=m["opponent"], venue=m["venue"], competition=m["competition"], score=m["score"], result=m["result"],
                 formation=m["formation"], opponent_elo=m["opponent_elo"])
            for m in rr["matches"] if m["opp_elo_band"] == band]
    within = [m for m in rr["matches"] if abs((m["opponent_elo"] or 0) - opp_elo_now) <= 100]
    wdl_ = Counter(m["result"] for m in within)
    scen_key = f"{band} | {egy_side_venue}"
    sc = hassan["predictive"]["formation_next_match_scenarios"]["scenarios"].get(scen_key)
    bt = hassan["predictive"]["formation_backtest"]
    pers = last_match
    return dict(
        elo_band=band, band_record=bands[band], all_bands=bands,
        by_venue_record=rr["by_venue"].get(egy_side_venue),
        within_100_elo_points=dict(n=len(within), W=wdl_["W"], D=wdl_["D"], L=wdl_["L"],
                                   note="Read from coach_hassan.json record_results.matches (opponent_elo within +-100 of today's Elo); the band table above is the module A figure."),
        same_band_matches=same,
        elo_expectation_vs_actual=rr["elo_expectation"],
        formation_model=dict(scenario=scen_key, top=sc["top"] if sc else None, n_matches_in_cell=sc["n_matches_in_cell"] if sc else None,
                             hassan_json_ref="predictive.formation_next_match_scenarios.scenarios['%s']" % scen_key),
        formation_persistence_baseline=dict(formation=pers["formation"], from_match=pers["label"],
                                            note="Same starting formation as Egypt's most recent match. Valid as a prediction for the very next match only; "
                                                 "for later fixtures it is the formation Egypt used in the match before that fixture (not yet known)."),
        backtest=dict(model_exact_hit_rate=bt["exact_hit_rate"]["mean"], persistence_exact_hit_rate=bt["baseline_same_as_previous_match"]["mean"],
                      most_frequent_so_far_exact_hit_rate=bt["baseline_most_frequent_so_far"]["mean"], n_test_matches=bt["n_test_matches"],
                      flag="Persistence (same as last match) beat the Dirichlet formation model in backtests: 0.46 vs 0.29 exact. Treat the persistence formation as the stronger call; the model is shown for the scenario mix only."),
        source="data/export/coach_hassan.json (module A) - record_results, predictive.formation_next_match_scenarios, predictive.formation_backtest")


# ===================================================================== main
def build(conn):
    hassan = json.loads(HASSAN_JSON.read_text())
    world, elo_names = load_elo()
    hist = egypt_history()
    d0, d0_n = fit_draw_scale(hist)
    egy_elo = world["EG"]["rating"]
    log(f"Egypt Elo {egy_elo}; draw scale d0={d0:.3f} over {d0_n} matches")

    fx_by_id, other_upcoming, group = egypt_fixtures(OPPONENTS)
    fix_by_name = {o["name"]: fx_by_id.get(o["fm_id"], []) for o in OPPONENTS}
    wiki = wiki_crosscheck(fix_by_name)
    wcq = wcq_status()
    sb_matches = _load_sb()

    last = conn.execute("""SELECT date, formation_egypt FROM matches WHERE is_egypt=1 AND source='fotmob' ORDER BY date DESC, kickoff_utc DESC LIMIT 1""").fetchone()
    last_match = dict(formation=last["formation_egypt"], label=f"Egypt {last['date']} (FotMob)")

    dossiers, raw_store = [], {}
    for o in OPPONENTS:
        log(f"== {o['name']}")
        fxs = sorted(fix_by_name[o["name"]], key=lambda f: f["date"])
        matches, squad, page = opp_fotmob.fetch_team_matches(o["fm_id"], o["slug"], SINCE_12M, log=log)
        raw_store[o["name"]] = dict(fixtures=fxs, matches=matches)
        elo = world[o["elo_code"]]
        fixtures_out = []
        for f in fxs:
            egy_home = f["egypt_side"] == "home"
            dr, e = elo_expect(egy_elo, elo["rating"], egy_home, not egy_home)
            fixtures_out.append(dict(
                date=f["date"], kickoff_utc=f["kickoff_utc"], date_tbd_flag=f["date_tbd"], competition=f["competition"], round=f["round"],
                home=f["home"], away=f["away"], egypt_side=f["egypt_side"],
                venue=f["venue"] or "TBD (no stadium listed by FotMob)", city=f["city"], country=f["country"],
                status="played" if f["finished"] else "upcoming", score=f["score"],
                elo=dict(egypt=egy_elo, opponent=elo["rating"], egypt_rank=world["EG"]["rank"], opponent_rank=elo["rank"], as_of=AS_OF,
                         home_advantage_applied=HOME_ADV if egy_home else -HOME_ADV,
                         adjusted_diff=dr, egypt_expected_score=round(e, 3),
                         wdl=wdl(e, d0)),
                source_urls=[f["source_url"], ELO_WORLD]))
        nxt = next((x for x in fixtures_out if x["status"] == "upcoming"), fixtures_out[0] if fixtures_out else None)
        venue_for_scen = "neutral"
        if nxt:
            venue_for_scen = nxt["egypt_side"]
            if nxt["country"] and nxt["country"] not in ("Egypt", o["name"]):
                venue_for_scen = "neutral"
        ah = all_time_h2h(hist, o["elo_code"])
        angle = egypt_angle(hassan, elo["rating"], venue_for_scen, last_match)
        sb = sb_metrics(o["sb_name"], sb_matches) if o["sb_name"] else None
        d = dict(
            opponent=o["name"], fotmob_team_id=o["fm_id"], competition_context=("Friendly" if all("Friend" in f["competition"] for f in fxs) else
                                                                                "AFCON 2027 qualification, Group B"),
            next_fixture_date=nxt["date"] if nxt else None,
            fixtures=dict(items=fixtures_out,
                          wikipedia_crosscheck=(wiki["checks"].get(o["name"]) if any("Qualification" in f["competition"] for f in fxs) else
                                                "n/a: friendly, not listed in the qualification article; FotMob is the only source for this fixture (unconfirmed by federation/Wikipedia)"),
                          elo_method=dict(
                              formula="We = 1 / (10^(-dr/400) + 1), dr = Egypt Elo - opponent Elo + 100 if Egypt at home / - 100 if opponent at home (eloratings.net convention).",
                              draw_split=f"pD = d0 * 4E(1-E) with d0={d0:.3f} fitted to Egypt's own draw rate in eloratings.net history since 1990 ({d0_n} matches); pW = E - pD/2, pL = 1 - E - pD/2, so pW + pD/2 = We exactly. Own modelling choice, not part of the Elo standard.",
                              sources=[ELO_ABOUT, ELO_WORLD, ELO_EGYPT])),
            head_to_head=dict(since_2018_db=h2h_db(conn, o["name"]), all_time=ah,
                              fotmob_recent_h2h=(fxs[0]["fotmob_h2h_matches"][:8] if fxs else None),
                              source_urls=[ELO_EGYPT, (fxs[0]["source_url"] if fxs else None)]),
            recent_form=form_block(matches, squad),
            typical_xi_and_key_players=key_players(matches, squad),
            style_indicators=dict(fotmob=style_block(matches), afcon_2023_statsbomb=sb if sb else dict(
                label=SB_LABEL, available=False, reason=f"{o['name']} did not play AFCON 2023 (not among the 24 teams in the StatsBomb open data).")),
            egypt_angle=angle,
            coverage=dict(form_matches=len(matches), matches_with_lineup=sum(m["has_lineup"] for m in matches), matches_with_shotmap=sum(m["has_shotmap"] for m in matches),
                          statsbomb_matches=sb["n_matches"] if sb else 0, fixtures_found=len(fxs), squad_players=len(squad["players"])),
            sources=dict(fixtures=[f["source_url"] for f in fxs], team_page=f"https://www.fotmob.com/teams/{o['fm_id']}/overview/{o['slug']}",
                         elo=[ELO_WORLD, ELO_EGYPT], wikipedia=WIKI_QUAL, statsbomb="https://github.com/statsbomb/open-data" if sb else None))
        dossiers.append(d)
    dossiers.sort(key=lambda d: d["next_fixture_date"] or "9999")
    payload = dict(
        module="opponent_dossiers", as_of=AS_OF, generated_at=now_iso(),
        egypt=dict(elo=egy_elo, rank=world["EG"]["rank"], source=ELO_WORLD, last_match=last_match, coach="Hossam Hassan"),
        qualification_group=dict(name="AFCON 2027 qualification, Group B", standings=group, source_urls=[WIKI_QUAL, "https://www.fotmob.com/leagues/10608/overview/africa-cup-nations-qualification"],
                                 wikipedia_crosscheck=dict(teams_found=wiki["group_b_teams_on_wikipedia"], url=WIKI_QUAL)),
        other_announced_fixtures=other_upcoming,
        world_cup_2030_qualifying=wcq,
        opponents=dossiers,
        attribution=SB_ATTR,
        methodology_notes=[
            "Fixtures, form, lineups: FotMob public pages via the polite client. Elo: eloratings.net TSVs. Group cross-check: Wikipedia.",
            "Recent form = last 10 finished matches; typical XI/key players/style use matches in the last 12 months (from " + SINCE_12M + ").",
            "Minutes are nominal (sub minute vs 90/120), as in the main ETL.",
            "Sofascore and Transfermarkt block the project and were not used.",
        ],
    )
    return payload, raw_store, dict(d0=d0)


def persist(conn, payload, raw):
    ts = now_iso()
    conn.executescript(DDL)
    with conn:
        for t in ("opp_fixtures", "opp_form", "opp_lineups", "opp_goals", "opp_sb_metrics"):
            conn.execute(f"DELETE FROM {t}")
        for name, r in raw.items():
            for f in r["fixtures"]:
                conn.execute("INSERT OR REPLACE INTO opp_fixtures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (f["fm_match_id"], name, f["date"], f["kickoff_utc"], f["competition"], f["round"], f["home"], f["away"], f["egypt_side"],
                              f["venue"], f["city"], f["country"], int(f["date_tbd"]), int(f["finished"]), f["score"], f["source_url"], ts))
            for m in r["matches"]:
                conn.execute("INSERT OR REPLACE INTO opp_form VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (m["mid"], name, m["date"], m["competition"], m["side"], m["opponent"], m["gf"], m["ga"], m["result"], m["decided_by"],
                              m["shootout"], m["formation"], m["opp_formation"], m["coach"], m["venue"], m["url"], ts))
                for p in m["players"]:
                    conn.execute("INSERT OR REPLACE INTO opp_lineups VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (m["mid"], name, m["date"], p["fm_id"], p["name"], p["club"], p["pos_id"], p["pos_group"], p["started"], p["minutes"], p["rating"], m["url"]))
                for g in m["goals"]:
                    conn.execute("INSERT INTO opp_goals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                                 (m["mid"], name, m["date"], g["minute"], g["added"], g["for_team"], g["situation"], g["player"], int(g["has_shotmap"]), g["xg"], m["url"]))
        for d in payload["opponents"]:
            sb = d["style_indicators"]["afcon_2023_statsbomb"]
            if sb.get("n_matches"):
                for k in ("ppda", "possession_share", "directness", "shots", "pass_network_top_players"):
                    conn.execute("INSERT OR REPLACE INTO opp_sb_metrics VALUES (?,?,?,?)", (d["opponent"], k, json.dumps(sb[k]), SB_ATTR))


# ===================================================================== summary
def _fx_line(f):
    e = f["elo"]
    w = e["wdl"]
    when = f["date"] + (" (date flagged TBD on FotMob)" if f["date_tbd_flag"] else "")
    if f["status"] == "played":
        return f"{when} played, {f['home']} {f['score']} {f['away']} ({f['venue']})"
    return (f"{when}, {f['home']} v {f['away']}, {f['venue']}{', ' + f['city'] if f['city'] else ''} "
            f"(Elo-based W/D/L for Egypt {w['win']:.0%}/{w['draw']:.0%}/{w['loss']:.0%})")


def brief(d):
    nxt = next((f for f in d["fixtures"]["items"] if f["status"] == "upcoming"), None)
    fm = d["recent_form"]
    r = fm["record"]
    e0 = (nxt or d["fixtures"]["items"][0])["elo"]
    ah = d["head_to_head"]["all_time"]
    db = d["head_to_head"]["since_2018_db"]
    xi = d["typical_xi_and_key_players"]
    st = d["style_indicators"]["fotmob"]
    ang = d["egypt_angle"]
    forms = ", ".join(f"{k} x{v}" for k, v in sorted(fm["formations_last10"].items(), key=lambda kv: -kv[1])[:3]) or "formation data unavailable"
    ms = xi["most_started"][:3]
    keyp = ", ".join(f"{p['player']} ({p['club'] or 'club n/a'}, {p['starts']} starts)" for p in ms) or "no lineup data"
    gp = st["goals_by_period"]
    tot_s, tot_c = st["goals_total"]["scored"], st["goals_total"]["conceded"]
    late = gp["76-90+"]["conceded"]
    sb = d["style_indicators"]["afcon_2023_statsbomb"]
    sbtxt = ""
    if sb.get("n_matches"):
        sbtxt = (f" AFCON 2023 (StatsBomb), may be outdated: PPDA {sb['ppda']['value']}, possession {sb['possession_share']['value']:.0%}, "
                 f"set-piece shot share {sb['shots']['set_piece_shot_share']:.0%}, forward-progress ratio {sb['directness']['forward_progress_ratio']}.")
    fs = (d["egypt_angle"]["formation_model"]["top"] or [{}])[0]
    pers = ang["formation_persistence_baseline"]["formation"]
    band = ang["band_record"]
    text = (
        f"Elo {e0['opponent']} (rank {e0['opponent_rank']}) v Egypt {e0['egypt']} (rank {e0['egypt_rank']}). "
        f"Last {fm['n']} ({fm['span']['first']} to {fm['span']['last']}): W{r['W']} D{r['D']} L{r['L']}, {fm['goals_for']} scored / {fm['goals_against']} conceded; coach {fm['current_coach']['name'] or 'n/a'}; "
        f"usual shapes {forms}. Key players by starts over the last 12 months: {keyp}. "
        f"Goals by period across {st['matches_used']} matches: {tot_s} for, {tot_c} against ({late} conceded in 76-90+)."
        f"{sbtxt} "
        f"Head-to-head: {db['W']}W {db['D']}D {db['L']}L for Egypt since 2018 in our DB; all-time {ah['W']}W {ah['D']}D {ah['L']}L in {ah['P']} (eloratings.net). "
        f"Under Hassan Egypt are {band['P']} matches, W{band['W']} D{band['D']} L{band['L']} against this Elo band ({ang['elo_band']}). "
        f"Formation call: persistence baseline says {pers} (the stronger method in backtests, 0.46 vs 0.29 exact); the Dirichlet model's top scenario is {fs.get('formation')}."
    )
    return text


def write_summary(payload):
    L = [f"# Opponent dossiers - Pharaohs Lab (module D)", "",
         f"As of {payload['as_of']}. Egypt Elo {payload['egypt']['elo']} (rank {payload['egypt']['rank']}), coach Hossam Hassan.",
         f"Data: FotMob public pages, eloratings.net, Wikipedia; StatsBomb open data for AFCON 2023 sections. {SB_ATTR}.", ""]
    g = payload["qualification_group"]
    L += ["## Context", "", "AFCON 2027 qualification, Group B (cross-checked against Wikipedia): Egypt, Angola, Malawi, South Sudan.", ""]
    if g["standings"]:
        L += ["| Team | P | W | D | L | GF-GA | Pts |", "|---|---|---|---|---|---|---|"]
        for r in g["standings"]["table"]:
            L.append(f"| {r['team']} | {r['P']} | {r['W']} | {r['D']} | {r['L']} | {r['GF_GA']} | {r['pts']} |")
        L.append("")
    if payload["other_announced_fixtures"]:
        L.append("Announced non-qualifier fixtures: " + "; ".join(f"{x['opponent']} ({x['date']}, {x['competition']})" for x in payload["other_announced_fixtures"] if "Qualification" not in x["competition"]) + ".")
    w = payload["world_cup_2030_qualifying"]
    L += ["", f"2030 World Cup qualifying (CAF): **{w['status']}**. {w['detail']} ({w['source_url']})", "", "## Opponents (by next fixture)", ""]
    for d in payload["opponents"]:
        L += [f"### {d['opponent']} - {d['competition_context']}", ""]
        L += ["Fixtures vs Egypt:"] + [f"- {_fx_line(f)}" for f in d["fixtures"]["items"]] + [""]
        L += [brief(d), "", "Coverage: " + ", ".join(f"{k}={v}" for k, v in d["coverage"].items()) + ".", ""]
    L += ["## Method notes", "",
          "- Elo expectation: We = 1/(10^(-dr/400)+1) (https://www.eloratings.net/about), 100-point home advantage; W/D/L split uses a fitted draw term (own choice, documented in the JSON).",
          "- Head-to-head all-time is from eloratings.net's Egypt history; no Wikipedia all-time head-to-head page exists for Egypt.",
          "- Formation model did not beat persistence in backtests (0.29 vs 0.46 exact); persistence is the stronger baseline.",
          "- Full sources per section are in `opponent_dossiers.json`.", ""]
    OUT_MD.write_text("\n".join(L))


def run(conn=None):
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    own = conn is None
    conn = conn or connect()
    conn.execute("PRAGMA busy_timeout=120000")
    payload, raw, _ = build(conn)
    persist(conn, payload, raw)
    OUT_JSON.write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    write_summary(payload)
    log(f"wrote {OUT_JSON} and {OUT_MD}")
    if own:
        conn.close()
    return payload


if __name__ == "__main__":
    run()
