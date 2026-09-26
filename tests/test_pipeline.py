"""Tests for the gate, ranker and verifier. No network required."""
import datetime

from reqstream import gate, rank
from reqstream import verify as V

PROFILE = {
    "seniority": {"maxYears": 2},
    "geography": {"include": ["US"], "preferred": ["san francisco"]},
    "compensation": {"floorUSD": 100000},
    "exclude": {"clearance": True, "defensePrimes": True, "staffingAgencies": True},
}


def rec(**kw):
    base = {"title": "Software Engineer", "company": "acme",
            "location": "San Francisco, CA", "url": "u/" + str(id(kw)),
            "ats": "Greenhouse", "is_recruiter": False, "skill_level": "mid"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- gate

def test_keeps_a_plain_matching_role():
    r = gate.Gate(PROFILE).apply([rec()])
    assert len(r.kept) == 1


def test_rejects_senior_and_intern_titles():
    r = gate.Gate(PROFILE).apply([
        rec(title="Senior Software Engineer", url="a"),
        rec(title="Software Engineering Intern", url="b"),
        rec(title="Software Engineering Manager", url="c"),
    ])
    assert r.kept == []
    assert r.reasons["title out of band"] == 3


def test_non_software_titles_are_rejected_before_the_title_rule():
    """Gate order matters: a marketing role is 'not a software role',
    not 'title out of band'. Reasons must name the rule that actually fired."""
    r = gate.Gate(PROFILE).apply([rec(title="Engineering Manager", url="a")])
    assert r.reasons["not a software role"] == 1


def test_blank_location_survives_absence_is_not_evidence():
    """The core gate principle: a missing signal never excludes a record."""
    r = gate.Gate(PROFILE).apply([rec(location="", url="a")])
    assert len(r.kept) == 1


def test_non_us_location_is_excluded():
    r = gate.Gate(PROFILE).apply([rec(location="Bengaluru, India", url="a")])
    assert r.reasons["outside target geography"] == 1


def test_staffing_agency_flag_is_honoured():
    r = gate.Gate(PROFILE).apply([rec(is_recruiter=True, url="a")])
    assert r.reasons["staffing agency, no named end client"] == 1


def test_salary_below_floor_excluded_but_missing_salary_kept():
    r = gate.Gate(PROFILE).apply([
        rec(salary={"median": 60000}, url="a"),
        rec(salary={}, url="b"),
        rec(url="c"),
    ])
    assert r.reasons["compensation below floor"] == 1
    assert len(r.kept) == 2


def test_duplicate_urls_collapse_and_seen_urls_excluded():
    r = gate.Gate(PROFILE).apply(
        [rec(url="dup"), rec(url="dup"), rec(url="old")], seen_urls={"old"})
    assert len(r.kept) == 1
    assert r.reasons["duplicate url"] == 1
    assert r.reasons["already seen"] == 1


# ---------------------------------------------------------------- rank

def test_preferred_location_outranks_elsewhere():
    a = rec(location="San Francisco, CA", url="a")
    b = rec(location="Omaha, NE", url="b")
    out = rank.rank([b, a], PROFILE)
    assert out[0]["url"] == "a"


def test_future_cohort_is_penalised_not_gated():
    year = datetime.date.today().year + 1
    out = rank.rank([rec(title=f"Software Engineer, {year} New Grad", url="a")],
                    PROFILE)
    assert out[0]["score"] < 60          # scored down
    assert len(out) == 1                 # but still present


def test_recency_is_a_tiebreak_not_a_sort_key():
    """A strong old posting must beat a weak fresh one."""
    today = datetime.date.today()
    old_strong = rec(title="Backend Infrastructure Engineer", url="a",
                     first_seen="2026-01-01")
    new_weak = rec(title="Salesforce Developer", url="b",
                   first_seen=today.isoformat())
    out = rank.rank([new_weak, old_strong], PROFILE, today=today)
    assert out[0]["url"] == "a"


# ---------------------------------------------------------------- verify

DOC_PROFILE = {
    "experience": [{"company": "Northwind", "bullets": [
        "Built an event-ingestion service in Go handling 40k messages per second.",
        "Cut median retrieval latency from 800ms to 45ms using FAISS vector search.",
    ]}],
    "education": [{"institution": "Example State University"}],
    "projects": [{"title": "Driftwood"}],
    "skills": ["PostgreSQL"],
}

FULL = ("Northwind. Built an event ingestion service in Go handling 40k "
        "messages per second. Cut median retrieval latency from 800ms to 45ms "
        "using FAISS vector search. Example State University. Driftwood. "
        "PostgreSQL.")


def test_complete_document_passes():
    assert V.verify(DOC_PROFILE, FULL) == []


def test_rewording_does_not_register_as_loss():
    """The whole point: tailoring rewrites prose and must not trip the diff."""
    reworded = ("Northwind. Shipped a Go event-ingestion service sustaining 40k "
                "messages per second in production. Drove median retrieval "
                "latency down from 800ms to just 45ms with FAISS vector search. "
                "Example State University. Driftwood. PostgreSQL.")
    assert V.verify(DOC_PROFILE, reworded) == []


def test_silently_dropped_bullet_is_caught():
    without = ("Northwind. Built an event ingestion service in Go handling 40k "
               "messages per second. Example State University. Driftwood. "
               "PostgreSQL.")
    failures = V.verify(DOC_PROFILE, without)
    assert any("DROPPED BULLET" in f for f in failures)


def test_dropped_skill_and_project_are_caught():
    failures = V.verify(DOC_PROFILE, FULL.replace("PostgreSQL.", "")
                                        .replace("Driftwood.", ""))
    assert any("MISSING SKILL" in f for f in failures)
    assert any("MISSING PROJECT" in f for f in failures)


def test_anchors_ignore_common_words_and_keep_figures():
    a = V.anchors("Reduced manual effort by 70% via hybrid similarity scoring")
    assert "70%" in a
    assert "similarity" in a
    assert "the" not in a and "by" not in a
