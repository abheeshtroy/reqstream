"""
Hard exclusion rules.

Two principles, both of which exist because the naive version got them wrong.

**Explicit signal only.** A gate fires on something the posting actually says.
A missing years-of-experience field does not mean the role is senior; a blank
location does not mean it is overseas. Absence is never evidence. This keeps
recall high at the cost of some noise, which is the right trade when the
downstream cost of a false negative (never seeing a job) far exceeds the cost
of a false positive (ranking it and skipping it).

**Every rejection is recorded with its reason.** A filter you cannot audit is
a filter you cannot debug or defend. `GateReport` carries the counts, so any
run can answer "why did I only see 400 of 1.5M" precisely.
"""
from __future__ import annotations

import collections
import re
from dataclasses import dataclass, field

SOFTWARE_ROLE = re.compile(
    r"software engineer|software developer|\bswe\b|backend|back-end|front.?end"
    r"|full.?stack|platform engineer|infrastructure engineer|ai engineer"
    r"|\bml engineer|machine learning engineer|applied (ai|ml|scientist)"
    r"|member of technical staff|forward deployed|solutions engineer"
    r"|systems engineer|api engineer|data engineer|devops|site reliability"
    r"|\bsre\b|developer\b|programmer|engineer i+\b|engineer [12]\b", re.I)

CLEARANCE = re.compile(
    r"clearance|TS/SCI|polygraph|\bITAR\b|US citizen|U\.S\. citizen"
    r"|citizenship required|public trust", re.I)

DEFENSE_PRIMES = re.compile(
    r"^(raytheon|rtx|northrop|lockheed|l3harris|general dynamics|leidos|booz"
    r"|caci|saic|mantech|peraton|sierra nevada|aerovironment|v2x|saalex"
    r"|base-2|captivation|parsons|mitre|johns hopkins)", re.I)

# Country and city names that positively indicate a location outside the US.
NON_US = re.compile(
    r"India|China|Canada|Toronto|Vancouver|Montreal|Ottawa|London|United Kingdom"
    r"|Ireland|Dublin|Germany|Berlin|Munich|France|Paris|Netherlands|Amsterdam"
    r"|Poland|Warsaw|Krakow|Spain|Madrid|Barcelona|Portugal|Lisbon|Brazil|Mexico"
    r"|Japan|Tokyo|Singapore|Australia|Sydney|Melbourne|Israel|Tel Aviv"
    r"|Philippines|Manila|Ukraine|Romania|Bulgaria|Czech|Prague|Sweden|Stockholm"
    r"|Norway|Denmark|Copenhagen|Switzerland|Zurich|Korea|Seoul|Taiwan"
    r"|Hong Kong|Vietnam|Indonesia|Thailand|Malaysia|Argentina|Colombia|Chile"
    r"|Peru|Egypt|Nigeria|Kenya|South Africa|Turkey|Istanbul|Dubai|UAE|Saudi"
    r"|Pakistan|Bangladesh|Sri Lanka|Nepal|Costa Rica|Uruguay|Bogota|EMEA|APAC",
    re.I)

US_HINT = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN"
    r"|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA"
    r"|WA|WV|WI|WY|DC)\b"
    r"|United States|USA|U\.S\.|New York|San Francisco|Bay Area|Seattle|Austin"
    r"|Boston|Chicago|Los Angeles|Denver|Atlanta|Palo Alto|Mountain View"
    r"|Sunnyvale|Redwood City|San Jose|Cambridge|Brooklyn|Remote", re.I)

SENIOR_BANDS = {"senior", "lead", "principal", "executive"}

DEFAULT_REJECT_TITLES = [
    "senior", "sr.", "staff", "principal", "distinguished", "fellow", "lead",
    "manager", "director", "head of", "vp", "chief", "intern", "internship",
    "co-op", "coop", "apprentice", "trainee", "architect",
]


@dataclass
class GateReport:
    kept: list = field(default_factory=list)
    reasons: collections.Counter = field(default_factory=collections.Counter)

    def summary(self, top: int = 10) -> str:
        w = max((len(f"{v:,}") for v in self.reasons.values()), default=1)
        return "\n".join(f"  {v:>{w},}  {k}"
                         for k, v in self.reasons.most_common(top))


class Gate:
    """Applies hard exclusions from a profile's constraint block."""

    def __init__(self, profile: dict):
        sen = profile.get("seniority", {})
        geo = profile.get("geography", {})
        comp = profile.get("compensation", {})
        exc = profile.get("exclude", {})

        titles = sen.get("rejectTitles") or DEFAULT_REJECT_TITLES
        self.reject_title = re.compile(
            "|".join(r"\b" + re.escape(t) + r"\b" for t in titles), re.I)
        self.reject_roman = re.compile(r"\b(III|IV|V)\b|\blevel [3-9]\b|\bL[4-9]\b")

        self.us_only = "US" in (geo.get("include") or ["US"])
        self.floor = comp.get("floorUSD")
        self.block_clearance = exc.get("clearance", True)
        self.block_defense = exc.get("defensePrimes", True)
        self.block_staffing = exc.get("staffingAgencies", True)

    def apply(self, records, seen_urls=None) -> GateReport:
        rep = GateReport()
        seen_urls = set(seen_urls or ())
        seen_now = set()

        for j in records:
            title = (j.get("title") or "").strip()
            loc = j.get("location") or ""
            url = j.get("url")
            company = j.get("company") or ""

            if not url or not title:
                rep.reasons["record missing url or title"] += 1;      continue
            if url in seen_now:
                rep.reasons["duplicate url"] += 1;                    continue
            if url in seen_urls:
                rep.reasons["already seen"] += 1;                     continue
            if self.block_staffing and j.get("is_recruiter"):
                rep.reasons["staffing agency, no named end client"] += 1; continue
            if not SOFTWARE_ROLE.search(title):
                rep.reasons["not a software role"] += 1;              continue
            if self.reject_title.search(title) or self.reject_roman.search(title):
                rep.reasons["title out of band"] += 1;                continue
            if self.block_clearance and CLEARANCE.search(title):
                rep.reasons["clearance or citizenship required"] += 1; continue
            if self.block_defense and DEFENSE_PRIMES.match(company):
                rep.reasons["defense prime"] += 1;                    continue
            if (j.get("skill_level") or "").lower() in SENIOR_BANDS:
                rep.reasons["seniority band above target"] += 1;      continue
            if self.us_only:
                if NON_US.search(loc):
                    rep.reasons["outside target geography"] += 1;     continue
                # Blank location survives: absence is not evidence.
                if loc.strip() and not US_HINT.search(loc):
                    rep.reasons["location unresolved"] += 1;          continue
            if self.floor is not None:
                med = (j.get("salary") or {}).get("median")
                if isinstance(med, (int, float)) and med < self.floor:
                    rep.reasons["compensation below floor"] += 1;     continue

            seen_now.add(url)
            rep.kept.append(j)

        return rep
