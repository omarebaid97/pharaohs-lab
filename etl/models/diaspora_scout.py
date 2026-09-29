"""Module C: diaspora eligibility scout.

Finds professional footballers (born 1996+) NOT capped by Egypt's senior team who have a publicly documented
Egyptian link AND are diaspora / abroad-based (born outside Egypt, or currently at a club outside Egypt).

Discovery paths (all documented, structured or quoted from public pages):
  1. Wikidata SPARQL: citizenship, birthplace, Egyptian national-team membership (parents -> private only)
  2. Wikipedia categories "<Nationality> people of Egyptian descent" / "Egyptian emigrants to ..." intersected
     with footballers (short description), plus the article sentence that supports the link
  3. Wikipedia "Current squad" tables of Egypt U-17 / U-20 / U-23 (youth call-ups)

Outputs
  data/export/diaspora_candidates.json  public: diaspora/abroad, documented only (asserted in code)
  data/export/domestic_uncapped.json    public, UNRANKED: documented Egyptians based in Egypt / no club known
  data/private/diaspora_review.json     private: inferred signals, minors, likely_ineligible, missing data
  data/export/diaspora_summary.md       public-safe methodology + top 15 + caveats

Ethics: heritage is NEVER inferred from names, surnames, religion or appearance. Only structured statements
or quoted sentences in public sources count. Coptic / Jewish categories are deliberately not crawled.
Nothing is written to the Phase 1 tables (players, eligibility_evidence); evidence lives in diaspora_evidence.

Run: .venv/bin/python -m etl.models.diaspora_scout
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, unquote

from .. import db, http
from ..sources import transfermarkt

ROOT = Path(__file__).resolve().parent.parent.parent
EXPORT = ROOT / "data" / "export"
PRIVATE = ROOT / "data" / "private"
TODAY = date(2026, 9, 28)
SPARQL = "https://query.wikidata.org/sparql"
WP_API = "https://en.wikipedia.org/w/api.php"
WD = "https://www.wikidata.org/wiki/"
MIN_BIRTH_YEAR = 1996
PUBLIC_MIN_AGE = 18          # minors go to the private list only
TOP_N = 30
LEAD_LIST = ("This is a lead list for scouts, built from public sources. It is NOT a statement that any player is "
             "eligible to play for Egypt or that he has not been capped elsewhere.")
FIFA_URLS = {
    "Commentary on the Rules Governing Eligibility to Play for Representative Teams (Jan 2021)":
        "https://digitalhub.fifa.com/m/ccab990abf45fcf6/original/ro8mje8vw98yp3rvfbmi-pdf.pdf",
    "Guide to Submitting a Request for Eligibility or Change of Association (Jan 2021)":
        "https://digitalhub.fifa.com/m/b98d35fc16dc274b/original/elcthdgwfgx7dcxdenas-pdf.pdf",
}
FIFA_NOTE = (
    "FIFA Regulations Governing the Application of the Statutes (RGAS), arts 5-9, as quoted in FIFA's Commentary "
    "(Jan 2021 edition; both PDFs were downloaded and read). "
    "Art. 5.1: a person holding a permanent nationality not dependent on residence is eligible for that association. "
    "Art. 5.3: with the exception of art. 9, a player who has participated (in full or part, even seconds as a substitute; "
    "being called up or unused on the bench does not count) in a match in an OFFICIAL competition of ANY age category or "
    "any type of football (association, futsal, beach) for one association may not play an international match for another. "
    "'Official competition' means a representative-team competition organised by FIFA or a confederation, so friendlies "
    "and matches of non-confederation regional tournaments do not tie a player (Commentary paras 18-22). "
    "Art. 6/7 (nationality plus genuine link): for a second nationality the player must also have been born on the "
    "territory, or have a biological parent or grandparent born there, or have lived there 5 years (art. 7.1(d): 3 years "
    "if living there before age 10). "
    "Art. 9 (change of association): only once, only to another association whose nationality the player holds, and only "
    "if (a) the earlier matches were official matches below 'A' level and the player already held the new nationality at "
    "the first such match; or (b) same, but the nationality was not yet held and the last such match was before age 21 and "
    "art. 6/7 is met; or (c) 'A' level: at most three A matches (official or not), first official match played while "
    "holding the new nationality, last official match before age 21, three years since the last A match, and no World Cup "
    "or confederation final tournament at A level; or (d) a newly admitted FIFA member. Requests go to the Players' Status Committee. "
    "How this module applies it: senior-team evidence for another nation (Wikidata P54, Wikipedia text, infobox with caps > 0, "
    "Transfermarkt) marks a player likely_ineligible, because those sources cannot tell friendlies from official matches, "
    "and art. 9(2)(c) is narrow. Youth-team appearances for another nation are shown as a caveat, not an exclusion: "
    "official youth matches do tie a player until a change of association is granted (art. 5.3), but art. 9(2)(a)/(b) can allow one switch."
)

# ------------------------------------------------------------------ schema
SCHEMA = """
CREATE TABLE IF NOT EXISTS diaspora_candidates (
  cand_key TEXT PRIMARY KEY,                    -- Wikidata QID, or wp:<team>:<name> for squad-only players
  qid TEXT, name TEXT, dob TEXT, status TEXT,   -- public | domestic | private_review | likely_ineligible | already_capped | not_professional
  reason TEXT, score REAL, payload TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS diaspora_evidence (
  cand_key TEXT NOT NULL, claim TEXT NOT NULL, evidence_text TEXT, source_url TEXT,
  confidence TEXT CHECK (confidence IN ('documented','inferred')),
  UNIQUE(cand_key, claim, source_url)
);
CREATE TABLE IF NOT EXISTS diaspora_position_need (
  pos_group TEXT PRIMARY KEY, minutes_12m REAL, n_regulars INTEGER, top2_avg_age REAL,
  age_score REAL, thin_score REAL, need REAL, detail TEXT, updated_at TEXT
);
"""


# ------------------------------------------------------------------ helpers
def _sparql(q: str) -> list[dict]:
    txt = http.get(SPARQL, params={"query": q, "format": "json"}, ttl=3 * 24 * 3600)
    return json.loads(txt)["results"]["bindings"]


def _v(r: dict, k: str):
    return r.get(k, {}).get("value")


def _qid(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


def _key(name: str | None) -> str:
    return " ".join(sorted(db.norm(name).split()))


def _is_capped_name(idx: dict, name: str | None, dob: str | None) -> bool:
    """Conservative: same name tokens and birth year within +-1 (Wikidata often has placeholder Jan-1/Dec-31
    dates) or unknown DB dob. Better to hide a namesake than to list an Egypt international as uncapped."""
    if not name or not dob:
        return False
    by = int(dob[:4])
    return any(y is None or abs(y - by) <= 1 for y in idx.get(_key(name), []))


def _age(dob: str | None) -> float | None:
    if not dob:
        return None
    try:
        d = date.fromisoformat(dob[:10])
    except ValueError:
        return None
    return round((TODAY - d).days / 365.25, 1)


def pos_group(s: str | None) -> str | None:
    if not s:
        return None
    s = s.lower().strip()
    if s in ("gk", "df", "mf", "fw"):
        return {"gk": "GK", "df": "DEF", "mf": "MID", "fw": "ATT"}[s]
    if "keeper" in s or "goalkeeper" in s:
        return "GK"
    if "back" in s or "defen" in s or "sweeper" in s:
        return "DEF"
    if "winger" in s or "forward" in s or "striker" in s or s in ("attacker", "attack"):
        return "ATT"
    if "midfield" in s:
        return "MID"
    return None


def _slot_group(slot, pos) -> str | None:
    """FotMob positionId rows: 11 GK, 32-38 back line, 62-89 midfield, 103-117 attack."""
    try:
        n = int(slot)
    except (TypeError, ValueError):
        n = None
    if n is not None and n >= 11:
        if n == 11:
            return "GK"
        if 30 <= n <= 39:
            return "DEF"
        if 60 <= n <= 99:
            return "MID"
        if 100 <= n <= 119:
            return "ATT"
    return pos_group(pos)


_NOT_SENIOR = re.compile(r"under|\bu-?\d{2}\b|olympic|youth|\bb team\b|amateur|futsal|beach|universiade|"
                         r"military|student|reserve|\bxi\b|league|selection|all-stars", re.I)
_NAT_LABEL = re.compile(r"national (association )?football team$", re.I)


def is_senior_nt(label: str) -> bool:
    return bool(_NAT_LABEL.search(label)) and not _NOT_SENIOR.search(label)


# ------------------------------------------------------------------ 1. discovery
DISCOVERY_Q = """SELECT ?p ?sig WHERE {
 ?p wdt:P106 wd:Q937857 ; wdt:P569 ?d .
 FILTER(?d >= "%d-01-01T00:00:00Z"^^xsd:dateTime && ?d < "%d-01-01T00:00:00Z"^^xsd:dateTime)
 { ?p wdt:P27 wd:Q79 . BIND("citizenship" AS ?sig) }
 UNION { ?p wdt:P19 ?b . ?b wdt:P17 wd:Q79 . BIND("birth" AS ?sig) }
 UNION { ?p wdt:P54 ?t . ?t wdt:P1532 wd:Q79 . BIND("nt" AS ?sig) }
 UNION { ?p (wdt:P22|wdt:P25) ?par . ?par wdt:P27 wd:Q79 . BIND("parent_citizenship" AS ?sig) }
 UNION { ?p (wdt:P22|wdt:P25) ?par . ?par wdt:P19 ?pb . ?pb wdt:P17 wd:Q79 . BIND("parent_birth" AS ?sig) }
}"""
BUCKETS = [(1996, 1999), (1999, 2002), (2002, 2005), (2005, 2008), (2008, 2015)]


def discover() -> dict[str, set[str]]:
    sigs: dict[str, set[str]] = defaultdict(set)
    for a, b in BUCKETS:   # paginate by birth-year bucket; http client spaces requests >= 2 s
        for r in _sparql(DISCOVERY_Q % (a, b)):
            sigs[_qid(_v(r, "p"))].add(_v(r, "sig"))
    return sigs


# ------------------------------------------------------------------ 2. enrichment
BASIC_Q = """SELECT ?p (SAMPLE(?en) AS ?name) (SAMPLE(?dob) AS ?dob1) (SAMPLE(?pobL) AS ?pob)
 (SAMPLE(?pobcL) AS ?pobc) (GROUP_CONCAT(DISTINCT ?ctzL; separator="|") AS ?ctz)
 (GROUP_CONCAT(DISTINCT ?posL; separator="|") AS ?pos) (SAMPLE(?tm) AS ?tm1) (SAMPLE(?wp) AS ?wp1) (SAMPLE(?g) AS ?gender) WHERE {
 VALUES ?p { %s }
 OPTIONAL { ?p rdfs:label ?en FILTER(lang(?en)="en") }
 OPTIONAL { ?p wdt:P569 ?dob }
 OPTIONAL { ?p wdt:P19 ?b . ?b rdfs:label ?pobL FILTER(lang(?pobL)="en")
            OPTIONAL { ?b wdt:P17 ?bc . ?bc rdfs:label ?pobcL FILTER(lang(?pobcL)="en") } }
 OPTIONAL { ?p wdt:P27 ?c . ?c rdfs:label ?ctzL FILTER(lang(?ctzL)="en") }
 OPTIONAL { ?p wdt:P413 ?ps . ?ps rdfs:label ?posL FILTER(lang(?posL)="en") }
 OPTIONAL { ?p wdt:P2446 ?tm }
 OPTIONAL { ?p wdt:P21 ?g }
 OPTIONAL { ?wp schema:about ?p ; schema:isPartOf <https://en.wikipedia.org/> }
} GROUP BY ?p"""
TEAMS_Q = """SELECT ?p ?t ?tl ?start ?end ?nat ?lgL ?cL WHERE {
 VALUES ?p { %s }
 ?p p:P54 ?st . ?st ps:P54 ?t .
 OPTIONAL { ?st pq:P580 ?start } OPTIONAL { ?st pq:P582 ?end }
 ?t rdfs:label ?tl FILTER(lang(?tl)="en")
 OPTIONAL { ?t wdt:P1532 ?nat }
 OPTIONAL { ?t wdt:P118 ?lg . ?lg rdfs:label ?lgL FILTER(lang(?lgL)="en") }
 OPTIONAL { ?t wdt:P17 ?c . ?c rdfs:label ?cL FILTER(lang(?cL)="en") }
}"""
PARENT_Q = """SELECT ?p ?rel ?parL ?parctz ?parbirth WHERE {
 VALUES ?p { %s }
 { ?p wdt:P22 ?par . BIND("father" AS ?rel) } UNION { ?p wdt:P25 ?par . BIND("mother" AS ?rel) }
 OPTIONAL { ?par rdfs:label ?parL FILTER(lang(?parL)="en") }
 OPTIONAL { ?par wdt:P27 wd:Q79 . BIND("Egypt" AS ?parctz) }
 OPTIONAL { ?par wdt:P19 ?pb . ?pb wdt:P17 wd:Q79 . ?pb rdfs:label ?parbirth FILTER(lang(?parbirth)="en") }
}"""




def _batches(xs, n=40):
    xs = sorted(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def enrich(qids, parent_qids=()) -> dict[str, dict]:
    """Basic facts for all QIDs; club/national-team history and parents only for those born >= MIN_BIRTH_YEAR."""
    info: dict[str, dict] = {q: {"qid": q, "teams": [], "parents": []} for q in qids}
    for b in _batches(qids):
        for r in _sparql(BASIC_Q % " ".join(f"wd:{q}" for q in b)):
            q = _qid(_v(r, "p"))
            info[q].update(name=_v(r, "name"), dob=(_v(r, "dob1") or "")[:10] or None, pob=_v(r, "pob"),
                           pob_country=_v(r, "pobc"), citizenships=[x for x in (_v(r, "ctz") or "").split("|") if x],
                           positions=[x for x in (_v(r, "pos") or "").split("|") if x], tm_id=_v(r, "tm1"),
                           enwiki=_v(r, "wp1"), female=(_v(r, "gender") or "").endswith("Q6581072"))
    young = [q for q, i in info.items() if i.get("dob") and int(i["dob"][:4]) >= MIN_BIRTH_YEAR]
    for b in _batches(young):
        vals = " ".join(f"wd:{q}" for q in b)
        for r in _sparql(TEAMS_Q % vals):
            q = _qid(_v(r, "p"))
            info[q]["teams"].append(dict(
                qid=_qid(_v(r, "t")), label=_v(r, "tl"), start=(_v(r, "start") or "")[:10] or None,
                end=(_v(r, "end") or "")[:10] or None,
                nat=_qid(_v(r, "nat")) if _v(r, "nat") else None, league=_v(r, "lgL"), country=_v(r, "cL")))
        pb = [q for q in b if q in parent_qids]
        for r in (_sparql(PARENT_Q % " ".join(f"wd:{q}" for q in pb)) if pb else []):
            info[_qid(_v(r, "p"))]["parents"].append(dict(
                rel=_v(r, "rel"), label=_v(r, "parL"), egypt_citizen=bool(_v(r, "parctz")),
                egypt_birth=_v(r, "parbirth")))
    return {q: info[q] for q in young}


EGYPT_SENIOR_Q = """SELECT DISTINCT ?p ?en ?dob WHERE {
 ?p wdt:P54 ?t . ?t wdt:P1532 wd:Q79 ; rdfs:label ?tl FILTER(lang(?tl)="en" && REGEX(?tl, "^Egypt (men's )?national (association )?football team$"))
 OPTIONAL { ?p rdfs:label ?en FILTER(lang(?en)="en") } OPTIONAL { ?p wdt:P569 ?dob } }"""


def egypt_internationals() -> dict[str, dict]:
    """All-time Egypt senior players on Wikidata (qid -> name/dob), used for capped detection + name-collision notes."""
    return {_qid(_v(r, "p")): dict(name=_v(r, "en"), dob=(_v(r, "dob") or "")[:10] or None)
            for r in _sparql(EGYPT_SENIOR_Q)}


def summarize_career(i: dict) -> None:
    """Derive current club / league / national-team history from Wikidata P54."""
    clubs, nts = [], []
    for t in i["teams"]:
        # clubs can carry P1532 too, so require a national-team-looking label
        (nts if t["nat"] and re.search(r"national|olympic", t["label"], re.I) else clubs).append(t)
    i["senior_nts"] = sorted({t["label"] for t in nts if is_senior_nt(t["label"]) and t["nat"] != "Q79"
                              and not t["label"].lower().startswith("egypt")})
    i["egypt_senior"] = any(is_senior_nt(t["label"]) and (t["nat"] == "Q79" or t["label"].lower().startswith("egypt"))
                            for t in nts)
    i["egypt_youth_teams"] = sorted({t["label"] for t in nts
                                     if (t["nat"] == "Q79" or t["label"].lower().startswith("egypt"))
                                     and not is_senior_nt(t["label"])})
    i["nt_history"] = sorted({t["label"] for t in nts})
    i["n_clubs"] = len(clubs)
    open_clubs = sorted([t for t in clubs if not t["end"]], key=lambda t: t["start"] or "")
    cur = open_clubs[-1] if open_clubs else None
    i["wd_club"] = cur["label"] if cur else None
    i["wd_league"] = cur["league"] if cur else None
    i["wd_club_country"] = cur["country"] if cur else None


# ------------------------------------------------------------------ 2b. Wikipedia sources
def _wp(**p) -> dict:
    p["format"] = "json"
    return json.loads(http.get(WP_API, params=p, ttl=3 * 24 * 3600))


def wp_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + quote(title.replace(" ", "_"), safe="_(),'!-")


_CAT_OK = re.compile(r"Egyptian descent|Egyptian emigrants to|Egyptian diaspora", re.I)
_CAT_SKIP = re.compile(r"Coptic|Jewish|Jews|Muslim|Christian|religio", re.I)   # never crawl religion-based categories


def category_path() -> tuple[dict[str, dict], dict]:
    """Crawl 'X people of Egyptian descent' / 'Egyptian emigrants to X' (depth <= 4) and keep footballers
    (short description or category name says footballer). Returns qid -> {title, cats}, plus crawl stats."""
    queue = [("Category:People of Egyptian descent", 0), ("Category:Egyptian emigrants", 0)]
    seen: set[str] = set()
    hits: dict[str, dict] = {}
    n_pages = 0
    while queue:
        cat, depth = queue.pop(0)
        if cat in seen:
            continue
        seen.add(cat)
        pages: dict[int, dict] = {}
        cont: dict = {}
        while True:
            d = _wp(action="query", generator="categorymembers", gcmtitle=cat, gcmnamespace="0|14", gcmlimit=500,
                    prop="description|pageprops", ppprop="wikibase_item", **cont)
            for pid, p in d.get("query", {}).get("pages", {}).items():
                cur = pages.setdefault(int(pid), {})
                cur.update({k: v for k, v in p.items() if v not in (None, "")})
            if "continue" not in d:
                break
            cont = d["continue"]
        for p in pages.values():
            title = p["title"]
            if p.get("ns") == 14:
                if depth < 4 and _CAT_OK.search(title) and not _CAT_SKIP.search(title):
                    queue.append((title, depth + 1))
                continue
            n_pages += 1
            desc = (p.get("description") or "").lower()
            qid = p.get("pageprops", {}).get("wikibase_item")
            if qid and ("footballer" in desc or "soccer" in desc or "football" in desc and "player" in desc):
                h = hits.setdefault(qid, {"title": title, "cats": []})
                if cat not in h["cats"]:
                    h["cats"].append(cat)
    return hits, {"categories_crawled": len(seen), "member_pages": n_pages, "footballers": len(hits)}


YOUTH_PAGES = ["Egypt national under-23 football team", "Egypt national under-20 football team",
               "Egypt national under-17 football team"]


def _field(line: str, key: str) -> str | None:
    m = re.search(rf"\|\s*{key}\s*=\s*((?:\[\[.*?\]\]|[^|}}])*)", line)
    return m.group(1).strip() if m else None


def youth_squads() -> list[dict]:
    """Parse the 'Current squad' tables of the Egypt youth national team pages (Wikipedia)."""
    rows = []
    for page in YOUTH_PAGES:
        try:
            secs = _wp(action="parse", page=page, prop="sections", redirects=1)["parse"]["sections"]
            idx = [s["index"] for s in secs if s["line"].strip().lower() == "current squad"]
            if not idx:
                continue
            pr = _wp(action="parse", page=page, prop="wikitext", section=idx[0], redirects=1)["parse"]
        except (KeyError, http.NotFound, http.Blocked):
            continue
        wt = pr["wikitext"]["*"]
        ctx = next((re.sub(r"<ref.*?(?:</ref>|/>)|\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", ln).strip()
                    for ln in wt.splitlines() if ln.startswith("The following")), "")[:200]
        for line in wt.splitlines():
            if not re.match(r"\s*\{\{nat fs [^|]*player", line):
                continue
            nm = _field(line, "name")
            if not nm:
                continue
            m = re.match(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", nm)
            link, name = (m.group(1), m.group(2) or m.group(1)) if m else (None, nm)
            name = re.sub(r"\s*\(.*?\)$", "", name).strip()
            cl = _field(line, "club") or ""
            cm = re.match(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", cl)
            club = (cm.group(2) or cm.group(1)) if cm else (cl or None)
            b = re.search(r"birth date(?: and age)?\|(\d{4})\|(\d{1,2})\|(\d{1,2})", line)
            rows.append(dict(team=pr["title"], page_url=wp_url(pr["title"]), link=link, name=name,
                             pos=_field(line, "pos"), club=club, clubnat=(_field(line, "clubnat") or "").upper() or None,
                             dob=f"{b.group(1)}-{int(b.group(2)):02d}-{int(b.group(3)):02d}" if b else None,
                             context=ctx))
    # resolve linked players to Wikidata QIDs
    titles = sorted({r["link"] for r in rows if r["link"]})
    t2q: dict[str, str] = {}
    for k in range(0, len(titles), 40):
        d = _wp(action="query", titles="|".join(titles[k:k + 40]), prop="pageprops", ppprop="wikibase_item", redirects=1)
        norm = {n["from"]: n["to"] for n in d["query"].get("normalized", [])}
        norm.update({n["from"]: n["to"] for n in d["query"].get("redirects", [])})
        q_by = {p["title"]: p.get("pageprops", {}).get("wikibase_item") for p in d["query"]["pages"].values()}
        for t in titles[k:k + 40]:
            t1 = norm.get(t, t)
            q = q_by.get(norm.get(t1, t1))
            if q:
                t2q[t] = q
    for r in rows:
        r["qid"] = t2q.get(r["link"]) if r["link"] else None
    return rows


_NT_RE = re.compile(r"(?:the )?([A-Z][A-Za-z'\-]+(?: [A-Z][A-Za-z'\-]+)*) (?:men's )?(?:senior )?national (?:football |soccer )?team")
_CAP_VERBS = re.compile(r"plays? for|played for|represent|debut|\bcaps?\b|capped|international appearances", re.I)
_SKIP = re.compile(r"under-?\d|\bU-?\d\d\b|youth|olympic|\bB team|eligible|could|choose|declined|call-?ed? up|uncapped|"
                   r"switch|opt(?:ed)? to", re.I)
_EGYPT_SENIOR_RE = re.compile(
    r"represent(?:s|ed|ing)? Egypt\b|plays? for (?:the )?Egypt(?:ian)? (?:national|senior)|"
    r"(?:made|making) (?:his|her) (?:senior |international )?debut for Egypt|Egypt(?:ian)? international\b|"
    r"international (?:for|with) Egypt|capped (?:by|for) Egypt|\bcaps? for Egypt", re.I)
_YOUTH_Q = re.compile(r"under-?\d|\bU-?\d\d\b|youth|olympic|junior|academy", re.I)
_NOISE = re.compile(r"Egyptian Premier League|Egyptian Second Division|Egyptian Cup|Egypt Cup|Egyptian Super Cup|"
                    r"Egyptian League|Egyptian (?:club|side|team|Football Association)", re.I)
_LINK_WORDS = re.compile(r"descent|heritage|origin|ancest|father|mother|parents?|grandparents?|grandfather|grandmother|"
                         r"family|emigrat|immigrat|\bborn\b|moved|raised|grew up|refugee|youth|under-?\d|\bU-?\d\d\b|"
                         r"represent", re.I)


def heritage_sentences(text: str) -> list[str]:
    """Sentences (verbatim) that tie the subject to Egypt by descent / family / birth / youth. Parenthetical
    birth dates and Egyptian-club mentions are ignored so plain 'Egyptian footballer' intros don't count."""
    out = []
    text = re.sub(r"={2,}[^=\n]*={2,}", "\n", text)
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        s = sent.strip()
        if not s or len(s) > 300:
            continue
        core = _NOISE.sub(" ", re.sub(r"\([^)]*\)", " ", s))
        if not re.search(r"\bEgypt(?:ian)?\b", core) or not _LINK_WORDS.search(core):
            continue
        if re.search(r"national (?:football )?team", core, re.I) and not re.search(r"youth|under|U-?\d\d", core, re.I):
            continue    # a senior-cap statement, not a heritage statement
        if _EGYPT_SENIOR_RE.search(core) and not _YOUTH_Q.search(core):
            continue
        out.append(s)
    return out


COUNTRIES = {c for c in """Afghanistan Albania Algeria Argentina Australia Austria Azerbaijan Bahrain Belgium Bosnia Brazil Bulgaria
Cameroon Canada Chad Chile China Colombia Croatia Cyprus Czech Denmark England Estonia Ethiopia Finland France Georgia Germany Ghana
Greece Hungary Iceland India Indonesia Iran Iraq Ireland Israel Italy Japan Jordan Kenya Kuwait Latvia Lebanon Libya Lithuania
Luxembourg Malaysia Malta Mexico Morocco Netherlands Nigeria Norway Oman Pakistan Palestine Poland Portugal Qatar Romania Russia
Scotland Senegal Serbia Slovakia Slovenia Somalia Spain Sudan Sweden Switzerland Syria Tunisia Turkey Ukraine Wales Yemen Egypt
Canada Zambia Zimbabwe Denmark Norway""".split()} | {"United States", "United Kingdom", "United Arab Emirates", "Saudi Arabia",
"South Africa", "South Sudan", "New Zealand", "Northern Ireland", "Czech Republic", "North Macedonia", "Ivory Coast",
"Republic of Ireland", "DR Congo", "Saudi", "USA", "UAE"}
_BORN_RE = re.compile(r"\b[Bb]orn in ((?:[A-Z][\w\.\-' ]*)(?:, [A-Z][\w\.\-' ]*){0,3})")
_PLAYS_RE = re.compile(r"plays? as (?:an? |the )?([A-Za-z\- ]+?)(?: for\b| and\b| in\b| at\b| who\b|,|\.|$)")
_SEG_RE = re.compile(r"^(.*?)(?: on loan| in the| in [A-Z]|,| and | who | since |;| \(|\.\s|\.$|$)")
_LEAGUE_WORDS = re.compile(r"League|Liga|Serie|Division|Championship|Ligue|Bundesliga|Eredivisie|Superligaen|Allsvenskan|"
                           r"Premier|Primeira|Super Lig|Botola|Stars|Pro", re.I)


def lead_facts(text: str) -> dict:
    """Best-effort structured facts read from a Wikipedia lead ('plays as a winger for Czech First League club X')."""
    out = {}
    m = _BORN_RE.search(text or "")
    if m:
        for part in reversed([p.strip() for p in m.group(1).split(",")]):
            if part in COUNTRIES:
                out["birth_country"] = "United States" if part in ("USA",) else part
                break
    m = _PLAYS_RE.search(text or "")
    if m:
        out["position"] = m.group(1).strip()
        rest = (text or "")[m.end(1):]
        f = re.match(r"\s+for (?:the )?(.*)", rest, re.S)
        if f:
            seg = _SEG_RE.match(f.group(1).split("\n")[0]).group(1).strip()
            if re.search(r"\bclub\b", seg):
                lg, _, club = re.split(r"\s+club\s+", seg, maxsplit=1)[0], None, re.split(r"\s+club\s+", seg, maxsplit=1)[-1]
                out["league_phrase"] = lg.strip()
                seg = club.strip()
            if seg and seg[0].isupper() and len(seg) <= 50:
                out["club"] = seg
    return out


def _lead_texts(titles: list[str]) -> dict[str, str]:
    """title -> lead extract, 20 titles per request."""
    res = {}
    for k in range(0, len(titles), 20):
        chunk = titles[k:k + 20]
        try:
            d = _wp(action="query", prop="extracts", exintro=1, explaintext=1, exlimit="max",
                    titles="|".join(chunk), redirects=1)
        except (http.Blocked, http.NotFound, RuntimeError):
            continue
        norm = {n["from"]: n["to"] for n in d["query"].get("normalized", [])}
        norm.update({n["from"]: n["to"] for n in d["query"].get("redirects", [])})
        by = {p["title"]: p.get("extract", "") for p in d["query"]["pages"].values()}
        for t in chunk:
            t1 = norm.get(t, t)
            res[t] = by.get(norm.get(t1, t1), "")
    return res


def _full_text(title: str) -> str:
    try:
        d = _wp(action="query", prop="extracts", explaintext=1, titles=title, redirects=1)
    except (http.Blocked, http.NotFound, RuntimeError):
        return ""
    return next(iter(d["query"]["pages"].values())).get("extract", "")


_IB_KEY = re.compile(r"^\|\s*(nationalteam|nationalyears|nationalcaps|nationalgoals)(\d+)\s*=\s*(.*)$", re.M)
_YOUTH_TEAM = re.compile(r"\bU-?\d{2}\b|under-?\d|youth|olympic|junior|\bB\b|\bXI\b|amateur|futsal|beach", re.I)


def parse_infobox(wt: str) -> dict | None:
    """National-team section of an {{Infobox football biography}}: senior/youth entries with caps. None = no infobox."""
    if "infobox" not in wt.lower():
        return None
    rows: dict[str, dict] = defaultdict(dict)
    for m in _IB_KEY.finditer(wt):
        rows[m.group(2)][m.group(1)] = m.group(3).strip()
    senior, youth = [], []
    for n, r in sorted(rows.items(), key=lambda kv: int(kv[0])):
        team = r.get("nationalteam", "")
        lm = re.search(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", team)
        label = (lm.group(1) if lm else re.sub(r"<[^>]+>|\{\{.*?\}\}", "", team)).strip()
        if not label:
            continue
        cm = re.match(r"(\d+)", r.get("nationalcaps", ""))
        caps = int(cm.group(1)) if cm else None
        nation = re.sub(r"\s+(?:U-?\d{2}|under-?\d{2}|Olympic|B|XI|youth).*$", "", label, flags=re.I).strip()
        entry = dict(team=label, nation=nation, caps=caps, years=r.get("nationalyears", ""))
        (youth if _YOUTH_TEAM.search(label) else senior).append(entry)
    return dict(senior=senior, youth=youth)


def infobox_check(cands: dict) -> int:
    """Read the infobox national-team section (lead wikitext) for every pending candidate with an article."""
    todo = {unquote(i["enwiki"].rsplit("/", 1)[-1]).replace("_", " "): q
            for q, i in cands.items() if i["status"] == "pending" and i.get("enwiki")}
    titles, n = sorted(todo), 0
    for k in range(0, len(titles), 40):
        chunk = titles[k:k + 40]
        try:
            d = json.loads(http.get(WP_API, ttl=3 * 24 * 3600, params=dict(
                action="query", prop="revisions", rvprop="content", rvslots="main", rvsection=0,
                titles="|".join(chunk), redirects=1, format="json", formatversion=2)))
        except (http.Blocked, http.NotFound, RuntimeError):
            continue
        norm = {x["from"]: x["to"] for x in d["query"].get("normalized", [])}
        norm.update({x["from"]: x["to"] for x in d["query"].get("redirects", [])})
        by = {p["title"]: p["revisions"][0]["slots"]["main"]["content"] for p in d["query"]["pages"] if p.get("revisions")}
        for t in chunk:
            t1 = norm.get(t, t)
            wt = by.get(norm.get(t1, t1))
            i = cands[todo[t]]
            i["infobox"] = parse_infobox(wt) if wt else None
            n += i["infobox"] is not None
    return n


def wikipedia_check(cands: dict, cat_hits: dict) -> None:
    """Read the Wikipedia lead of every pending candidate. Adds quoted heritage sentences (documented), flags senior
    caps for Egypt (already_capped) or another nation (likely_ineligible). Category-path players without a
    supporting sentence in the lead get one full-article read."""
    todo = {unquote(i["enwiki"].rsplit("/", 1)[-1]).replace("_", " "): q
            for q, i in cands.items() if i["status"] == "pending" and i.get("enwiki")}
    leads = _lead_texts(sorted(todo))
    for t, q in todo.items():
        i = cands[q]
        text = leads.get(t, "")
        i["lead_text"] = text
        sents = heritage_sentences(text)
        if not sents and q in cat_hits:
            text_full = _full_text(t)
            sents = heritage_sentences(text_full)
            text += " " + text_full
        _apply_wiki(i, text, sents, q in cat_hits)


def _apply_wiki(i: dict, text: str, sents: list[str], from_cat: bool) -> None:
    url = i["enwiki"]
    for s in sents[:2]:
        if from_cat and i.get("category_cats"):
            cats = "; ".join(i["category_cats"])
            _add(i["evidence"], "Wikipedia category + supporting article sentence",
                 f'{cats} -- "{s}"', url, "documented")
        else:
            _add(i["evidence"], "Wikipedia article states an Egyptian link", s, url, "documented")
    if from_cat and not sents and i.get("category_cats"):
        _add(i["evidence"], "Wikipedia category only (no supporting sentence found)",
             "; ".join(i["category_cats"]), url, "inferred")
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text):
        if _EGYPT_SENIOR_RE.search(sent) and not _YOUTH_Q.search(sent):
            i["wiki_egypt_senior"] = sent.strip()
        for m in _NT_RE.finditer(sent):
            if _CAP_VERBS.search(sent) and not _SKIP.search(sent):
                if m.group(1).lower().startswith("egypt"):
                    i["wiki_egypt_senior"] = sent.strip()
                else:
                    i.setdefault("wiki_other_nt", []).append((m.group(1), sent.strip()))


# ------------------------------------------------------------------ 3. positional need
TARGET_REGULARS = {"GK": 2, "DEF": 5, "MID": 5, "ATT": 5}
REGULAR_MINUTES = 270
AGE_LO, AGE_HI = 25.0, 33.0


def compute_need(conn: sqlite3.Connection) -> dict[str, dict]:
    """Need per position group in [0,1] from Egypt senior lineups over the last 12 months.

    age_score  = clamp((mean age of the two most-used players in the group - 25) / (33 - 25))
    thin_score = clamp(1 - n_regulars / target), n_regulars = players with >= 270 min in the group,
                 target = 2 GK / 5 DEF / 5 MID / 5 ATT
    need       = 0.6 * age_score + 0.4 * thin_score
    """
    since = (TODAY - timedelta(days=365)).isoformat()
    rows = conn.execute(
        """SELECT l.player_id, l.slot, l.position, COALESCE(l.minutes_played,0), p.dob, p.position, p.name
           FROM lineups l JOIN matches m ON m.id = l.match_id JOIN players p ON p.id = l.player_id
           WHERE m.is_egypt = 1 AND l.team_id = (SELECT id FROM teams WHERE name='Egypt' AND kind='national')
             AND m.date >= ? AND m.date <= ?""", (since, TODAY.isoformat())).fetchall()
    per: dict[str, dict[int, dict]] = {g: {} for g in TARGET_REGULARS}
    for pid, slot, pos, mins, dob, ppos, name in rows:
        g = _slot_group(slot, pos) or pos_group(ppos)
        if not g:
            continue
        e = per[g].setdefault(pid, {"name": name, "dob": dob, "min": 0.0})
        e["min"] += mins
    out = {}
    for g, players in per.items():
        ranked = sorted(players.values(), key=lambda e: -e["min"])
        total = sum(e["min"] for e in ranked)
        regulars = [e for e in ranked if e["min"] >= REGULAR_MINUTES]
        top2 = [e for e in ranked[:2] if _age(e["dob"]) is not None]
        avg_age = round(sum(_age(e["dob"]) for e in top2) / len(top2), 1) if top2 else None
        age_score = 0.5 if avg_age is None else min(1, max(0, (avg_age - AGE_LO) / (AGE_HI - AGE_LO)))
        thin = min(1, max(0, 1 - len(regulars) / TARGET_REGULARS[g]))
        need = round(0.6 * age_score + 0.4 * thin, 3)
        out[g] = dict(pos_group=g, minutes_12m=round(total), n_regulars=len(regulars), top2_avg_age=avg_age,
                      age_score=round(age_score, 3), thin_score=round(thin, 3), need=need,
                      top2=[f"{e['name']} ({_age(e['dob'])})" for e in top2])
    return out


# ------------------------------------------------------------------ 4. scoring
TOP5 = {"premier league", "laliga", "serie a", "bundesliga", "ligue 1"}
W = {"need": 0.35, "age": 0.20, "minutes": 0.25, "league": 0.20, "verification": 0.10}


def league_score(league: str | None, level: str | None) -> tuple[float | None, str]:
    """Approximate tiering: Transfermarkt 'League level' (First/Second/Third Tier) + top-5 European
    leagues on top. Unknown league -> None (component dropped and weights renormalised)."""
    lv = (level or "").lower()
    if league and league.lower() in TOP5:
        return 1.0, "top-5 European league"
    if lv.startswith("first"):
        return 0.7, "first tier"
    if lv.startswith("second"):
        return 0.45, "second tier"
    if lv.startswith("third"):
        return 0.3, "third tier"
    if lv:
        return 0.15, lv
    return None, "unknown"


def age_fit(age: float | None) -> float | None:
    """1.0 for 18-24 (long international runway), falling linearly to 0.2 at 34."""
    if age is None:
        return None
    if age <= 24:
        return 1.0
    return round(max(0.2, 1 - 0.8 * (age - 24) / 10), 3)


def score(c: dict, need: dict) -> None:
    g = c.get("pos_group")
    comps = {
        # unknown position -> mean need of the four groups (neutral); unknown league -> 0.1 (assume low level)
        "need": need[g]["need"] if g in need else round(sum(n["need"] for n in need.values()) / len(need), 3),
        "age": age_fit(c.get("age")),
        "verification": 1.0 if c.get("tm") else 0.5,   # 1.0 = Transfermarkt header checked for senior caps
        "minutes": None,    # no cheap public minutes source reachable (see caveats): dropped + renormalised
        "league": c.get("_league_score") if c.get("_league_score") is not None else 0.1,
    }
    used = {k: v for k, v in comps.items() if v is not None}
    wsum = sum(W[k] for k in used)
    c["score"] = round(100 * sum(W[k] * v for k, v in used.items()) / wsum, 1) if wsum else 0.0
    c["score_components"] = {k: (None if v is None else round(v, 3)) for k, v in comps.items()}
    c["score_weights_used"] = {k: round(W[k] / wsum, 3) for k in used}




# ------------------------------------------------------------------ main pipeline
_YOUTH_CLUB = re.compile(r"\bU-?\d{2}\b|academy|youth|under-?\d{2}", re.I)
_MY_OLD_CLAIMS = ("citizenship: Egypt", "born in Egypt", "Egypt youth/non-senior national team",
                  "Transfermarkt lists Egypt as citizenship", "Wikipedia lead states an Egyptian link")


def _add(sig: list, claim, text, url, conf):
    e = dict(claim=claim, evidence_text=text, source_url=url, confidence=conf)
    if e not in sig:
        sig.append(e)


def cleanup_phase1(conn: sqlite3.Connection) -> dict:
    """Earlier versions inserted rows into Phase 1 tables. Remove them (idempotent) and report FK orphans."""
    ids = [r[0] for r in conn.execute("SELECT id FROM players WHERE source='diaspora_scout'")]
    removed_ev = 0
    if ids:
        ph = ",".join("?" * len(ids))
        removed_ev += conn.execute(f"DELETE FROM eligibility_evidence WHERE player_id IN ({ph})", ids).rowcount
    removed_ev += conn.execute(
        "DELETE FROM eligibility_evidence WHERE confidence='documented' AND claim IN (%s)" % ",".join("?" * len(_MY_OLD_CLAIMS)),
        _MY_OLD_CLAIMS).rowcount
    if ids:
        conn.execute(f"DELETE FROM players WHERE id IN ({','.join('?' * len(ids))})", ids)
    conn.commit()
    orphans = conn.execute("""SELECT COUNT(*) FROM eligibility_evidence e LEFT JOIN players p ON p.id=e.player_id
                              WHERE p.id IS NULL""").fetchone()[0]
    return dict(players_removed=len(ids), evidence_removed=removed_ev, evidence_orphans=orphans,
                evidence_rows_left=conn.execute("SELECT COUNT(*) FROM eligibility_evidence").fetchone()[0])


def run(conn: sqlite3.Connection) -> dict:
    conn.execute("PRAGMA busy_timeout=120000")   # other ETL agents (e.g. Salah module) write concurrently
    for t in ("diaspora_signals", "diaspora_candidates", "diaspora_evidence"):
        conn.execute(f"DROP TABLE IF EXISTS {t}")     # own tables, rebuilt every run
    conn.executescript(SCHEMA)
    cleanup = cleanup_phase1(conn)
    EXPORT.mkdir(parents=True, exist_ok=True)
    PRIVATE.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # ---- already-capped index: DB lineups (Egypt, 2018+) and Wikidata all-time Egypt senior players
    capped_db: dict[str, list] = defaultdict(list)
    for qid, name, dob in conn.execute(
            """SELECT DISTINCT p.wikidata_qid, p.name, p.dob FROM players p JOIN lineups l ON l.player_id=p.id
               WHERE p.source != 'diaspora_scout'
                 AND l.team_id=(SELECT id FROM teams WHERE name='Egypt' AND kind='national')"""):
        capped_db[_key(name)].append((int(dob[:4]) if dob else None, qid, name, dob))
    egypt_int = egypt_internationals()
    wd_names: dict[str, list] = defaultdict(list)
    for q, e in egypt_int.items():
        if e["name"]:
            wd_names[_key(e["name"])].append((q, e["dob"], e["name"]))

    # ---- discovery
    sigs = discover()
    cat_hits, cat_stats = category_path()
    squads = youth_squads()
    squad_by_q: dict[str, list] = defaultdict(list)
    squad_only: list[dict] = []
    for r in squads:
        if r["qid"]:
            squad_by_q[r["qid"]].append(r)
        else:
            squad_only.append(r)
    all_q = set(sigs) | set(cat_hits) | set(squad_by_q)
    parent_q = {q for q, s_ in sigs.items() if s_ & {"parent_citizenship", "parent_birth"}}
    info = enrich(sorted(all_q), parent_q)     # born >= 1996 only
    for q, i in info.items():
        summarize_career(i)
        i["key"] = q
        i["paths"] = sorted(({"wikidata"} if q in sigs else set()) | ({"wp_category"} if q in cat_hits else set())
                            | ({"youth_squad"} if q in squad_by_q else set()))
        i["signals"] = sigs.get(q, set())
        i["squad_rows"] = squad_by_q.get(q, [])
        if q in cat_hits:
            i["category_cats"] = cat_hits[q]["cats"]
            i["enwiki"] = i.get("enwiki") or wp_url(cat_hits[q]["title"])
        if not i.get("enwiki") and i["squad_rows"] and i["squad_rows"][0]["link"]:
            i["enwiki"] = wp_url(i["squad_rows"][0]["link"])
    for r in squad_only:   # squad players without a Wikipedia page: only what the squad table says
        by = int(r["dob"][:4]) if r["dob"] else None
        if by is not None and by < MIN_BIRTH_YEAR:
            continue
        k = f"wp:{r['team'].replace('Egypt national ', '').replace(' football team', '')}:{db.norm(r['name'])}"
        info[k] = dict(qid=None, key=k, name=r["name"], dob=r["dob"], pob=None, pob_country=None, citizenships=[],
                       positions=[], tm_id=None, enwiki=None, teams=[], parents=[], signals=set(), paths=["youth_squad"],
                       squad_rows=[r], senior_nts=[], egypt_senior=False, egypt_youth_teams=[r["team"]],
                       nt_history=[r["team"]], n_clubs=0, wd_club=None, wd_league=None, wd_club_country=None)
    n_disc = dict(wikidata=len(sigs), wp_category=len(cat_hits), youth_squad=len(squads),
                  union_after_dob=len(info))

    need = compute_need(conn)
    cands, counts = {}, defaultdict(int)
    for k, i in info.items():
        i["evidence"] = []
        i["collision"] = None
        name = i.get("name")
        wurl = WD + i["qid"] if i.get("qid") else None
        if i.get("female"):
            counts["dropped_women"] += 1     # men's national team scope
            continue
        if i.get("dob") is None and not i["squad_rows"] or (i.get("dob") and int(i["dob"][:4]) < MIN_BIRTH_YEAR):
            counts["dropped_dob"] += 1
            continue
        has_club = i["n_clubs"] > 0 or i.get("tm_id") or any(r.get("club") for r in i["squad_rows"])
        by = int(i["dob"][:4]) if i.get("dob") else None
        i["status"], i["reason"] = "pending", ""
        if not has_club:
            i["status"], i["reason"] = "not_professional", "no club membership on Wikidata / Transfermarkt / squad table"
        elif i.get("qid") in egypt_int or i["egypt_senior"]:
            i["status"], i["reason"] = "already_capped", "Egypt senior player on Wikidata (P54)"
        elif name and by is not None:
            for y, q2, n2, d2 in capped_db.get(_key(name), []):
                if q2 and i.get("qid") and q2 != i["qid"]:
                    i["collision"] = dict(shares_name_with=n2, their_dob=d2, their_qid=q2)
                elif y is None or abs(y - by) <= 1:
                    i["status"], i["reason"] = "already_capped", "same name/birth year as an Egypt international in lineups"
        if name and i["status"] == "pending":
            for q2, d2, n2 in wd_names.get(_key(name), []):
                if q2 != i.get("qid"):
                    i["collision"] = dict(shares_name_with=n2, their_dob=d2, their_qid=q2)
        if i["collision"]:
            i["collision"].update(this_dob=i.get("dob"), this_qid=i.get("qid"),
                                  note=f"Not the same person as Egypt international {i['collision']['shares_name_with']} "
                                       f"(DOB {i['collision']['their_dob']}, {i['collision']['their_qid']}); "
                                       f"this player: DOB {i.get('dob')}, {i.get('qid')}.")
        cands[k] = i
        ev = i["evidence"]
        if "Egypt" in i.get("citizenships", []) and wurl:
            _add(ev, "citizenship: Egypt", f"country of citizenship (P27): {', '.join(i['citizenships'])}", wurl, "documented")
        if i.get("pob_country") == "Egypt" and wurl:
            _add(ev, "born in Egypt", f"place of birth (P19): {i['pob']}" + ("" if i["pob"] == "Egypt" else ", Egypt"), wurl, "documented")
        if i["egypt_youth_teams"] and wurl and i["teams"]:
            _add(ev, "Egypt youth/non-senior national team",
                 "member of sports team (P54): " + "; ".join(i["egypt_youth_teams"]), wurl, "documented")
        for r in i["squad_rows"]:
            _add(ev, f"Egypt youth squad call-up ({r['team']})",
                 f"{r['team']}, \"Current squad\" table: name={r['name']}, pos={r['pos']}, club={r['club']}, "
                 f"clubnat={r['clubnat']}. {r['context']}".strip(), r["page_url"], "documented")
        for par in i["parents"]:
            if par["egypt_citizen"]:
                _add(ev, f"{par['rel']} recorded as Egyptian citizen",
                     f"{par['rel']} (P22/P25): {par['label']}; country of citizenship (P27): Egypt", wurl, "inferred")
            if par["egypt_birth"]:
                _add(ev, f"{par['rel']} born in Egypt",
                     f"{par['rel']} (P22/P25): {par['label']}; place of birth (P19): {par['egypt_birth']}, Egypt", wurl, "inferred")

    # ---- Transfermarkt cross-check (respects the 24 h block marker; never retried inside a run)
    tm_blocked = None
    block_file = ROOT / "data" / "cache" / "transfermarkt_blocked.txt"
    if block_file.exists() and datetime.now().timestamp() - block_file.stat().st_mtime < 24 * 3600:
        tm_blocked = block_file.read_text().strip()
    n_tm = 0
    for k, i in cands.items():
        if i["status"] != "pending" or not i.get("tm_id"):
            continue
        cached = http._cache_path(transfermarkt.profile_url(i["tm_id"])).exists()
        if tm_blocked and not cached:
            continue
        try:
            t = transfermarkt.profile(i["tm_id"])
        except (http.Blocked, http.NotFound) as e:
            if isinstance(e, http.Blocked):
                tm_blocked = str(e)
                block_file.parent.mkdir(parents=True, exist_ok=True)
                block_file.write_text(tm_blocked)
            continue
        n_tm += 1
        i["tm"] = t
        if i["n_clubs"] == 0 and (not t.get("club") or t["club"].lower() in ("retired", "without club", "career break")):
            i["status"], i["reason"] = "not_professional", "no club on Wikidata or Transfermarkt"
            continue
        if "Egypt" in t["citizenships"]:
            _add(i["evidence"], "Transfermarkt lists Egypt as citizenship",
                 f"Citizenship: {', '.join(t['citizenships'])}", t["source_url"], "documented")
        if t.get("current_international") == "Egypt" and (t.get("caps") or 0) > 0:
            i["status"], i["reason"] = "already_capped", f"Transfermarkt: Egypt international, {t['caps']} caps"
        elif t.get("current_international") and (t.get("caps") or 0) > 0:
            i["tm_other_nt"] = f"{t['current_international']} ({t['caps']} caps)"

    wikipedia_check(cands, cat_hits)
    n_infobox = infobox_check(cands)
    for k, i in cands.items():
        if i["status"] != "pending":
            continue
        ib = i.get("infobox")
        i["other_youth"] = sorted({f"{y['team']}" + (f" ({y['caps']} caps)" if y["caps"] else "")
                                   for y in (ib or {}).get("youth", []) if y["nation"] and not y["nation"].lower().startswith("egypt")
                                   and (y["caps"] or 0) > 0})
        eg_ib = [x for x in (ib or {}).get("senior", []) if x["nation"].lower().startswith("egypt")]
        oth_ib = [x for x in (ib or {}).get("senior", []) if x["nation"] and not x["nation"].lower().startswith("egypt")
                  and (x["caps"] or 0) > 0]
        if any((x["caps"] or 0) > 0 for x in eg_ib):
            i["status"], i["reason"] = "already_capped", "Wikipedia infobox: senior Egypt caps " + ", ".join(
                f"{x['team']} {x['caps']}" for x in eg_ib)
            continue
        if eg_ib or i.get("wiki_egypt_senior"):
            i["status"] = "possibly_capped_verify"
            i["reason"] = ("Wikipedia text suggests senior Egypt appearances: \"" + (i.get("wiki_egypt_senior") or "") + "\""
                           + ("; infobox lists senior Egypt entry with unknown caps" if eg_ib else "")
                           + ("; infobox national-team section: " + ", ".join(f"{y['team']} {y['caps']}" for y in ib["youth"] + ib["senior"])
                              if ib else "; no infobox found") + f" ({i['enwiki']})")
            continue
        other = []
        if oth_ib:
            other.append("Wikipedia infobox senior caps: " + ", ".join(f"{x['team']} {x['caps']}" for x in oth_ib))
        if i["senior_nts"]:
            other.append("Wikidata P54 senior team(s): " + ", ".join(i["senior_nts"]))
        if i.get("wiki_other_nt"):
            other.append("Wikipedia: \"" + i["wiki_other_nt"][0][1] + "\" (" + i["enwiki"] + ")")
        if i.get("tm_other_nt"):
            other.append("Transfermarkt current international: " + i["tm_other_nt"])
        if other:
            i["status"] = "likely_ineligible"
            i["reason"] = ("; ".join(other) + " -- FIFA RGAS art. 5.3 ties a player to an association after a match in an "
                           "official competition; sources cannot separate friendlies, so treated as tied unless art. 9 applies")

    # ---- club / position / location facts, then classification + scoring
    for k, i in cands.items():
        t = i.get("tm") or {}
        sq = next((r for r in i["squad_rows"] if r.get("club")), None)
        club = league = level = country = src = None
        lf = lead_facts(i.get("lead_text", ""))
        abroad_hint = False
        if t.get("club") and t["club"].lower() not in ("retired", "without club", "career break"):
            club, league, level, country, src = t["club"], t.get("league"), t.get("league_level"), t.get("league_country"), "Transfermarkt"
        elif lf.get("club"):
            lp = lf.get("league_phrase")
            club, league, country, src = lf["club"], lp, ("Egypt" if lp and "egypt" in lp.lower() else None), "Wikipedia lead"
            abroad_hint = bool(lp and "egypt" not in lp.lower())
        elif i.get("wd_club"):
            club, league, country, src = i["wd_club"], i.get("wd_league"), i.get("wd_club_country"), "Wikidata P54"
        elif sq:
            club, country, src = sq["club"], ("Egypt" if sq["clubnat"] == "EGY" else sq["clubnat"]), f"Wikipedia squad table ({sq['team']})"
        i["club"], i["league"], i["league_level"], i["club_country"], i["club_source"] = club, league, level, country, src
        i["plays_in_egypt"] = bool(club and (country == "Egypt" or "egypt" in (league or "").lower()))
        pg = pos_group(t.get("position")) or pos_group(lf.get("position")) or next((pos_group(p) for p in i.get("positions", []) if pos_group(p)), None) \
            or next((pos_group(r["pos"]) for r in i["squad_rows"] if pos_group(r["pos"])), None)
        i["pos_group"] = pg
        i["position"] = t.get("position") or lf.get("position") or (i["positions"][0] if i.get("positions") else None) or \
            next(({"GK": "Goalkeeper", "DF": "Defender", "MF": "Midfielder", "FW": "Forward"}.get((r["pos"] or "").upper(), r["pos"])
                  for r in i["squad_rows"] if r["pos"]), None)
        i["age"] = _age(i.get("dob"))
        i["birth_country"] = i.get("pob_country") or t.get("birth_country") or lf.get("birth_country")
        i["born_abroad"] = bool(i["birth_country"] and i["birth_country"] != "Egypt")
        i["club_abroad"] = bool(club and "egypt" not in (league or "").lower() and (
            (country and country != "Egypt") or abroad_hint))
        lv = (level or "").lower()
        i["pro"] = bool(club and not _YOUTH_CLUB.search(club) and (
            lv.startswith(("first", "second", "third")) or (src == "Wikidata P54" and i.get("wd_league"))
            or (src == "Wikipedia lead" and league and _LEAGUE_WORDS.search(league))
            or (sq and sq["clubnat"] and src.startswith("Wikipedia squad"))))
        i["_league_score"], i["league_basis"] = league_score(league, level)
        i["citizenships_all"] = t.get("citizenships") or i.get("citizenships", [])
        i["club_verified"] = src in ("Transfermarkt", "Wikipedia lead")
        i["caps_verified"] = i.get("infobox") is not None or bool(t)
        i["verification"] = "checked" if (i["club_verified"] and i["caps_verified"] and not i.get("other_youth")) else "partial"
        if i["status"] != "pending":
            continue
        doc = [e for e in i["evidence"] if e["confidence"] == "documented"]
        missing = [n for n, ok in (("club", bool(club)), ("professional-level club", i["pro"]), ("position", bool(pg)),
                                    ("dob", bool(i.get("dob")))) if not ok]
        if not doc:
            i["status"], i["reason"] = "private_review", "only inferred signals" if i["evidence"] else "no evidence rows survived"
        elif not i.get("name"):
            i["status"], i["reason"] = "private_review", "no English Wikidata label"
        elif not i.get("dob"):
            i["status"], i["reason"] = "private_review", "documented link but no DOB (age/identity unverifiable)"
        elif i["age"] < PUBLIC_MIN_AGE:
            i["status"], i["reason"] = "private_review", f"documented link but under {PUBLIC_MIN_AGE}: never published"
        elif (all(e["claim"] == "born in Egypt" for e in doc) and i.get("citizenships")
              and "Egypt" not in i["citizenships"]):
            i["status"], i["reason"] = "private_review", (
                "only documented link is birth in Egypt, and Wikidata lists other citizenship(s) "
                f"({', '.join(i['citizenships'])}) without Egypt: eligibility and senior-cap status unverified")
        elif not missing and (i["born_abroad"] or i["club_abroad"]):
            i["status"] = "public"
        elif i["collision"] and missing:
            i["status"], i["reason"] = "private_review", "name shared with an Egypt international and context missing: " + ", ".join(missing)
        elif i["plays_in_egypt"] and not i["born_abroad"]:
            i["status"], i["bucket"] = "domestic", "egypt_based"
        elif not club and not i["born_abroad"]:
            i["status"], i["bucket"] = "domestic", "no_club_known"
        else:
            i["status"], i["reason"] = "private_review", "diaspora signal but missing: " + (", ".join(missing) or "abroad status")
    for i in cands.values():
        counts[i["status"]] += 1
        if i["status"] in ("public", "private_review", "likely_ineligible", "possibly_capped_verify"):
            score(i, need)

    public = sorted([i for i in cands.values() if i["status"] == "public"], key=lambda x: (-x["score"], x["name"] or ""))
    top = public[:TOP_N]
    domestic = sorted([i for i in cands.values() if i["status"] == "domestic"], key=lambda x: x["name"] or "")

    # ---- persistence (own tables only; Phase 1 tables are never written)
    for g, n in need.items():
        conn.execute("INSERT OR REPLACE INTO diaspora_position_need VALUES (?,?,?,?,?,?,?,?,?)",
                     (g, n["minutes_12m"], n["n_regulars"], n["top2_avg_age"], n["age_score"], n["thin_score"],
                      n["need"], json.dumps(n["top2"]), now))
    for i in cands.values():
        for e in i["evidence"]:
            conn.execute("INSERT OR IGNORE INTO diaspora_evidence VALUES (?,?,?,?,?)",
                         (i["key"], e["claim"], e["evidence_text"], e["source_url"], e["confidence"]))
        payload = {kk: i.get(kk) for kk in ("position", "pos_group", "club", "league", "league_level", "club_country",
                                             "birth_country", "paths", "score_components")}
        conn.execute("INSERT INTO diaspora_candidates VALUES (?,?,?,?,?,?,?,?,?)",
                     (i["key"], i.get("qid"), i.get("name"), i.get("dob"), i["status"], i["reason"], i.get("score"),
                      json.dumps(payload), now))
    conn.commit()

    # ---- exports
    def links(i):
        return ([WD + i["qid"]] if i.get("qid") else []) + ([i["tm"]["source_url"]] if i.get("tm") else []) + (
            [i["enwiki"]] if i.get("enwiki") else []) + [r["page_url"] for r in i["squad_rows"]]

    def docs_of(i):
        return [dict(claim=e["claim"], evidence_text=e["evidence_text"], source_url=e["source_url"],
                     confidence="documented") for e in i["evidence"] if e["confidence"] == "documented"]

    def base_entry(i):
        return dict(name=i["name"], wikidata_qid=i.get("qid"), dob=i["dob"], age=i["age"], position=i["position"],
                    position_group=i["pos_group"], club=i["club"], club_source=i["club_source"], league=i["league"],
                    league_level=i["league_level"], club_country=i["club_country"], birthplace=i.get("pob"),
                    birth_country=i["birth_country"], citizenships=i["citizenships_all"],
                    national_team_history=i["nt_history"], discovery_paths=i["paths"],
                    name_collision_note=(i["collision"] or {}).get("note"), evidence=docs_of(i), source_links=links(i))

    entries = []
    for n, i in enumerate(top, 1):
        e = base_entry(i)
        e.update(rank=n, score=i["score"], score_components=i["score_components"], score_weights_used=i["score_weights_used"],
                 league_basis=i["league_basis"], why_abroad=("born outside Egypt" if i["born_abroad"] else "") +
                 (" and " if i["born_abroad"] and i["club_abroad"] else "") + ("club outside Egypt" if i["club_abroad"] else ""),
                 club_verified=i["club_verified"], caps_verified=i["caps_verified"], verification=i["verification"],
                 other_nation_youth_appearances=i.get("other_youth", []),
                 infobox_national_team_section=(i.get("infobox") or {}),
                 cap_check=("Wikidata P54 + Wikipedia lead + Transfermarkt header: no senior caps found" if i.get("tm")
                            else "Wikidata P54 + Wikipedia lead only (Transfermarkt cross-check unavailable): senior caps not fully verified"),
                 recent_minutes=None)
        entries.append(e)
    for e in entries:   # HARD RULES for the public file
        assert e["evidence"], f"public entry without evidence: {e['name']}"
        assert all(x["confidence"] == "documented" and x["source_url"] for x in e["evidence"]), e["name"]
        assert e["club"] and e["position"] and e["dob"], f"public entry missing club/position/dob: {e['name']}"
    pub = dict(generated_at=now, as_of=TODAY.isoformat(), count=len(entries), n_documented_total=len(public),
               position_need=need, fifa_rule=FIFA_NOTE, fifa_sources=FIFA_URLS,
               disclaimer=LEAD_LIST, method="see diaspora_summary.md", candidates=entries)
    (EXPORT / "diaspora_candidates.json").write_text(json.dumps(pub, indent=2, ensure_ascii=False))

    dom_entries = []
    for i in domestic:
        e = base_entry(i)
        e["bucket"] = i["bucket"]
        dom_entries.append(e)
        assert e["evidence"] and all(x["confidence"] == "documented" and x["source_url"] for x in e["evidence"]), e["name"]
    dom = dict(generated_at=now, as_of=TODAY.isoformat(), ranked=False,
               note="UNRANKED data for a later product: documented-Egyptian, not senior-capped players who are based in "
                    "Egypt (egypt_based) or have no club on record (no_club_known). Not diaspora candidates.",
               counts={b: sum(1 for e in dom_entries if e["bucket"] == b) for b in ("egypt_based", "no_club_known")},
               players=dom_entries)
    (EXPORT / "domestic_uncapped.json").write_text(json.dumps(dom, indent=2, ensure_ascii=False))

    review = [dict(name=i["name"] or "[no English label] " + str(i.get("qid")), wikidata_qid=i.get("qid"), dob=i.get("dob"),
                   status=i["status"], reason=i["reason"], club=i.get("club"), league=i.get("league"),
                   position=i.get("position"), paths=i["paths"], signals=sorted(i["signals"]), evidence=i["evidence"],
                   name_collision_note=(i["collision"] or {}).get("note"),
                   confidence="inferred" if i["status"] == "private_review" else None, score=i.get("score"))
              for i in cands.values() if i["status"] in ("private_review", "likely_ineligible", "possibly_capped_verify")]
    review.sort(key=lambda r: (r["status"], r["name"] or ""))
    (PRIVATE / "diaspora_review.json").write_text(json.dumps(
        dict(generated_at=now, note="PRIVATE - do not publish. inferred / minor / missing-data / likely_ineligible entries.",
             count=len(review), entries=review), indent=2, ensure_ascii=False))

    cat_public = sum(1 for i in public if "wp_category" in i["paths"])
    cat_doc_public = sum(1 for i in public if any(e["claim"].startswith("Wikipedia category +") for e in i["evidence"]))
    stats = dict(discovered=n_disc, category_crawl=cat_stats, transfermarkt_profiles_read=n_tm, infoboxes_read=n_infobox,
                 **{kk: v for kk, v in counts.items()}, public_from_category_path=cat_public,
                 public_with_category_sentence_evidence=cat_doc_public,
                 top30_from_category_path=sum(1 for i in top if "wp_category" in i["paths"]),
                 top30_from_youth_squad=sum(1 for i in top if "youth_squad" in i["paths"]),
                 phase1_cleanup=cleanup)
    write_summary(stats, need, public, top, tm_blocked, len(review))
    return stats


def write_summary(stats, need, public, top, tm_blocked, n_review):
    d = stats["discovered"]
    L = ["# Diaspora eligibility scout - summary", "", f"**{LEAD_LIST}**", "",
         f"As of {TODAY.isoformat()}. Public data only (Wikidata, Wikipedia, Transfermarkt public profiles, Egypt lineups from FotMob).", "",
         "## Counts", "",
         f"- Discovered (raw hits before the 1996+ birth filter): Wikidata SPARQL {d['wikidata']}, Wikipedia categories {d['wp_category']} footballers "
         f"({stats['category_crawl']['categories_crawled']} categories crawled), Egypt U-17/U-20/U-23 squad rows {d['youth_squad']}",
         f"- Distinct candidates born {MIN_BIRTH_YEAR}+: {d['union_after_dob']}",
         f"- Already capped by Egypt (excluded): {stats.get('already_capped', 0)}",
         f"- No club record (not shown as professional): {stats.get('not_professional', 0)}",
         f"- likely_ineligible (senior caps for another nation): {stats.get('likely_ineligible', 0)}",
         f"- possibly_capped_verify (text suggests Egypt senior appearances; private): {stats.get('possibly_capped_verify', 0)}",
         f"- Public diaspora/abroad candidates (documented link, abroad, known professional club and position): {stats.get('public', 0)}",
         f"- Domestic uncapped (documented, Egypt-based or no club on record; unranked, separate file): {stats.get('domestic', 0)}",
         f"- Private review (weak signals, minors, missing data): {stats.get('private_review', 0)}",
         f"- Public candidates found via the Wikipedia category path: {stats['public_from_category_path']}", "",
         "## Methodology", "",
         "1. Public target: (a) a documented Egyptian link AND (b) birth outside Egypt OR a current club outside Egypt AND "
         "(c) a known current club at professional level (Transfermarkt league tier 1-3, a league on Wikidata, or a club in a Wikipedia squad table) and a known position. "
         "Egypt-based players go to `domestic_uncapped.json` (unranked).",
         "2. Discovery: (i) Wikidata SPARQL (citizenship, Egyptian birthplace, Egyptian national-team membership; parent signals are private only); "
         "(ii) Wikipedia categories '<Nationality> people of Egyptian descent' and 'Egyptian emigrants to <country>' intersected with footballers "
         "(short description), evidence = category plus the quoted article sentence; religion-based categories are never crawled; "
         "(iii) 'Current squad' tables of the Egypt U-17/U-20/U-23 pages (youth call-ups; some tables are old).",
         "3. Evidence is documented only when it is a structured statement or a verbatim quoted sentence with a source URL. "
         "Heritage is never inferred from names, religion or appearance.",
         "4. Exclusions: Egypt senior players (DB lineups 2018+, Wikidata P54, Transfermarkt, Wikipedia lead); players with senior caps for another nation are marked likely_ineligible. "
         "Players under 18 are never published. Wikipedia lead sentences such as 'represents Egypt' without a youth qualifier, or a senior Egypt entry in the infobox, "
         "put a player in private review as possibly_capped_verify (infobox caps > 0 = capped, excluded).",
         "5. Name-collision guard: if a candidate shares an exact name with an Egypt international, the export carries a note with both DOBs and QIDs; without club, position and DOB the candidate is dropped from the public list.",
         f"6. FIFA rule. {FIFA_NOTE} Sources: " + "; ".join(f"{k}: {u}" for k, u in FIFA_URLS.items()) + ".",
         "7. Positional need (Egypt lineups, last 12 months): per group need = 0.6 * age_score + 0.4 * thin_score. "
         f"age_score = (mean age of the two most-used players - {AGE_LO:g}) / ({AGE_HI:g} - {AGE_LO:g}), clamped 0-1; "
         f"thin_score = 1 - (players with >= {REGULAR_MINUTES} min / target), targets GK 2, DEF 5, MID 5, ATT 5.",
         "8. Ranking score (0-100) = weighted mean of need (0.35), age fit (0.20: 1.0 up to age 24, falling to 0.2 at 34), recent minutes (0.25), "
         "league level (0.20: top-5 European league 1.0, first tier 0.7, second 0.45, third 0.3, unknown 0.1; approximate) and verification "
         "(0.10: 1.0 if the Transfermarkt header was checked for caps, else 0.5). Components with no data are dropped and weights renormalised.", "",
         "### Positional need", "", "| Group | Minutes 12m | Regulars | Top-2 avg age | Need |", "|---|---|---|---|---|"]
    for g, n in need.items():
        L.append(f"| {g} | {n['minutes_12m']} | {n['n_regulars']} | {n['top2_avg_age']} | {n['need']} |")
    L += ["", "## Top 15", "", "| # | Player | Age | Pos | Club (league) | Born | Score | Verification (club/caps) | Documented link |", "|---|---|---|---|---|---|---|---|---|"]
    for n, i in enumerate(top[:15], 1):
        ev = "; ".join(e["claim"] for e in i["evidence"] if e["confidence"] == "documented")
        L.append(f"| {n} | {i['name']} | {i['age']} | {i['position']} | {i['club']} ({i['league'] or 'league n/a'}) | "
                 f"{i.get('pob') or '?'}, {i['birth_country'] or '?'} | {i['score']} | {i['verification']} (club {'yes' if i['club_verified'] else 'no'}, caps {'yes' if i['caps_verified'] else 'no'}) | {ev} |")
    L += ["", "## Caveats", "",
          "- Wikidata and Wikipedia are sparse for young and lower-league players; absence from this list means nothing about eligibility.",
          "- A documented link is a public-record fact, not a claim about intent or willingness to play for Egypt; eligibility depends on FIFA rules and the player's associations.",
          "- Youth squad tables are call-up lists (some dated 2023), not proof of caps; club data from a squad table may be stale.",
          "- Recent-minutes data was not reachable cheaply (FotMob needs a player id not on Wikidata; Transfermarkt performance tables render client-side), so the minutes component is dropped from the score.",
          "- Cap detection uses Wikidata P54, Wikipedia lead text and the Transfermarkt header; it cannot separate friendlies from competitive caps, so likely_ineligible is conservative, and some other-nation internationals may be missed where all sources are silent.",
          "- Current club from Wikidata is the latest open-ended P54 membership and can be stale.",
          "- Name matching against Egypt lineups (2018+) uses name tokens plus birth year +-1 and can hide namesakes."]
    if tm_blocked:
        L.append(f"- Transfermarkt returned a block ({tm_blocked}); it was not retried and only cached profiles were used, so many candidates lack the Transfermarkt cross-check.")
    (EXPORT / "diaspora_summary.md").write_text("\n".join(L) + "\n")


def main() -> int:
    conn = db.connect()
    db.init(conn)
    stats = run(conn)
    print(json.dumps(stats, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
