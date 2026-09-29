"""Coordinates from Wikidata (P625) via the keyless MediaWiki API; used to build the static lookup CSVs
data/manual/club_cities.csv and data/manual/venues.csv. No geocoding APIs that need keys.

Build (needs specs in this file):  .venv/bin/python -m etl.sources.geo_wikidata
"""
from __future__ import annotations

import json
import math

from .. import http

API = "https://www.wikidata.org/w/api.php"


def _hav(a, b):
    R = 6371.0088
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def coords(name: str, approx: tuple[float, float], max_km: float = 150.0):
    """Return (qid, label, lat, lon) of the first Wikidata search hit for `name` with a P625 coordinate within
    `max_km` of the analyst's approximate position (guards against same-named places). None if no hit."""
    s = json.loads(http.get(API, params={"action": "wbsearchentities", "search": name, "language": "en",
                                         "format": "json", "limit": 10}, ttl=90 * 86400))
    ids = [x["id"] for x in s.get("search", [])]
    if not ids:
        return None
    e = json.loads(http.get(API, params={"action": "wbgetentities", "ids": "|".join(ids), "props": "claims|labels",
                                         "languages": "en", "format": "json"}, ttl=90 * 86400))["entities"]
    for i in ids:
        try:
            v = e[i]["claims"]["P625"][0]["mainsnak"]["datavalue"]["value"]
        except (KeyError, IndexError):
            continue
        if _hav(approx, (v["latitude"], v["longitude"])) <= max_km:
            return i, e[i]["labels"].get("en", {}).get("value", name), round(v["latitude"], 4), round(v["longitude"], 4)
    return None


def haversine_km(lat1, lon1, lat2, lon2) -> float:
    return _hav((lat1, lon1), (lat2, lon2))


