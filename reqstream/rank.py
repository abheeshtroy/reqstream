"""
Weighted scoring of gated records against a structured profile.

The one opinionated choice worth explaining: **recency is worth 3 points, not
30.** Most job tooling sorts by date, which quietly encodes the assumption that
an older posting is a worse posting. It isn't. Age is a proxy for how much
competition a requisition has accumulated, not for whether it is still open. A
45-day-old req that matches well beats a same-day req that doesn't, and sorting
by date throws away most of the corpus for no reason.
"""
from __future__ import annotations

import datetime
import re

DEFAULT_STRONG = [
    "ai", "llm", "agent", "machine learning", "ml", "backend", "full stack",
    "fullstack", "platform", "infrastructure", "distributed",
    "forward deployed", "member of technical staff", "data pipeline",
]
DEFAULT_ENTRY = [
    "new grad", "newgrad", "graduate", "early career", "entry level",
    "university", "campus", "engineer i", "engineer 1", "associate",
    "junior", "jr",
]


def _compile(words):
    return re.compile("|".join(re.escape(w) for w in words), re.I) if words else None


def _future_cohort(years_ahead: int = 1):
    y = datetime.date.today().year
    return re.compile("|".join(str(y + n) for n in range(1, years_ahead + 3)))


def rank(records, profile: dict, today: datetime.date | None = None) -> list:
    """Attach a 0-100 `score` to each record and return them sorted descending."""
    today = today or datetime.date.today()
    kw = profile.get("keywords", {})
    strong = _compile(kw.get("strong") or DEFAULT_STRONG)
    entry = _compile(kw.get("entry") or DEFAULT_ENTRY)
    negative = _compile(kw.get("negative") or [])

    geo = profile.get("geography", {})
    preferred = [p.lower() for p in geo.get("preferred", [])]
    secondary = [p.lower() for p in geo.get("secondary", [])]

    comp = profile.get("compensation", {})
    floor = comp.get("floorUSD") or 0
    cohort = _future_cohort()

    for j in records:
        title = j.get("title") or ""
        loc = (j.get("location") or "").lower()
        s = 50

        if strong and strong.search(title):
            s += 18
        if entry and entry.search(title):
            s += 10
        if negative and negative.search(title):
            s -= 15
        if (j.get("skill_level") or "").lower() == "entry":
            s += 6

        # A requisition addressed to a future graduating class is a near-certain
        # reject for someone who has already graduated. Score it down; do not
        # gate it, because cohort language is often loose.
        if cohort.search(title):
            s -= 22

        med = (j.get("salary") or {}).get("median")
        if isinstance(med, (int, float)) and floor:
            if med >= floor * 1.8:
                s += 8
            elif med >= floor * 1.4:
                s += 5
            elif med >= floor * 1.1:
                s += 2

        if any(p in loc for p in preferred):
            s += 7
        elif "remote" in loc:
            s += 4
        elif any(p in loc for p in secondary):
            s += 3

        first_seen = (j.get("first_seen") or "")[:10]
        if first_seen:
            try:
                age = (today - datetime.date.fromisoformat(first_seen)).days
                s += 3 if age <= 7 else (1 if age <= 21 else 0)
            except ValueError:
                pass

        j["score"] = max(0, min(100, s))

    records.sort(key=lambda r: (-r["score"], r.get("first_seen") or ""))
    return records
