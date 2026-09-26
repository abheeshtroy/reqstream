"""
Corpus ingestion and ATS tenant resolution.

The corpus is published as gzipped JSON chunks of 25,000 records each. Fetching
them twelve at a time pulls the whole ~76MB set in roughly five seconds, which
is fast enough that there is no reason to cache aggressively or to work from a
stale copy.

Integrity matters more than speed here: a chunk that arrives truncated will
still parse as a valid file right up until it doesn't, so every chunk is gzip
integrity-checked, retried once serially, and reported if it still fails. The
caller is never handed a silently short corpus.
"""
from __future__ import annotations

import gzip
import json
import pathlib
import subprocess
import time
from dataclasses import dataclass, field
from typing import Iterable

CORPUS_BASE = ("https://raw.githubusercontent.com/Feashliaa/job-board-data"
               "/main/data/chunks")
MANIFEST_URL = f"{CORPUS_BASE}/jobs_manifest.json"
TENANT_URL = ("https://raw.githubusercontent.com/Feashliaa/job-board-aggregator"
              "/main/data/{ats}_companies.json")

TENANT_SOURCES = ("greenhouse", "ashby", "lever", "workday")
DEFAULT_CACHE = pathlib.Path.home() / ".cache" / "reqstream"
PARALLEL = 12


class IngestError(RuntimeError):
    """Raised when the corpus cannot be retrieved or parsed.

    Deliberately an exception rather than an empty list: a caller must never be
    able to mistake an upstream outage for "nothing matched today".
    """


@dataclass
class Corpus:
    records: list = field(default_factory=list)
    chunks_expected: int = 0
    chunks_read: int = 0
    chunks_failed: list = field(default_factory=list)
    elapsed_s: float = 0.0

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterable[dict]:
        return iter(self.records)


def _curl(url: str, dest: pathlib.Path, timeout: int = 60) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ["curl", "-sS", "--fail", "--max-time", str(timeout), "-o", str(dest), url],
        capture_output=True, text=True,
    )
    return r.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def _readable(path: pathlib.Path) -> bool:
    """Gzip integrity check. A truncated download passes a size check but not this."""
    if not path.exists() or path.stat().st_size == 0:
        return False
    return subprocess.run(["gzip", "-t", str(path)],
                          capture_output=True).returncode == 0


def fetch_corpus(cache: pathlib.Path | str = DEFAULT_CACHE,
                 verbose: bool = True) -> Corpus:
    """Download and parse the full corpus. Raises IngestError on failure."""
    cache = pathlib.Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()

    manifest_path = cache / "jobs_manifest.json"
    if not _curl(MANIFEST_URL, manifest_path):
        raise IngestError(f"could not fetch manifest from {MANIFEST_URL}")
    manifest = json.loads(manifest_path.read_text())
    chunks = manifest["chunks"]

    if verbose:
        print(f"manifest: {len(chunks)} chunks, "
              f"{manifest.get('totalJobs', 0):,} records", flush=True)

    # Parallel fetch in waves. Shelling out to backgrounded curl keeps this
    # dependency-free; the wave size bounds concurrent connections.
    todo = [(c, cache / c) for c in chunks]
    lines = []
    for i, (name, dest) in enumerate(todo, 1):
        # Newline-separated, never "; " — a backgrounded command already ends
        # in "&", and "&;" is a bash syntax error that kills the whole script.
        lines.append(
            f'curl -sS --fail --max-time 60 -o "{dest}" "{CORPUS_BASE}/{name}" &')
        if i % PARALLEL == 0:
            lines.append("wait")
    lines.append("wait")
    subprocess.run(["bash", "-c", "\n".join(lines)], capture_output=True)

    corpus = Corpus(chunks_expected=len(chunks))
    retry = []
    for name, dest in todo:
        if not _readable(dest):
            retry.append((name, dest))
            continue
        try:
            corpus.records.extend(
                json.loads(gzip.open(dest, "rt", encoding="utf-8").read()))
            corpus.chunks_read += 1
        except (OSError, ValueError):
            retry.append((name, dest))

    for name, dest in retry:                      # one serial retry pass
        if _curl(f"{CORPUS_BASE}/{name}", dest, timeout=90) and _readable(dest):
            try:
                corpus.records.extend(
                    json.loads(gzip.open(dest, "rt", encoding="utf-8").read()))
                corpus.chunks_read += 1
                continue
            except (OSError, ValueError):
                pass
        corpus.chunks_failed.append(name)

    corpus.elapsed_s = time.monotonic() - t0
    if not corpus.records:
        raise IngestError("corpus fetched but zero records parsed")
    if verbose:
        note = f", {len(corpus.chunks_failed)} unrecoverable" if corpus.chunks_failed else ""
        print(f"parsed {len(corpus.records):,} records in "
              f"{corpus.elapsed_s:.1f}s ({len(retry)} retried{note})", flush=True)
    return corpus


def load_tenants(cache: pathlib.Path | str = DEFAULT_CACHE) -> dict:
    """Return {ats: [tenant, ...]} for the ATS directories.

    Workday entries encode tenant and site (`23andme|wd5|23`), which is what a
    working Workday URL requires. Workday publishes no global index, so this
    directory is the only practical route to those boards.
    """
    cache = pathlib.Path(cache)
    out = {}
    for ats in TENANT_SOURCES:
        dest = cache / f"{ats}_companies.json"
        if not dest.exists() and not _curl(TENANT_URL.format(ats=ats), dest):
            continue
        try:
            out[ats] = json.loads(dest.read_text())
        except ValueError:
            continue
    if not out:
        raise IngestError("no ATS tenant directories could be loaded")
    return out


def resolve_tenant(company: str, cache: pathlib.Path | str = DEFAULT_CACHE) -> dict:
    """Find ATS tenants matching a company name. Exact hits first, then prefix."""
    needle = "".join(ch for ch in company.lower() if ch.isalnum())
    hits = {}
    for ats, tokens in load_tenants(cache).items():
        exact, partial = [], []
        for tok in tokens:
            stem = tok.split("|")[0]
            flat = "".join(ch for ch in stem.lower() if ch.isalnum())
            if flat == needle:
                exact.append(tok)
            elif needle and (flat.startswith(needle) or needle in flat):
                partial.append(tok)
        found = exact + partial[: max(0, 8 - len(exact))]
        if found:
            hits[ats] = found
    return hits


def board_url(ats: str, tenant: str) -> str | None:
    """Construct a job-board URL from an ATS tenant identifier."""
    if ats == "greenhouse":
        return f"https://job-boards.greenhouse.io/{tenant}"
    if ats == "ashby":
        return f"https://jobs.ashbyhq.com/{tenant}"
    if ats == "lever":
        return f"https://jobs.lever.co/{tenant}"
    if ats == "workday":
        parts = tenant.split("|")
        if len(parts) == 3:
            org, pod, site = parts
            return f"https://{org}.{pod}.myworkdayjobs.com/{site}"
    return None
