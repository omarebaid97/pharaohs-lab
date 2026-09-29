"""Module E - set-piece analyzer.

    python -m etl.models.set_pieces

Reads data/pharaohs.db, cached FotMob match pages (via etl.sources.fotmob.next_data, cache only - never
hits the network) and the raw StatsBomb AFCON 2023 open data in data/statsbomb/. Writes
  data/export/set_pieces.json
  data/export/set_pieces_summary.md
Own tables (rebuilt every run): sp_goals, sp_shots, sp_penalties, sp_sb_corners, sp_sb_set_piece_shots.
Descriptive frequencies only; small samples get Wilson 90% intervals. Data provided by StatsBomb (AFCON 2023 sections).
"""
from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from .. import db as dbm

ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT_DIR = ROOT / "data" / "export"
MANUAL_CSV = ROOT / "data" / "manual" / "set_piece_routines.csv"
SB_DIR = ROOT / "data" / "statsbomb"
CACHE_DIR = ROOT / "data" / "cache" / "www.fotmob.com"
OUT_JSON = EXPORT_DIR / "set_pieces.json"
OUT_MD = EXPORT_DIR / "set_pieces_summary.md"
MANUAL_HEADER = ["match_date", "opponent", "minute", "situation", "routine_description", "outcome", "video_or_source_url"]

EGYPT_FM = 10255
HASSAN = "Hossam Hassan"
AS_OF = "2026-09-28"
CATS = ["open_play", "corner", "free_kick", "throw_in", "other_set_piece", "penalty", "own_goal", "unknown"]
SP_CATS = ["corner", "free_kick", "throw_in", "other_set_piece"]      # set pieces excluding penalties
FM_SIT = {"RegularPlay": "open_play", "FastBreak": "open_play", "FromCorner": "corner", "FreeKick": "free_kick",
          "Penalty": "penalty", "ThrowInSetPiece": "throw_in", "SetPiece": "other_set_piece"}
SB_ATTRIB = "Data provided by StatsBomb (open data, AFCON 2023)"
SB_LABEL = "AFCON 2023, pre-Hassan, StatsBomb open data"

URLS = {
    "fotmob_match_pages": "https://www.fotmob.com/match/<id> (public web page, __NEXT_DATA__; cached, no new fetches by this module)",
    "statsbomb_open_data": "https://github.com/statsbomb/open-data (raw: https://raw.githubusercontent.com/statsbomb/open-data/master/data/events/<match_id>.json)",
    "statsbomb_spec": "https://github.com/statsbomb/open-data/blob/master/doc/Open%20Data%20Events%20v4.0.0.pdf",
    "eloratings": "https://www.eloratings.net/Egypt.tsv (opponent Elo, inherited from Phase 1 DB; not used in this module)",
    "wikipedia_tenures": "https://en.wikipedia.org/wiki/Hossam_Hassan (coach tenure tags come from the Phase 1 DB)",
}


# ================================================================================================ helpers
def wilson(k, n, z=1.645):
    """90% Wilson interval for a proportion."""
    if not n:
        return {"k": k, "n": n, "p": None, "lo": None, "hi": None}
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return {"k": k, "n": n, "p": round(p, 3), "lo": round(max(0, c - h), 3), "hi": round(min(1, c + h), 3)}


def r(x, nd=3):
    return None if x is None else round(x, nd)


def div(a, b, nd=3):
    return None if not b else round(a / b, nd)


def pct(x, nd=0):
    return "n/a" if x is None else f"{100 * x:.{nd}f}%"


