# Pharaohs Lab - data foundation (Phase 1)

Public analytics site about the Egypt men's national football team, built only on publicly available data.
The ETL builds `data/pharaohs.db` (SQLite) and `data/validation_report.md`; six analytics modules export JSON; a publish step copies the public subset to `data/public/`; a static site in `site/` reads it.

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
  models/        coach_hassan, salah_succession, diaspora_scout, opponent_dossiers, set_pieces, load_tracker
  publish.py     allowlisted copy data/export -> data/public (+ meta.json)
  scheduler.py   container entrypoint: daily run at 04:00, plus once at startup if needed
site/            Vite + vanilla JS + Chart.js static site (UI strings in src/i18n/en.json)
Dockerfile.web, Dockerfile.etl, docker-compose.yml, nginx.conf
data/            pharaohs.db, cache/, statsbomb/, export/, public/, private/ (all gitignored), manual/ (committed CSVs)
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

## Set-piece routine tagging (module E)

`data/manual/set_piece_routines.csv` is a hand-filled log of set-piece routines seen on video or in match reports; `python -m etl.models.set_pieces` merges any rows into `data/export/set_pieces.json` (`manual_routines`) and counts them in the summary. It is never blended into the automated counts.

- One row per set piece. Columns: `match_date` (YYYY-MM-DD, as in `matches.date`), `opponent`, `minute`, `situation` (corner / free_kick_direct / free_kick_indirect / throw_in / penalty), `routine_description` (short free text: delivery type, target zone, blocker/screen, short-corner pattern, who takes and who attacks the ball), `outcome` (goal / shot / cleared / won_foul / other), `video_or_source_url` (public link with timestamp, e.g. YouTube `?t=` or a match-report URL).
- Keep the header row unchanged; quote fields containing commas. Cite only public sources; do not paste or upload footage.
- Re-run the module after editing. Rows are merged verbatim (whitespace trimmed).

## Pipeline

```bash
.venv/bin/python -m etl.run          # ingest steps, then the six models in dependency order
.venv/bin/python -m etl.publish      # data/export -> data/public (allowlist) + meta.json
```

Models run in this order: `coach_hassan`, `diaspora_scout`, `salah_succession`, `opponent_dossiers`, `set_pieces`, `load_tracker` (the diaspora scout runs before the Salah model because the Salah board reads its output). `--only` accepts ingest steps and model names, repeatable; models always run in the order above. A failing model is logged in `ingest_runs` (step `model:<name>`), its previous export is restored, and the other models still run. `PHARAOHS_AS_OF=YYYY-MM-DD` overrides "today" for the models.

Only the files listed in `PUBLIC_FILES` in `etl/publish.py` are published; `data/private/` and `domestic_uncapped.json` never are. Files are written to a temp name and renamed, so readers never see a partial file. `data/public/meta.json` carries the last-updated time, per-module status (`stale` means the last run failed and the previous export is being served) and the source list.

## Development (site)

```bash
cd site
npm install
npm run build                          # -> site/dist
ln -sfn ../../data/public dist/data    # let the preview serve the published JSON at /data
npm run preview                        # http://127.0.0.1:4173
```

`npm run dev` also works if `data/public` is linked at `site/public/data` (do not commit that link). The site is plain DOM: data strings are only ever set as text, and only `http(s)` URLs become links. UI strings live in `site/src/i18n/en.json`.

## Deploy

```bash
cp .env.example .env      # set TZ (the ETL runs daily at 04:00 in this zone)
docker compose up -d --build
```

- `pharaohs-lab-web` serves the built site with nginx and mounts `./data/public` read-only at `/data/`. It publishes no host port; put your reverse proxy in front of port 80 on the compose network.
- `pharaohs-lab-etl` mounts `./data`, runs `python -m etl.run && python -m etl.publish` daily at 04:00 (container TZ), and once at startup if `data/public/meta.json` is missing. The first full run takes a while because of the polite request rate; later runs mostly hit the cache.
- The repository must be checked out with `data/manual/` present (it is committed). Everything else under `data/` is generated.
