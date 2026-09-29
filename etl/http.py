"""Shared polite HTTP client. Every outbound request in the ETL goes through here.

- descriptive User-Agent
- >= MIN_INTERVAL seconds between requests to the same host
- on-disk cache keyed by URL with a TTL
- retries with exponential backoff (5xx / 429 / network errors)
- 403 / challenge pages are NOT evaded: they raise Blocked and the caller falls back.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "data" / "cache"
USER_AGENT = os.environ.get(
    "PHARAOHS_UA", "PharaohsLab/0.1 (+https://pharaohs.omarebaid.com)"
)
MIN_INTERVAL = 2.0
DEFAULT_TTL = float(os.environ.get("PHARAOHS_CACHE_TTL_HOURS", "24")) * 3600
MAX_RETRIES = 3

_last_hit: dict[str, float] = {}
_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "en"})

# Per-run stats consumed by validate.py / ingest_runs
STATS = {"network": 0, "cache_hits": 0}
BLOCKED_HOSTS: dict[str, str] = {}


class Blocked(Exception):
    """Site refused us (403/anti-bot). Do not retry or evade."""


class NotFound(Exception):
    pass


def _cache_path(url: str) -> Path:
    host = urlparse(url).netloc.replace(":", "_")
    h = hashlib.sha256(url.encode()).hexdigest()[:40]
    return CACHE_DIR / host / f"{h}.json"


def get(url: str, *, ttl: float | None = None, params: dict | None = None, store: bool = True) -> str:
    """Return response text (cached). Raises Blocked / NotFound / requests errors."""
    ttl = DEFAULT_TTL if ttl is None else ttl
    if params:
        url = requests.Request("GET", url, params=params).prepare().url
    host = urlparse(url).netloc
    p = _cache_path(url)
    if p.exists() and ttl > 0 and time.time() - p.stat().st_mtime < ttl:
        STATS["cache_hits"] += 1
        return json.loads(p.read_text())["body"]

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        wait = MIN_INTERVAL - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        STATS["network"] += 1
        try:
            r = _session.get(url, timeout=30)
        except requests.RequestException as e:
            last_err = e
            time.sleep(2 ** (attempt + 1))
            continue
        if r.status_code in (401, 403):
            BLOCKED_HOSTS[host] = f"HTTP {r.status_code}"
            raise Blocked(f"{host} returned {r.status_code} for {url}")
        if r.status_code == 404:
            raise NotFound(url)
        if r.status_code == 429 or r.status_code >= 500:
            last_err = RuntimeError(f"HTTP {r.status_code}")
            time.sleep(min(60, 5 * 2 ** attempt))
            continue
        r.raise_for_status()
        if store:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"url": url, "fetched_at": time.time(), "body": r.text}))
        return r.text
    raise RuntimeError(f"giving up on {url}: {last_err}")


def get_json(url: str, **kw):
    return json.loads(get(url, **kw))