# ================================================================================================ FotMob layer
def load_fotmob(conn):
    """Parse cached FotMob pages of every Egypt match: goals, shots, penalties, shootouts."""
    from ..sources import fotmob
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT m.id, m.date, m.competition, m.stage, m.source_url, m.egypt_side, m.decided_by, m.pens_home, m.pens_away,
               m.home_score, m.away_score, c.name AS coach,
               CASE m.egypt_side WHEN 'home' THEN at.name ELSE ht.name END AS opponent
        FROM matches m
        LEFT JOIN coaches c ON c.id = m.egypt_coach_id
        LEFT JOIN teams ht ON ht.id = m.home_team_id LEFT JOIN teams at ON at.id = m.away_team_id
        WHERE m.is_egypt = 1 AND m.source = 'fotmob' ORDER BY m.date""").fetchall()
    matches, goals, shots, pens, shoot, notes = [], [], [], [], [], []
    for m in rows:
        try:
            nd = fotmob.next_data(m["source_url"], ttl=10 ** 10)   # cache only: ttl huge => never refetches
        except Exception as e:  # not cached -> skip and report
            notes.append(f"{m['date']} {m['source_url']}: page unavailable ({type(e).__name__})")
            continue
        c = nd["props"]["pageProps"]["content"]
        egy_home = m["egypt_side"] == "home"
        sm = (c.get("shotmap") or {}).get("shots") or []
        has_sm = bool([s for s in sm if s.get("period") != "PenaltyShootout"])
        info = dict(match_id=m["id"], date=m["date"], competition=m["competition"], opponent=m["opponent"],
                    coach=m["coach"] or "Unassigned (coach unknown)", year=int(m["date"][:4]), has_shotmap=has_sm,
                    source_url=m["source_url"])
        matches.append(dict(info, decided_by=m["decided_by"], stage=m["stage"], egypt_side=m["egypt_side"],
                            pens_home=m["pens_home"], pens_away=m["pens_away"]))
        play = [s for s in sm if s.get("period") != "PenaltyShootout" and not s.get("isOwnGoal")]
        evs = (c["matchFacts"].get("events") or {})
        # ---- goals
        gcount = Counter()
        for e in evs.get("events") or []:
            t = e.get("type")
            if t == "Goal" and not e.get("isPenaltyShootoutEvent"):
                # isHome = side credited with the goal (own goals included: the scorer is on the other side)
                side = "for" if bool(e.get("isHome")) == egy_home else "against"
                gcount[side] += 1
                pid = e.get("playerId") or (e.get("player") or {}).get("id")
                sme = e.get("shotmapEvent")
                if not sme and has_sm:
                    for s in play:
                        if s.get("eventType") == "Goal" and s.get("playerId") == pid and s.get("min") == e.get("time"):
                            sme = s
                            break
                desc = (e.get("goalDescriptionKey") or "").lower()
                reason = None
                if e.get("ownGoal"):
                    cat = "own_goal"
                elif desc == "penalty" or (sme and sme.get("situation") == "Penalty"):
                    cat = "penalty"
                elif sme:
                    cat = FM_SIT.get(sme.get("situation"), "unknown")
                else:
                    cat = "unknown"
                    reason = "goal not matched to a shot-map entry" if has_sm else "no shot map for this match"
                goals.append(dict(info, side=side, minute=e.get("time"), category=cat, scorer=(e.get("player") or {}).get("name"), scorer_id=pid,
                                  assister=(e.get("assistInput") or re.sub(r"^assist by ", "", e.get("assistStr") or "", flags=re.I)) or None,
                                  assister_id=e.get("assistPlayerId"), header=(desc == "header"),
                                  xg=(sme or {}).get("expectedGoals"), shot_type=(sme or {}).get("shotType"), unknown_reason=reason))
            elif t == "MissedPenalty" and not e.get("isPenaltyShootoutEvent"):
                side = "for" if bool(e.get("isHome")) == egy_home else "against"
                pens.append(dict(info, side=side, minute=e.get("time"), taker=(e.get("player") or {}).get("name"), taker_id=(e.get("player") or {}).get("id"), result="missed_or_saved"))
        # score check (in-play goals vs final score; shootout excluded)
        hs, as_ = m["home_score"], m["away_score"]
        if hs is not None and as_ is not None:
            gf, ga = (hs, as_) if egy_home else (as_, hs)
            if m["decided_by"] == "pens":
                pass  # score fields hold the in-play score for shootouts (verified below via counts)
            if (gcount["for"], gcount["against"]) != (gf, ga):
                notes.append(f"{m['date']} vs {m['opponent']}: goal events {gcount['for']}-{gcount['against']} != score {gf}-{ga}")
        # ---- shots
        for s in play:
            side = "for" if s.get("teamId") == EGYPT_FM else "against"
            shots.append(dict(info, side=side, minute=s.get("min"), player=s.get("playerName"), player_id=s.get("playerId"), category=FM_SIT.get(s.get("situation"), "unknown"),
                              raw_situation=s.get("situation"), outcome=s.get("eventType"), xg=s.get("expectedGoals") or 0.0,
                              shot_type=s.get("shotType")))
        # ---- penalty goals (scored) in play
        for g in goals:
            if g["match_id"] == m["id"] and g["category"] == "penalty":
                pens.append(dict(info, side=g["side"], minute=g["minute"], taker=g["scorer"], taker_id=g["scorer_id"], result="scored"))
        # ---- shootouts
        pso = evs.get("penaltyShootoutEvents") or []
        if pso:
            kicks = []
            for k in pso:
                side = "for" if bool(k.get("isHome")) == egy_home else "against"
                kicks.append(dict(side=side, player=(k.get("player") or {}).get("name"), player_id=(k.get("player") or {}).get("id"), scored=(k.get("type") == "Goal"), raw=k.get("type")))
            shoot.append(dict(info, decided_by=m["decided_by"], pens_home=m["pens_home"], pens_away=m["pens_away"], egypt_side=m["egypt_side"], kicks=kicks))
    # canonical player names: one spelling per FotMob player id (legacy pages spell names differently)
    names = defaultdict(Counter)
    for lst, kn, ki in ((goals, "scorer", "scorer_id"), (goals, "assister", "assister_id"), (pens, "taker", "taker_id"), (shots, "player", "player_id")):
        for x in lst:
            if x.get(ki) and x.get(kn):
                names[int(x[ki])][x[kn]] += 1
    for sh_ in shoot:
        for k in sh_["kicks"]:
            if k.get("player_id") and k.get("player"):
                names[int(k["player_id"])][k["player"]] += 1
    canon = {pid: max(c.items(), key=lambda kv: (kv[1], len(kv[0])))[0] for pid, c in names.items()}
    for lst, kn, ki in ((goals, "scorer", "scorer_id"), (goals, "assister", "assister_id"), (pens, "taker", "taker_id"), (shots, "player", "player_id")):
        for x in lst:
            if x.get(ki) and int(x[ki]) in canon:
                x[kn] = canon[int(x[ki])]
    for sh_ in shoot:
        for k in sh_["kicks"]:
            if k.get("player_id") and int(k["player_id"]) in canon:
                k["player"] = canon[int(k["player_id"])]
    return matches, goals, shots, pens, shoot, notes


# ================================================================================================ goals section
def goal_group(goals_f, goals_a, matches):
    """Counts of goals for/against by situation with coverage next to every number."""
    out = {"matches": len(matches), "matches_with_shotmap": sum(1 for m in matches if m["has_shotmap"])}
    for label, gl in (("for", goals_f), ("against", goals_a)):
        cnt = Counter(g["category"] for g in gl)
        n = len(gl)
        non_og = n - cnt["own_goal"]
        known = non_og - cnt["unknown"]
        sp_min = sum(cnt[c] for c in SP_CATS)
        # full-coverage subset: goals from matches that have a shot map (penalties + own goals are known everywhere)
        sub = [g for g in gl if g["has_shotmap"]]
        sc = Counter(g["category"] for g in sub)
        sub_non_og = len(sub) - sc["own_goal"]
        out[label] = {
            "goals": n, "by_situation": {c: cnt[c] for c in CATS},
            "own_goals_credited": cnt["own_goal"],
            "situation_known": known, "situation_unknown": cnt["unknown"], "of_non_own_goals": non_og,
            "coverage": div(known, non_og),
            "penalty_goals_complete": cnt["penalty"],
            "set_piece_goals_lower_bound_excl_pens": sp_min,
            "shotmap_matches_subset": {
                "goals": len(sub), "by_situation": {c: sc[c] for c in CATS},
                "coverage": div(sub_non_og - sc["unknown"], sub_non_og),
                "set_piece_share_excl_pens": wilson(sum(sc[c] for c in SP_CATS), sub_non_og - sc["unknown"] - sc["penalty"]) if sub else None,
            },
        }
    return out


def goals_section(matches, goals):
    def sel(pred):
        ms = [m for m in matches if pred(m)]
        ids = {m["match_id"] for m in ms}
        gf = [g for g in goals if g["match_id"] in ids and g["side"] == "for"]
        ga = [g for g in goals if g["match_id"] in ids and g["side"] == "against"]
        return goal_group(gf, ga, ms)
    by_year = {str(y): sel(lambda m, y=y: m["year"] == y) for y in sorted({m["year"] for m in matches})}
    order = []
    for m in matches:
        if m["coach"] not in order:
            order.append(m["coach"])
    by_coach = {c: sel(lambda m, c=c: m["coach"] == c) for c in order}
    for c in order:
        ms = [m for m in matches if m["coach"] == c]
        by_coach[c]["first_match"], by_coach[c]["last_match"] = ms[0]["date"], ms[-1]["date"]
    return {
        "note": ("Situation is only knowable per goal where (a) FotMob labels a penalty, or (b) the match page has a shot map (16 of 109 Egypt matches since 2018-01-01). "
                 "Goals in other matches stay 'unknown' - they are NOT assumed open play. Non-penalty shares are therefore quoted on the shot-map subset; "
                 "penalty counts are complete for all matches. Own goals carry no situation and are shown separately. Year = UTC calendar year of kickoff."),
        "categories": CATS, "overall": sel(lambda m: True), "by_year": by_year, "by_coach": by_coach,
        "hassan_era": sel(lambda m: m["coach"] == HASSAN),
        "pre_hassan_shotmap_sample": sel(lambda m: m["coach"] != HASSAN),
    }


# ================================================================================================ shots section
def shot_group(shots, matches, label):
    """Set-piece shots/xG for and against over shot-map matches only."""
    ms = [m for m in matches if m["has_shotmap"]]
    ids = {m["match_id"] for m in ms}
    n_m = len(ms)
    out = {"label": label, "matches": n_m, "match_dates": [m["date"] for m in ms]}
    for side in ("for", "against"):
        sl = [s for s in shots if s["side"] == side and s["match_id"] in ids]
        tot_xg = sum(s["xg"] for s in sl)
        nopen = [s for s in sl if s["category"] != "penalty"]
        xg_nopen = sum(s["xg"] for s in nopen)
        blocks = {}
        for cat in CATS[:6]:
            cl = [s for s in sl if s["category"] == cat]
            g = sum(1 for s in cl if s["outcome"] == "Goal")
            blocks[cat] = dict(shots=len(cl), per_match=div(len(cl), n_m, 2), goals=g, conversion=wilson(g, len(cl)),
                               xg=r(sum(s["xg"] for s in cl)), xg_per_match=div(sum(s["xg"] for s in cl), n_m, 3),
                               xg_per_shot=div(sum(s["xg"] for s in cl), len(cl)), share_of_xg_excl_pens=div(sum(s["xg"] for s in cl), xg_nopen) if cat != "penalty" else None)
        sp = [s for s in sl if s["category"] in SP_CATS]
        g = sum(1 for s in sp if s["outcome"] == "Goal")
        blocks["all_set_pieces_excl_pens"] = dict(
            shots=len(sp), per_match=div(len(sp), n_m, 2), goals=g, conversion=wilson(g, len(sp)), xg=r(sum(s["xg"] for s in sp)),
            xg_per_match=div(sum(s["xg"] for s in sp), n_m), xg_per_shot=div(sum(s["xg"] for s in sp), len(sp)),
            share_of_xg_excl_pens=div(sum(s["xg"] for s in sp), xg_nopen), share_of_shots_excl_pens=div(len(sp), len(nopen)))
        out[side] = dict(total_shots=len(sl), total_xg=r(tot_xg), xg_excl_pens=r(xg_nopen), by_situation=blocks)
    return out


def shots_section(matches, shots):
    haz = [m for m in matches if m["coach"] == HASSAN]
    afcon23 = [m for m in matches if m["date"] >= "2024-01-01" and m["date"] <= "2024-01-31" and m["has_shotmap"]]
    return {
        "note": ("Shot-map coverage only. FotMob shot-map situations: FromCorner, FreeKick, ThrowInSetPiece, SetPiece (other dead-ball, incl. indirect free kicks; shown as other_set_piece), "
                 "Penalty, RegularPlay/FastBreak (open_play). Set piece = corner + free kick + throw-in + other set piece; penalties reported separately and excluded from the xG share. "
                 "Conversion = goals / shots (goals include the shot that scored); intervals are 90% Wilson."),
        "coverage": {"egypt_matches_since_2018": len(matches), "with_shotmap": sum(1 for m in matches if m["has_shotmap"]),
                     "hassan_matches": len(haz), "hassan_with_shotmap": sum(1 for m in haz if m["has_shotmap"]),
                     "by_year": {str(y): [sum(1 for m in matches if m["year"] == y and m["has_shotmap"]), sum(1 for m in matches if m["year"] == y)]
                                 for y in sorted({m["year"] for m in matches})},
                     "double_count_warning": "The '2024: 8/19' figure double counts the 4 AFCON 2023 matches (FotMob row + StatsBomb row). Distinct matches: 4 of 15 FotMob-2024 matches have shots."},
        "all_shotmap_matches": shot_group(shots, matches, "All Egypt matches with a FotMob shot map"),
        "hassan_era": shot_group(shots, haz, "Hassan era (shot-map matches only)"),
        "pre_hassan_afcon2023_fotmob": shot_group(shots, afcon23, "AFCON 2023 (Vitoria), FotMob shot map"),
    }


# ================================================================================================ penalties section
def penalties_section(matches, pens, shoot, shots, goals):
    def rec(side):
        sl = [p for p in pens if p["side"] == side]
        sc = sum(1 for p in sl if p["result"] == "scored")
        ms = sum(1 for p in sl if p["result"] != "scored")
        return dict(awarded=len(sl), scored=sc, missed_or_saved=ms, conversion=wilson(sc, len(sl)))
    takers = defaultdict(lambda: [0, 0])
    for p in pens:
        if p["side"] == "for":
            takers[p["taker"]][0 if p["result"] == "scored" else 1] += 1
    tk = sorted(([n, a, a + b] for n, (a, b) in takers.items()), key=lambda x: (-x[2], -x[1], x[0]))
    per_year = {}
    for y in sorted({m["year"] for m in matches}):
        per_year[str(y)] = {"won": rec_of(pens, "for", y), "conceded": rec_of(pens, "against", y)}
    # cross-check with shot maps
    sm_ids = {m["match_id"] for m in matches if m["has_shotmap"]}
    sm_pens = [s for s in shots if s["category"] == "penalty" and s["match_id"] in sm_ids]
    ev_pens = [p for p in pens if p["match_id"] in sm_ids]
    # shootouts
    so_matches = []
    tot = {"for": [0, 0], "against": [0, 0]}
    player = defaultdict(lambda: [0, 0])
    w = l = 0
    for m_ in matches:
        if m_["decided_by"] == "pens" and m_["pens_home"] is not None:
            ep = m_["pens_home"] if m_["egypt_side"] == "home" else m_["pens_away"]
            op = m_["pens_away"] if m_["egypt_side"] == "home" else m_["pens_home"]
            if ep > op: w += 1
            else: l += 1
    for s in shoot:
        egy_p = s["pens_home"] if s["egypt_side"] == "home" else s["pens_away"]
        opp_p = s["pens_away"] if s["egypt_side"] == "home" else s["pens_home"]
        res = None if egy_p is None else ("won" if egy_p > opp_p else "lost")
        e_sc = sum(1 for k in s["kicks"] if k["side"] == "for" and k["scored"]); e_n = sum(1 for k in s["kicks"] if k["side"] == "for")
        o_sc = sum(1 for k in s["kicks"] if k["side"] == "against" and k["scored"]); o_n = sum(1 for k in s["kicks"] if k["side"] == "against")
        tot["for"][0] += e_sc; tot["for"][1] += e_n; tot["against"][0] += o_sc; tot["against"][1] += o_n
        for k in s["kicks"]:
            if k["side"] == "for":
                player[k["player"]][0 if k["scored"] else 1] += 1
        so_matches.append(dict(date=s["date"], opponent=s["opponent"], competition=s["competition"], decided_by_flag=s["decided_by"],
                               shootout=f"{egy_p}-{opp_p}" if egy_p is not None else None, result=res,
                               egypt_kicks=f"{e_sc}/{e_n}", opponent_kicks=f"{o_sc}/{o_n}",
                               egypt_sequence=[(k["player"], "scored" if k["scored"] else "missed_or_saved") for k in s["kicks"] if k["side"] == "for"]))
    pl = sorted(([n, a, a + b] for n, (a, b) in player.items()), key=lambda x: (-x[2], -x[1], x[0]))
    pens_flag = sum(1 for m in matches if m["decided_by"] == "pens")
    return {
        "note": ("In-play penalties come from FotMob match events (goal 'Penalty' descriptions + MissedPenalty events) for all matches since 2018; 'missed_or_saved' merges misses, saves and posts "
                 "(FotMob does not split them). Shootout kicks are excluded from in-play counts. Older pages may be less complete than 2024+ ones; no independent source to verify."),
        "coverage": {"matches": len(matches), "note": "events feed present for all cached Egypt matches since 2018-01-01",
                     "shotmap_cross_check": {"matches_with_shotmap": len(sm_ids), "penalty_shots_in_shotmaps": len(sm_pens), "penalty_events_in_same_matches": len(ev_pens)}},
        "in_play_won_by_egypt": rec("for"), "in_play_conceded": rec("against"),
        "by_year": per_year,
        "egypt_takers_in_play": [dict(player=n, scored=a, taken=t) for n, a, t in tk],
        "shootouts": {
            "matches_with_shootout_events": len(shoot), "matches_flagged_decided_by_pens": pens_flag,
            "record": dict(won=w, lost=l), "matches_with_kick_data": len(shoot), "egypt_kicks": wilson(tot["for"][0], tot["for"][1]), "opponent_kicks": wilson(tot["against"][0], tot["against"][1]),
            "egypt_takers": [dict(player=n, scored=a, taken=t) for n, a, t in pl], "matches": so_matches,
            "note": ("Record counts every match with decided_by='pens' (from matches.pens_home/pens_away). Kick-by-kick data (FotMob penaltyShootoutEvents) exists for fewer matches "
                     "(the 2021-12-18 Arab Cup shootout page has no kick list), so kick conversion and takers cover only the matches listed."),
        },
    }


def rec_of(pens, side, y):
    sl = [p for p in pens if p["side"] == side and p["year"] == y]
    sc = sum(1 for p in sl if p["result"] == "scored")
    return dict(awarded=len(sl), scored=sc, missed_or_saved=len(sl) - sc)


# ================================================================================================ StatsBomb layer
def sb_load():
    lineups, events = {}, {}
    names = {}
    for f in sorted((SB_DIR / "events").glob("*.json")):
        mid = int(f.stem)
        events[mid] = json.loads(f.read_text())
        lf = SB_DIR / "lineups" / f.name
        if lf.exists():
            for t in json.loads(lf.read_text()):
                for p in t["lineup"]:
                    names[p["player_id"]] = p.get("player_nickname") or p["player_name"]
    return events, names


def sb_shot_cat(e):
    s = e["shot"]
    st, pp = s["type"]["name"], (e.get("play_pattern") or {}).get("name", "")
    if st == "Penalty": return "penalty"
    if st == "Free Kick": return "free_kick_direct"
    if st == "Corner" or pp == "From Corner": return "corner"
    if pp == "From Free Kick": return "free_kick_indirect"
    if pp == "From Throw In": return "throw_in"
    return "open_play"


SB_SP = ["corner", "free_kick_direct", "free_kick_indirect", "throw_in"]
BOX_X = 102.0


def corner_zone(end, y0, length):
    if length is not None and length < 15:
        return "short"
    if not end:
        return "no_end_location"
    x, y = end[0], end[1]
    if x < BOX_X or y < 18 or y > 62:
        return "outside_box"
    if x >= 114 and 30 <= y <= 50:
        return "six_yard_box"
    if 30 <= y <= 50:
        return "central_box"
    near_side_low = y0 < 40
    near = (y < 30) if near_side_low else (y > 50)
    return "near_post_zone" if near else "far_post_zone"


CONTACT_TYPES = {"Ball Receipt*", "Clearance", "Interception", "Goal Keeper", "Block", "Ball Recovery", "Duel", "Shot",
                 "Miscontrol", "Foul Won", "Foul Committed", "Dribble", "Pass"}


def first_contact(evs, i, att_team):
    c = evs[i]
    for j in range(i + 1, min(i + 16, len(evs))):
        e = evs[j]
        if e.get("period") != c.get("period") or e.get("possession") != c.get("possession") and j > i + 3:
            break
        t = e["type"]["name"]
        if t not in CONTACT_TYPES:
            continue
        team = e["team"]["name"]
        if t == "Ball Receipt*":
            if (e.get("ball_receipt") or {}).get("outcome"):
                continue  # failed receipt
            return "attacker" if team == att_team else "defender"
        if t == "Duel":
            d = (e.get("duel") or {}).get("type", {}).get("name")
            if d == "Aerial Lost":
                return "defender" if team == att_team else "attacker"
            continue
        if t == "Goal Keeper":
            return "goalkeeper"
        if t == "Foul Committed":
            return "foul_by_attackers" if team == att_team else "foul_by_defenders"
        if t == "Foul Won":
            continue
        return "attacker" if team == att_team else "defender"
    return "none_found"


def sb_analyse(events, names, conn):
    egypt_matches = [m for m, ev in events.items() if any(e["team"]["name"] == "Egypt" for e in ev[:5])]
    pname = lambda e: names.get((e.get("player") or {}).get("id"), (e.get("player") or {}).get("name"))
    corners = []       # one row per corner delivery
    sp_shots = []      # shots (all categories) with team labels
    fk_takers = []
    match_meta = {}
    for mid, evs in events.items():
        teams = [e["team"]["name"] for e in evs[:2]]
        id_idx = {e["id"]: i for i, e in enumerate(evs)}
        match_meta[mid] = teams
        for i, e in enumerate(evs):
            t = e["type"]["name"]
            if e.get("period") == 5:
                continue
            if t == "Shot":
                s = e["shot"]
                kp = id_idx.get(s.get("key_pass_id"))
                # set-piece taker: latest set-piece pass by the same team earlier in the same possession
                taker = None
                for j in range(i - 1, max(-1, i - 40), -1):
                    q = evs[j]
                    if q.get("possession") != e.get("possession"):
                        break
                    if q["type"]["name"] == "Pass" and q["pass"].get("type", {}).get("name") in ("Corner", "Free Kick", "Throw-in") and q["team"]["name"] == e["team"]["name"]:
                        taker = pname(q)
                        break
                sp_shots.append(dict(match=mid, team=e["team"]["name"], opp=[x for x in teams if x != e["team"]["name"]][0] if len(teams) > 1 else None,
                                     minute=e["minute"], player=pname(e), cat=sb_shot_cat(e), xg=s.get("statsbomb_xg") or 0.0,
                                     goal=s["outcome"]["name"] == "Goal", outcome=s["outcome"]["name"], body_part=s.get("body_part", {}).get("name"),
                                     assister=pname(evs[kp]) if kp is not None else None, taker=taker, first_time=bool(s.get("first_time"))))
            elif t == "Pass":
                p = e["pass"]
                ptype = p.get("type", {}).get("name")
                if ptype == "Corner":
                    end = p.get("end_location")
                    tech = (p.get("technique") or {}).get("name") or ("Inswinging" if p.get("inswinging") else "Outswinging" if p.get("outswinging") else "Straight" if p.get("straight") else "Unspecified")
                    zone = corner_zone(end, e["location"][1], p.get("length"))
                    fc = "n/a_short" if zone == "short" else first_contact(evs, i, e["team"]["name"])
                    # shots in the same possession after the corner
                    res_shots = [x for x in evs[i + 1:i + 60] if x.get("possession") == e.get("possession") and x["type"]["name"] == "Shot" and x["team"]["name"] == e["team"]["name"]]
                    corners.append(dict(match=mid, team=e["team"]["name"], minute=e["minute"], taker=pname(e), technique=tech, zone=zone,
                                        side="left" if e["location"][1] < 40 else "right", length=p.get("length"),
                                        recipient=names.get((p.get("recipient") or {}).get("id"), (p.get("recipient") or {}).get("name")),
                                        completed=not p.get("outcome"), first_contact=fc, shots=len(res_shots),
                                        xg=sum((x["shot"].get("statsbomb_xg") or 0) for x in res_shots), goals=sum(1 for x in res_shots if x["shot"]["outcome"]["name"] == "Goal")))
                elif ptype == "Free Kick":
                    end = p.get("end_location") or [0, 0]
                    fk_takers.append(dict(match=mid, team=e["team"]["name"], taker=pname(e), into_box=(end[0] >= BOX_X and 18 <= end[1] <= 62 and (p.get("length") or 0) >= 15),
                                          from_x=e["location"][0], length=p.get("length")))
    return egypt_matches, corners, sp_shots, fk_takers, match_meta


def sb_section(events, names, conn):
    egypt_matches, corners, sp_shots, fk_takers, meta = sb_analyse(events, names, conn)
    E = "Egypt"
    egy_set = set(egypt_matches)
    all_sh = [s for s in sp_shots]

    def share_block(shots, n_teammatches):
        nop = [s for s in shots if s["cat"] != "penalty"]
        tot = sum(s["xg"] for s in nop)
        spx = sum(s["xg"] for s in nop if s["cat"] in SB_SP)
        d = {"team_matches": n_teammatches, "shots": len(shots), "xg_total": r(sum(s["xg"] for s in shots)), "xg_excl_pens": r(tot),
             "set_piece_xg_share_excl_pens": div(spx, tot),
             "set_piece_shots_share_excl_pens": div(sum(1 for s in nop if s["cat"] in SB_SP), len(nop)),
             "narrow_corner_plus_direct_fk_xg_share_excl_pens": div(sum(s["xg"] for s in nop if s["cat"] in ("corner", "free_kick_direct")), tot), "by_category": {}}
        for c in SB_SP + ["open_play", "penalty"]:
            cl = [s for s in shots if s["cat"] == c]
            d["by_category"][c] = dict(shots=len(cl), goals=sum(1 for s in cl if s["goal"]), xg=r(sum(s["xg"] for s in cl)),
                                      xg_per_shot=div(sum(s["xg"] for s in cl), len(cl)))
        return d

    egy_for = [s for s in all_sh if s["match"] in egy_set and s["team"] == E]
    egy_ag = [s for s in all_sh if s["match"] in egy_set and s["team"] != E]
    n_tm = len(events) * 2
    tourn = share_block(all_sh, n_tm)
    others_for = [s for s in all_sh if s["team"] != E]
    # shot-map sharing: per-team-match xg share, egypt vs field
    corner_stats = lambda cl: dict(
        corners=len(cl), short=sum(1 for c in cl if c["zone"] == "short"), delivered=sum(1 for c in cl if c["zone"] != "short"),
        techniques=dict(Counter(c["technique"] for c in cl if c["zone"] != "short")), end_zones=dict(Counter(c["zone"] for c in cl)),
        completed=wilson(sum(1 for c in cl if c["completed"]), len(cl)),
        first_contact=dict(Counter(c["first_contact"] for c in cl)),
        first_contact_win_rate_attackers=_fc_rate(cl), shots_after_corner=sum(c["shots"] for c in cl),
        shots_per_corner=div(sum(c["shots"] for c in cl), len(cl)), goals=sum(c["goals"] for c in cl),
        xg=r(sum(c["xg"] for c in cl)), xg_per_corner=div(sum(c["xg"] for c in cl), len(cl)),
        corners_with_a_shot=sum(1 for c in cl if c["shots"]))
    egy_att = [c for c in corners if c["match"] in egy_set and c["team"] == E]
    egy_def = [c for c in corners if c["match"] in egy_set and c["team"] != E]
    field = [c for c in corners if not (c["match"] in egy_set and c["team"] == E)]
    # takers / targets
    tk = Counter(c["taker"] for c in egy_att)
    tk_tech = defaultdict(Counter)
    for c in egy_att:
        tk_tech[c["taker"]][c["technique"] if c["zone"] != "short" else "Short"] += 1
    rec = Counter(c["recipient"] for c in egy_att if c["recipient"])
    fk = Counter(f["taker"] for f in fk_takers if f["team"] == E and f["match"] in egy_set)
    fk_box = Counter(f["taker"] for f in fk_takers if f["team"] == E and f["match"] in egy_set and f["into_box"])
    shooters = Counter(s["player"] for s in egy_for if s["cat"] in SB_SP)
    goal_rows = [s for s in egy_for + egy_ag if s["goal"]]
    conn.row_factory = sqlite3.Row
    dm = {r_["source_match_id"]: r_ for r_ in conn.execute("SELECT source_match_id, date FROM matches WHERE source='statsbomb'")}

    def shot_row(s):
        return dict(date=dm[str(s["match"])]["date"] if str(s["match"]) in dm else None, opponent=s["opp"] if s["team"] == E else s["team"],
                    minute=s["minute"], category=s["cat"], scorer=s["player"], assister=s["assister"], set_piece_taker=s["taker"], xg=r(s["xg"]), body_part=s["body_part"])

    def sp_shot_rows(sl):
        return [shot_row(s) for s in sl if s["cat"] in SB_SP + ["penalty"]]
    return {
        "label": SB_LABEL, "attribution": SB_ATTRIB,
        "matches": [{"statsbomb_match_id": m, "date": dm[str(m)]["date"] if str(m) in dm else None, "teams": meta[m]} for m in sorted(egypt_matches)],
        "note": ("StatsBomb classifies shots by shot.type and play_pattern: 'corner'/'free_kick_indirect'/'throw_in' include the whole possession that started from that restart "
                 "(second balls too), 'free_kick_direct' = shot.type Free Kick. This is broader than FotMob's shot-level tag, so the two sources' set-piece shares are not directly comparable. "
                 "Corner delivery classification: short = pass length < 15 yd; box = x>=102 and 18<=y<=62 on the 120x80 pitch; near/far post relative to the taker's side. "
                 "First contact = the first attacker/defender/goalkeeper event after the delivery (heuristic on the event stream; aerial duels via 'Aerial Lost')."),
        "egypt_set_piece_shooting": {"for": share_block(egy_for, 4), "against": share_block(egy_ag, 4)},
        "tournament_benchmark": {
            "scope": "all 52 AFCON 2023 matches, both teams, StatsBomb xG",
            "all_teams_pooled": tourn,
            "excluding_egypt_pooled": share_block(others_for, (len(events) - len(egy_set)) * 2 + len(egy_set)),
        },
        "corners_egypt_attacking": corner_stats(egy_att),
        "corners_egypt_defending": corner_stats(egy_def),
        "corners_field_benchmark": corner_stats(field) | {"note": "all corners in the 52 matches except Egypt's own attacking corners"},
        "corner_takers": [dict(player=p, corners=n, delivery=dict(tk_tech[p])) for p, n in tk.most_common()],
        "corner_targets_first_recipient": [dict(player=p, times=n) for p, n in rec.most_common(10)],
        "free_kick_takers": [dict(player=p, free_kick_passes=n, into_box=fk_box.get(p, 0)) for p, n in fk.most_common()],
        "set_piece_shooters_egypt": [dict(player=p, shots=n) for p, n in shooters.most_common()],
        "egypt_set_piece_goals_and_penalties": sp_shot_rows([s for s in egy_for if s["goal"]]),
        "goals_conceded_from_set_pieces": sp_shot_rows([s for s in egy_ag if s["goal"]]),
        "all_goals_egypt_matches": len(goal_rows),
        "egypt_shots_from_corners": [shot_row(s) | {"outcome": s["outcome"]} for s in egy_for if s["cat"] == "corner"],
        "coverage": {"egypt_matches": len(egy_set), "tournament_matches": len(events),
                     "note": "complete event data for all matches; shootout (period 5) excluded"},
        "_rows": {"corners": corners, "shots": sp_shots},
    }


def _fc_rate(cl):
    d = [c for c in cl if c["zone"] != "short"]
    a = sum(1 for c in d if c["first_contact"] == "attacker")
    df = sum(1 for c in d if c["first_contact"] in ("defender", "goalkeeper"))
    return wilson(a, a + df)


# ================================================================================================ takers / targets (FotMob)
def takers_section(goals, shots):
    sp_goals = [g for g in goals if g["side"] == "for" and g["category"] in SP_CATS]
    sp_against = [g for g in goals if g["side"] == "against" and g["category"] in SP_CATS]
    def cnt(gl, k):
        return [dict(player=p, goals=n) for p, n in Counter(g[k] for g in gl if g[k]).most_common(12)]
    sh = [s for s in shots if s["side"] == "for" and s["category"] in SP_CATS]
    by_player = defaultdict(lambda: [0, 0, 0.0])
    for s in sh:
        b = by_player[s["player"]]; b[0] += 1; b[1] += s["outcome"] == "Goal"; b[2] += s["xg"]
    shooters = sorted(([p, a, g, x] for p, (a, g, x) in by_player.items()), key=lambda z: (-z[1], -z[3]))[:12]
    def row(g):
        return dict(date=g["date"], opponent=g["opponent"], competition=g["competition"], minute=g["minute"], situation=g["category"],
                    scorer=g["scorer"], assister=g["assister"], header=g["header"], coach=g["coach"])
    ng = len([g for g in goals if g["side"] == "for" and g["category"] not in ("own_goal",)])
    return {
        "note": ("FotMob shot-map matches only (16 of 109). 'assister' is FotMob's assist credit on the goal - for a corner or free kick it is normally the taker, but a header-on or a "
                 "second-phase pass can take the credit instead, and FotMob gives no assist on some goals. Takers themselves are not in FotMob data; see the StatsBomb section for AFCON 2023 takers."),
        "coverage": {"egypt_goals_non_own": ng, "egypt_set_piece_goals_identified": len(sp_goals),
                     "penalty_goals_identified": sum(1 for g in goals if g["side"] == "for" and g["category"] == "penalty")},
        "egypt_set_piece_goals": [row(g) for g in sp_goals],
        "egypt_set_piece_scorers": cnt(sp_goals, "scorer"),
        "egypt_set_piece_goal_assisters": cnt(sp_goals, "assister"),
        "egypt_set_piece_goals_missing_assist": sum(1 for g in sp_goals if not g["assister"]),
        "goals_conceded_from_set_pieces": [row(g) for g in sp_against],
        "top_set_piece_shooters_shotmap": [dict(player=p, shots=a, goals=int(g), xg=r(x, 2)) for p, a, g, x in shooters],
    }


# ================================================================================================ manual tagging
def manual_section():
    MANUAL_CSV.parent.mkdir(parents=True, exist_ok=True)
    if not MANUAL_CSV.exists():
        MANUAL_CSV.write_text(",".join(MANUAL_HEADER) + "\n", encoding="utf-8")
    rows = []
    with MANUAL_CSV.open(newline="", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        header_ok = rd.fieldnames == MANUAL_HEADER
        for row in rd:
            if any((v or "").strip() for v in row.values()):
                rows.append({k: (row.get(k) or "").strip() for k in MANUAL_HEADER})
    return {"file": "data/manual/set_piece_routines.csv", "header_ok": header_ok, "rows": len(rows),
            "by_situation": dict(Counter(x["situation"] for x in rows)), "by_outcome": dict(Counter(x["outcome"] for x in rows)), "entries": rows}


# ================================================================================================ tables
def write_tables(conn, goals, shots, pens, sb):
    conn.executescript("""
        DROP TABLE IF EXISTS sp_goals; DROP TABLE IF EXISTS sp_shots; DROP TABLE IF EXISTS sp_penalties;
        DROP TABLE IF EXISTS sp_sb_corners; DROP TABLE IF EXISTS sp_sb_shots;
        CREATE TABLE sp_goals (match_id INTEGER, date TEXT, opponent TEXT, coach TEXT, side TEXT, minute INTEGER, category TEXT, scorer TEXT, assister TEXT,
                               header INTEGER, xg REAL, has_shotmap INTEGER, unknown_reason TEXT);
        CREATE TABLE sp_shots (match_id INTEGER, date TEXT, opponent TEXT, side TEXT, minute INTEGER, player TEXT, category TEXT, raw_situation TEXT, outcome TEXT, xg REAL);
        CREATE TABLE sp_penalties (match_id INTEGER, date TEXT, opponent TEXT, side TEXT, minute INTEGER, taker TEXT, result TEXT);
        CREATE TABLE sp_sb_corners (statsbomb_match_id INTEGER, team TEXT, minute INTEGER, taker TEXT, technique TEXT, zone TEXT, recipient TEXT, first_contact TEXT, shots INTEGER, xg REAL, goals INTEGER);
        CREATE TABLE sp_sb_shots (statsbomb_match_id INTEGER, team TEXT, minute INTEGER, player TEXT, category TEXT, xg REAL, goal INTEGER, assister TEXT, set_piece_taker TEXT);
    """)
    conn.executemany("INSERT INTO sp_goals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [(g["match_id"], g["date"], g["opponent"], g["coach"], g["side"], g["minute"], g["category"],
                     g["scorer"], g["assister"], int(g["header"]), g["xg"], int(g["has_shotmap"]), g["unknown_reason"]) for g in goals])
    conn.executemany("INSERT INTO sp_shots VALUES (?,?,?,?,?,?,?,?,?,?)", [(s["match_id"], s["date"], s["opponent"], s["side"], s["minute"], s["player"], s["category"], s["raw_situation"], s["outcome"], s["xg"]) for s in shots])
    conn.executemany("INSERT INTO sp_penalties VALUES (?,?,?,?,?,?,?)", [(p["match_id"], p["date"], p["opponent"], p["side"], p["minute"], p["taker"], p["result"]) for p in pens])
    conn.executemany("INSERT INTO sp_sb_corners VALUES (?,?,?,?,?,?,?,?,?,?,?)", [(c["match"], c["team"], c["minute"], c["taker"], c["technique"], c["zone"], c["recipient"], c["first_contact"], c["shots"], c["xg"], c["goals"]) for c in sb["_rows"]["corners"]])
    conn.executemany("INSERT INTO sp_sb_shots VALUES (?,?,?,?,?,?,?,?,?)", [(s["match"], s["team"], s["minute"], s["player"], s["cat"], s["xg"], int(s["goal"]), s["assister"], s["taker"]) for s in sb["_rows"]["shots"]])
    conn.commit()


# ================================================================================================ run
def run(conn):
    conn.execute("PRAGMA busy_timeout=120000")
    matches, goals, shots, pens, shoot, notes = load_fotmob(conn)
    events, names = sb_load()
    sb = sb_section(events, names, conn)
    goals_s = goals_section(matches, goals)
    shots_s = shots_section(matches, shots)
    pens_s = penalties_section(matches, pens, shoot, shots, goals)
    take_s = takers_section(goals, shots)
    manual = manual_section()
    # cross-source check on the 4 AFCON 2023 matches (FotMob shot map vs StatsBomb)
    cross = cross_check(goals, shots, sb)
    write_tables(conn, goals, shots, pens, sb)
    sb.pop("_rows")
    unknown_reasons = Counter(g["unknown_reason"] for g in goals if g["category"] == "unknown")
    data = {
        "meta": {"module": "set_pieces", "as_of": AS_OF, "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "egypt_matches_fotmob_since_2018": len(matches), "matches_with_shotmap": sum(1 for m in matches if m["has_shotmap"]),
                 "hassan_matches": sum(1 for m in matches if m["coach"] == HASSAN),
                 "attribution": SB_ATTRIB, "load_notes": notes},
        "goals_by_situation": goals_s, "set_piece_shots_xg": shots_s, "penalties": pens_s, "takers_targets_fotmob": take_s,
        "statsbomb_afcon2023": sb, "cross_check_fotmob_vs_statsbomb": cross, "manual_routines": manual,
        "unknown_goal_reasons": dict(unknown_reasons),
    }
    data["key_findings"] = key_findings(data, goals, shots)
    data["methodology"] = methodology(data)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    OUT_MD.write_text(summary_md(data), encoding="utf-8")
    json.loads(OUT_JSON.read_text(encoding="utf-8"))  # validity check
    return data


def cross_check(goals, shots, sb):
    """FotMob (shot map) vs StatsBomb on Egypt's 4 AFCON 2023 matches: goal situations and set-piece shot counts."""
    ids = {g["match_id"] for g in goals if "2024-01-01" <= g["date"] <= "2024-01-31" and g["has_shotmap"]}
    fm = [g for g in goals if g["match_id"] in ids]
    fmc = {s: Counter(g["category"] for g in fm if g["side"] == s) for s in ("for", "against")}
    fms = [s for s in shots if "2024-01-01" <= s["date"] <= "2024-01-31" and s["match_id"] in ids]
    fm_sp = {s: dict(shots=sum(1 for x in fms if x["side"] == s and x["category"] in SP_CATS),
                     xg=r(sum(x["xg"] for x in fms if x["side"] == s and x["category"] in SP_CATS)),
                     total_xg_excl_pens=r(sum(x["xg"] for x in fms if x["side"] == s and x["category"] != "penalty"))) for s in ("for", "against")}
    rows = sb["_rows"]["shots"]
    egy = {m["statsbomb_match_id"] for m in sb["matches"]}
    sbg = {"for": Counter(), "against": Counter()}
    for s in rows:
        if s["match"] in egy and s["goal"]:
            sbg["for" if s["team"] == "Egypt" else "against"][s["cat"]] += 1
    return {"note": "Same 4 matches, two sources. Goal-situation labels differ by design (StatsBomb labels the whole restart possession).",
            "fotmob_goals_by_category": {k: dict(v) for k, v in fmc.items()}, "statsbomb_goals_by_category": {k: dict(v) for k, v in sbg.items()},
            "fotmob_set_piece_shots": fm_sp}


