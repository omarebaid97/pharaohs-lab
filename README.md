# Pharaohs Lab - data foundation (Phase 1)

Public analytics site about the Egypt men's national football team, built only on publicly available data.
Phase 1 is the local ETL: it builds `data/pharaohs.db` (SQLite) and `data/validation_report.md`.
No Docker/deploy in this phase.

## Setup

Requires Python 3.11+ (built with 3.12).

```bash
cd pharaohs-lab
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # optional; only cache TTL / UA overrides
```

## Run

```bash
.venv/bin/python -m etl.run                   # everything (first run ~35 min, mostly polite waiting; reruns ~1-4 min from cache)
.venv/bin/python -m etl.run --only fotmob_egypt
.venv/bin/python -m etl.run --only statsbomb --only validate
.venv/bin/python -m etl.run --limit 5         # smoke test: cap items per step
```

Steps, in order: `wikipedia`, `fotmob_egypt`, `coaches`, `elo`, `fotmob_league`, `fotmob_players`, `statsbomb`, `wikidata`, `validate`.
`--only` is repeatable. Order matters (`coaches`/`elo` need matches, `fotmob_players` needs Egypt lineups).

The run is idempotent: every write is an upsert on a natural key, and per-match children (lineups, events) are replaced wholesale.
`data/row_counts.json` holds before/after counts of the last run; the validation report prints them (deltas are 0 on a re-run).

## Sources

| Source | Used for | Access |
|---|---|---|
| FotMob public pages (`__NEXT_DATA__` on `/match/<id>`, `/teams/..`, `/leagues/..`, `/players/..`) | Egypt matches since 2018-01-01, lineups, formations, coach, goals/cards/subs, shot maps + xG, Egyptian Premier League (id 519) current + previous season, club match minutes of Egypt-capped players | HTML pages; the signed `/api/*` endpoints are not used |
| Wikipedia (MediaWiki API) | Egypt results list (cross-check), Hossam Hassan tenure start + managerial record, coaching-history year ranges | API |
| eloratings.net (`Egypt.tsv`) | Pre-match Elo of Egypt and opponent | TSV |
| StatsBomb open data (GitHub) | AFCON 2023, all 52 matches: events, lineups; raw JSON kept in `data/statsbomb/` | raw.githubusercontent.com |
| Wikidata SPARQL | QIDs, Arabic names, DOB, Transfermarkt ids, citizenships for Egypt-sport footballers | SPARQL |

Tried and not usable: Sofascore (HTTP 403, not evaded), FotMob `/api/*` (needs signed headers), Transfermarkt and FBref (not needed in Phase 1).
See "Sources: failed / blocked" in `data/validation_report.md`.

Every ingested row carries `source`, `source_url`, `fetched_at` (per-row on lineups/events/matches/players/minutes) so each public page can cite its sources.

## Scraping policy

All HTTP goes through `etl/http.py`:

- descriptive User-Agent: `PharaohsLab/0.1 (+https://pharaohs.omarebaid.com)`
- at least 2 seconds between requests to the same host
- on-disk cache in `data/cache/` keyed by URL with a TTL (default 24h, longer for finished matches)
- retries with exponential backoff on 5xx/429/network errors
- a 401/403 or challenge page raises `Blocked`; we never spoof headers, rotate proxies, or use headless browsers. The step records the block and the report lists the fallback used.

We store only what analysis needs. The public site publishes derived and aggregated stats, never raw feeds.

StatsBomb open data: keep the "Data provided by StatsBomb" attribution on any page using it, and do not republish the raw events.

## Data sources & attribution

- **StatsBomb open data**: data provided by StatsBomb (https://github.com/statsbomb/open-data), used under their open data license; the "Data provided by StatsBomb" credit and logo requirements apply to anything published from it.
- **FotMob**: public match, team, league and player pages.
- **Wikipedia**: text and facts under CC BY-SA 4.0.
- **Wikidata**: structured data under CC0.
- **eloratings.net**: Elo ratings.

Raw scraped data (the database, HTTP cache, StatsBomb JSON, exports) is not redistributed in this repo. Only the hand-curated, source-cited files in `data/manual/` are committed; the rest is rebuilt locally by the ETL.

## Layout

```
etl/
  http.py        polite client (UA, rate limit, cache, retries)
  db.py          schema + upsert helpers + run log
  run.py         entrypoint
  validate.py    writes data/validation_report.md
  sources/       wikipedia, fotmob, elo, statsbomb, wikidata
  models/        empty until Phase 2
data/            pharaohs.db, cache/, statsbomb/ (all gitignored), validation_report.md
```

## Data notes

- `matches.source` is `fotmob` or `statsbomb`. Egypt's AFCON 2023 matches exist in both sources (separate rows, same scores); pick by `source` in queries.
- `matches.date` for FotMob is the UTC kickoff date (can be a day later than the local date). `neutral` is a heuristic (stadium country vs team names).
- `egypt_coach_id` + `coach_source`: `fotmob`/`statsbomb` = given by the source; `wikipedia_tenure`, `wikipedia_year`, `gap_fill_bracketed` = inferred where the source has none; NULL = ambiguous.
- Older FotMob matches (roughly pre-2023) have no formation/coach and name-only lineups; players are resolved via the events feed, else by name (`players.source = 'fotmob-legacy'`).
- `lineups.minutes_played` is nominal (sub minute vs 90, or 120 after extra time); NULL when the feed has a bench but no substitution data.
- `events_lite` goal rows carry `situation` and `xg` when a shot record matches; every shot is also a `shot` row. Own-goal `team_id` is the team of the player who scored it. xG differs by source (FotMob vs StatsBomb models).
- Shot maps/xG exist only for recent FotMob matches and for StatsBomb AFCON 2023; the Egyptian league has none.
- `opponent_elo` is the opponent's rating before the match (post-match rating minus the change reported by eloratings.net).
- `eligibility_evidence` is schema only (populated in Phase 2).
