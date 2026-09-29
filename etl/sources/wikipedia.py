"""Wikipedia: Egypt fixture list (cross-check) + Hossam Hassan managerial record."""
from __future__ import annotations

import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from .. import http

API = "https://en.wikipedia.org/w/api.php"
RESULT_PAGES = [
    "Egypt national football team results (2000–2019)",
    "Egypt national football team results (2020–present)",
]


def _page_url(title: str) -> str:
    return "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")


def _html(title: str, ttl=None) -> str:
    t = http.get(API, params={"action": "parse", "page": title, "prop": "text", "format": "json", "formatversion": 2}, ttl=ttl)
    return json.loads(t)["parse"]["text"]


def _txt(el) -> str:
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip() if el else ""


def _parse_date(txt: str, year: int | None):
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)(?:\s+(\d{4}))?", txt)
    if not m:
        return None
    y = int(m.group(3)) if m.group(3) else year
    try:
        return datetime.strptime(f"{m.group(1)} {m.group(2)} {y}", "%d %B %Y").date().isoformat()
    except Exception:
        return None


def fetch_fixtures(since="2018-01-01") -> list[dict]:
    out = []
    for title in RESULT_PAGES:
        soup = BeautifulSoup(_html(title), "lxml")
        url = _page_url(title)
        for box in soup.select("table.vevent"):
            hd = box.find_previous(["h2", "h3"])
            ym = re.search(r"(\d{4})", _txt(hd)) if hd else None
            year = int(ym.group(1)) if ym else None
            tr = box.find("tr")
            tds = tr.find_all("td", recursive=False) if tr else []
            if len(tds) < 5:
                continue
            date_span = tds[0].find("span")
            date = _parse_date(_txt(date_span), year)
            comp = _txt(tds[0].find("small"))
            home, score, away, venue = _txt(tds[1]), _txt(tds[2]), _txt(tds[3]), _txt(tds[4])
            m = re.match(r"^(\d+)\s*[–-]\s*(\d+)", score)
            if not date or not m or date < since:
                continue
            out.append(dict(date=date, home=home, away=away, home_score=int(m.group(1)), away_score=int(m.group(2)),
                            score_raw=score, competition=comp, venue=venue, source_url=url))
    return out


def hassan_record() -> dict:
    """Parse the 'Managerial record by team and tenure' table on Hossam Hassan's page."""
    title = "Hossam Hassan"
    soup = BeautifulSoup(_html(title), "lxml")
    res = dict(source_url=_page_url(title), found=False)
    for tb in soup.select("table.wikitable"):
        cap = _txt(tb.find("caption"))
        if "Managerial record" not in cap:
            continue
        for tr in tb.find_all("tr"):
            cells = [_txt(c) for c in tr.find_all(["th", "td"])]
            if cells and cells[0].startswith("Egypt"):
                nums = re.findall(r"\d+", " ".join(cells[1:]))
                res.update(found=True, row=cells)
                # columns: Team | From | To | P | W | D | L | ...
                m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", cells[1]) if len(cells) > 1 else None
                if m:
                    res["from"] = datetime.strptime(m.group(1), "%d %B %Y").date().isoformat()
                # first plain integer after the date columns = matches played
                ints = [c for c in cells[2:] if re.fullmatch(r"\d+", c)]
                if ints:
                    res["played"] = int(ints[0])
    return res


def coaching_history() -> list[dict]:
    """[{name, from_year, to_year}] from the 'Coaching history' navbox on the Egypt national team page."""
    title = "Egypt national football team"
    soup = BeautifulSoup(_html(title), "lxml")
    txt = re.sub(r"\s+", " ", soup.get_text(" "))
    out = []
    i = txt.find("Coaching history")
    seg = txt[i:i + 4000] if i >= 0 else txt
    j = seg.find("Hussein Labib")
    if j < 0:
        j = 0
    for m in re.finditer(r"([A-ZÁ-Ž][^()\d\[\]]{3,40}?)\s*\(([\d–,\s\-]+(?:present)?)\)", seg[j:]):
        yrs = [int(y) for y in re.findall(r"\d{4}", m.group(2))]
        if not yrs or min(yrs) < 2010:
            continue
        to = 9999 if "present" in m.group(2) else max(yrs)
        out.append(dict(name=m.group(1).strip().split("  ")[-1].strip(), from_year=min(yrs), to_year=to, source_url=_page_url(title)))
    return out