def _blk(d, side, key="all_set_pieces_excl_pens"):
    return d[side]["by_situation"][key]


def key_findings(d, goals, shots):
    g = d["goals_by_situation"]; s = d["set_piece_shots_xg"]; p = d["penalties"]; sb = d["statsbomb_afcon2023"]
    out = []
    # 1 goals by situation, shotmap subset
    ov = g["overall"]
    f, a = ov["for"]["shotmap_matches_subset"], ov["against"]["shotmap_matches_subset"]
    spf, spa = f["set_piece_share_excl_pens"], a["set_piece_share_excl_pens"]
    out.append({"n": 1, "topic": "Goals by situation",
                "text": (f"In the {ov['matches_with_shotmap']} of {ov['matches']} Egypt matches (since 2018) with a shot map, {spf['k']} of {spf['n']} non-penalty goals scored ({pct(spf['p'])}) and "
                         f"{spa['k']} of {spa['n']} conceded ({pct(spa['p'])}) came from set pieces (corner, free kick, throw-in, other). "
                         f"Situation is known for {ov['for']['situation_known']}/{ov['for']['of_non_own_goals']} goals scored and {ov['against']['situation_known']}/{ov['against']['of_non_own_goals']} conceded overall, "
                         f"so most goals since 2018 cannot be classified; 90% intervals are wide ({pct(spf['lo'])}-{pct(spf['hi'])} for, {pct(spa['lo'])}-{pct(spa['hi'])} against)."),
                "sample": f"{spf['n']} goals for / {spa['n']} against, {ov['matches_with_shotmap']} matches"})
    # 2 set piece xG share
    h = s["hassan_era"]; hf, ha = h["for"]["by_situation"]["all_set_pieces_excl_pens"], h["against"]["by_situation"]["all_set_pieces_excl_pens"]
    tb = sb["tournament_benchmark"]["all_teams_pooled"]
    out.append({"n": 2, "topic": "Set-piece xG share, Hassan era",
                "text": (f"Hassan era ({h['matches']} shot-map matches of {d['meta']['hassan_matches']}): set pieces (excl. penalties) produced {pct(hf['share_of_xg_excl_pens'])} of Egypt's non-penalty xG "
                         f"({hf['shots']} shots, {hf['xg']} xG, {hf['goals']} goals) and {pct(ha['share_of_xg_excl_pens'])} of the xG conceded ({ha['shots']} shots, {ha['xg']} xG, {ha['goals']} goals). "
                         f"AFCON 2023 tournament average (StatsBomb, 52 matches, different xG model): {pct(tb['set_piece_xg_share_excl_pens'])} on StatsBomb's possession-based definition, "
                         f"{pct(tb['narrow_corner_plus_direct_fk_xg_share_excl_pens'])} for corner + direct free kick possessions only (closer to FotMob's tags, still not like-for-like). "
                         f"World Cup 2026 benchmark: not computable (no other WC match pages cached)."),
                "sample": f"{h['matches']} matches, {hf['shots']} set-piece shots for / {ha['shots']} against"})
    # 3 penalties
    w, c = p["in_play_won_by_egypt"], p["in_play_conceded"]; so = p["shootouts"]
    out.append({"n": 3, "topic": "Penalties",
                "text": (f"Since 2018 Egypt won {w['awarded']} in-play penalties and scored {w['scored']} ({pct(w['conversion']['p'])}); it conceded {c['awarded']}, with opponents scoring {c['scored']}. "
                         f"Shootouts: {so['record']['won']} won, {so['record']['lost']} lost ({so['matches_flagged_decided_by_pens']} shootouts; kick data for {so['matches_with_kick_data']}); Egypt kicks {so['egypt_kicks']['k']}/{so['egypt_kicks']['n']}, "
                         f"opponents {so['opponent_kicks']['k']}/{so['opponent_kicks']['n']}."),
                "sample": f"{w['awarded'] + c['awarded']} in-play penalties over {d['meta']['egypt_matches_fotmob_since_2018']} matches; {so['egypt_kicks']['n'] + so['opponent_kicks']['n']} shootout kicks"})
    # 4 AFCON 2023 corners
    ca, cd, cf = sb["corners_egypt_attacking"], sb["corners_egypt_defending"], sb["corners_field_benchmark"]
    fa, fd, ff = ca["first_contact_win_rate_attackers"], cd["first_contact_win_rate_attackers"], cf["first_contact_win_rate_attackers"]
    out.append({"n": 4, "topic": "AFCON 2023 corners (pre-Hassan)",
                "text": (f"{SB_LABEL}: Egypt took {ca['corners']} corners ({ca['delivered']} delivered, {ca['short']} short) for {ca['shots_after_corner']} shots, {ca['goals']} goal(s), {ca['xg']} xG; "
                         f"attackers won first contact on {fa['k']}/{fa['n']} delivered corners ({pct(fa['p'])}) vs {pct(ff['p'])} for the rest of the field ({ff['n']} delivered). "
                         f"Defending {cd['corners']} opponent corners Egypt conceded {cd['shots_after_corner']} shots, {cd['goals']} goal(s), {cd['xg']} xG, and the opponent won first contact on {fd['k']}/{fd['n']} ({pct(fd['p'])})."),
                "sample": f"{ca['corners']} Egypt corners, {cd['corners']} defended, 4 matches"})
    # 5 takers / targets
    t = sb["corner_takers"][:2]; fk = sb["free_kick_takers"][:2]
    tk = d["takers_targets_fotmob"]
    top_sc = tk["egypt_set_piece_scorers"][:3]; top_as = tk["egypt_set_piece_goal_assisters"][:3]
    out.append({"n": 5, "topic": "Takers and targets",
                "text": ("AFCON 2023 corner takers: " + ", ".join(f"{x['player']} ({x['corners']})" for x in t) + "; free-kick passes: " + ", ".join(f"{x['player']} ({x['free_kick_passes']})" for x in fk)
                         + f". Egypt set-piece goals identified in shot-map matches ({tk['coverage']['egypt_set_piece_goals_identified']}): scorers "
                         + (", ".join(f"{x['player']} ({x['goals']})" for x in top_sc) or "none") + "; credited assisters " + (", ".join(f"{x['player']} ({x['goals']})" for x in top_as) or "none") + "."),
                "sample": f"{ca['corners']} corners (StatsBomb) and {tk['coverage']['egypt_set_piece_goals_identified']} FotMob set-piece goals"})
    return out


