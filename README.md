# Job Radar

Finds remote backend roles (Python/Django/FastAPI or Node.js/TypeScript) that are open to candidates in Nigeria, every morning.

A GitHub Action runs `radar/collect.py` at 05:10 UTC (06:10 in Lagos). It pulls complete listings from public job feeds, filters and ranks them in code, and commits the result to `data/`. A Claude scheduled task reads the shortlist at 06:58 Lagos time, opens the best postings to confirm them, and writes tailored application drafts to the tracker.

## Sources

- **Remote job boards:** Himalayas, Remotive, Remote OK, Jobicy, We Work Remotely (RSS), Working Nomads, Arbeitnow
- **Hacker News "Who is hiring?":** remote posts from the current month's thread
- **Company job boards:** Greenhouse, Lever, Ashby and Workable boards listed in `config/companies.json`, plus boards discovered automatically from links in aggregator listings (`data/discovered_companies.json`)

## Filters (`config/search.json`)

- **Title:** a backend, Python, Node or general software engineering title. Junior, intern, staff, principal, management, frontend, mobile and other-language roles are excluded.
- **Stack:** enough Python/Django/FastAPI or Node/TypeScript/Postgres/Redis signal in the posting.
- **Location:** `yes` for worldwide, EMEA, Africa, Nigeria or a UTC+1-compatible timezone; `maybe` for "Remote", "Europe" or unclear; dropped for postings limited to other countries or that require US work authorization.
- **Recency:** posted within the last 8 days.
- **Pay:** dropped only when a stated salary is below about $12,000 a year. Postings without a salary are kept.

## Output (`data/`)

| File | What it holds |
|---|---|
| `candidates.json` | Ranked shortlist (top 40) with details, plus every other role that passed |
| `candidates.md` | The shortlist as a readable table |
| `report.json` | Listings fetched per source, errors, and company boards that failed to load |
| `discovered_companies.json` | Company boards found through aggregator links, scanned on later runs |
| `seen.json` | Roles seen on earlier runs, so new ones are flagged |

## Run it yourself

```bash
python tests/test_filters.py   # offline filter checks
python -m radar.collect        # needs internet access
```

Or open the **Actions** tab, choose **Collect jobs** and click **Run workflow**.
