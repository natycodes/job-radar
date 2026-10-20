"""Job Radar: collect remote backend roles from public job feeds, filter and rank them.

Runs daily on GitHub Actions (open internet). Standard library only.
Output (in data/):
  candidates.json   ranked shortlist + every role that passed the filters
  candidates.md     the shortlist as a readable table
  report.json       per-source counts and errors, failed company boards
  discovered_companies.json  ATS boards found through aggregator links (scanned on later runs)
  seen.json         ids seen on earlier runs, so new roles can be flagged
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DATA = ROOT / "data"
UA = "job-radar/1.0 (personal job search; github.com/natycodes/job-radar)"
NOW = datetime.now(timezone.utc)

SEARCH = json.loads((CONFIG / "search.json").read_text())
MAX_AGE = timedelta(days=SEARCH["max_age_days"])

report: dict = {"generated_at": NOW.isoformat(), "sources": {}, "failed_boards": [], "errors": []}


# ---------------------------------------------------------------- http helpers

def fetch(url: str, timeout: int = 25, tries: int = 2, accept: str = "application/json") -> bytes:
    last = None
    for attempt in range(tries):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": accept})
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except HTTPError as e:
            last = e
            if e.code in (404, 410, 401, 403):
                break
            time.sleep(1.5 * (attempt + 1))
        except (URLError, TimeoutError, ConnectionError, OSError) as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last  # type: ignore[misc]


def fetch_json(url: str, **kw):
    return json.loads(fetch(url, **kw).decode("utf-8", "replace"))


# ---------------------------------------------------------------- text helpers

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean(text) -> str:
    if not text:
        return ""
    text = html.unescape(html.unescape(str(text)))
    text = TAG_RE.sub(" ", text)
    return WS_RE.sub(" ", text).strip()


def to_dt(value) -> datetime | None:
    if value in (None, "", 0):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            v = float(value)
            if v > 1e12:  # milliseconds
                v /= 1000
            return datetime.fromtimestamp(v, tz=timezone.utc)
        s = str(value).strip()
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc)
    except Exception:
        return None


def has_term(text: str, term: str) -> bool:
    """Whole-word-ish match; terms with punctuation or trailing spaces match as substrings."""
    t = term.lower()
    if t != t.strip() or not re.match(r"^[a-z0-9+ ]+$", t):
        return t in text
    return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", text) is not None


def any_term(text: str, terms) -> list[str]:
    return [t for t in terms if has_term(text, t)]


# ---------------------------------------------------------------- job record

def job(source, title, company, url, posted, location="", description="", salary="",
        employment_type="", worldwide_hint=None, extra=None) -> dict:
    return {
        "source": source,
        "title": clean(title),
        "company": clean(company),
        "url": url,
        "posted": posted.isoformat() if posted else None,
        "location": clean(location),
        "description": clean(description)[:6000],
        "salary": clean(salary),
        "employment_type": clean(employment_type),
        "worldwide_hint": worldwide_hint,
        **(extra or {}),
    }


# ---------------------------------------------------------------- sources: aggregators

def src_himalayas():
    out, cursor, pages = [], None, 0
    queries = ["python", "django", "fastapi", "node.js", "nodejs", "backend"]
    seen = set()
    # Search endpoint first; fall back to the full feed if it is not available.
    try:
        for q in queries:
            for page in range(1, 6):
                url = f"https://himalayas.app/jobs/api/search?q={quote(q)}&sort=recent&page={page}"
                data = fetch_json(url)
                jobs = data.get("jobs") or []
                if not jobs:
                    break
                stop = False
                for j in jobs:
                    gid = j.get("guid") or j.get("applicationLink")
                    if gid in seen:
                        continue
                    seen.add(gid)
                    d = to_dt(j.get("pubDate"))
                    if d and NOW - d > MAX_AGE:
                        stop = True
                    out.append(_himalayas_job(j, d))
                if stop:
                    break
        if out:
            return out
    except Exception as e:
        report["errors"].append(f"himalayas search: {e}")
    while pages < 400:
        url = "https://himalayas.app/jobs/api?limit=20" + (f"&cursor={quote(cursor)}" if cursor else "")
        data = fetch_json(url)
        pages += 1
        jobs = data.get("jobs") or []
        oldest = None
        for j in jobs:
            d = to_dt(j.get("pubDate"))
            oldest = d if (oldest is None or (d and d < oldest)) else oldest
            out.append(_himalayas_job(j, d))
        cursor = data.get("nextCursor")
        if not cursor or not jobs or (oldest and NOW - oldest > MAX_AGE):
            break
        time.sleep(0.25)
    return out


def _himalayas_job(j, d):
    locs = j.get("locationRestrictions") or []
    tz = j.get("timezoneRestrictions") or []
    loc = ", ".join(str(x) for x in locs) or "Worldwide"
    if tz:
        loc += f" (timezones: {', '.join(str(x) for x in tz)})"
    sal = ""
    if j.get("minSalary") or j.get("maxSalary"):
        sal = f"{j.get('minSalary') or ''}-{j.get('maxSalary') or ''} {j.get('currency') or 'USD'} per year"
    return job("Himalayas", j.get("title"), j.get("companyName"), j.get("applicationLink") or j.get("guid"), d,
               loc, (j.get("excerpt") or "") + " " + (j.get("description") or ""), sal,
               j.get("employmentType"), worldwide_hint=not locs,
               extra={"seniority": j.get("seniority"), "salary_min": j.get("minSalary"),
                      "salary_max": j.get("maxSalary"), "salary_currency": j.get("currency")})


def src_remotive():
    out = []
    for cat in ["software-dev", "backend"]:
        try:
            data = fetch_json(f"https://remotive.com/api/remote-jobs?category={cat}&limit=500")
        except Exception as e:
            report["errors"].append(f"remotive {cat}: {e}")
            continue
        for j in data.get("jobs", []):
            out.append(job("Remotive", j.get("title"), j.get("company_name"), j.get("url"),
                           to_dt(j.get("publication_date")), j.get("candidate_required_location"),
                           j.get("description"), j.get("salary"), j.get("job_type")))
    return out


def src_remoteok():
    data = fetch_json("https://remoteok.com/api")
    out = []
    for j in data:
        if not isinstance(j, dict) or not j.get("position"):
            continue
        sal = ""
        if j.get("salary_min") or j.get("salary_max"):
            sal = f"{j.get('salary_min')}-{j.get('salary_max')} USD per year"
        out.append(job("Remote OK", j.get("position"), j.get("company"), j.get("url") or j.get("apply_url"),
                       to_dt(j.get("epoch") or j.get("date")), j.get("location") or "",
                       (j.get("description") or "") + " " + " ".join(j.get("tags") or []), sal,
                       extra={"salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
                              "salary_currency": "USD"}))
    return out


def src_jobicy():
    out = []
    for tag in ["python", "django", "nodejs", "backend", "fastapi"]:
        try:
            data = fetch_json(f"https://jobicy.com/api/v2/remote-jobs?count=100&tag={tag}")
        except Exception as e:
            report["errors"].append(f"jobicy {tag}: {e}")
            continue
        for j in data.get("jobs", []):
            sal = ""
            if j.get("annualSalaryMin") or j.get("annualSalaryMax"):
                sal = f"{j.get('annualSalaryMin')}-{j.get('annualSalaryMax')} {j.get('salaryCurrency') or ''} per year"
            out.append(job("Jobicy", j.get("jobTitle"), j.get("companyName"), j.get("url"),
                           to_dt(j.get("pubDate")), j.get("jobGeo"),
                           (j.get("jobExcerpt") or "") + " " + (j.get("jobDescription") or ""), sal,
                           ", ".join(j.get("jobType") or []) if isinstance(j.get("jobType"), list) else j.get("jobType"),
                           extra={"salary_min": j.get("annualSalaryMin"), "salary_max": j.get("annualSalaryMax"),
                                  "salary_currency": j.get("salaryCurrency")}))
    return out


def src_weworkremotely():
    out = []
    feeds = ["remote-back-end-programming-jobs", "remote-full-stack-programming-jobs", "remote-programming-jobs"]
    for f in feeds:
        try:
            raw = fetch(f"https://weworkremotely.com/categories/{f}.rss", accept="application/rss+xml")
        except Exception as e:
            report["errors"].append(f"weworkremotely {f}: {e}")
            continue
        root = ET.fromstring(raw)
        for it in root.iter("item"):
            title = it.findtext("title") or ""
            company, _, role = title.partition(":")
            if not role:
                company, role = "", title
            out.append(job("We Work Remotely", role, company, it.findtext("link"),
                           to_dt(it.findtext("pubDate")), it.findtext("region") or "",
                           it.findtext("description"), "", it.findtext("type") or ""))
    return out


def src_workingnomads():
    data = fetch_json("https://www.workingnomads.com/api/exposed_jobs/")
    out = []
    for j in data:
        if (j.get("category_name") or "").lower() not in ("development", "programming", "software development", ""):
            continue
        out.append(job("Working Nomads", j.get("title"), j.get("company_name"), j.get("url"),
                       to_dt(j.get("pub_date")), j.get("location") or "",
                       (j.get("description") or "") + " " + (j.get("tags") or ""), ""))
    return out


def src_arbeitnow():
    out = []
    for page in range(1, 6):
        data = fetch_json(f"https://www.arbeitnow.com/api/job-board-api?page={page}")
        rows = data.get("data") or []
        if not rows:
            break
        for j in rows:
            if not j.get("remote"):
                continue
            out.append(job("Arbeitnow", j.get("title"), j.get("company_name"), j.get("url"),
                           to_dt(j.get("created_at")), j.get("location") or "",
                           (j.get("description") or "") + " " + " ".join(j.get("tags") or []), "",
                           ", ".join(j.get("job_types") or [])))
    return out


def src_hackernews():
    """Latest 'Ask HN: Who is hiring?' thread; keep comments that mention REMOTE."""
    s = fetch_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=10")
    story = next((h for h in s.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if not story:
        return []
    item = fetch_json(f"https://hn.algolia.com/api/v1/items/{story['objectID']}", timeout=60)
    out = []
    for c in item.get("children") or []:
        text = clean(c.get("text"))
        if not text or "remote" not in text.lower():
            continue
        first = text.split(" | ")
        company = first[0][:80] if len(first) > 1 else text[:60]
        title = next((p for p in first[1:4] if re.search(r"engineer|developer|backend|python|node", p, re.I)), "")
        if not title:
            m = re.search(r"([A-Za-z\- ]{0,30}(?:Backend|Back-end|Software|Python|Node(?:\.js)?|Full[- ]?Stack|Platform)"
                          r"[A-Za-z\- ]{0,20}(?:Engineer|Developer))", text)
            title = m.group(1).strip() if m else "Software Engineer (see post)"
        out.append(job("Hacker News: Who is hiring", title, company,
                       f"https://news.ycombinator.com/item?id={c.get('id')}", to_dt(c.get("created_at")),
                       " | ".join(first[1:5]) if len(first) > 1 else "", text, ""))
    return out


# ---------------------------------------------------------------- sources: company job boards

def board_greenhouse(token):
    data = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    out = []
    for j in data.get("jobs", []):
        d = to_dt(j.get("first_published")) or to_dt(j.get("updated_at"))
        name = j.get("company_name") or token
        out.append(job(f"Greenhouse ({token})", j.get("title"), name, j.get("absolute_url"), d,
                       (j.get("location") or {}).get("name", ""), j.get("content"), "",
                       extra={"date_is_update": not j.get("first_published")}))
    return out


def board_lever(token):
    data = fetch_json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ""
        if j.get("workplaceType"):
            loc = f"{loc} ({j['workplaceType']})"
        allloc = cat.get("allLocations")
        if allloc:
            loc += " | " + ", ".join(allloc)
        sal = ""
        sr = j.get("salaryRange") or {}
        if sr.get("min") or sr.get("max"):
            sal = f"{sr.get('min')}-{sr.get('max')} {sr.get('currency', '')} {sr.get('interval', '')}"
        out.append(job(f"Lever ({token})", j.get("text"), pretty(token), j.get("hostedUrl"), to_dt(j.get("createdAt")),
                       loc, (j.get("descriptionPlain") or "") + " " + (j.get("additionalPlain") or ""), sal,
                       cat.get("commitment") or ""))
    return out


def board_ashby(token):
    data = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location") or ""] + [
            (x.get("location") if isinstance(x, dict) else str(x)) for x in (j.get("secondaryLocations") or [])]
        loc = ", ".join(x for x in locs if x)
        if j.get("isRemote") or (j.get("workplaceType") or "").lower() == "remote":
            loc += " (Remote)"
        comp = (j.get("compensation") or {}).get("compensationTierSummary") or ""
        out.append(job(f"Ashby ({token})", j.get("title"), pretty(token), j.get("jobUrl"), to_dt(j.get("publishedAt")),
                       loc, j.get("descriptionPlain") or j.get("descriptionHtml"), comp, j.get("employmentType") or ""))
    return out


def board_workable(token):
    data = fetch_json(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true")
    out = []
    name = data.get("name") or token
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
        if j.get("telecommuting"):
            loc += " (Remote)"
        out.append(job(f"Workable ({token})", j.get("title"), name,
                       j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{token}/j/{j.get('shortcode')}/",
                       to_dt(j.get("published_on") or j.get("created_at")), loc, j.get("description"), "",
                       j.get("employment_type") or ""))
    return out


def pretty(token: str) -> str:
    return re.sub(r"[-_]+", " ", token).title()


BOARDS = {"greenhouse": board_greenhouse, "lever": board_lever, "ashby": board_ashby, "workable": board_workable}

ATS_PATTERNS = [
    ("greenhouse", re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)")),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)")),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)")),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)")),
]


def discover_boards(jobs: list[dict]) -> dict:
    found: dict[str, set] = {k: set() for k in BOARDS}
    for j in jobs:
        blob = f"{j.get('url') or ''} {j.get('description') or ''}"
        for kind, pat in ATS_PATTERNS:
            for m in pat.findall(blob):
                tok = m.strip("/").lower()
                if tok and tok not in ("embed", "api", "v1", "j", "jobs"):
                    found[kind].add(tok)
    return {k: sorted(v) for k, v in found.items()}


# ---------------------------------------------------------------- filtering & ranking

US_ONLY_RE = re.compile(
    r"(must|need to|required to|should) (be )?(located|based|reside|residing|live) in the (us|u\.s\.|united states)"
    r"|(us|u\.s\.) citizens? only|authori[sz]ed to work in the (us|u\.s\.|united states)"
    r"|security clearance|green card", re.I)


def eligibility(j: dict) -> tuple[str, str]:
    loc = (j.get("location") or "").lower()
    loc_clean = re.sub(r"(south|north|east|west) africa", " ", loc)
    desc = (j.get("description") or "").lower()
    if j.get("worldwide_hint") is True:
        verdict, why = "yes", "no location restriction listed"
    elif any_term(loc_clean, SEARCH["eligible_location_terms"]):
        verdict, why = "yes", "location: " + ", ".join(any_term(loc_clean, SEARCH["eligible_location_terms"]))
    elif any_term(loc, SEARCH["restricted_location_terms"]):
        return "no", "restricted to: " + ", ".join(any_term(loc, SEARCH["restricted_location_terms"]))[:120]
    elif j["source"].startswith("Hacker News"):
        head = desc[:400]
        if any_term(head, ["worldwide", "anywhere", "global", "emea", "africa", "europe", "remote (eu", "remote eu"]):
            verdict, why = "maybe", "HN post mentions remote beyond the US"
        elif any_term(head, ["us only", "us-only", "remote (us", "remote us", "us remote", "usa", "us/canada", "north america", "(us/", "us/remote", "remote/us", "(us)", "us-remote", "remote-us"]):
            return "no", "HN post is US-only remote"
        else:
            verdict, why = "maybe", "HN remote post, region unclear"
    else:
        # Anything left after removing generic words names a specific place that isn't on the eligible list.
        rest = re.sub(r"\(timezones:[^)]*\)", " ", loc)
        rest = re.sub(r"\b(remote|remotely|hybrid|fully|full|work from home|wfh|home|based|distributed|"
                      r"position|first|friendly|ok|only|or|and|in|the|any|location|locations|n/a|na|tbd|"
                      r"flexible|various|multiple)\b", " ", rest)
        rest = re.sub(r"[^a-z]+", " ", rest).strip()
        if not rest:
            verdict, why = "maybe", "location unclear: " + (j.get("location") or "not stated")
        elif re.fullmatch(r"(eu|europe|european union|european)( (eu|europe|european union|european))*", rest):
            verdict, why = "maybe", "Europe: check whether non-EU residents can apply"
        else:
            return "no", "limited to: " + (j.get("location") or "")[:100]
    if US_ONLY_RE.search(desc):
        return ("maybe" if verdict == "yes" else "no"), why + "; description mentions US work authorization"
    return verdict, why


def stack_score(j: dict) -> tuple[int, list[str]]:
    text = (j["title"] + " " + j["title"] + " " + (j.get("description") or "")).lower()
    hits, score = [], 0
    for term, w in SEARCH["stack_terms"].items():
        if has_term(text, term):
            hits.append(term.strip())
            score += w
    return score, hits


def annual_usd(j: dict) -> float | None:
    rates = {"usd": 1, "eur": 1.08, "gbp": 1.27, "cad": 0.73, "aud": 0.65, "chf": 1.13, "ngn": 0.00065,
             "pln": 0.25, "inr": 0.012, "sek": 0.095, "brl": 0.18, "mxn": 0.055}
    mx = j.get("salary_max") or j.get("salary_min")
    cur = (j.get("salary_currency") or "usd").lower()
    if mx:
        try:
            return float(mx) * rates.get(cur, 1)  # structured salary fields are annual
        except (TypeError, ValueError):
            return None
    s = (j.get("salary") or "").lower().replace(",", "")
    nums = [float(n) * (1000 if k else 1) for n, k in re.findall(r"(\d+(?:\.\d+)?)\s*(k)?\b", s)]
    nums = [n for n in nums if n >= 100 and not (1990 <= n <= 2100 and "k" not in s)]
    if not nums:
        return None
    cur = next((c for c in rates if c in s or {"usd": "$", "eur": "€", "gbp": "£", "ngn": "₦"}.get(c, "#") in s), "usd")
    v = max(nums) * rates[cur]
    if "hour" in s or "/hr" in s:
        v *= 2000
    elif "month" in s or "/mo" in s:
        v *= 12
    return v


def evaluate(j: dict) -> dict | None:
    title = " " + j["title"].lower() + " "
    if not j.get("url") or not j["title"]:
        return None
    if not any_term(title, SEARCH["title_must_match_any"]):
        return None
    if any_term(title, SEARCH["title_exclude_any"]):
        return None
    if isinstance(j.get("seniority"), list) and j["seniority"] and \
            all(s.lower() in ("entry-level", "junior", "intern", "executive") for s in j["seniority"]):
        return None
    posted = to_dt(j.get("posted"))
    if not posted or NOW - posted > MAX_AGE or posted - NOW > timedelta(days=1):
        return None
    elig, why = eligibility(j)
    if elig == "no":
        return None
    score, hits = stack_score(j)
    if score < 5:
        return None
    pay = annual_usd(j)
    if pay is not None and pay < SEARCH["min_annual_usd"]:
        return None
    age_days = (NOW - posted).total_seconds() / 86400
    rank = score + (8 if elig == "yes" else 0) + max(0, 6 - age_days) + (2 if pay else 0) \
        + (2 if j["source"].split(" (")[0] in ("Greenhouse", "Lever", "Ashby", "Workable") else 0) \
        - (3 if j.get("date_is_update") else 0)
    if re.search(r"\bsenior\b|\bsr\.?\b|\blead\b", title):
        rank += 1
    j2 = {k: v for k, v in j.items() if k not in ("worldwide_hint",)}
    j2.update({"eligibility": elig, "eligibility_reason": why, "stack_hits": hits, "stack_score": score,
               "annual_usd_estimate": round(pay) if pay else None, "age_days": round(age_days, 1),
               "rank": round(rank, 2)})
    j2["description"] = j2["description"][:1500]
    return j2


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe(jobs: list[dict]) -> list[dict]:
    """Same company + similar title = one role. Prefer the employer's own board, then the richest record."""
    best: dict[str, dict] = {}
    for j in jobs:
        title = re.sub(r"\b(senior|sr|remote|m f d|f m d|w m d)\b", "", norm(j["title"])).strip()
        key = norm(j["company"]).replace(" inc", "").replace(" ltd", "") + "|" + title
        cur = best.get(key)
        if cur is None:
            j["also_on"] = []
            best[key] = j
            continue
        _merge(best, key, cur, j)
    # second pass: board tokens vs display names ("nitkatech" vs "Nitka") with the same title
    items = list(best.items())
    merged: dict[str, dict] = {}
    for key, j in items:
        comp, title = key.split("|", 1)
        c1 = comp.replace(" ", "")
        hit = next((k for k in merged if k.split("|", 1)[1] == title and len(c1) >= 4 and
                    (k.split("|")[0].replace(" ", "").startswith(c1) or c1.startswith(k.split("|")[0].replace(" ", "")))),
                   None)
        if hit:
            _merge(merged, hit, merged[hit], j)
        else:
            merged[key] = j
    return list(merged.values())


