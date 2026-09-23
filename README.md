# Job Hunt

Every morning at 9am IST, GitHub scans careers portals, LinkedIn and Naukri for strategy, growth, program, Founder's Office and similar roles. It filters them against my profile and updates the dashboard.

**Dashboard:** https://kanav94144-byte.github.io/Job-Hunt/

## How roles are sorted
| Tier | Meaning |
|---|---|
| **A** | MBA / PG asked or preferred · 1–5 yrs · pay ≥ ₹20 LPA (stated or estimated) |
| **B** | MBA not mentioned, but the role is relevant to my CV |
| **C** | Stretch: asks 5–6 yrs, pay may be low, or weak match |

## Where roles come from
1. **Company careers sites**, read in parallel: your list (`config/companies.yml`), a wider SaaS / B2B / consumer list
   (`config/seed_companies.yml`), and an **auto-watchlist** (`data/companies_auto.json`) of companies the scan found
   posting good roles on its own. Portals are recognised from apply links (Lever, Greenhouse, Ashby, Workday,
   Darwinbox, Keka, Workable, Recruitee, SmartRecruiters) and remembered in `data/portals.json`.
2. **LinkedIn** by company name, for companies on your list without a readable careers site.
3. **Open searches** on LinkedIn, Indeed and Google Jobs (`discovery_searches` / `discovery_sites`).
4. **iimjobs** (full job description read for each role) and **Naukri** (collected in the browser).

## What to edit
- `config/companies.yml` – your list (gets its own LinkedIn search), open searches, iimjobs searches
- `config/seed_companies.yml` – wider list, careers sites only (cheap to grow)
- `config/profile.yml` – keywords, excluded roles, experience range, salary floor, watchlist settings
- Check the offline tests still pass after code changes: `python -m tests.test_offline`

## Run it now
Go to **Actions → Daily job scan → Run workflow**.
Today's new roles appear on the dashboard, and in the run's **Summary** tab.
