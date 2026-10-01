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

# Matched against the corpus `company` field, which is an ATS tenant slug and
# not always the trading name. `globalhr` is RTX's tenant; without it every RTX
# requisition reached the shortlist and was gated by hand.
DEFENSE_PRIMES = re.compile(
    r"^(raytheon|rtx|globalhr|northrop|lockheed|l3harris|general dynamics"
    r"|leidos|booz|caci|saic|mantech|peraton|sierra nevada|aerovironment|v2x"
    r"|saalex|base-2|captivation|parsons|mitre|johns hopkins|draper|aerospacecorp)",
    re.I)

# Country and city names that positively indicate a location outside the US.
#
# Indian metros are listed individually: the corpus frequently gives a bare city
# with no country, so matching on "India" alone let Hyderabad, Bengaluru, Pune
# and Chennai roles through to the shortlist, where they had to be removed by
# hand on three separate runs. Same reasoning for the bare region names.
NON_US = re.compile(
    r"India|Hyderabad|Bengaluru|Bangalore|Pune|Chennai|Mumbai|Delhi|Gurgaon"
    r"|Gurugram|Noida|Kolkata|Ahmedabad|Jaipur|Kochi|Coimbatore|Trivandrum"
    r"|Thiruvananthapuram|Indore|Chandigarh|Mysore|Mysuru"
    r"|China|Shanghai|Beijing|Shenzhen|Canada|Toronto|Vancouver|Montreal|Ottawa"
    r"|London|United Kingdom|\bUK\b|Ireland|Dublin|Germany|Berlin|Munich"
    r"|France|Paris|Netherlands|Amsterdam|Poland|Warsaw|Krakow|Spain|Madrid"
    r"|Barcelona|Portugal|Lisbon|Brazil|Mexico|Japan|Tokyo|Singapore"
    r"|Australia|Sydney|Melbourne|Israel|Tel Aviv|Philippines|Manila|Ukraine"
    r"|Romania|Bulgaria|Czech|Prague|Sweden|Stockholm|Norway|Denmark"
    r"|Copenhagen|Switzerland|Zurich|Korea|Seoul|Taiwan|Hong Kong|Vietnam"
    r"|Indonesia|Thailand|Malaysia|Argentina|Colombia|Chile|Peru|Egypt"
    r"|Nigeria|Kenya|South Africa|Turkey|Istanbul|Dubai|UAE|Saudi|Pakistan"
    r"|Bangladesh|Sri Lanka|Nepal|Costa Rica|Uruguay|Bogota"
    r"|\bEurope\b|\bEMEA\b|\bAPAC\b|\bLATAM\b|Latin America",
    re.I)

US_HINT = re.compile(
    r"\b(AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN"
    r"|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA"
    r"|WA|WV|WI|WY|DC)\b"
    r"|United States|USA|U\.S\.|New York|San Francisco|Bay Area|Seattle|Austin"
    r"|Boston|Chicago|Los Angeles|Denver|Atlanta|Palo Alto|Mountain View"
    r"|Sunnyvale|Redwood City|San Jose|Cambridge|Brooklyn|Remote", re.I)

SENIOR_BANDS = {"senior", "lead", "principal", "executive"}

# Requisition-id patterns per ATS. The same job listed on a company board and on
# an aggregator has two different URLs and slips past URL dedupe; the req id is
# the same in both. Observed twice in one run before this existed.
# (family, pattern). The family label matters: an embedded Greenhouse widget and
# a Greenhouse board URL carry the same requisition id in different shapes, so
# both must key to "gh" or they never collapse.
_REQ_PATTERNS = (
    ("gh", re.compile(r"[?&]gh_jid=(\d+)", re.I)),
    ("gh", re.compile(r"greenhouse\.io/(?:[^/]+/)*jobs/(\d+)", re.I)),
    ("lever", re.compile(r"lever\.co/[^/]+/([0-9a-f-]{36})", re.I)),
    ("ashby", re.compile(r"ashbyhq\.com/[^/]+/([0-9a-f-]{36})", re.I)),
    ("wd", re.compile(r"myworkdayjobs\.com/.*?[_/](R-?\d{4,})", re.I)),
    ("sr", re.compile(r"smartrecruiters\.com/[^/]+/(\d{6,})", re.I)),
)


def req_key(url: str):
    """Return an ATS-stable requisition key for `url`, or None if unrecognised.

    None means "no opinion" and the caller falls back to URL matching. It must
    never collapse two genuinely different jobs, so an unmatched URL is left
    alone rather than guessed at.
    """
    if not url:
        return None
    for family, pat in _REQ_PATTERNS:
        m = pat.search(url)
        if m:
            return f"{family}:{m.group(1).lower()}"
    return None

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
        seen_reqs = {k for k in (req_key(u) for u in seen_urls) if k}
        seen_now, reqs_now = set(), set()

        for j in records:
            title = (j.get("title") or "").strip()
            loc = j.get("location") or ""
            url = j.get("url")
            company = j.get("company") or ""
            rk = req_key(url)

            if not url or not title:
                rep.reasons["record missing url or title"] += 1;      continue
            if url in seen_now:
                rep.reasons["duplicate url"] += 1;                    continue
            if rk and rk in reqs_now:
                rep.reasons["duplicate req id, different url"] += 1;  continue
            if url in seen_urls:
                rep.reasons["already seen"] += 1;                     continue
            if rk and rk in seen_reqs:
                rep.reasons["already seen, different url"] += 1;      continue
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
            # Compensation is deliberately NOT gated here. The corpus `salary`
            # field is the scraper's inference, sometimes from a sample of
            # eight postings, not the employer's posted range. Excluding on it
            # dropped roughly 700 roles a run on a guess. It is scored in
            # rank.py instead, where being wrong costs a few points rather
            # than the whole job.

            seen_now.add(url)
            if rk:
                reqs_now.add(rk)
            rep.kept.append(j)

        return rep
