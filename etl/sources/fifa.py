"""FIFA eligibility PDFs (digitalhub.fifa.com). etl.http.get is text-only, so binary PDFs are fetched here with
the same session, User-Agent, per-host spacing and on-disk cache. No block evasion: 401/403 raises http.Blocked."""
from __future__ import annotations

import time
from urllib.parse import urlparse

from .. import http

PDFS = {
    "commentary": "https://digitalhub.fifa.com/m/ccab990abf45fcf6/original/ro8mje8vw98yp3rvfbmi-pdf.pdf",
    "guide": "https://digitalhub.fifa.com/m/b98d35fc16dc274b/original/elcthdgwfgx7dcxdenas-pdf.pdf",
}


def fetch_pdf(url: str, ttl: float = 30 * 24 * 3600) -> bytes:
    host = urlparse(url).netloc
    p = http.CACHE_DIR / host / (http.hashlib.sha256(url.encode()).hexdigest()[:40] + ".pdf")
    if p.exists() and time.time() - p.stat().st_mtime < ttl:
        return p.read_bytes()
    wait = http.MIN_INTERVAL - (time.time() - http._last_hit.get(host, 0))
    if wait > 0:
        time.sleep(wait)
    http._last_hit[host] = time.time()
    r = http._session.get(url, timeout=60)
    if r.status_code in (401, 403):
        raise http.Blocked(f"{host} returned {r.status_code}")
    r.raise_for_status()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(r.content)
    return r.content


def pdf_text(url: str) -> str:
    import io
    from pypdf import PdfReader
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(fetch_pdf(url))).pages)