def methodology(d):
    return {
        "sources": URLS,
        "attribution": SB_ATTRIB + ". Raw StatsBomb events are not republished; this export contains derived aggregates and a handful of individual set-piece goal/shot rows.",
        "coverage_caveats": [
            "Goals: situation is known only where FotMob labels a penalty or the match page has a shot map (16 of 109 Egypt matches since 2018-01-01: 4 in 2024 [AFCON 2023], 3 in 2025, 9 in 2026). No shot maps exist before 2024; qualifiers and friendlies are almost entirely uncovered.",
            "Goals without a known situation are reported as 'unknown', never assumed to be open play. Penalty counts are complete; other categories are lower bounds outside shot-map matches, so non-penalty shares are quoted on the shot-map subset.",
            "Coverage shown as '2024: 8/19' double counts the four AFCON 2023 matches (one FotMob row and one StatsBomb row each). This module uses the FotMob rows for the main tables and StatsBomb only for the AFCON 2023 deep dive and cross-check; StatsBomb match rows are never added to FotMob counts.",
            "Hassan-era shot-map coverage is 12 of 36 Hassan matches (AFCON 2025 group stage onward plus the World Cup); no qualifiers or friendlies. Per-match rates and conversion rates from 12 matches carry wide intervals.",
            "FotMob 'FreeKick' vs 'SetPiece': FotMob does not tell direct from indirect free kicks; 'SetPiece' is reported as other_set_piece (unspecified dead-ball, includes indirect free kicks). Direct/indirect split exists only in the StatsBomb section.",
            "FotMob and StatsBomb xG come from different models; set-piece definitions differ (StatsBomb = whole possession from the restart, FotMob = shot-level tag). Cross-source comparisons are indicative only. The 4 AFCON 2023 matches exist in both, see cross_check_fotmob_vs_statsbomb.",
            "Tournament benchmark: AFCON 2023 (StatsBomb, all 52 matches) is computed. World Cup 2026 is not computable: only Egypt's own WC matches are in the FotMob cache and bulk scraping was ruled out. Egyptian Premier League pages in the cache were not used (different level of play).",
            "Penalties: 'missed_or_saved' merges misses, saves and hitting the woodwork. Older FotMob event feeds could be less complete than recent ones; there is no independent verification.",
            "Takers: FotMob has no set-piece taker field. Outside AFCON 2023 the FotMob assist credit on set-piece goals stands in for the taker and is only present for goals in shot-map matches.",
            "First-contact and end-zone classification of StatsBomb corners is a heuristic on the event stream (see note in the StatsBomb section) and is unvalidated against video.",
            "Coach assignment comes from matches.egypt_coach_id in the Phase 1 DB (some early matches are inferred or NULL, shown as 'Unassigned'); Hassan era = the 36 matches tagged Hossam Hassan, excluding the 3 Toulan Arab Cup matches.",
            "Own goals are credited to the benefiting side and have no situation.",
            "Manual routine tags (data/manual/set_piece_routines.csv) are merged verbatim if present and are not blended into the automated counts.",
        ],
    }


