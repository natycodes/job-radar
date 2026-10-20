"""Offline checks of the filter, ranking and dedupe logic (no network)."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from radar import collect as c  # noqa: E402

NOW = datetime.now(timezone.utc)


def mk(title, loc="Remote, Worldwide", desc="We use Python, Django, PostgreSQL and Redis to build REST APIs.",
       days=1, source="Greenhouse (acme)", company="Acme", salary="", **kw):
    return c.job(source, title, company, f"https://x.test/{abs(hash(title + loc + company))}",
                 NOW - timedelta(days=days), loc, desc, salary, **kw)


def ev(*a, **k):
    return c.evaluate(mk(*a, **k))


def test_basic_pass():
    r = ev("Senior Backend Engineer (Python)")
    assert r and r["eligibility"] == "yes", r


def test_location_rules():
    assert ev("Backend Engineer", loc="Remote, United States") is None
    assert ev("Backend Engineer", loc="Remote - Poland, Germany") is None
    assert ev("Backend Engineer", loc="Remote, United Kingdom") is None
    assert ev("Backend Engineer", loc="EMEA (Remote)")["eligibility"] == "yes"
    assert ev("Backend Engineer", loc="Lagos, Nigeria (Remote)")["eligibility"] == "yes"
    assert ev("Backend Engineer", loc="Remote, South Africa") is None
    assert ev("Backend Engineer", loc="Europe, Africa")["eligibility"] == "yes"
    assert ev("Backend Engineer", loc="Remote")["eligibility"] == "maybe"
    assert ev("Backend Engineer", loc="Remote - Europe")["eligibility"] == "maybe"
    r = ev("Backend Engineer", loc="Remote, Worldwide",
           desc="Python Django Postgres. You must be authorized to work in the United States.")
    assert r["eligibility"] == "maybe"
    assert ev("Backend Engineer", loc="Remote",
              desc="Python Django Postgres REST API. Must be located in the US.") is None


def test_title_rules():
    for bad in ["Junior Python Developer", "Staff Backend Engineer", "Engineering Manager, Backend",
                "Senior Frontend Engineer", "Backend Engineer (Ruby)", "Senior Java Developer",
                "Python Developer Intern", "Golang Backend Engineer", "Head of Engineering",
                "AI Trainer - Python", "Support Engineer, Python"]:
        assert ev(bad) is None, bad
    for good in ["Senior Python Developer", "Backend Developer (Node.js)", "Software Engineer, Platform",
                 "Senior Software Engineer - Backend", "Lead Backend Engineer", "Full Stack Engineer (Django)",
                 "Node.js Engineer", "Python Engineer"]:
        assert ev(good, desc="Node.js, TypeScript, Python, Django, PostgreSQL, Redis, REST APIs"), good
    assert ev("JavaScript Developer") is None  # no title match


def test_stack_required():
    assert ev("Software Engineer", desc="We build iOS apps in Swift and Kotlin.") is None


def test_age():
    assert ev("Backend Engineer", days=12) is None
    assert ev("Backend Engineer", days=7.5)


def test_salary_floor():
    assert ev("Backend Engineer", salary="$400 per month") is None
    assert ev("Backend Engineer", salary="5000-8000 USD per year") is None
    assert ev("Backend Engineer", salary="$60k - $90k")["annual_usd_estimate"] == 90000
    assert ev("Backend Engineer", salary="€4,000 per month")["annual_usd_estimate"] > 40000


def test_dedupe_prefers_employer_board():
    a = c.evaluate(mk("Senior Backend Engineer", source="Himalayas", company="Acme"))
    b = c.evaluate(mk("Backend Engineer", source="Lever (acme)", company="Acme", loc="Remote, EMEA"))
    out = c.dedupe([a, b])
    assert len(out) == 1 and out[0]["source"] == "Lever (acme)" and "Himalayas" in out[0]["also_on"]


def test_discover_boards():
    jobs = [{"url": "https://jobs.lever.co/coolco/123", "description": ""},
            {"url": "https://x", "description": "apply at https://boards.greenhouse.io/neatco/jobs/9 or "
                                                "https://jobs.ashbyhq.com/shinyco/abc"}]
    d = c.discover_boards(jobs)
    assert d["lever"] == ["coolco"] and d["greenhouse"] == ["neatco"] and d["ashby"] == ["shinyco"]


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)
