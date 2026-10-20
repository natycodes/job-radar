"""End-to-end run with every network call replaced by fixture data (writes to a temp copy)."""
import json, shutil, sys, tempfile, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

src = Path(__file__).resolve().parent.parent
tmp = Path(tempfile.mkdtemp()) / "job-radar"
shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("data", ".git"))
sys.path.insert(0, str(tmp))
from radar import collect as c  # noqa

now = datetime.now(timezone.utc)
iso = lambda d: (now - timedelta(days=d)).isoformat()
DESC = "We build REST APIs with Python, Django, FastAPI, PostgreSQL, Redis and Celery on AWS."
NODE = "Node.js, TypeScript, Express, PostgreSQL, Redis, REST API microservices."

def fake_json(url, **kw):
    if "himalayas.app/jobs/api/search" in url:
        if "page=1" not in url: return {"jobs": []}
        return {"jobs": [{"guid": "h1", "title": "Senior Python Developer", "companyName": "Nitka",
                          "applicationLink": "https://himalayas.app/companies/nitka/jobs/spd",
                          "pubDate": int((now - timedelta(days=1)).timestamp()), "locationRestrictions": [],
                          "description": DESC + " Apply: https://jobs.lever.co/nitkatech/xyz", "seniority": ["Senior"]},
                         {"guid": "h2", "title": "Backend Engineer", "companyName": "USCo",
                          "applicationLink": "https://x/2", "pubDate": int(now.timestamp()),
                          "locationRestrictions": ["United States"], "description": DESC}]}
    if "remotive.com" in url:
        return {"jobs": [{"title": "Backend Engineer (Node.js)", "company_name": "Wavy", "url": "https://remotive.com/1",
                          "publication_date": iso(2), "candidate_required_location": "EMEA", "description": NODE,
                          "salary": "$50k - $70k", "job_type": "full_time"}]}
    if "remoteok.com" in url:
        return [{"legal": "x"}, {"position": "Python Engineer", "company": "Cheapo", "url": "https://remoteok.com/1",
                                 "epoch": int(now.timestamp()), "location": "Worldwide", "description": DESC,
                                 "salary_min": 3000, "salary_max": 6000, "tags": ["python"]}]
    if "jobicy.com" in url:
        return {"jobs": [{"jobTitle": "Senior Django Developer", "companyName": "Acme", "url": "https://jobicy.com/1",
                          "pubDate": iso(3), "jobGeo": "Anywhere", "jobDescription": DESC}]}
    if "workingnomads" in url:
        return [{"title": "Junior Python Dev", "company_name": "J", "url": "https://wn/1", "pub_date": iso(1),
                 "location": "Anywhere", "description": DESC, "category_name": "Development", "tags": ""}]
    if "arbeitnow" in url:
        return {"data": []}
    if "search_by_date" in url:
        return {"hits": [{"title": "Ask HN: Who is hiring? (October 2026)", "objectID": "999"}]}
    if "hn.algolia.com/api/v1/items/999" in url:
        return {"children": [{"id": 1, "created_at": iso(2),
                              "text": "Fintechy | Senior Backend Engineer | REMOTE (EMEA) | Python, Django, Postgres, Redis"},
                             {"id": 2, "created_at": iso(2), "text": "OnSite Inc | NYC | Python"}]}
    if "boards-api.greenhouse.io/v1/boards/gitlab" in url:
        return {"jobs": [{"title": "Backend Engineer (Python), AI", "absolute_url": "https://gh/1",
                          "first_published": iso(2), "updated_at": iso(1), "location": {"name": "Remote, EMEA"},
                          "content": "&lt;p&gt;" + DESC + "&lt;/p&gt;", "company_name": "GitLab"},
                         {"title": "Backend Engineer (Ruby)", "absolute_url": "https://gh/2", "first_published": iso(2),
                          "location": {"name": "Remote, EMEA"}, "content": DESC}]}
    if "api.lever.co/v0/postings/nitkatech" in url:
        return [{"text": "Senior Python Developer", "hostedUrl": "https://jobs.lever.co/nitkatech/xyz",
                 "createdAt": int((now - timedelta(days=1)).timestamp() * 1000),
                 "categories": {"location": "Remote - Worldwide", "commitment": "Full-time"},
                 "descriptionPlain": DESC}]
    if "api.ashbyhq.com/posting-api/job-board/supabase" in url:
        return {"jobs": [{"title": "Backend Engineer (Node/TypeScript)", "location": "Remote, Global", "isRemote": True,
                          "publishedAt": iso(4), "jobUrl": "https://ash/1", "descriptionPlain": NODE}]}
    raise c.HTTPError(url, 404, "nf", {}, None)

def fake_fetch(url, **kw):
    if "weworkremotely" in url and "back-end" in url:
        d = (now - timedelta(days=1)).strftime("%a, %d %b %Y %H:%M:%S +0000")
        return f"""<rss><channel><item><title>Proxify: Senior Backend Developer (Python)</title>
        <link>https://wwr/1</link><pubDate>{d}</pubDate><region>Anywhere in the World</region>
        <description>{DESC}</description></item></channel></rss>""".encode()
    raise c.HTTPError(url, 404, "nf", {}, None)

c.fetch_json = fake_json
c.fetch = fake_fetch
c.time.sleep = lambda s: None
t = time.time()
c.main()
out = json.loads((tmp / "data/candidates.json").read_text())
rep = json.loads((tmp / "data/report.json").read_text())
print("\n--- shortlist ---")
for j in out["shortlist"]:
    print(f"{j['rank']:>6}  {j['title']} @ {j['company']} | {j['source']} | {j['eligibility']} | also_on={j.get('also_on')}")
print("discovered:", json.loads((tmp / "data/discovered_companies.json").read_text()))
print("failed boards:", len(rep["failed_boards"]), "| seconds:", round(time.time() - t, 1))
print((tmp / "data/candidates.md").read_text()[:900])