# ================================================================================================ summary
def _row(label, grp, side):
    x = grp[side]
    b = x["by_situation"]
    return (f"| {label} | {grp['matches']} ({grp['matches_with_shotmap']}) | {x['goals']} | {b['open_play']} | {b['corner']} | {b['free_kick']} | {b['throw_in']} | {b['other_set_piece']} | "
            f"{b['penalty']} | {b['own_goal']} | {b['unknown']} | {x['situation_known']}/{x['of_non_own_goals']} ({pct(x['coverage'])}) |")


def summary_md(d):
    g = d["goals_by_situation"]; s = d["set_piece_shots_xg"]; p = d["penalties"]; sb = d["statsbomb_afcon2023"]; tk = d["takers_targets_fotmob"]
    L = []
    L.append("# Set-piece analyzer (module E) - summary\n")
    L.append(f"As of {AS_OF}. Full data: `data/export/set_pieces.json`. Built from FotMob public match pages (cached), and StatsBomb open data for AFCON 2023. {SB_ATTRIB}.\n")
    L.append("## Key findings\n")
    for k in d["key_findings"]:
        L.append(f"{k['n']}. **{k['topic']}** - {k['text']} *(sample: {k['sample']})*")
    L.append("\n## Coverage at a glance\n")
    m = d["meta"]
    L.append(f"- {m['egypt_matches_fotmob_since_2018']} Egypt matches since 2018-01-01 (FotMob), {m['matches_with_shotmap']} with a shot map, {m['hassan_matches']} under Hassan ({s['coverage']['hassan_with_shotmap']} with a shot map).")
    L.append(f"- Shot maps by year (with/total): " + ", ".join(f"{y}: {v[0]}/{v[1]}" for y, v in s['coverage']['by_year'].items()) + ". The '2024: 8/19' figure double counts AFCON 2023 (FotMob + StatsBomb rows).")
    ov = g["overall"]
    L.append(f"- Goals with a known situation: scored {ov['for']['situation_known']}/{ov['for']['of_non_own_goals']}, conceded {ov['against']['situation_known']}/{ov['against']['of_non_own_goals']} (non-own goals; penalties are known everywhere, everything else only in shot-map matches).")
    L.append("\n## 1. Goals by situation (for / against)\n")
    L.append("Counts per situation, next to matches (with shot map) and coverage = goals with known situation / non-own goals. 'unknown' = no shot map and not a penalty; not assumed open play.\n")
    hdr = "| Group | Matches (shot map) | Goals | Open | Corner | Free kick | Throw-in | Other SP | Pen | Own goal | Unknown | Known/non-own |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
    for side, title in (("for", "Goals scored"), ("against", "Goals conceded")):
        L.append(f"**{title}**\n")
        L.append(hdr)
        L.append(_row("All since 2018", g["overall"], side))
        L.append(_row("Hassan era", g["hassan_era"], side))
        for c, grp in g["by_coach"].items():
            L.append(_row(c, grp, side))
        for y, grp in g["by_year"].items():
            L.append(_row(y, grp, side))
        L.append("")
    sf, sa = ov["for"]["shotmap_matches_subset"], ov["against"]["shotmap_matches_subset"]
    L.append(f"Shot-map subset ({ov['matches_with_shotmap']} matches): scored {sf['goals']} goals, set-piece share of non-penalty known goals {sf['set_piece_share_excl_pens']['k']}/{sf['set_piece_share_excl_pens']['n']}; "
             f"conceded {sa['goals']}, {sa['set_piece_share_excl_pens']['k']}/{sa['set_piece_share_excl_pens']['n']}.\n")
    L.append("## 2. Set-piece shots and xG (shot-map matches only)\n")
    for key in ("all_shotmap_matches", "hassan_era", "pre_hassan_afcon2023_fotmob"):
        grp = s[key]
        L.append(f"**{grp['label']}** - {grp['matches']} matches\n")
        L.append("| Side | Situation | Shots | Shots/match | Goals | Conversion (90% CI) | xG | xG/shot | Share of non-pen xG |\n|---|---|---|---|---|---|---|---|---|")
        for side in ("for", "against"):
            for cat in ("corner", "free_kick", "throw_in", "other_set_piece", "all_set_pieces_excl_pens", "penalty", "open_play"):
                b = grp[side]["by_situation"][cat]
                cv = b["conversion"]
                L.append(f"| {side} | {cat} | {b['shots']} | {b['per_match']} | {b['goals']} | {pct(cv['p'])} ({pct(cv['lo'])}-{pct(cv['hi'])}) | {b['xg']} | {b['xg_per_shot']} | {pct(b.get('share_of_xg_excl_pens'))} |")
        L.append("")
    tb = sb["tournament_benchmark"]
    L.append("**Benchmark (AFCON 2023, StatsBomb xG)** - set-piece share of non-penalty xG "
             f"(corner + direct FK + indirect FK possessions + throw-in possessions): tournament pooled {pct(tb['all_teams_pooled']['set_piece_xg_share_excl_pens'])} "
             f"({tb['all_teams_pooled']['shots']} shots), rest of field {pct(tb['excluding_egypt_pooled']['set_piece_xg_share_excl_pens'])}, "
             f"Egypt for {pct(sb['egypt_set_piece_shooting']['for']['set_piece_xg_share_excl_pens'])} ({sb['egypt_set_piece_shooting']['for']['shots']} shots), "
             f"Egypt against {pct(sb['egypt_set_piece_shooting']['against']['set_piece_xg_share_excl_pens'])} ({sb['egypt_set_piece_shooting']['against']['shots']} shots). "
             "World Cup 2026 benchmark: not computable (no non-Egypt WC pages cached). Do not compare FotMob and StatsBomb shares directly.\n")
    L.append("## 3. Penalties\n")
    w, c, so = p["in_play_won_by_egypt"], p["in_play_conceded"], p["shootouts"]
    L.append(f"- Won: {w['awarded']} (scored {w['scored']}, missed/saved {w['missed_or_saved']}); conceded: {c['awarded']} (scored {c['scored']}, missed/saved {c['missed_or_saved']}). Coverage: all {m['egypt_matches_fotmob_since_2018']} matches' event feeds.")
    L.append("- Takers (in play): " + (", ".join(f"{x['player']} {x['scored']}/{x['taken']}" for x in p["egypt_takers_in_play"]) or "none"))
    L.append(f"- Shootouts: {so['record']['won']}W-{so['record']['lost']}L ({so['matches_with_kick_data']} with kick data, {so['matches_flagged_decided_by_pens']} flagged decided_by='pens'); Egypt {so['egypt_kicks']['k']}/{so['egypt_kicks']['n']}, opponents {so['opponent_kicks']['k']}/{so['opponent_kicks']['n']}.")
    L.append("- Shootout takers: " + (", ".join(f"{x['player']} {x['scored']}/{x['taken']}" for x in so["egypt_takers"]) or "none"))
    for x in so["matches"]:
        L.append(f"  - {x['date']} v {x['opponent']} ({x['competition']}): {x['result']} {x['shootout']}, Egypt {x['egypt_kicks']}, opp {x['opponent_kicks']}")
    L.append("\n## 4. Takers and targets\n")
    L.append("- AFCON 2023 corner takers (StatsBomb): " + ", ".join(f"{x['player']} {x['corners']}" for x in sb["corner_takers"]))
    L.append("- AFCON 2023 first recipients of Egypt corners: " + ", ".join(f"{x['player']} {x['times']}" for x in sb["corner_targets_first_recipient"][:6]))
    L.append("- AFCON 2023 free-kick passes (into box): " + ", ".join(f"{x['player']} {x['free_kick_passes']} ({x['into_box']})" for x in sb["free_kick_takers"]))
    L.append("- AFCON 2023 set-piece shooters (Egypt): " + ", ".join(f"{x['player']} {x['shots']}" for x in sb["set_piece_shooters_egypt"]))
    L.append(f"- FotMob shot-map set-piece goals scored ({tk['coverage']['egypt_set_piece_goals_identified']}): scorers " + (", ".join(f"{x['player']} {x['goals']}" for x in tk["egypt_set_piece_scorers"]) or "none")
             + "; assisters " + (", ".join(f"{x['player']} {x['goals']}" for x in tk["egypt_set_piece_goal_assisters"]) or "none") + f"; {tk['egypt_set_piece_goals_missing_assist']} with no assist credit.")
    L.append("- Top set-piece shooters (shot maps): " + ", ".join(f"{x['player']} {x['shots']} shots/{x['goals']} goals/{x['xg']} xG" for x in tk["top_set_piece_shooters_shotmap"][:6]))
    L.append(f"\n## 5. {SB_LABEL}\n")
    L.append(f"Egypt's 4 matches. {SB_ATTRIB}.\n")
    for key, title in (("corners_egypt_attacking", "Egypt attacking corners"), ("corners_egypt_defending", "Opponent corners defended by Egypt"), ("corners_field_benchmark", "Field benchmark (all other corners in the tournament)")):
        x = sb[key]; fc = x["first_contact_win_rate_attackers"]
        L.append(f"- **{title}**: {x['corners']} corners ({x['short']} short, {x['delivered']} delivered); techniques {x['techniques']}; end zones {x['end_zones']}; "
                 f"attackers' first-contact rate {fc['k']}/{fc['n']} ({pct(fc['p'])}, CI {pct(fc['lo'])}-{pct(fc['hi'])}); first contact detail {x['first_contact']}; "
                 f"{x['corners_with_a_shot']} corners led to a shot ({x['shots_after_corner']} shots, {x['goals']} goals, {x['xg']} xG, {x['xg_per_corner']} xG/corner).")
    egf, ega = sb["egypt_set_piece_shooting"]["for"], sb["egypt_set_piece_shooting"]["against"]
    L.append(f"- Set-piece shooting (StatsBomb possessions): for {egf['by_category']}; against {ega['by_category']}.")
    L.append("- Set-piece goals for/penalties: " + "; ".join(f"{x['date']} v {x['opponent']} {x['minute']}' {x['category']} {x['scorer']} (assist {x['assister']}, taker {x['set_piece_taker']})" for x in sb["egypt_set_piece_goals_and_penalties"]))
    L.append("- Goals conceded from set-piece possessions: " + "; ".join(f"{x['date']} v {x['opponent']} {x['minute']}' {x['category']} {x['scorer']}" for x in sb["goals_conceded_from_set_pieces"]))
    L.append(f"- Cross-check FotMob vs StatsBomb (same 4 matches): {json.dumps(d['cross_check_fotmob_vs_statsbomb'], default=str)}")
    L.append("\n## 6. Manual routine tagging\n")
    mr = d["manual_routines"]
    L.append(f"`data/manual/set_piece_routines.csv`: {mr['rows']} rows merged. See README section 'Set-piece routine tagging'.\n")
    L.append("## 7. Methodology and caveats\n")
    L.append("Sources:")
    for k, v in d["methodology"]["sources"].items():
        L.append(f"- {k}: {v}")
    L.append("\nCaveats:")
    for c in d["methodology"]["coverage_caveats"]:
        L.append(f"- {c}")
    L.append(f"\n{d['methodology']['attribution']}\n")
    return "\n".join(L)


def main():
    conn = dbm.connect()
    conn.execute("PRAGMA busy_timeout=120000")
    data = run(conn)
    print(f"wrote {OUT_JSON} and {OUT_MD}")
    for k in data["key_findings"]:
        print(f"{k['n']}. {k['topic']}: {k['text']}")
    if data["meta"]["load_notes"]:
        print("load notes:", *data["meta"]["load_notes"], sep="\n  ")


if __name__ == "__main__":
    main()
