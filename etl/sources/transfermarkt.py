"""Transfermarkt public profile pages (robots.txt allows `User-agent: *` everywhere). Polite via etl.http.
A 403/challenge raises http.Blocked and callers drop the source. Nothing here evades blocks."""
from __future__ import annotations

import html as _html
import re

from .. import http

BASE = "https://www.transfermarkt.com"


def _t(s: str) -> str:
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def profile_url(tm_id: str) -> str:
    return f"{BASE}/x/profil/spieler/{tm_id}"


def profile(tm_id: str, ttl: float = 7 * 24 * 3600) -> dict:
    h = http.get(profile_url(tm_id), ttl=ttl)
    if "data-header__label" not in h:
        raise http.Blocked(f"unexpected Transfermarkt page for {tm_id}")
    out: dict = {"tm_id": str(tm_id), "source_url": profile_url(tm_id)}

    def block(label: str, span: int = 900) -> str:
        i = h.find(label)
        return h[i:i + span] if i >= 0 else ""

    b = block("Citizenship:")
    end = b.find("</li>")
    b = b[:end] if end > 0 else b
    out["citizenships"] = re.findall(r'<img[^>]*title="([^"]+)"[^>]*flaggenrahmen', b)
    m = re.search(r'itemprop="birthPlace">(.*?)</span>', h, re.S)
    out["birthplace"] = _t(m.group(1)) if m else None
    bp = block("Place of birth:", 500)
    m = re.search(r'title="([^"]+)"', bp)
    out["birth_country"] = m.group(1) if m else None
    m = re.search(r'itemprop="birthDate"[^>]*>\s*(\d\d)/(\d\d)/(\d{4})', h)
    out["dob"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None
    m = re.search(r'Position:\s*<span[^>]*>(.*?)</span>', h, re.S)
    out["position"] = _t(m.group(1)) if m else None
    m = re.search(r'data-header__club"[^>]*>\s*<a title="([^"]+)"', h)
    out["club"] = m.group(1) if m else None
    m = re.search(r'data-header__league-link"[^>]*>.*?title="([^"]*)"', h, re.S)
    out["league"] = m.group(1) if m else None
    m = re.search(r'League level:.*?</span>\s*([A-Za-z ]+?)\s*</span>', h, re.S)
    out["league_level"] = _t(m.group(1)) if m else None
    m = re.search(r'League level:.*?title="([^"]+)"', h, re.S)
    out["league_country"] = m.group(1) if m else None   # flag next to the league level = country of the league
    m = re.search(r'Current international:.*?title="([^"]+)"', h, re.S)
    out["current_international"] = m.group(1) if m else None
    m = re.search(r'Caps/Goals:.*?>\s*(\d+)\s*</a>', h, re.S)
    out["caps"] = int(m.group(1)) if m else None
    m = re.search(r'Market value.*?data-header__market-value-wrapper">\s*(.*?)\s*<span', h, re.S)
    out["market_value"] = _t(m.group(1)) if m else None
    return out