def _direct(x):
    return x["source"].split(" (")[0] in ("Greenhouse", "Lever", "Ashby", "Workable")


def _merge(store, key, cur, j):
    if (_direct(j) and not _direct(cur)) or (_direct(j) == _direct(cur) and j["rank"] > cur["rank"]):
        winner, loser = j, cur
        if not _direct(loser) and _direct(winner) and winner["company"].lower() == winner["source"].split("(")[-1].rstrip(")").lower():
            winner["company"] = loser["company"]  # keep the readable company name
        store[key] = winner
    else:
        winner, loser = cur, j
    also = list(dict.fromkeys((winner.get("also_on") or []) + (loser.get("also_on") or []) + [loser["source"]]))
    winner["also_on"] = [a for a in also if a != winner["source"]]


def job_id(j: dict) -> str:
    return norm(j["company"])[:40] + "|" + norm(j["title"])[:60]


# ---------------------------------------------------------------- main

def run_source(name, fn):
    t = time.time()
    try:
        rows = fn()
        report["sources"][name] = {"fetched": len(rows), "seconds": round(time.time() - t, 1)}
        return rows
    except Exception as e:
        report["sources"][name] = {"fetched": 0, "error": f"{type(e).__name__}: {e}"[:300]}
        return []


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def main():
    DATA.mkdir(exist_ok=True)
    companies = json.loads((CONFIG / "companies.json").read_text())
    discovered = load_json(DATA / "discovered_companies.json", {k: [] for k in BOARDS})

    all_jobs: list[dict] = []
    aggregators = {
        "Himalayas": src_himalayas, "Remotive": src_remotive, "Remote OK": src_remoteok, "Jobicy": src_jobicy,
        "We Work Remotely": src_weworkremotely, "Working Nomads": src_workingnomads, "Arbeitnow": src_arbeitnow,
        "Hacker News": src_hackernews,
    }
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(run_source, n, f): n for n, f in aggregators.items()}
        for f in as_completed(futs):
            all_jobs.extend(f.result())

    # merge configured + discovered boards
    newly = discover_boards(all_jobs)
    boards = {k: sorted(set(companies.get(k, [])) | set(discovered.get(k, [])) | set(newly.get(k, [])))
              for k in BOARDS}
    board_counts = {k: {"boards": len(v), "ok": 0, "jobs": 0} for k, v in boards.items()}

    def scan(kind, token):
        try:
            return kind, token, BOARDS[kind](token), None
        except Exception as e:
            return kind, token, [], f"{type(e).__name__}: {e}"[:160]

    with ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(scan, k, t) for k, toks in boards.items() for t in toks]
        for f in as_completed(futs):
            kind, token, rows, err = f.result()
            if err:
                report["failed_boards"].append({"ats": kind, "token": token, "error": err})
            else:
                board_counts[kind]["ok"] += 1
                board_counts[kind]["jobs"] += len(rows)
                all_jobs.extend(rows)
    for k, c in board_counts.items():
        report["sources"][f"{k.title()} company boards"] = c

    # keep discovered boards that worked, so later runs scan them too
    failed = {(b["ats"], b["token"]) for b in report["failed_boards"]}
    merged_disc = {k: sorted({t for t in set(discovered.get(k, [])) | set(newly.get(k, []))
                              if (k, t) not in failed and t not in companies.get(k, [])}) for k in BOARDS}
    (DATA / "discovered_companies.json").write_text(json.dumps(merged_disc, indent=1))

    passed = [e for e in (evaluate(j) for j in all_jobs) if e]
    passed = dedupe(passed)
    passed.sort(key=lambda j: j["rank"], reverse=True)

    seen = load_json(DATA / "seen.json", {})
    for j in passed:
        jid = job_id(j)
        j["id"] = jid
        j["first_seen"] = seen.get(jid, NOW.date().isoformat())
        j["new_today"] = jid not in seen
        seen[jid] = j["first_seen"]
    cutoff = (NOW - timedelta(days=45)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    (DATA / "seen.json").write_text(json.dumps(seen, indent=0, sort_keys=True))

    n = SEARCH["shortlist_size"]
    out = {
        "generated_at": NOW.isoformat(),
        "criteria": {"max_age_days": SEARCH["max_age_days"], "min_annual_usd": SEARCH["min_annual_usd"]},
        "counts": {"fetched": len(all_jobs), "passed_filters": len(passed), "shortlist": min(n, len(passed)),
                   "new_today": sum(1 for j in passed if j["new_today"])},
        "shortlist": passed[:n],
        "others": [{k: j[k] for k in ("title", "company", "url", "source", "location", "posted", "eligibility", "rank")}
                   for j in passed[n:]],
    }
    (DATA / "candidates.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))

    lines = [f"# Job Radar shortlist — {NOW.strftime('%Y-%m-%d %H:%M UTC')}", "",
             f"Fetched {len(all_jobs)} listings, {len(passed)} passed the filters, {out['counts']['new_today']} new today.", "",
             "| # | Role | Company | Where | Posted | Fit | Source |", "|---|---|---|---|---|---|---|"]
    for i, j in enumerate(passed[:n], 1):
        star = " (new)" if j["new_today"] else ""
        lines.append(f"| {i} | [{j['title']}]({j['url']}){star} | {j['company']} | {j['location'][:40]} "
                     f"({j['eligibility']}) | {(j['posted'] or '')[:10]} | {j['rank']} | {j['source']} |")
    (DATA / "candidates.md").write_text("\n".join(lines) + "\n")

    report["counts"] = out["counts"]
    report["failed_boards"].sort(key=lambda b: (b["ats"], b["token"]))
    (DATA / "report.json").write_text(json.dumps(report, indent=1))
    print(json.dumps({"counts": out["counts"], "sources": report["sources"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
