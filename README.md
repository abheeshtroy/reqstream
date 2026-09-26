# reqstream

Streaming ingestion and ranking over open-requisition data.

Most job tooling searches a feed that somebody else already curated. `reqstream`
works from the raw corpus instead: it pulls ~1.5M live postings from distributed
ATS datasets, applies auditable exclusion rules, and ranks what survives against
a structured candidate profile.

```
$ reqstream ingest
manifest: 59 chunks, 1,458,032 records
parsed 1,458,032 records in 19.3s (0 retried)

$ reqstream rank --profile profile.json --top 400
scanned 1,458,032  ->  qualified 12,736  ->  returned 400
306 distinct companies
```

Six ATS families, **28,746 resolved tenants**, refreshed daily.

---

## Why this exists

The obvious way to collect job postings is to keep a list of company job-board
tokens and poll them. That approach has a ceiling you cannot raise: it only ever
sees companies somebody already thought to add. A hand-maintained list of fifty
tokens misses essentially the entire long tail, and no amount of diligence fixes
it, because the failure is enumeration itself.

`reqstream` treats coverage as a property of the index rather than of somebody's
list. It reads a web-scale corpus harvested from Common Crawl, so a company is
reachable whether or not anyone knew it existed.

The practical difference, measured: a curated feed surfaced roughly 260 roles in
a day. The same filters over the corpus surface **12,736 qualifying roles across
~2,800 companies**, including employers the curated feed never carried at all.

## How it works

```
ingest/   parallel chunked fetch of the corpus + ATS tenant resolution
gate/     hard exclusion rules, explicit signal only
rank/     weighted scoring against a structured profile
verify/   render -> extract -> diff generated documents vs source of truth
```

### Ingest

The corpus ships as 59 gzipped JSON chunks, 25,000 records each, ~76MB total.
Fetched twelve at a time, the download takes about **5 seconds**; decompressing
and parsing 1.46M records brings a cold `ingest` to roughly **19 seconds** end
to end. Chunks that fail a gzip integrity check are retried serially, and a
chunk that cannot be read after retry is reported rather than silently skipped.

A companion directory resolves **28,746 ATS tenant identifiers** across
Greenhouse, Ashby, Lever and Workday. Workday entries carry tenant *and* site
(`23andme|wd5|23`), which is what you need to construct a working URL — Workday
has no global index, so this is the only practical way to reach those boards
programmatically.

### Gate

Exclusion is **explicit-signal only**: a missing field never removes a record.
If a posting does not state a years-of-experience minimum, it survives to
ranking rather than being guessed away. This keeps recall high and, more
importantly, makes every rejection auditable — each one is recorded with the
rule that produced it.

```
1,357,995  not a software role
   44,824  title out of band (senior / staff / intern / manager)
   17,683  staffing agency, no named end client
   12,688  location unresolved
   10,640  outside target geography
      785  defense prime
      707  posted compensation below floor
      295  clearance or citizenship required
```

Three of those gates come free from the corpus schema: `is_recruiter` flags
staffing agencies, `skill_level` gives a seniority band, and `salary.median`
gives a compensation signal.

### Rank

Weighted scoring against the profile: role-family match, seniority signals,
compensation band, geography, and recency. Recency is deliberately worth only 3
points rather than 30 — freshness is a proxy for how much competition a posting
has, not for whether it is still open, and a 45-day-old requisition is still a
real job.

### Verify

This is the part I would point at first.

Language models generating documents drop content and then report that they
didn't. That is not a hypothetical: it is the bug this module exists because of.
A generated résumé silently lost two bullet points and one skill, and the
accompanying change log said "cut to fit: none."

So completeness stopped being a judgement call and became a test. `verify`
renders the document to PDF, extracts the text, and diffs it against the
structured source profile using **fact-anchor matching** — it pulls the tokens
that must survive any legitimate rewording (numbers, percentages, proper nouns,
technical terms, rare words) and checks each one reached the page.

```
$ reqstream verify --profile profile.json --html out.html --pdf out.pdf
PAGES: 1
OMISSIONS: 0

RESULT: PASS
```

It tolerates rewording, catches omission, and exits non-zero on any loss.
Prose claims about completeness are not trusted; only the diff is.

## Install

```bash
git clone https://github.com/abheeshtroy/reqstream
cd reqstream
pip install -e .
```

Python 3.9+. The pipeline itself is standard library only — no dependencies for
ingest, gate or rank. `verify` additionally needs a Chromium binary for PDF
rendering and `pdftotext` (poppler-utils) for extraction.

## Usage

```bash
# fetch the corpus into the local cache
reqstream ingest

# rank against a profile, excluding anything already seen
reqstream rank --profile profile.json --applied seen.txt --top 400

# resolve a company's ATS tenant
reqstream tenant stripe
#   greenhouse  stripe
#   workday     stripe|wd1|stripe

# verify a generated document preserves every fact in the profile
reqstream verify --profile profile.json --html draft.html --pdf draft.pdf

# append today's aggregate market counts to data/market/
reqstream stats
```

`rank` writes both JSON (full records, gate breakdown, provenance) and TSV
(one line per role, for reading in a terminal).

### Profile format

See [`profile.example.json`](profile.example.json). The shape:

```json
{
  "roleFamilies": ["software engineering", "ai/ml"],
  "seniority":    {"maxYears": 2, "rejectTitles": ["senior", "staff"]},
  "geography":    {"include": ["US"], "preferred": ["San Francisco"]},
  "compensation": {"floorUSD": 100000},
  "exclude":      {"clearance": true, "defensePrimes": true},
  "skills":       ["python", "typescript", "postgresql"],
  "experience":   [ ... ]
}
```

`verify` reads the `experience`, `education`, `projects` and `skills` sections;
`gate` and `rank` read the rest.

## Market data

`reqstream stats` appends one aggregate row per day to `data/market/`: total
postings, qualifying count by seniority band, distinct employers, and a
per-ATS breakdown. No posting text, no employer-identifying detail beyond
counts. Over time this is a public time series of software hiring volume,
which is a more interesting artifact than any single day's shortlist.

## Honest limitations

1. **The corpus is a third-party scrape**, not an official feed. `first_seen`
   and `scraped_at` are the scraper's timestamps, not the employer's. A row can
   be a filled requisition. Treat a 404 at apply time as normal.
2. **Coverage is six ATS families.** Greenhouse, Ashby, Lever, Workday,
   Paylocity, BambooHR. It does *not* cover SmartRecruiters, Workable, Rippling,
   Oracle Cloud, iCIMS, Taleo, SuccessFactors, or in-house career sites. Roughly
   speaking this reaches most startups and a good share of mid-market, and
   misses a meaningful slice of large enterprises.
3. **`salary` values are inferred by the scraper**, not the posted range — note
   the `n` field on each. Use as a soft signal; never quote one as fact.
4. **Single upstream dependency.** If the corpus repository disappears, ingest
   fails. It exits non-zero rather than returning an empty set, so a caller
   never mistakes an outage for "no matches today."
5. **`verify` proves nothing was dropped. It cannot prove nothing was added.**
   Fabrication is a different problem and this does not solve it.

## License

MIT
