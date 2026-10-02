"""Publish step: copy an ALLOWLIST of public JSON from data/export/ to data/public/.

    python -m etl.publish

Only files named in PUBLIC are ever copied (domestic_uncapped.json, private data, summaries and
raw feeds are deliberately absent). Each copy is written to a temp name then renamed, so the web
container never serves a half-written file. Also writes data/public/meta.json.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXPORT = ROOT / "data" / "export"
PUBLIC = ROOT / "data" / "public"
DB = ROOT / "data" / "pharaohs.db"

# module name (as in etl.run.MODELS) -> public file
PUBLIC_FILES = {
    "coach_hassan": "coach_hassan.json",
    "salah_succession": "salah_succession.json",
    "diaspora_scout": "diaspora_candidates.json",
    "opponent_dossiers": "opponent_dossiers.json",
    "set_pieces": "set_pieces.json",
    "load_tracker": "load_tracker.json",
}

SOURCES = [
    {"name": "FotMob", "url": "https://www.fotmob.com/", "use": "Egypt matches, lineups, shot maps, league and player pages"},
    {"name": "Wikipedia", "url": "https://en.wikipedia.org/", "use": "Results cross-check, coach record, biography", "license": "CC BY-SA 4.0"},
    {"name": "Wikidata", "url": "https://www.wikidata.org/", "use": "Player identifiers, birth dates, citizenships, venue coordinates", "license": "CC0"},
    {"name": "eloratings.net", "url": "https://www.eloratings.net/", "use": "Pre-match Elo ratings"},
    {"name": "StatsBomb open data", "url": "https://github.com/statsbomb/open-data", "use": "AFCON 2023 event data", "license": "StatsBomb open data terms; attribution required"},
    {"name": "Transfermarkt", "url": "https://www.transfermarkt.com/", "use": "Diaspora candidate profiles (public pages)"},
    {"name": "FIFA", "url": "https://www.fifa.com/", "use": "Eligibility regulations (cited PDFs)"},
]


def _atomic_copy(src: Path, dst: Path) -> None:
    tmp = dst.with_name(f".{dst.name}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)


def _atomic_write(dst: Path, text: str) -> None:
    tmp = dst.with_name(f".{dst.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, dst)


def _last_runs() -> dict:
    out: dict = {}
    if not DB.exists():
        return out
    con = sqlite3.connect(DB)
    try:
        rows = con.execute(
            "SELECT step, status, finished_at FROM ingest_runs WHERE step LIKE 'model:%' "
            "AND id IN (SELECT MAX(id) FROM ingest_runs WHERE step LIKE 'model:%' GROUP BY step)").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        con.close()
    for step, status, fin in rows:
        out[step.split(":", 1)[1]] = {"status": status, "finished_at": fin}
    return out


def main() -> int:
    PUBLIC.mkdir(parents=True, exist_ok=True)
    runs = _last_runs()
    modules = {}
    for name, fname in PUBLIC_FILES.items():
        src = EXPORT / fname
        info = {"file": fname, "last_run": runs.get(name)}
        if not src.exists():
            info["status"] = "missing"
        else:
            try:
                data = json.loads(src.read_text(encoding="utf-8"))   # refuse to publish invalid JSON
            except ValueError as e:
                info["status"] = "invalid"
                info["error"] = str(e)[:200]
                modules[name] = info
                print(f"skip {fname}: invalid JSON ({e})", file=sys.stderr)
                continue
            _atomic_copy(src, PUBLIC / fname)
            m = data.get("meta", data) if isinstance(data, dict) else {}
            info["as_of"] = m.get("as_of") if isinstance(m, dict) else None
            info["exported_at"] = datetime.fromtimestamp(src.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            info["status"] = "ok" if not runs.get(name) or runs[name]["status"] == "ok" else "stale"
            if info["status"] == "stale":
                info["note"] = "last run did not succeed; previous export is being served"
        modules[name] = info
    meta = {
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "modules": modules,
        "sources": SOURCES,
    }
    _atomic_write(PUBLIC / "meta.json", json.dumps(meta, indent=1, ensure_ascii=False) + "\n")
    # remove anything in data/public that is not on the allowlist (keeps the served dir clean)
    keep = set(PUBLIC_FILES.values()) | {"meta.json"}
    for p in PUBLIC.iterdir():
        if p.is_file() and p.name not in keep and not p.name.endswith(".tmp"):
            p.unlink()
    print("published:", ", ".join(f"{k}={v['status']}" for k, v in modules.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