# ---------------------------------------------------------------- one-off builder for the static CSVs
# (club fotmob id, club name as FotMob shows it, city, country, IANA tz, Wikidata search term, approx (lat, lon))
CLUBS = [
    (101745, "Al Ahly SC", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (80591, "Zamalek SC", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (517894, "Pyramids FC", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (608449, "Ceramica Cleopatra", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (687954, "National Bank", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (585662, "ZED FC", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (797212, "Modern Sport FC", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (101754, "Al Sharkeyah-ENPPI", "Cairo", "Egypt", "Africa/Cairo", "Cairo", (30.04, 31.24)),
    (101762, "Al Masry SC", "Port Said", "Egypt", "Africa/Cairo", "Port Said", (31.26, 32.30)),
    (178268, "El Gouna FC", "El Gouna", "Egypt", "Africa/Cairo", "El Gouna", (27.39, 33.68)),
    (101752, "Ghazl Al Mahalla", "El Mahalla El Kubra", "Egypt", "Africa/Cairo", "El Mahalla El Kubra", (30.97, 31.17)),
    (103017, "Petrojet-Suez", "Suez", "Egypt", "Africa/Cairo", "Suez", (29.97, 32.55)),
    (1523707, "Al Najma", "Unaizah", "Saudi Arabia", "Asia/Riyadh", "Unaizah", (26.08, 43.99)),
    (102117, "Al-Ain", "Al Ain", "United Arab Emirates", "Asia/Dubai", "Al Ain", (24.21, 55.76)),
    (102101, "Al-Jazira", "Abu Dhabi", "United Arab Emirates", "Asia/Dubai", "Abu Dhabi", (24.47, 54.37)),
    (101904, "Al-Shamal", "Madinat ash Shamal", "Qatar", "Asia/Qatar", "Madinat ash Shamal", (26.12, 51.20)),
    (101901, "Al-Wakrah", "Al Wakrah", "Qatar", "Asia/Qatar", "Al Wakrah", (25.17, 51.60)),
    (8634, "Barcelona", "Barcelona", "Spain", "Europe/Madrid", "Barcelona", (41.39, 2.17)),
    (8670, "Real Oviedo", "Oviedo", "Spain", "Europe/Madrid", "Oviedo", (43.36, -5.85)),
    (9925, "Celtic", "Glasgow", "Scotland", "Europe/London", "Glasgow", (55.86, -4.25)),
    (8650, "Liverpool", "Liverpool", "England", "Europe/London", "Liverpool", (53.41, -2.99)),
    (8456, "Manchester City", "Manchester", "England", "Europe/London", "Manchester", (53.48, -2.24)),
    (8586, "Tottenham Hotspur", "London", "England", "Europe/London", "London", (51.51, -0.13)),
    (9891, "Frosinone", "Frosinone", "Italy", "Europe/Rome", "Frosinone", (41.64, 13.34)),
    (210173, "Ludogorets Razgrad", "Razgrad", "Bulgaria", "Europe/Sofia", "Razgrad", (43.53, 26.52)),
    (9830, "Nantes", "Nantes", "France", "Europe/Paris", "Nantes", (47.22, -1.55)),
    (9831, "Nice", "Nice", "France", "Europe/Paris", "Nice", (43.70, 7.27)),
    (10202, "Nordsjaelland", "Farum", "Denmark", "Europe/Copenhagen", "Farum", (55.81, 12.36)),
    (9752, "Trabzonspor", "Trabzon", "Turkey", "Europe/Istanbul", "Trabzon", (41.00, 39.72)),
]
# (db venue_city, db venue_country, stadium/venue label, tz, Wikidata search term, approx (lat, lon), fallback search term)
VENUES = [
    ("al-Qāhirah (Cairo)", "Egypt", "Cairo International Stadium", "Africa/Cairo", "Cairo International Stadium", (30.07, 31.31), "Cairo"),
    ("Cairo", "Egypt", "Cairo International Stadium", "Africa/Cairo", "Cairo International Stadium", (30.07, 31.31), "Cairo"),
    ("Juba", "South Sudan", "Juba Stadium", "Africa/Juba", "Juba Stadium", (4.85, 31.6), "Juba"),
    ("Agadir", "Morocco", "Agadir (city centre)", "Africa/Casablanca", "Agadir", (30.42, -9.60), "Agadir"),
    ("Al Khor", "Qatar", "Al Bayt Stadium", "Asia/Qatar", "Al Bayt Stadium", (25.65, 51.49), "Al Khor"),
    ("Al-'Ayn (Al Ain)", "United Arab Emirates", "Al Ain (city centre)", "Asia/Dubai", "Al Ain", (24.21, 55.76), "Al Ain"),
    ("Arlington, Texas", "United States", "AT&T Stadium (Dallas Stadium)", "America/Chicago", "AT&T Stadium", (32.75, -97.09), "Arlington"),
    ("Atlanta, Georgia", "United States", "Mercedes-Benz Stadium (Atlanta Stadium)", "America/New_York", "Mercedes-Benz Stadium", (33.755, -84.40), "Atlanta"),
    ("Bissau", "Guinea-Bissau", "Bissau (city centre)", "Africa/Bissau", "Bissau", (11.86, -15.6), "Bissau"),
    ("Casablanca", "Morocco", "Casablanca (city centre)", "Africa/Casablanca", "Casablanca", (33.57, -7.59), "Casablanca"),
    ("Cleveland, Ohio", "United States", "Huntington Bank Field", "America/New_York", "Cleveland Browns Stadium", (41.506, -81.70), "Cleveland"),
    ("Cornella de Llobregat", "Spain", "RCDE Stadium", "Europe/Madrid", "RCDE Stadium", (41.348, 2.075), "Cornella de Llobregat"),
    ("Francistown", "Botswana", "Francistown (city centre)", "Africa/Gaborone", "Francistown", (-21.17, 27.51), "Francistown"),
    ("Jeddah", "Saudi Arabia", "King Abdullah Sports City", "Asia/Riyadh", "King Abdullah Sports City", (21.62, 39.15), "Jeddah"),
    ("Lusail", "Qatar", "Lusail Stadium", "Asia/Qatar", "Lusail Stadium", (25.42, 51.49), "Lusail"),
    ("Nouakchott", "Mauritania", "Nouakchott (city centre)", "Africa/Nouakchott", "Nouakchott", (18.09, -15.98), "Nouakchott"),
    ("Ouagadougou", "Burkina Faso", "Ouagadougou (city centre)", "Africa/Ouagadougou", "Ouagadougou", (12.37, -1.52), "Ouagadougou"),
    ("Praia", "Cape Verde", "Praia (city centre)", "Atlantic/Cape_Verde", "Praia", (14.93, -23.51), "Praia"),
    ("Seattle, Washington", "United States", "Lumen Field (Seattle Stadium)", "America/Los_Angeles", "Lumen Field", (47.595, -122.33), "Seattle"),
    ("Tanger", "Morocco", "Tangier (city centre)", "Africa/Casablanca", "Tangier", (35.76, -5.80), "Tangier"),
    ("Vancouver, British Columbia", "Canada", "BC Place", "America/Vancouver", "BC Place", (49.277, -123.11), "Vancouver"),
]


def build(out_dir):
    import csv
    from pathlib import Path
    out_dir = Path(out_dir)
    bad = []
    with (out_dir / "club_cities.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["club", "city", "country", "lat", "lon", "tz", "source_url", "fotmob_id"])
        for fid, club, city, country, tz, term, approx in CLUBS:
            r = coords(term, approx)
            if not r:
                bad.append(club)
                continue
            w.writerow([club, city, country, r[2], r[3], tz, f"https://www.wikidata.org/wiki/{r[0]}", fid])
    with (out_dir / "venues.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["venue_city", "venue_country", "venue", "lat", "lon", "tz", "source_url"])
        for city, country, venue, tz, term, approx, fb in VENUES:
            r = coords(term, approx) or coords(fb, approx)
            if not r:
                bad.append(venue)
                continue
            w.writerow([city, country, venue, r[2], r[3], tz, f"https://www.wikidata.org/wiki/{r[0]}"])
    return bad


if __name__ == "__main__":
    from ..db import ROOT
    print("unresolved:", build(ROOT / "data" / "manual"))
