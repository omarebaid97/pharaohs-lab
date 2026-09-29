"""SQLite schema + helpers. All writes are upserts keyed on natural keys => idempotent."""
from __future__ import annotations

import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "pharaohs.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS coaches (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  name_ar TEXT,
  nationality TEXT,
  fotmob_id INTEGER,
  team TEXT,                 -- team coached (e.g. Egypt)
  tenure_start TEXT,         -- ISO date
  tenure_end TEXT,
  source TEXT, source_url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS teams (
  id INTEGER PRIMARY KEY,
  fotmob_id INTEGER UNIQUE,
  statsbomb_id INTEGER UNIQUE,
  name TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'national',   -- national | club
  country TEXT,
  source TEXT, source_url TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_teams_name ON teams(name, kind);
CREATE TABLE IF NOT EXISTS clubs (
  id INTEGER PRIMARY KEY,
  fotmob_id INTEGER UNIQUE,
  transfermarkt_id TEXT,
  name TEXT NOT NULL,
  country TEXT,
  team_id INTEGER REFERENCES teams(id),
  source TEXT, source_url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS players (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  name_ar TEXT,
  dob TEXT,
  position TEXT,
  nationality TEXT,          -- primary (as listed by the source)
  nationality_code TEXT,
  other_nationalities TEXT,  -- e.g. from Wikidata citizenship, comma separated
  birth_country TEXT,
  wikidata_qid TEXT,
  transfermarkt_id TEXT,
  fotmob_id INTEGER UNIQUE,
  statsbomb_id INTEGER UNIQUE,
  current_club_id INTEGER REFERENCES clubs(id),
  source TEXT, source_url TEXT, fetched_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_players_name ON players(name);
CREATE TABLE IF NOT EXISTS matches (
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,                -- fotmob | statsbomb
  source_match_id TEXT NOT NULL,
  scope TEXT NOT NULL,                 -- national | egyptian_league
  date TEXT NOT NULL,                  -- ISO date (UTC)
  kickoff_utc TEXT,
  competition TEXT, stage TEXT, season TEXT,
  home_team_id INTEGER REFERENCES teams(id),
  away_team_id INTEGER REFERENCES teams(id),
  home_score INTEGER, away_score INTEGER,
  decided_by TEXT,                     -- NULL | aet | pens
  pens_home INTEGER, pens_away INTEGER,
  venue TEXT, venue_city TEXT, venue_country TEXT,
  neutral INTEGER,                     -- 1 neutral, 0 not, NULL unknown (heuristic, see README)
  is_egypt INTEGER NOT NULL DEFAULT 0, -- Egypt national team played
  egypt_side TEXT,                     -- home | away
  egypt_coach_id INTEGER REFERENCES coaches(id),
  coach_source TEXT,                   -- fotmob | statsbomb | gap_fill_bracketed | wikipedia_year
  formation_home TEXT, formation_away TEXT,
  formation_egypt TEXT, formation_opp TEXT,
  opponent_elo INTEGER,                -- eloratings.net rating of opponent BEFORE the match
  egypt_elo INTEGER,
  has_lineup INTEGER DEFAULT 0, has_events INTEGER DEFAULT 0,
  source_url TEXT, fetched_at TEXT,
  UNIQUE(source, source_match_id)
);
CREATE INDEX IF NOT EXISTS ix_matches_date ON matches(date);
CREATE TABLE IF NOT EXISTS lineups (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES matches(id),
  player_id INTEGER NOT NULL REFERENCES players(id),
  team_id INTEGER REFERENCES teams(id),
  team_side TEXT,                      -- home | away
  started INTEGER NOT NULL,
  position TEXT,                       -- role/slot label
  slot TEXT,                           -- source slot id (FotMob positionId / StatsBomb position id)
  shirt TEXT,
  minutes_played REAL,                 -- approximate: nominal sub minute vs actual match length
  sub_on_minute INTEGER, sub_off_minute INTEGER,
  captain INTEGER,
  rating REAL,
  source TEXT, source_url TEXT, fetched_at TEXT,
  UNIQUE(match_id, player_id)
);
CREATE TABLE IF NOT EXISTS events_lite (
  id INTEGER PRIMARY KEY,
  match_id INTEGER NOT NULL REFERENCES matches(id),
  event_key TEXT NOT NULL,             -- deterministic per-source key
  minute INTEGER, added_minute INTEGER,
  type TEXT NOT NULL CHECK (type IN ('goal','own_goal','pen_goal','yellow','red','sub_on','sub_off','shot')),
  team_id INTEGER REFERENCES teams(id),
  player_id INTEGER REFERENCES players(id),
  related_player_id INTEGER REFERENCES players(id),
  situation TEXT CHECK (situation IN ('open_play','corner','free_kick','penalty','throw_in','unknown')),
  outcome TEXT,                        -- shots: goal/saved/blocked/off_target/post
  xg REAL,
  source TEXT, source_url TEXT, fetched_at TEXT,
  UNIQUE(match_id, event_key)
);
CREATE TABLE IF NOT EXISTS player_club_minutes (
  id INTEGER PRIMARY KEY,
  player_id INTEGER NOT NULL REFERENCES players(id),
  club_id INTEGER REFERENCES clubs(id),
  season TEXT, competition TEXT, competition_id INTEGER,
  date TEXT,
  source_match_id TEXT NOT NULL,
  opponent TEXT,
  minutes INTEGER,
  started INTEGER,
  goals INTEGER, assists INTEGER, yellow INTEGER, red INTEGER,
  source TEXT, source_url TEXT, fetched_at TEXT,
  UNIQUE(player_id, source_match_id)
);
CREATE TABLE IF NOT EXISTS eligibility_evidence (   -- populated in Phase 2
  id INTEGER PRIMARY KEY,
  player_id INTEGER NOT NULL REFERENCES players(id),
  claim TEXT NOT NULL,
  evidence_text TEXT,
  source_url TEXT,
  confidence TEXT CHECK (confidence IN ('documented','inferred')),
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS wiki_fixtures (        -- Wikipedia results list, used to cross-check FotMob
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL, home TEXT NOT NULL, away TEXT NOT NULL,
  home_score INTEGER, away_score INTEGER, score_raw TEXT,
  competition TEXT, venue TEXT,
  source_url TEXT, fetched_at TEXT,
  UNIQUE(date, home, away)
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT, source_url TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS ingest_runs (
  id INTEGER PRIMARY KEY,
  step TEXT NOT NULL, started_at TEXT, finished_at TEXT,
  status TEXT,                          -- ok | partial | failed | blocked
  rows_written INTEGER, http_network INTEGER, http_cache_hits INTEGER,
  notes TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path: Path | str = DB_PATH) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    con.commit()


def upsert(con, table: str, key_cols: list[str], row: dict, *, keep_existing: tuple = ()) -> int:
    """INSERT .. ON CONFLICT(key) DO UPDATE. Returns row id.

    NULL values never overwrite existing non-NULL values (COALESCE), so a sparse source
    can't erase what a richer one wrote. Columns in keep_existing are never overwritten.
    """
    cols = list(row)
    ph = ",".join("?" for _ in cols)
    upd = [
        f"{c}=COALESCE(excluded.{c}, {table}.{c})"
        for c in cols
        if c not in key_cols and c not in keep_existing
    ]
    sql = f"INSERT INTO {table} ({','.join(cols)}) VALUES ({ph}) ON CONFLICT({','.join(key_cols)}) DO "
    sql += ("UPDATE SET " + ",".join(upd)) if upd else "NOTHING"
    con.execute(sql, [row[c] for c in cols])
    where = " AND ".join(f"{k}=?" for k in key_cols)
    return con.execute(f"SELECT rowid FROM {table} WHERE {where}", [row[k] for k in key_cols]).fetchone()[0]


def norm(s: str | None) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower()
    return " ".join("".join(c if c.isalnum() else " " for c in s).split())


def season_of(date_iso: str) -> str:
    y, m = int(date_iso[:4]), int(date_iso[5:7])
    return f"{y}/{y+1}" if m >= 7 else f"{y-1}/{y}"


def counts(con) -> dict[str, int]:
    tabs = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tabs if t != "ingest_runs"}


class Run:
    """Context manager logging one step into ingest_runs."""

    def __init__(self, con, step: str):
        self.con, self.step = con, step
        self.notes: list[str] = []
        self.status = "ok"
        self.rows = 0

    def __enter__(self):
        from . import http
        self._n0, self._c0 = http.STATS["network"], http.STATS["cache_hits"]
        self.started = now()
        return self

    def note(self, msg: str, status: str | None = None):
        self.notes.append(msg)
        if status and not (self.status == "failed"):
            self.status = status

    def __exit__(self, et, ev, tb):
        from . import http
        if et is not None:
            self.status = "failed"
            self.notes.append(f"{et.__name__}: {ev}")
        self.con.execute(
            "INSERT INTO ingest_runs(step,started_at,finished_at,status,rows_written,http_network,http_cache_hits,notes) VALUES (?,?,?,?,?,?,?,?)",
            (self.step, self.started, now(), self.status, self.rows,
             http.STATS["network"] - self._n0, http.STATS["cache_hits"] - self._c0, "\n".join(self.notes)[:8000]),
        )
        self.con.commit()
        return False  # propagate exceptions
