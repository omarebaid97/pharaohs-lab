"""Wikidata SPARQL: QIDs, Arabic labels, DOB, Transfermarkt IDs, citizenships for Egypt-sport footballers."""
from __future__ import annotations

import json

from .. import http

ENDPOINT = "https://query.wikidata.org/sparql"
QUERY = """
SELECT ?p ?en ?ar ?dob ?tm (GROUP_CONCAT(DISTINCT ?ctzL; separator=", ") AS ?ctz) WHERE {
  ?p wdt:P106 wd:Q937857 ; wdt:P1532 wd:Q79 .
  OPTIONAL { ?p rdfs:label ?en FILTER(lang(?en)="en") }
  OPTIONAL { ?p rdfs:label ?ar FILTER(lang(?ar)="ar") }
  OPTIONAL { ?p wdt:P569 ?dob }
  OPTIONAL { ?p wdt:P2446 ?tm }
  OPTIONAL { ?p wdt:P27 ?c . ?c rdfs:label ?ctzL FILTER(lang(?ctzL)="en") }
} GROUP BY ?p ?en ?ar ?dob ?tm
"""


def fetch():
    url = ENDPOINT
    txt = http.get(url, params={"query": QUERY, "format": "json"}, ttl=7 * 24 * 3600)
    rows = json.loads(txt)["results"]["bindings"]
    out = []
    for r in rows:
        g = lambda k: r.get(k, {}).get("value")
        out.append(dict(qid=g("p").rsplit("/", 1)[-1], en=g("en"), ar=g("ar"), dob=(g("dob") or "")[:10] or None,
                        tm=g("tm"), citizenships=g("ctz")))
    return out
