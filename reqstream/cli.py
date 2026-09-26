"""Command line interface."""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import sys

from . import ingest, gate, rank as rank_mod, verify as verify_mod


def _load_profile(path: str) -> dict:
    p = pathlib.Path(path)
    if not p.exists():
        sys.exit(f"profile not found: {path}\n"
                 f"copy profile.example.json and edit it")
    return json.loads(p.read_text())


def _seen(path: str | None) -> set:
    if not path:
        return set()
    p = pathlib.Path(path)
    if not p.exists():
        return set()
    return {l.strip() for l in p.read_text().splitlines() if l.strip()}


# ------------------------------------------------------------------ commands

def cmd_ingest(a) -> int:
    c = ingest.fetch_corpus(a.cache)
    if c.chunks_failed:
        print(f"WARNING: {len(c.chunks_failed)} chunks unreadable: "
              f"{', '.join(c.chunks_failed)}", file=sys.stderr)
    return 0


def cmd_rank(a) -> int:
    profile = _load_profile(a.profile)
    corpus = ingest.fetch_corpus(a.cache, verbose=not a.quiet)

    report = gate.Gate(profile).apply(corpus.records, _seen(a.applied))
    ranked = rank_mod.rank(report.kept, profile)
    kept = [r for r in ranked if r["score"] >= a.floor][: a.top]

    out = pathlib.Path(a.out)
    out.write_text(json.dumps({
        "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "corpus": {"records": len(corpus), "chunks_read": corpus.chunks_read,
                   "chunks_failed": corpus.chunks_failed,
                   "fetch_seconds": round(corpus.elapsed_s, 1)},
        "scanned": len(corpus), "qualified": len(report.kept),
        "returned": len(kept), "score_floor": a.floor,
        "gated": dict(report.reasons.most_common()),
        "jobs": kept,
    }, indent=1))

    tsv = out.with_suffix(".tsv")
    with tsv.open("w") as f:
        f.write("score\tcompany\ttitle\tlocation\tats\tmedian\tfirst_seen\turl\n")
        for r in kept:
            f.write("\t".join([
                str(r["score"]), r.get("company", ""),
                (r.get("title") or "").strip(),
                (r.get("location") or "").replace("\t", " "),
                r.get("ats", ""), str((r.get("salary") or {}).get("median") or ""),
                (r.get("first_seen") or "")[:10], r["url"]]) + "\n")

    print(f"\nscanned {len(corpus):,}  ->  qualified {len(report.kept):,}"
          f"  ->  returned {len(kept):,}")
    print(f"{len({r['company'] for r in kept}):,} distinct companies")
    print(f"\ngates:\n{report.summary()}")
    print(f"\nwrote {out} and {tsv}")
    return 0


def cmd_tenant(a) -> int:
    hits = ingest.resolve_tenant(a.company, a.cache)
    if not hits:
        print(f"no ATS tenant found for {a.company!r}")
        return 1
    for ats, tokens in hits.items():
        for tok in tokens:
            url = ingest.board_url(ats, tok) or ""
            print(f"  {ats:<11} {tok:<34} {url}")
    return 0


def cmd_verify(a) -> int:
    profile = _load_profile(a.profile)
    ok, pages, failures = verify_mod.check(
        profile, pathlib.Path(a.html), pathlib.Path(a.pdf),
        max_pages=a.max_pages, chrome=a.chrome)
    print(f"PAGES: {pages}")
    print(f"OMISSIONS: {len(failures)}")
    for f in failures:
        print("  FAIL:", f)
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def cmd_stats(a) -> int:
    """Append one aggregate row per day. Counts only, no posting text."""
    corpus = ingest.fetch_corpus(a.cache, verbose=not a.quiet)
    by_ats = collections.Counter()
    by_level = collections.Counter()
    software = 0
    for j in corpus.records:
        by_ats[j.get("ats") or "unknown"] += 1
        by_level[(j.get("skill_level") or "unknown").lower()] += 1
        if gate.SOFTWARE_ROLE.search(j.get("title") or ""):
            software += 1

    row = {
        "date": datetime.date.today().isoformat(),
        "total_postings": len(corpus),
        "software_roles": software,
        "distinct_employers": len({j.get("company") for j in corpus.records}),
        "by_ats": dict(by_ats.most_common()),
        "by_skill_level": dict(by_level.most_common()),
    }
    d = pathlib.Path(a.dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{row['date']}.json").write_text(json.dumps(row, indent=1))
    print(json.dumps(row, indent=1))
    return 0


# ------------------------------------------------------------------ parser

def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="reqstream",
        description="Streaming ingestion and ranking over open-requisition data")
    p.add_argument("--cache", default=str(ingest.DEFAULT_CACHE))
    p.add_argument("--quiet", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ingest", help="fetch the corpus into the local cache"
                   ).set_defaults(fn=cmd_ingest)

    r = sub.add_parser("rank", help="gate and rank the corpus against a profile")
    r.add_argument("--profile", default="profile.json")
    r.add_argument("--applied", help="file of URLs to exclude, one per line")
    r.add_argument("--out", default="shortlist.json")
    r.add_argument("--top", type=int, default=400)
    r.add_argument("--floor", type=int, default=60, help="minimum score")
    r.set_defaults(fn=cmd_rank)

    t = sub.add_parser("tenant", help="resolve a company's ATS tenant")
    t.add_argument("company")
    t.set_defaults(fn=cmd_tenant)

    v = sub.add_parser("verify", help="check a document preserves its source facts")
    v.add_argument("--profile", default="profile.json")
    v.add_argument("--html", required=True)
    v.add_argument("--pdf", required=True)
    v.add_argument("--max-pages", type=int, default=1)
    v.add_argument("--chrome", help="path to a Chromium binary")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("stats", help="append today's aggregate market counts")
    s.add_argument("--dir", default="data/market")
    s.set_defaults(fn=cmd_stats)

    a = p.parse_args(argv)
    try:
        return a.fn(a)
    except (ingest.IngestError, verify_mod.VerificationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
