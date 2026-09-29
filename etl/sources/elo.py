"""eloratings.net: Egypt match history with ratings. Pre-match rating = post-match rating - change."""
from __future__ import annotations

from .. import http

URL = "https://www.eloratings.net/Egypt.tsv"


def _i(x):
    return int(x.replace("−", "-").replace("+", ""))


def fetch(since="2018-01-01"):
    txt = http.get(URL, ttl=24 * 3600)
    out = {}
    for line in txt.splitlines():
        f = line.split("\t")
        if len(f) < 13:
            continue
        try:
            date = f"{int(f[0]):04d}-{int(f[1]):02d}-{int(f[2]):02d}"
            if date < since:
                continue
            t1, t2, s1, s2 = f[3], f[4], int(f[5]), int(f[6])
            chg, r1, r2 = _i(f[9]), int(f[10]), int(f[11])
        except ValueError:
            continue
        if t1 == "EG":
            out[date] = dict(egy_score=s1, opp_score=s2, egy_elo=r1 - chg, opp_elo=r2 + chg, opp_code=t2, home=True)
        elif t2 == "EG":
            out[date] = dict(egy_score=s2, opp_score=s1, egy_elo=r2 + chg, opp_elo=r1 - chg, opp_code=t1, home=False)
    return out, URL
