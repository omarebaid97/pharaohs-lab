"""Module B: Salah dependency analysis + succession model.

Run:  .venv/bin/python -m etl.models.salah_succession [--no-fetch] [--max-fetch N]

Writes data/export/salah_succession.json and data/export/salah_succession_summary.md.
Own tables (all prefixed salah_) are created here; nothing else in the ETL is modified.
All HTTP goes through etl.http (via etl.sources.fotmob.next_data); cached pages are reused.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .. import db, http
from ..sources import fotmob

ROOT = db.ROOT
EXPORT = ROOT / "data" / "export"
AS_OF = date.fromisoformat("2026-09-28")
SINCE = "2018-01-01"
MARMOUSH_ERA = "2024-02-06"
PAGE_TTL = 7 * 86400
MIN_MINUTES = 600
SALAH_FM = 292462
MARMOUSH_FM = 839204

SCHEMA = """
CREATE TABLE IF NOT EXISTS salah_profiles (
  fotmob_id INTEGER PRIMARY KEY,
  name TEXT, dob TEXT, age REAL, position_key TEXT, position_label TEXT,
  club TEXT, league_id INTEGER, league TEXT, league_tier INTEGER,
  pools TEXT,                 -- comma list: a_capped,b_epl,c_diaspora,target
  window_start TEXT, window_end TEXT,
  minutes_12m INTEGER, starts_12m INTEGER, apps_12m INTEGER,
  goals_12m INTEGER, assists_12m INTEGER, g90 REAL, a90 REAL,
  minutes_l6 INTEGER, minutes_p6 INTEGER,
  traits_json TEXT, deep_json TEXT, deep_minutes INTEGER, deep_season TEXT, deep_league TEXT,
  n_traits INTEGER, nationality TEXT, egypt_eligible TEXT, eligibility_basis TEXT, low_confidence INTEGER, eligible INTEGER, exclude_reason TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS salah_similarity (
  fotmob_id INTEGER, role TEXT, sim REAL, rank INTEGER, drivers_json TEXT,
  PRIMARY KEY (fotmob_id, role)
);
CREATE TABLE IF NOT EXISTS salah_board (
  fotmob_id INTEGER PRIMARY KEY, name TEXT, tier TEXT, fit REAL, reasons_json TEXT
);
"""

# ------------------------------------------------------------------ league tiers
# Analyst judgement (NOT a fetched dataset). Rough public-knowledge ordering informed by the UEFA association
# coefficient ranking and general reputation; used ONLY for the succession board rules, not for similarity.
# 1 = top-5 European leagues; 2 = strong second-tier leagues; 3 = Egyptian PL / other mid leagues; 4 = unknown/lower.
TIER1 = ("premier league", "laliga", "la liga", "bundesliga", "serie a", "ligue 1")
TIER2 = ("super lig", "eredivisie", "liga portugal", "primeira liga", "pro league", "jupiler", "championship",
         "premiership", "saudi", "mls", "2. bundesliga", "laliga2", "serie b", "ligue 2", "russian", "super league",
         "bundesliga 2", "premier liga", "allsvenskan", "eliteserien", "superliga", "ekstraklasa", "liga mx")
TIER3 = ("egyptian premier", "botola", "premier soccer", "ligue professionnelle", "npfl", "ghana", "cafcl")
FALLBACK_TIER = 4
DIASPORA_STATS: dict = {}
DIASPORA_FM_IDS: set = set()
# PARAMETER: tier assigned to the Egyptian Premier League (FotMob league id 519). Board rule ready_now needs tier <= READY_MAX_TIER.
EGYPT_LEAGUE_TIER = 3
READY_MAX_TIER = 2

TRAIT_KEYS = ["chances_created", "aerials_won", "defensive_actions", "goals", "shot_attempts", "touches"]
CORE_FEATURES = ["g90", "a90"] + TRAIT_KEYS
ATTACK_KEYS = ("winger", "forward", "striker", "attackingmid", "secondstriker")

# Synthetic role archetypes, in percentile space (0-100) over the same 8 core features.
# Right-sided inside forward: high finishing volume + shot volume, good chance creation, touches in between,
# low aerial, low defensive work. Analyst-defined; labelled as such in the export.
ROLE_INSIDE_FORWARD = {"g90": 88, "a90": 70, "chances_created": 78, "aerials_won": 25, "defensive_actions": 30,
                       "goals": 88, "shot_attempts": 90, "touches": 62}


def tier_of(name: str | None, league_id=None) -> int:
    # FotMob calls several leagues just "Premier League": disambiguate by id (47 = England, 519 = Egypt)
    if league_id == 519:
        return EGYPT_LEAGUE_TIER
    if league_id == 47:
        return 1
    n = (name or "").lower()
    if n == "premier league":
        return FALLBACK_TIER
    if any(k in n for k in TIER3):
        return 3
    if any(k == n or k in n for k in TIER1):
        return 1
    if any(k in n for k in TIER2):
        return 2
    return FALLBACK_TIER


# ================================================================== 1. dependency analysis
def _salah_ids(con):
    return [r[0] for r in con.execute("SELECT id FROM players WHERE name='Mohamed Salah' AND (dob='1992-06-15' OR fotmob_id=?)", (SALAH_FM,))]


def _pids(con, fm, name, dob):
    return [r[0] for r in con.execute("SELECT id FROM players WHERE fotmob_id=? OR (name=? AND dob=?)", (fm, name, dob))]


def _result(gf, ga):
    return "W" if gf > ga else ("D" if gf == ga else "L")


def _stats(rows):
    """rows: list of dicts with gf, ga, pts, opp_elo, exp."""
    n = len(rows)
    if not n:
        return {"n": 0}
    w = sum(1 for r in rows if r["pts"] == 3)
    d = sum(1 for r in rows if r["pts"] == 1)
    return {"n": n, "W": w, "D": d, "L": n - w - d, "ppg": round(sum(r["pts"] for r in rows) / n, 3),
            "gf_pm": round(sum(r["gf"] for r in rows) / n, 3), "ga_pm": round(sum(r["ga"] for r in rows) / n, 3),
            "win_rate": round(w / n, 3), "mean_opp_elo": round(sum(r["opp_elo"] for r in rows) / n, 0),
            "elo_expected_ppg": round(sum(r["exp"] for r in rows) / n, 3)}


def _boot_diff(a, b, seed=7, k=3000):
    if len(a) < 3 or len(b) < 3:
        return None
    rnd = random.Random(seed)
    pa, pb = [r["pts"] for r in a], [r["pts"] for r in b]
    diffs = []
    for _ in range(k):
        ma = sum(rnd.choice(pa) for _ in pa) / len(pa)
        mb = sum(rnd.choice(pb) for _ in pb) / len(pb)
        diffs.append(ma - mb)
    diffs.sort()
    return [round(diffs[int(.025 * k)], 2), round(diffs[int(.975 * k)], 2)]


def dependency(con) -> dict:
    sal = _salah_ids(con)
    mar = _pids(con, MARMOUSH_FM, "Omar Marmoush", "1999-02-07")
    coach = {r[0]: r[1] for r in con.execute("SELECT id,name FROM coaches")}
    ms = con.execute("""SELECT id,date,competition,home_score,away_score,egypt_side,opponent_elo,egypt_elo,egypt_coach_id,home_team_id,away_team_id
                        FROM matches WHERE is_egypt=1 AND source='fotmob' AND scope='national' AND date>=? ORDER BY date""", (SINCE,)).fetchall()
    ph = lambda ids: ",".join("?" * len(ids))
    out_rows, excluded = [], []
    for m in ms:
        if m["home_score"] is None:
            continue
        side = m["egypt_side"]
        starters = con.execute("SELECT player_id FROM lineups WHERE match_id=? AND team_side=? AND started=1", (m["id"], side)).fetchall()
        starters = {r[0] for r in starters}
        if len(starters) < 11:
            excluded.append(m["date"])
            continue
        gf, ga = (m["home_score"], m["away_score"]) if side == "home" else (m["away_score"], m["home_score"])
        pts = 3 if gf > ga else (1 if gf == ga else 0)
        oe = m["opponent_elo"]
        ee = m["egypt_elo"] or 1700
        exp = 3 * 0 + (1 / (1 + 10 ** ((oe - ee) / 400)))  # win-expectancy (0..1) incl. draw as half
        egypt_tid = m["home_team_id"] if side == "home" else m["away_team_id"]
        sal_in = bool(starters & set(sal))
        mar_in = bool(starters & set(mar))
        sal_min = con.execute(f"SELECT COALESCE(SUM(minutes_played),0) FROM lineups WHERE match_id=? AND player_id IN ({ph(sal)})", [m["id"], *sal]).fetchone()[0]
        ev = con.execute("SELECT type,player_id,related_player_id,team_id FROM events_lite WHERE match_id=? AND type IN ('goal','pen_goal')", (m["id"],)).fetchall()
        egy_ev = [e for e in ev if e["team_id"] == egypt_tid]
        out_rows.append(dict(
            id=m["id"], date=m["date"], comp=m["competition"] or "", friendly="friend" in (m["competition"] or "").lower(),
            gf=gf, ga=ga, pts=pts, opp_elo=oe, exp=exp, coach=coach.get(m["egypt_coach_id"], "unknown"),
            sal_xi=sal_in, sal_min=sal_min, mar_xi=mar_in, has_events=bool(ev) or gf + ga == 0,
            sal_goals=sum(1 for e in egy_ev if e["player_id"] in sal), sal_ast=sum(1 for e in egy_ev if e["type"] == "goal" and e["related_player_id"] in sal),
            ev_goals=len(egy_ev), ev_assist_slots=sum(1 for e in egy_ev if e["type"] == "goal" and e["related_player_id"] is not None)))
    for r in out_rows:
        r["exp_ppg"] = 3 * r["exp"]  # crude: win-expectancy scaled to 3 points (ignores home advantage/draw shape)
    for r in out_rows:
        r["exp"] = r["exp_ppg"]

    def band(r):
        return "opp_elo<1400" if r["opp_elo"] < 1400 else ("opp_elo 1400-1699" if r["opp_elo"] < 1700 else "opp_elo>=1700")

    def split(rows, keyf):
        keys = sorted({keyf(r) for r in rows})
        res = {}
        for k in keys:
            sub = [r for r in rows if keyf(r) == k]
            w = [r for r in sub if r["sal_xi"]]
            wo = [r for r in sub if not r["sal_xi"]]
            res[k] = {"with_salah": _stats(w), "without_salah": _stats(wo)}
        return res

    w = [r for r in out_rows if r["sal_xi"]]
    wo = [r for r in out_rows if not r["sal_xi"]]

    def share(rows):
        ev_rows = [r for r in rows if r["has_events"]]
        goals = sum(r["ev_goals"] for r in ev_rows)
        ast = sum(r["ev_assist_slots"] for r in ev_rows)
        sg = sum(r["sal_goals"] for r in ev_rows)
        sa = sum(r["sal_ast"] for r in ev_rows)
        return {"matches_with_events": len(ev_rows), "egypt_goals_in_events": goals, "salah_goals": sg,
                "goal_share": round(sg / goals, 3) if goals else None,
                "goals_with_assist_recorded": ast, "salah_assists": sa,
                "assist_share_of_recorded": round(sa / ast, 3) if ast else None,
                "salah_goal_involvement_share": round((sg + sa) / goals, 3) if goals else None}

    # Marmoush split, Hassan era (>= 2024-02-06)
    era = [r for r in out_rows if r["date"] >= MARMOUSH_ERA]
    grid = {}
    for s in (True, False):
        for mm in (True, False):
            sub = [r for r in era if r["sal_xi"] == s and r["mar_xi"] == mm]
            grid[f"salah_{'in' if s else 'out'}__marmoush_{'in' if mm else 'out'}"] = _stats(sub)
    # bench usage
    bench = [r for r in out_rows if not r["sal_xi"] and r["sal_min"] > 0]

    confounders = [
        "Salah's absence is not random: injuries, AFCON-club release disputes and rest days cluster around weaker or dead-rubber matches, and rests occur mostly in friendlies and easy qualifiers.",
        "Opponent quality differs between the two groups (see mean_opp_elo and elo_expected_ppg); the Elo-expected column is a crude control, not a model (no home advantage, no draw shape).",
        "Coach changes coincide with Salah's availability and with squad ageing; Cuper/Queiroz/Vitoria/Hassan eras are not comparable in tactics or opposition.",
        "Small samples: 109 matches total, and the 'without' group is typically 20-40 matches; the bootstrap 95% interval on the PPG difference is wide.",
        "Salah's minutes are in-XI only: a substitute appearance counts as 'without in XI' (bench_used count reported separately).",
        "Shootouts are scored as draws (score after 120 min); AFCON 2019 and 2021/22 knock-outs are affected.",
        "Goal/assist shares only count matches whose FotMob feed has events; assist attribution is recorded on only some goals (denominator reported).",
        "Team-level results include everyone else's form: this is association, not Salah's causal effect (Marmoush, Trezeguet, Mostafa Mohamed etc. move together with him).",
    ]
    return {
        "scope": "Egypt men's national team, FotMob-sourced matches with a full XI (>=11 Egypt starters), since 2018-01-01",
        "n_matches_used": len(out_rows), "n_matches_excluded_no_lineup": len(excluded), "excluded_dates": excluded,
        "salah_player_ids": sal, "marmoush_player_ids": mar,
        "overall": {"with_salah_in_xi": _stats(w), "without_salah_in_xi": _stats(wo),
                    "ppg_diff_with_minus_without": round(_stats(w)["ppg"] - _stats(wo)["ppg"], 3) if w and wo else None,
                    "ppg_diff_bootstrap95": _boot_diff(w, wo),
                    "without_but_used_as_sub": len(bench)},
        "salah_share_when_in_xi": share(w),
        "salah_share_all_matches": share(out_rows),
        "by_opp_elo_band": split(out_rows, band),
        "by_competition_type": split(out_rows, lambda r: "friendly" if r["friendly"] else "competitive"),
        "by_coach": split(out_rows, lambda r: r["coach"]),
        "marmoush_era_since_2024_02_06": {"note": "matches on/after Hassan's start; Marmoush 'in' = in the starting XI", "grid": grid,
                                          "n": len(era)},
        "confounders": confounders,
    }


# ================================================================== 2. profiles
def candidate_ids(con) -> dict[int, dict]:
    """fotmob_id -> {pools:set, name, db_ids}."""
    pool: dict[int, dict] = {}
    DIASPORA_FM_IDS.clear()
    DIASPORA_STATS.clear()

    def add(fm, name, tag):
        if not fm:
            return
        e = pool.setdefault(int(fm), {"pools": set(), "name": name})
        e["pools"].add(tag)

    # (a) every Egypt-capped player since 2022 (started or played minutes; Egypt side only)
    for r in con.execute("""SELECT DISTINCT p.fotmob_id, p.name FROM lineups l JOIN matches m ON m.id=l.match_id JOIN players p ON p.id=l.player_id
                            WHERE m.is_egypt=1 AND m.date>='2022-01-01' AND l.team_side=m.egypt_side AND (l.started=1 OR COALESCE(l.minutes_played,0)>0)
                              AND p.fotmob_id IS NOT NULL"""):
        add(r[0], r[1], "a_capped")
    # (b) Egyptian Premier League wingers/forwards: >=5 starts in FotMob attacking-row slots (82-88 attacking-mid row,
    #     103-107 wide/attacking row, 113-117 striker row). Position confirmed later from the player page.
    att = ("82", "83", "84", "85", "86", "87", "88", "103", "104", "105", "106", "107", "113", "114", "115", "116", "117")
    for r in con.execute(f"""SELECT p.fotmob_id, p.name, COUNT(*) n FROM lineups l JOIN matches m ON m.id=l.match_id JOIN players p ON p.id=l.player_id
                             WHERE m.scope='egyptian_league' AND l.started=1 AND l.slot IN ({','.join('?'*len(att))}) AND p.fotmob_id IS NOT NULL
                             GROUP BY p.id HAVING n>=5""", att):
        add(r[0], r[1], "b_epl")
    # (c) diaspora hook (file is produced by another agent; format unknown -> best effort, never required)
    dj = EXPORT / "diaspora_candidates.json"
    if dj.exists():
        try:
            data = json.loads(dj.read_text())
            items = data if isinstance(data, list) else (data.get("candidates") or data.get("players") or [])
            unresolved = 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                fm = it.get("fotmob_id") or it.get("fotmobId")
                if not fm and it.get("wikidata_qid"):
                    r = con.execute("SELECT fotmob_id FROM players WHERE wikidata_qid=? AND fotmob_id IS NOT NULL", (it["wikidata_qid"],)).fetchone()
                    fm = r[0] if r else None
                if not fm and it.get("name") and it.get("dob"):
                    r = con.execute("SELECT fotmob_id FROM players WHERE dob=? AND fotmob_id IS NOT NULL AND name=?", (it["dob"], it["name"])).fetchone()
                    fm = r[0] if r else None
                if fm:
                    add(fm, it.get("name"), "c_diaspora")
                    DIASPORA_FM_IDS.add(int(fm))
                else:
                    unresolved += 1
            DIASPORA_STATS.update(items=len(items), unresolved_no_fotmob_id=unresolved)
        except Exception as e:  # noqa
            print(f"[salah] diaspora file unreadable: {e}", file=sys.stderr)
    add(SALAH_FM, "Mohamed Salah", "target")
    add(MARMOUSH_FM, "Omar Marmoush", "target")
    return pool


def _page(fm: int, allow_fetch: bool, budget: list):
    url = f"{fotmob.BASE}/players/{fm}/x"
    if not allow_fetch and not http._cache_path(url).exists():
        return None
    cached = http._cache_path(url).exists()
    if not cached:
        if budget[0] <= 0:
            return None
        budget[0] -= 1
    try:
        return fotmob.next_data(url, ttl=PAGE_TTL)["props"]["pageProps"]["data"]
    except (http.NotFound, http.Blocked) as e:
        print(f"[salah] page {fm}: {type(e).__name__}", file=sys.stderr)
        return None
    except Exception as e:  # noqa
        print(f"[salah] page {fm} failed: {e}", file=sys.stderr)
        return None


def extract(dd: dict, fm: int) -> dict:
    """Pull the fields we can actually get from a FotMob player page."""
    pd_ = (dd.get("positionDescription") or {}).get("primaryPosition") or {}
    dob = ((dd.get("birthDate") or {}).get("utcTime") or "")[:10] or None
    age = round((AS_OF - date.fromisoformat(dob)).days / 365.25, 1) if dob else None
    ml = dd.get("mainLeague") or {}
    lname = ml.get("leagueName")
    if ml.get("leagueId") == 519:
        lname = "Egyptian Premier League"
    elif ml.get("leagueId") == 47:
        lname = "English Premier League"
    # trailing window = the recentMatches list (FotMob caps it; window_start reports the true span)
    ms = [m for m in dd.get("recentMatches") or [] if m.get("playedInMatch") or (m.get("minutesPlayed") or 0) > 0]
    cutoff = date(AS_OF.year - 1, AS_OF.month, AS_OF.day).isoformat()
    ms = [m for m in ms if m["matchDate"]["utcTime"][:10] >= cutoff]
    mins = sum(m.get("minutesPlayed") or 0 for m in ms)
    g = sum(m.get("goals") or 0 for m in ms)
    a = sum(m.get("assists") or 0 for m in ms)
    mid = date(AS_OF.year, AS_OF.month, AS_OF.day).toordinal() - 182
    l6 = sum(m.get("minutesPlayed") or 0 for m in ms if date.fromisoformat(m["matchDate"]["utcTime"][:10]).toordinal() >= mid)
    dates = [m["matchDate"]["utcTime"][:10] for m in ms]
    traits = {t["key"]: t["value"] for t in (dd.get("traits") or {}).get("items") or [] if t.get("value") is not None}
    fs = dd.get("firstSeasonStats") or {}
    deep, dmin = {}, None
    for grp in ((fs.get("statsSection") or {}).get("items") or []):
        for it in grp.get("items") or []:
            deep[it["localizedTitleId"]] = {"per90": it.get("per90"), "pct90": it.get("percentileRankPer90"), "value": it.get("statValue")}
    for it in ((fs.get("topStatCard") or {}).get("items") or []):
        if it.get("localizedTitleId") == "minutes_played":
            try:
                dmin = int(float(it["statValue"]))
            except (TypeError, ValueError):
                pass
    seasons = dd.get("statSeasons") or []
    first_t = ((seasons[0].get("tournaments") or [{}])[0].get("name") if seasons else None)
    pt = dd.get("primaryTeam") or {}
    nat = None
    for x in dd.get("playerInformation") or []:
        if x.get("translationKey") == "country_sentencecase" or x.get("title") == "Country":
            nat = (x.get("value") or {}).get("fallback")
    if not nat:
        nat = ((dd.get("meta") or {}).get("personJSONLD") or {}).get("nationality", {}).get("name")
    return dict(nationality=nat, 
        fotmob_id=fm, name=dd.get("name"), dob=dob, age=age, position_key=pd_.get("key"), position_label=pd_.get("label"),
        club=pt.get("teamName"), league_id=ml.get("leagueId"), league=lname, league_tier=tier_of(lname, ml.get("leagueId")),
        window_start=min(dates) if dates else None, window_end=max(dates) if dates else None,
        minutes_12m=mins, starts_12m=sum(1 for m in ms if not m.get("onBench") and (m.get("minutesPlayed") or 0) > 0 and m.get("lineupPositionId") is not None),
        apps_12m=len(ms), goals_12m=g, assists_12m=a,
        g90=round(g * 90 / mins, 3) if mins else None, a90=round(a * 90 / mins, 3) if mins else None,
        minutes_l6=l6, minutes_p6=mins - l6, traits_json=json.dumps(traits), deep_json=json.dumps(deep), deep_minutes=dmin,
        deep_season=(seasons[0].get("seasonName") if seasons else None), deep_league=first_t, n_traits=len(traits))


def build_profiles(con, allow_fetch=True, max_fetch=400, log=print) -> dict:
    con.execute("DROP TABLE IF EXISTS salah_profiles")  # derived table, rebuilt every run (schema may have changed)
    con.executescript(SCHEMA)
    pool = candidate_ids(con)
    capped = {r[0] for r in con.execute("""SELECT DISTINCT p.fotmob_id FROM lineups l JOIN matches m ON m.id=l.match_id JOIN players p ON p.id=l.player_id
                                           WHERE m.is_egypt=1 AND l.team_side=m.egypt_side AND (l.started=1 OR COALESCE(l.minutes_played,0)>0) AND p.fotmob_id IS NOT NULL""")}
    budget = [max_fetch]
    stats = {"candidates": len(pool), "page_missing": 0, "fetched_or_cached": 0}
    con.execute("DELETE FROM salah_profiles")
    ts = db.now()
    order = sorted(pool, key=lambda f: (0 if "target" in pool[f]["pools"] else 1, f))
    for i, fm in enumerate(order):
        dd = _page(fm, allow_fetch, budget)
        if dd is None:
            stats["page_missing"] += 1
            con.execute("INSERT INTO salah_profiles(fotmob_id,name,pools,eligible,exclude_reason,fetched_at) VALUES (?,?,?,0,'no_page',?)",
                        (fm, pool[fm]["name"], ",".join(sorted(pool[fm]["pools"])), ts))
            continue
        stats["fetched_or_cached"] += 1
        p = extract(dd, fm)
        p["pools"] = ",".join(sorted(pool[fm]["pools"]))
        p["fetched_at"] = ts
        reason = None
        key = (p["position_key"] or "").lower()
        nat = (p["nationality"] or "").strip()
        if nat.lower() in ("egypt", "egyptian"):
            elig, basis = "yes", "fotmob_nationality_egypt"
        elif fm in capped:
            elig, basis = "yes", "capped_by_egypt_in_db"
        elif fm in DIASPORA_FM_IDS:
            elig, basis = "yes", "in_diaspora_candidates_file"
        elif nat:
            elig, basis = "no", f"fotmob_nationality_{nat}"
        else:
            elig, basis = "unknown", "no_nationality_on_page"
        p["egypt_eligible"], p["eligibility_basis"] = elig, basis
        p["low_confidence"] = 1 if p["n_traits"] < 4 else 0
        if "target" not in pool[fm]["pools"]:
            if elig == "no":
                reason = f"non_egyptian:{nat}"
            elif not any(k in key for k in ATTACK_KEYS):
                reason = f"position_not_attacking:{key or 'unknown'}"
            elif (p["minutes_12m"] or 0) < MIN_MINUTES:
                reason = f"under_{MIN_MINUTES}_min_in_window"
        p["exclude_reason"] = reason
        p["eligible"] = 0 if reason else 1
        cols = list(p)
        con.execute(f"INSERT OR REPLACE INTO salah_profiles({','.join(cols)}) VALUES ({','.join('?'*len(cols))})", [p[c] for c in cols])
        if (i + 1) % 25 == 0:
            con.commit()
            log(f"[salah] profiles {i+1}/{len(order)}")
    con.commit()
    return stats


# ================================================================== 3. similarity
def _pctile(vals: dict[int, float | None]) -> dict[int, float]:
    xs = sorted(v for v in vals.values() if v is not None)
    n = len(xs)
    out = {}
    for k, v in vals.items():
        if v is None or n == 0:
            continue
        lo = sum(1 for x in xs if x < v)
        eq = sum(1 for x in xs if x == v)
        out[k] = round(100 * (lo + 0.5 * eq) / n, 1)
    return out


def _cos(a: dict, b: dict, low=False):
    keys = [k for k in CORE_FEATURES if k in a and k in b]
    if len(keys) < (2 if low else 5):
        return None, {}
    va = [a[k] - 50 for k in keys]
    vb = [b[k] - 50 for k in keys]
    na, nb = math.sqrt(sum(x * x for x in va)), math.sqrt(sum(x * x for x in vb))
    if na == 0 or nb == 0:
        return None, {}
    contrib = {k: x * y / (na * nb) for k, x, y in zip(keys, va, vb)}
    return sum(contrib.values()), contrib


def similarity(con) -> dict:
    rows = {r["fotmob_id"]: dict(r) for r in con.execute("SELECT * FROM salah_profiles WHERE exclude_reason IS NULL OR exclude_reason='' OR eligible=1")}
    rows = {k: v for k, v in rows.items() if v["eligible"]}
    g_pct = _pctile({k: v["g90"] for k, v in rows.items()})
    a_pct = _pctile({k: v["a90"] for k, v in rows.items()})
    vec = {}
    for k, v in rows.items():
        t = json.loads(v["traits_json"] or "{}")
        d = {}
        if k in g_pct:
            d["g90"] = g_pct[k]
        if k in a_pct:
            d["a90"] = a_pct[k]
        for tk in TRAIT_KEYS:
            if tk in t and not v["low_confidence"]:
                d[tk] = round(100 * t[tk], 1)
        vec[k] = d
    targets = {"salah_current": vec.get(SALAH_FM), "inside_forward_archetype": dict(ROLE_INSIDE_FORWARD), "marmoush_roaming_striker": vec.get(MARMOUSH_FM)}
    out = {"roles": {}, "candidate_vectors": {}}
    con.execute("DELETE FROM salah_similarity")
    cands = [k for k, v in rows.items() if "target" not in v["pools"] .split(",")]
    for role, tv in targets.items():
        if tv is None:
            out["roles"][role] = {"error": "target profile unavailable"}
            continue
        scored = []
        for k in cands:
            s, contrib = _cos(tv, vec[k], low=bool(rows[k]["low_confidence"]))
            if s is None:
                continue
            scored.append((s, k, contrib))
        scored.sort(key=lambda x: -x[0])
        top, unv, rank_pub = [], [], 0
        for rank, (s, k, contrib) in enumerate(scored, 1):
            drivers = sorted(contrib.items(), key=lambda x: -x[1])[:3]
            gaps = sorted(((f, vec[k][f] - tv[f]) for f in contrib), key=lambda x: -abs(x[1]))[:2]
            dj = {"top_agreeing_features": [{"feature": f, "contribution": round(c, 3), "candidate_pct": vec[k][f], "target_pct": tv[f]} for f, c in drivers],
                  "largest_gaps": [{"feature": f, "candidate_minus_target_pct": round(g, 1)} for f, g in gaps]}
            con.execute("INSERT OR REPLACE INTO salah_similarity VALUES (?,?,?,?,?)", (k, role, round(s, 4), rank, json.dumps(dj)))
            r = rows[k]
            if r["egypt_eligible"] == "unknown" and not r["low_confidence"] and len(unv) < 15:
                unv.append({"fotmob_id": k, "name": r["name"], "sim": round(s, 3), "age": r["age"], "club": r["club"], "league": r["league"], "pools": r["pools"]})
            if r["egypt_eligible"] == "yes" and not r["low_confidence"]:
                rank_pub += 1
            if rank_pub <= 15 and r["egypt_eligible"] == "yes" and not r["low_confidence"]:
                top.append({"rank": rank_pub, "fotmob_id": k, "name": r["name"], "sim": round(s, 3), "age": r["age"], "club": r["club"], "league": r["league"],
                            "position": r["position_label"], "pools": r["pools"], "minutes_12m": r["minutes_12m"], **dj})
        out["roles"][role] = {"target_vector_pct": tv, "n_scored": len(scored), "top15": top, "nationality_unverified_top": unv}
    out["candidate_vectors"] = {str(k): vec[k] for k in cands}
    out["_vec"] = vec
    out["_rows"] = rows
    return out


# ================================================================== 4. succession board
RULES = {
    "fit": "mean of cosine similarity to the Salah-current profile and the inside-forward archetype (percentile-centred, 8 features)",
    "output_index": "mean of the candidate's goals/90 and assists/90 percentile within the eligible pool (level gate: cosine ignores magnitude)",
    "ready_now": "fit>=0.55 AND output_index>=60 AND 21<=age<=30 AND league_tier<=2 AND minutes_12m>=1500 AND minutes_l6>=0.7*minutes_p6",
    "one_two_years": "not ready_now AND fit>=0.45 AND output_index>=45 AND age<=26 AND minutes_12m>=900",
    "long_term": "not above AND fit>=0.35 AND age<=22 AND minutes_12m>=600",
    "otherwise": "not on board (listed as 'watch' only if fit>=0.55 - too old or too low output)",
    "low_confidence": "candidates with fewer than 4 FotMob trait percentiles are scored on goals/90 + assists/90 only (2 features); they never enter ready_now/one_two_years/watch or the top-15 lists, only long_term (fit=max of the three role sims >=0.35, age<=22, minutes>=600, output_index>=30) with a low-confidence flag",
    "egypt_eligibility": "public lists and the board contain only egypt_eligible=yes (FotMob nationality Egypt, OR Egypt-capped in our DB, OR listed in diaspora_candidates.json - re-read every run). unknown (no nationality on page) go to a separate nationality_unverified list; non-Egyptians are excluded and only counted",
    "not_applied": "no league-strength multiplier on per-90 numbers: any published coefficient set would be unverified here, so goals/assists per 90 are compared as-is inside pool percentiles and league level enters only through board rules",
}


def _board_once(con, sim, egypt_tier=None, write=False):
    rows, vec = sim["_rows"], sim["_vec"]
    sr = {(r[0], r[1]): r[2] for r in con.execute("SELECT fotmob_id,role,sim FROM salah_similarity")}
    if write:
        con.execute("DELETE FROM salah_board")
    res = {"ready_now": [], "one_two_years": [], "long_term": [], "watch": [], "nationality_unverified": []}
    for k, r in rows.items():
        if "target" in r["pools"].split(",") or r["egypt_eligible"] == "no":
            continue
        s1, s2 = sr.get((k, "salah_current")), sr.get((k, "inside_forward_archetype"))
        if s1 is None or s2 is None or r["age"] is None:
            continue
        fit = (s1 + s2) / 2
        if r["low_confidence"]:  # 2-feature vectors: a scorer-only profile can't match Salah's creator profile, so use the best of the three roles
            fit = max(s1, s2, sr.get((k, "marmoush_roaming_striker"), -1))
        v = vec[k]
        out_idx = ((v.get("g90", 0)) + (v.get("a90", 0))) / 2
        trend_ok = (r["minutes_l6"] or 0) >= 0.7 * (r["minutes_p6"] or 0)
        age, mins = r["age"], r["minutes_12m"] or 0
        tier = egypt_tier if (egypt_tier is not None and r["league_id"] == 519) else r["league_tier"]
        low = bool(r["low_confidence"])
        reasons = [f"fit {fit:.2f}" + (" (LOW CONFIDENCE: goals+assists per 90 only; fit = best of the 3 role sims)" if low else ""), f"output_index {out_idx:.0f}", f"age {age}",
                   f"league_tier {tier} ({r['league']})", f"minutes_12m {mins}", f"minutes trend l6/p6 {r['minutes_l6']}/{r['minutes_p6']}"]
        if fit >= .55 and out_idx >= 60 and 21 <= age <= 30 and tier <= READY_MAX_TIER and mins >= 1500 and trend_ok and not low:
            t = "ready_now"
        elif fit >= .45 and out_idx >= 45 and age <= 26 and mins >= 900 and not low:
            t = "one_two_years"
        elif fit >= .35 and age <= 22 and mins >= 600 and (out_idx >= 30 or not low):
            t = "long_term"
        elif fit >= .55 and not low:
            t = "watch"
        else:
            continue
        if r["egypt_eligible"] == "unknown":
            reasons.append("nationality unverified")
            t_out = "nationality_unverified"
        else:
            t_out = t
        item = {"fotmob_id": k, "name": r["name"], "tier": t, "fit": round(fit, 3), "low_confidence": low, "egypt_eligible": r["egypt_eligible"],
                "eligibility_basis": r["eligibility_basis"], "sim_salah": round(s1, 3), "sim_inside_forward": round(s2, 3),
                "sim_marmoush": sr.get((k, "marmoush_roaming_striker")), "output_index": round(out_idx, 1), "age": age, "club": r["club"], "league": r["league"],
                "league_tier_used": tier, "position": r["position_label"], "pools": r["pools"], "minutes_12m": mins, "goals_12m": r["goals_12m"],
                "assists_12m": r["assists_12m"], "reasons": reasons}
        res[t_out].append(item)
        if write and t_out != "nationality_unverified":
            con.execute("INSERT OR REPLACE INTO salah_board VALUES (?,?,?,?,?)", (k, r["name"], t, item["fit"], json.dumps(reasons)))
    for t in res:
        res[t].sort(key=lambda x: -x["fit"])
    return res


def board(con, sim: dict) -> dict:
    res = _board_once(con, sim, write=True)
    alt = _board_once(con, sim, egypt_tier=2)
    sens = {t: [x["name"] for x in alt[t]] for t in ("ready_now", "one_two_years", "long_term")}
    rules = dict(RULES)
    rules["league_tier_table"] = {
        "status": "ANALYST JUDGEMENT, not a fetched or published dataset. It gates only the board (ready_now needs tier <= %d); it does not enter similarity." % READY_MAX_TIER,
        "parameters": {"EGYPT_LEAGUE_TIER": EGYPT_LEAGUE_TIER, "READY_MAX_TIER": READY_MAX_TIER, "FALLBACK_TIER": FALLBACK_TIER},
        "tiers": {"1": {"leagues": ["England (id 47)", "Spain", "Germany", "Italy", "France"], "rationale": "Five leagues with the deepest talent/spending and highest UEFA association coefficients; a strong output here transfers most directly to the national side's opposition level."},
                  "2": {"leagues": list(TIER2), "rationale": "Competitive professional leagues below the top five (matched by name keywords); output is credible but less proven against elite defences."},
                  "3": {"leagues": ["Egyptian Premier League (id 519, parameter)", *TIER3], "rationale": "Domestic/African leagues: Egypt's own league is the pool source and the national team's usual recruitment base, but per-90 output is against weaker average opposition and with no xG feed."},
                  "4": {"leagues": ["anything unmatched, ambiguous bare 'Premier League' names other than ids 47/519"], "rationale": "Unknown level; treated as lowest."}},
        "sensitivity_if_egypt_league_tier_2": sens,
        "sensitivity_note": "Re-running the same rules with the Egyptian league at tier 2 shows how much the tiers depend on this one judgement."}
    return {"rules": rules, "tiers": res}


# ================================================================== orchestration
def coverage(con) -> dict:
    q = lambda s: con.execute(s).fetchone()[0]
    pools = defaultdict(int)
    for (p,) in con.execute("SELECT pools FROM salah_profiles"):
        for x in p.split(","):
            pools[x] += 1
    elig = defaultdict(int)
    for (p,) in con.execute("SELECT pools FROM salah_profiles WHERE eligible=1"):
        for x in p.split(","):
            elig[x] += 1
    reasons = defaultdict(int)
    for (r,) in con.execute("SELECT exclude_reason FROM salah_profiles WHERE exclude_reason IS NOT NULL"):
        reasons[r.split(":")[0]] += 1
    pos = defaultdict(int)
    for (r,) in con.execute("SELECT COALESCE(position_key,'?') FROM salah_profiles"):
        pos[r] += 1
    return {"profiles_total": q("SELECT COUNT(*) FROM salah_profiles"), "pages_missing": q("SELECT COUNT(*) FROM salah_profiles WHERE exclude_reason='no_page'"),
            "with_dob": q("SELECT COUNT(*) FROM salah_profiles WHERE dob IS NOT NULL"), "eligible": q("SELECT COUNT(*) FROM salah_profiles WHERE eligible=1"),
            "eligible_with_deep_season_stats_600min": q("SELECT COUNT(*) FROM salah_profiles WHERE eligible=1 AND deep_minutes>=600"),
            "pool_sizes": dict(pools), "pool_eligible": dict(elig), "exclusions": dict(reasons), "position_keys_seen": dict(pos),
            "diaspora_file_present": (EXPORT / "diaspora_candidates.json").exists(), "diaspora": dict(DIASPORA_STATS),
            "players_dob_before": q("SELECT COUNT(*) FROM players WHERE dob IS NOT NULL"),
            "dob_filled_for_candidates_missing_in_players": q("""SELECT COUNT(*) FROM salah_profiles sp JOIN players p ON p.fotmob_id=sp.fotmob_id
                                                                 WHERE p.dob IS NULL AND sp.dob IS NOT NULL""")}


FIELDS = {
    "available_from_fotmob_player_page": {
        "recentMatches (about the last 12 months, all competitions incl. national team)": "minutes, goals, assists, cards, lineupPositionId per match -> goals/90, assists/90, minutes trend (last 6 mo vs prior 6)",
        "traits (6 percentiles vs the player's position group)": TRAIT_KEYS,
        "firstSeasonStats (deep per-90 for the newest season's first tournament only)": "xG, xGOT, npxG, shots, SoT, xA, passes, chances created, dribbles, duels, aerials, touches, touches in box, tackles, interceptions, recoveries ... with per-90 and percentile",
        "mainLeague / primaryTeam": "current league, club", "birthDate": "DOB",
    },
    "not_available": [
        "previous-season deep stat block (only the newest season is server-rendered; the per-season endpoint needs signed headers / returns 400)",
        "xG / shot data for Egyptian Premier League players (no xG in that league's feed)",
        "non-penalty goals per 90 from recentMatches (goals include penalties)",
    ],
    "deep_stats_use": "stored in salah_profiles.deep_json but NOT used in similarity: in late September the current-season block rarely reaches the 600-minute floor (Salah himself: 481).",
    "min_minutes_rule": f"{MIN_MINUTES} minutes over the trailing window in recentMatches (the 12-month equivalent of 'current + last season'); targets (Salah, Marmoush) are exempt from the filter",
}


def run(conn, allow_fetch=True, max_fetch=400) -> dict:
    conn.executescript(SCHEMA)
    EXPORT.mkdir(parents=True, exist_ok=True)
    dep = dependency(conn)
    pstats = build_profiles(conn, allow_fetch=allow_fetch, max_fetch=max_fetch)
    sim = similarity(conn)
    bd = board(conn, sim)
    conn.commit()
    cov = coverage(conn)
    cov["nationality"] = {
        "egypt_eligible_counts_all_profiles": dict(conn.execute("SELECT COALESCE(egypt_eligible,'n/a'),COUNT(*) FROM salah_profiles GROUP BY 1").fetchall()),
        "excluded_non_egyptian": conn.execute("SELECT COUNT(*) FROM salah_profiles WHERE exclude_reason LIKE 'non_egyptian%'").fetchone()[0],
        "excluded_non_egyptian_by_nationality": dict(conn.execute("SELECT substr(exclude_reason,14),COUNT(*) FROM salah_profiles WHERE exclude_reason LIKE 'non_egyptian%' GROUP BY 1 ORDER BY 2 DESC").fetchall()),
        "unknown_nationality_scored": conn.execute("SELECT COUNT(*) FROM salah_profiles WHERE eligible=1 AND egypt_eligible='unknown'").fetchone()[0],
        "eligible_basis": dict(conn.execute("SELECT eligibility_basis,COUNT(*) FROM salah_profiles WHERE eligible=1 AND eligibility_basis NOT LIKE 'fotmob_nationality_%' OR eligibility_basis='fotmob_nationality_Egypt' GROUP BY 1").fetchall()),
    }
    cov["low_confidence_goals_assists_only"] = [
        dict(name=r["name"], age=r["age"], club=r["club"], league=r["league"], minutes_12m=r["minutes_12m"], g90=r["g90"], a90=r["a90"], pools=r["pools"])
        for r in conn.execute("SELECT * FROM salah_profiles WHERE low_confidence=1 AND eligible=1 AND egypt_eligible!='no' ORDER BY minutes_12m DESC")]
    cov["fetch"] = pstats
    sim.pop("_vec"); sim.pop("_rows")
    payload = {"module": "salah_succession", "as_of": AS_OF.isoformat(), "dependency": dep, "fields": FIELDS,
               "role_definitions": {
                   "salah_current": "Salah's own 8-feature percentile vector (trailing 12 months, FotMob)",
                   "inside_forward_archetype": {"kind": "analyst-defined synthetic profile", "percentiles": ROLE_INSIDE_FORWARD},
                   "marmoush_roaming_striker": "Marmoush's own 8-feature percentile vector (trailing 12 months, FotMob)"},
               "league_strength_multiplier": RULES["not_applied"],
               "similarity": sim["roles"], "board": bd, "coverage": cov}
    tmp = EXPORT / "salah_succession.json.tmp"
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str))
    tmp.replace(EXPORT / "salah_succession.json")
    write_summary(payload)
    return payload


def _fmt(s):
    return f"{s['ppg']:.2f} PPG, {s['gf_pm']:.2f} GF / {s['ga_pm']:.2f} GA, {s['win_rate']*100:.0f}% wins (n={s['n']})" if s.get("n") else "n=0"


def write_summary(p: dict):
    d = p["dependency"]
    o = d["overall"]
    L = [f"# Salah succession - summary (as of {p['as_of']})", "", "Generated by `etl/models/salah_succession.py`; public data only (FotMob pages, Elo ratings, Wikipedia-derived coach tags).", "",
         "## Dependency: Egypt with vs without Salah in the XI (2018-01-01 onward)", "",
         f"- Matches used: {d['n_matches_used']} (excluded {d['n_matches_excluded_no_lineup']} with no usable XI).",
         f"- With Salah starting: {_fmt(o['with_salah_in_xi'])}; opp Elo {o['with_salah_in_xi']['mean_opp_elo']:.0f}, Elo-expected PPG {o['with_salah_in_xi']['elo_expected_ppg']}.",
         f"- Without: {_fmt(o['without_salah_in_xi'])}; opp Elo {o['without_salah_in_xi']['mean_opp_elo']:.0f}, Elo-expected PPG {o['without_salah_in_xi']['elo_expected_ppg']}.",
         f"- PPG difference {o['ppg_diff_with_minus_without']:+.2f}, bootstrap 95% interval {o['ppg_diff_bootstrap95']} -> the interval includes zero; the direction is suggestive, not proven.",
         f"- Salah goal share when starting: {d['salah_share_when_in_xi']['goal_share']} of Egypt goals ({d['salah_share_when_in_xi']['salah_goals']}/{d['salah_share_when_in_xi']['egypt_goals_in_events']}, {d['salah_share_when_in_xi']['matches_with_events']} matches); "
         f"assists {d['salah_share_when_in_xi']['salah_assists']} of {d['salah_share_when_in_xi']['goals_with_assist_recorded']} goals with a recorded assister (small denominator); goal involvement {d['salah_share_when_in_xi']['salah_goal_involvement_share']}.",
         "", "### Splits (with / without Salah)", ""]
    for title, key in (("Opponent Elo band", "by_opp_elo_band"), ("Competition type", "by_competition_type"), ("Coach", "by_coach")):
        L += [f"**{title}**", "", "| split | with Salah | without Salah |", "|---|---|---|"]
        for k, v in d[key].items():
            L.append(f"| {k} | {_fmt(v['with_salah'])} | {_fmt(v['without_salah'])} |")
        L.append("")
    L += ["**Since 2024-02-06 (Hassan era), Salah x Marmoush in the XI**", "", "| cell | result |", "|---|---|"]
    for k, v in d["marmoush_era_since_2024_02_06"]["grid"].items():
        L.append(f"| {k} | {_fmt(v)} |")
    L += ["", "### Confounders", ""] + [f"- {c}" for c in d["confounders"]]
    L += ["", "## Data used for player profiles", ""]
    c = p["coverage"]
    L += [f"- Profiles built: {c['profiles_total']} (pages missing: {c['pages_missing']}); eligible after filters: {c['eligible']}. Pool sizes {c['pool_sizes']}; eligible per pool {c['pool_eligible']}.",
          f"- Exclusions: {c['exclusions']}.",
          f"- Nationality filter: non-Egyptian players excluded (counted only): {c['nationality']['excluded_non_egyptian']} {c['nationality']['excluded_non_egyptian_by_nationality']}; nationality unknown and scored but held in a separate 'nationality_unverified' list: {c['nationality']['unknown_nationality_scored']}. Eligible = FotMob nationality Egypt, OR Egypt-capped in DB, OR listed in diaspora_candidates.json (re-read each run). Percentile pool = Egypt-eligible + unknown players only.",
          f"- Diaspora file present: {c['diaspora_file_present']}; resolution to FotMob ids: {c['diaspora']} (no extra searches were run for unresolved names).",
          f"- Young attackers with too few trait percentiles to score (listed, not ranked): {c['young_attackers_unscored_insufficient_traits']}.",
          f"- Fields: per-90 goals and assists from the trailing-12-month match list; six FotMob position-group percentiles ({', '.join(TRAIT_KEYS)}). Deep season stats (xG, xA...) exist for only the newest season and only {c['eligible_with_deep_season_stats_600min']} eligible players clear 600 min there, so they are stored but not used.",
          "- No league-strength multiplier applied (label: unadjusted). League level only enters the board rules, as an analyst-judged tier.", "",
          "## Top 15 per role (cosine similarity, percentile-centred)", ""]
    for role, r in p["similarity"].items():
        L += [f"### {role}", ""]
        if "top15" not in r:
            L += [r.get("error", "n/a"), ""]
            continue
        L += [f"Nationality-unverified near-matches (not ranked): {[(u['name'], u['sim']) for u in r.get('nationality_unverified_top', [])[:7]]}", ""] if r.get("nationality_unverified_top") else []
        L += ["| # | player | age | club (league) | pos | min 12m | sim | main drivers |", "|---|---|---|---|---|---|---|---|"]
        for t in r["top15"]:
            drv = ", ".join(f"{x['feature']}({x['candidate_pct']:.0f} vs {x['target_pct']:.0f})" for x in t["top_agreeing_features"])
            L.append(f"| {t['rank']} | {t['name']} | {t['age']} | {t['club']} ({t['league']}) | {t['position']} | {t['minutes_12m']} | {t['sim']} | {drv} |")
        L.append("")
    L += ["## Succession board", "", "Rules:", ""] + [f"- {k}: {v}" for k, v in p["board"]["rules"].items() if k != "league_tier_table"] + [""]
    lt = p["board"]["rules"]["league_tier_table"]
    L += ["### League tier table (analyst judgement, drives the tiers)", "", lt["status"], "", f"Parameters: {lt['parameters']}", ""]
    for k, v in lt["tiers"].items():
        L.append(f"- Tier {k}: {', '.join(v['leagues'])}. {v['rationale']}")
    L += ["", f"Sensitivity with the Egyptian league at tier 2: {lt['sensitivity_if_egypt_league_tier_2']}", ""]
    for t, items in p["board"]["tiers"].items():
        L += [f"### {t} ({len(items)})", ""]
        for it in items[:15]:
            L.append(f"- {'[LOW CONFIDENCE] ' if it['low_confidence'] else ''}**{it['name']}** ({it['age']}, {it['club']}, {it['league']}): fit {it['fit']}, output {it['output_index']}, {it['goals_12m']}G {it['assists_12m']}A in {it['minutes_12m']} min.")
        L.append("")
    L += ["## Caveats", "",
          "- Similarity is shape-only (cosine on centred percentiles); the board adds a magnitude gate through output_index. Percentiles for goals/90 and assists/90 are relative to this pool, which mixes Egyptian PL and European players unadjusted for league strength.",
          "- goals/assists include penalties; assists are FotMob's official count; small-minute players are noisy (600-min floor only).",
          "- The trait percentiles are FotMob's position-group comparisons (league/season context unknown to us) and describe the current profile, not a 12-month one.",
          "- Salah's and Marmoush's vectors describe their current (post-move) situations; Salah's recent-match window mixes club and Egypt games.",
          "- Salah dependency numbers are correlational (see confounders).",
          "- The inside-forward archetype is an analyst-defined synthetic profile, not empirical.",
          "- Diaspora candidates are included only if `data/export/diaspora_candidates.json` carries a `fotmob_id` per item."]
    (EXPORT / "salah_succession_summary.md").write_text("\n".join(L) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-fetch", action="store_true", help="use only cached FotMob pages")
    ap.add_argument("--max-fetch", type=int, default=400)
    a = ap.parse_args(argv)
    con = db.connect()
    p = run(con, allow_fetch=not a.no_fetch, max_fetch=a.max_fetch)
    print(json.dumps({"eligible": p["coverage"]["eligible"], "board": {k: len(v) for k, v in p["board"]["tiers"].items()}}))


if __name__ == "__main__":
    main()
