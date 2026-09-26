"""
Mechanical verification that a generated document preserves its source facts.

WHY THIS MODULE EXISTS
----------------------
A language model asked to tailor a résumé to a job description dropped two
bullet points and one skill, and the change log it produced alongside said
"cut to fit: none." Not a hallucination in the usual sense — the output was
entirely truthful about the facts it kept. It was simply silent about the ones
it lost, and confidently wrong when asked.

The lesson generalises: a model's own account of what it did to a document is
not evidence. So completeness stopped being a judgement call and became a test.

HOW IT WORKS
------------
Naive string matching fails immediately, because legitimate tailoring rewords
almost everything. "Reduced manual effort by 70% via hybrid similarity scoring"
and "Cut manual matching 70% using a hybrid similarity system" are the same
fact in different words, and a diff should not flag the second as a loss.

So instead of matching text, this matches **fact anchors**: the tokens that
cannot survive being dropped even though the sentence around them changes.
Numbers and percentages (70%, 5x, 20+), proper nouns, technical terms, and
rare words all qualify. Common verbs and connectives do not — those are exactly
what rewording replaces.

An item passes when at least THRESHOLD of its anchors reached the page. Below
that, the fact content is judged absent regardless of how similar the prose
looks.

WHAT IT DOES NOT DO
-------------------
It proves nothing was *dropped*. It cannot prove nothing was *added*.
Fabrication is a separate problem and this is not a solution to it.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

THRESHOLD = 0.70

# Words that carry no distinguishing fact content. Rewording swaps these freely,
# so their absence means nothing and their presence proves nothing.
STOPWORDS = set("""
the a an and or of to in for with via across on by as is are was were that this
it its from at into over under between during each their his her our not no
built designed shipped owned served engineered reduced improved used using make
made end system systems work working real live full new other more most also
than then them across within while when where which who whom whose have has had
""".split())


class VerificationError(RuntimeError):
    pass


def _normalise(s: str) -> str:
    s = (s.replace("—", " ").replace("–", "-").replace("’", "'")
          .replace("&amp;", "&").replace("&times;", "x").replace("×", "x"))
    s = re.sub(r"[^a-z0-9%+.&/' -]", " ", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def anchors(text: str) -> set:
    """Tokens that must survive any legitimate rewording of `text`."""
    out = set()
    for tok in re.split(r"[ ,;:]+", _normalise(text)):
        tok = tok.rstrip(".").strip()
        if not tok:
            continue
        if re.search(r"\d", tok):              # 70%, 20+, 5x, 4.0
            out.add(tok)
        elif len(tok) > 5 and tok not in STOPWORDS:
            out.add(tok)
    return out


def coverage(item: str, haystack: str) -> float:
    a = anchors(item)
    if not a:
        return 1.0
    return sum(1 for tok in a if tok in haystack) / len(a)


def render_pdf(html: pathlib.Path, pdf: pathlib.Path,
               chrome: str | None = None) -> None:
    binary = chrome or _find_chrome()
    if not binary:
        raise VerificationError(
            "no Chromium binary found; pass --chrome or set CHROME_PATH")
    subprocess.run(
        [binary, "--headless", "--disable-gpu", "--no-sandbox",
         f"--print-to-pdf={pdf}", "--no-pdf-header-footer", f"file://{html}"],
        capture_output=True, check=True)


def _find_chrome() -> str | None:
    import os
    if os.environ.get("CHROME_PATH"):
        return os.environ["CHROME_PATH"]
    for name in ("chromium", "chromium-browser", "google-chrome",
                 "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    for pat in ("/opt/pw-browsers/chromium-*/chrome-linux/chrome",
                "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"):
        hits = sorted(pathlib.Path("/").glob(pat.lstrip("/")))
        if hits:
            return str(hits[-1])
    return None


def page_count(pdf: pathlib.Path) -> int:
    return len(re.findall(rb"/Type\s*/Page[^s]", pdf.read_bytes()))


def extract_text(pdf: pathlib.Path) -> str:
    if not shutil.which("pdftotext"):
        raise VerificationError("pdftotext not found; install poppler-utils")
    return subprocess.run(["pdftotext", "-layout", str(pdf), "-"],
                          capture_output=True, text=True, check=True).stdout


def verify(profile: dict, rendered_text: str) -> list:
    """Return a list of failure strings. Empty means every fact survived."""
    failures, hay = [], _normalise(rendered_text)

    for job in profile.get("experience", []):
        company = job.get("company", "")
        if company and _normalise(company).split()[0] not in hay:
            failures.append(f"MISSING EMPLOYER: {company}")
            continue
        for i, bullet in enumerate(job.get("bullets", []), 1):
            c = coverage(bullet, hay)
            if c < THRESHOLD:
                failures.append(
                    f"DROPPED BULLET ({c:.0%} of facts present): "
                    f"{company} #{i} -> \"{bullet[:65]}...\"")

    for ed in profile.get("education", []):
        inst = ed.get("institution", "")
        if inst and _normalise(inst).split()[0] not in hay:
            failures.append(f"MISSING EDUCATION: {inst}")

    for proj in profile.get("projects", []):
        title = proj.get("title", "")
        if title and _normalise(title) not in hay:
            failures.append(f"MISSING PROJECT: {title}")

    for skill in profile.get("skills", []):
        base = _normalise(skill).split("(")[0].strip()
        if base and base not in hay:
            failures.append(f"MISSING SKILL: {skill}")

    return failures


def check(profile: dict, html: pathlib.Path, pdf: pathlib.Path,
          max_pages: int = 1, chrome: str | None = None) -> tuple:
    """Render, extract, diff. Returns (ok, pages, failures)."""
    render_pdf(html, pdf, chrome)
    pages = page_count(pdf)
    failures = verify(profile, extract_text(pdf))
    return (pages <= max_pages and not failures), pages, failures
