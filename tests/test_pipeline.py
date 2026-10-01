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


def test_salary_is_not_a_gate_at_any_value():
    """Superseded: compensation used to be a hard gate on the corpus's
    inferred salary. See test_inferred_salary_no_longer_excludes_roles."""
    r = gate.Gate(PROFILE).apply([
        rec(salary={"median": 60000}, url="a"),
        rec(salary={}, url="b"),
        rec(url="c"),
    ])
    assert len(r.kept) == 3


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


# ------------------------------------------------- regional gating (regressions)

def test_indian_metros_are_gated_without_the_word_india():
    """The corpus often gives a bare city. Matching only on "India" let these
    through to the shortlist on three separate runs."""
    cities = ["Hyderabad", "Bengaluru", "Pune", "Chennai", "Gurugram", "Noida"]
    r = gate.Gate(PROFILE).apply(
        [rec(location=c, url=f"u{i}") for i, c in enumerate(cities)])
    assert r.kept == []
    assert r.reasons["outside target geography"] == len(cities)


def test_bare_region_names_are_gated():
    r = gate.Gate(PROFILE).apply([
        rec(location="Europe", url="a"), rec(location="EMEA", url="b"),
        rec(location="LATAM", url="c"),
    ])
    assert r.kept == []


def test_rtx_tenant_alias_is_treated_as_a_defense_prime():
    """`globalhr` is RTX's slug in the corpus; the trading name never appears."""
    r = gate.Gate(PROFILE).apply([rec(company="globalhr", url="a")])
    assert r.reasons["defense prime"] == 1


# ------------------------------------------------- requisition-id dedupe

def test_same_greenhouse_req_on_two_urls_collapses():
    a = rec(url="https://job-boards.greenhouse.io/acme/jobs/6210230004")
    b = rec(url="https://acme.com/careers?gh_jid=6210230004")
    r = gate.Gate(PROFILE).apply([a, b])
    assert len(r.kept) == 1
    assert r.reasons["duplicate req id, different url"] == 1


def test_req_id_dedupe_respects_the_already_applied_set():
    r = gate.Gate(PROFILE).apply(
        [rec(url="https://acme.com/careers?gh_jid=555000")],
        seen_urls={"https://job-boards.greenhouse.io/acme/jobs/555000"})
    assert r.kept == []
    assert r.reasons["already seen, different url"] == 1


def test_unrecognised_urls_are_never_collapsed():
    """req_key returns None rather than guessing; two different jobs on an
    unknown ATS must both survive."""
    r = gate.Gate(PROFILE).apply([
        rec(url="https://careers.example.com/one"),
        rec(url="https://careers.example.com/two"),
    ])
    assert len(r.kept) == 2


def test_req_key_extracts_across_ats_families():
    cases = {
        "https://job-boards.greenhouse.io/x/jobs/6210230004": "6210230004",
        "https://jobs.lever.co/x/7d75bed5-45d8-4876-840a-2d92ea000000": "7d75bed5-45d8-4876-840a-2d92ea000000",
        "https://jobs.ashbyhq.com/x/b9b9b5e0-7304-4265-aa71-d71d80d29402": "b9b9b5e0-7304-4265-aa71-d71d80d29402",
        "https://acme.wd5.myworkdayjobs.com/careers/job/sf/engineer_R30113": "r30113",
    }
    for url, frag in cases.items():
        k = gate.req_key(url)
        assert k is not None, url
        assert frag.lower() in k, (url, k)


# ------------------------------------------------- compensation

def test_inferred_salary_no_longer_excludes_roles():
    """The corpus salary is the scraper's inference, not the posted range.
    Gating on it dropped ~700 roles a run. It is scored, not gated."""
    r = gate.Gate(PROFILE).apply([rec(salary={"median": 40000, "n": 8}, url="a")])
    assert len(r.kept) == 1
