"""Network sources. Every function returns a list[Job] and raises on hard failure
(the runner catches errors per source and records them in data/health.json)."""
from __future__ import annotations

import random
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from .core import Job, html_to_text, to_iso

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
S = requests.Session()
S.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})


def nap(a=2.0, b=5.0):
    time.sleep(random.uniform(a, b))


def get(url, **kw):
    for attempt in range(3):
        r = S.get(url, timeout=30, **kw)
        if r.status_code == 429:
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    r.raise_for_status()
    return r


def days_ago(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).date().isoformat()


# ------------------------------------------------------------ LinkedIn + Naukri (via JobSpy)
def jobspy_search(site: str, term: str, location: str, hours_old: int, results: int) -> list[Job]:
    from jobspy import scrape_jobs
    df = scrape_jobs(
        site_name=[site],
        search_term=term,
        location=location,
        hours_old=hours_old,
        results_wanted=results,
        country_indeed="India",
        linkedin_fetch_description=(site == "naukri"),  # naukri descriptions come free with search
        description_format="markdown",
        verbose=0,
    )
    jobs = []
    if df is None or df.empty:
        return jobs
    for _, r in df.iterrows():
        val = lambda k: (None if k not in r or r[k] != r[k] else r[k])  # NaN-safe
        j = Job(
            company=str(val("company") or ""),
            title=str(val("title") or ""),
            location=str(val("location") or ""),
            url=str(val("job_url_direct") or val("job_url") or ""),
            source=site,
            posted=to_iso(val("date_posted")),
            description=str(val("description") or ""),
            exp_text=str(val("experience_range") or ""),
            salary_min=val("min_amount"),
            salary_max=val("max_amount"),
            currency=val("currency"),
        )
        if site == "linkedin":
            j.url = str(val("job_url") or j.url)
        jobs.append(j)
    return jobs


def linkedin_description(job_url: str) -> str:
    m = re.search(r"(\d{8,})", job_url or "")
    if not m:
        return ""
    r = get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}")
    mm = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    return html_to_text(mm.group(1)) if mm else ""


# ------------------------------------------------------------ SmartRecruiters (Visa, PhonePe)
def smartrecruiters(cfg: dict) -> list[Job]:
    cid, country = cfg["company_id"], cfg.get("country", "in")
    out, offset = [], 0
    while True:
        r = get(f"https://api.smartrecruiters.com/v1/companies/{cid}/postings",
                params={"country": country, "limit": 100, "offset": offset})
        d = r.json()
        for p in d.get("content", []):
            loc = p.get("location") or {}
            out.append(Job(
                company="", title=p.get("name", ""),
                location=", ".join(x for x in [loc.get("city"), loc.get("country", "").upper()] if x),
                url=f"https://jobs.smartrecruiters.com/{cid}/{p.get('id')}",
                source="careers portal (SmartRecruiters)",
                posted=to_iso(p.get("releasedDate")),
            ))
            out[-1]._detail = p.get("ref")
        offset += 100
        if offset >= d.get("totalFound", 0) or not d.get("content"):
            break
        nap(1, 2)
    return out


def smartrecruiters_detail(job: Job) -> None:
    ref = getattr(job, "_detail", None)
    if not ref:
        return
    d = get(ref).json()
    secs = (d.get("jobAd") or {}).get("sections") or {}
    job.description = html_to_text(" ".join((v or {}).get("text", "") for v in secs.values()))
    job.url = d.get("postingUrl") or job.url


# ------------------------------------------------------------ Workday (Walmart)
def workday(cfg: dict) -> list[Job]:
    t, site, wd = cfg["tenant"], cfg["site"], cfg.get("wd", "wd5")
    base = f"https://{t}.{wd}.myworkdayjobs.com/wday/cxs/{t}/{site}"
    seen, out = set(), []
    for term in cfg.get("search", [""]):
        for offset in range(0, 100, 20):
            r = S.post(f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
                       headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=30)
            r.raise_for_status()
            posts = r.json().get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath")
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = p.get("locationsText", "")
                if not re.search(r"\bIN\b|india|bangalore|bengaluru|chennai|gurgaon|gurugram|hyderabad|mumbai|pune|noida|delhi", loc + " " + path, re.I):
                    continue
                j = Job(company="", title=p.get("title", ""), location=loc if re.search(r"[a-z]{4}", loc.lower()) and "locations" not in loc.lower() else path.split("/")[2].replace("-", " "),
                        url=f"https://{t}.{wd}.myworkdayjobs.com/{site}{path}", source="careers portal (Workday)",
                        posted=_workday_posted(p.get("postedOn", "")))
                j._detail = f"{base}{path}"
                out.append(j)
            if len(posts) < 20:
                break
            nap(1, 2)
    return out


def _workday_posted(s: str):
    s = s.lower()
    if "today" in s:
        return days_ago(0)
    if "yesterday" in s:
        return days_ago(1)
    m = re.search(r"(\d+)\+?\s*days", s)
    return days_ago(int(m.group(1))) if m else None


def workday_detail(job: Job) -> None:
    d = get(job._detail, headers={"Accept": "application/json"}).json()
    info = d.get("jobPostingInfo", {})
    job.description = html_to_text(info.get("jobDescription", ""))
    job.location = info.get("location") or job.location
    if info.get("startDate"):
        job.posted = to_iso(info["startDate"])


# ------------------------------------------------------------ Eightfold (American Express)
def eightfold(cfg: dict) -> list[Job]:
    host, domain = cfg["host"], cfg["domain"]
    out = []
    for start in range(0, 300, 10):
        r = get(f"https://{host}/api/apply/v2/jobs",
                params={"domain": domain, "location": "India", "start": start, "num": 10, "sort_by": "timestamp"})
        d = r.json()
        pos = d.get("positions", [])
        for p in pos:
            ts = p.get("t_update") or p.get("t_create")
            j = Job(company="", title=p.get("name", ""), location=p.get("location", ""),
                    url=p.get("canonicalPositionUrl") or f"https://{host}/careers/job/{p.get('id')}",
                    source="careers portal (Eightfold)",
                    posted=datetime.fromtimestamp(ts, timezone.utc).date().isoformat() if ts else None)
            j._detail = f"https://{host}/api/apply/v2/jobs/{p.get('id')}?domain={domain}"
            out.append(j)
        if len(pos) < 10 or (out and out[-1].posted and out[-1].posted < days_ago(10)):
            break
        nap(1, 2)
    return out


def eightfold_detail(job: Job) -> None:
    d = get(job._detail).json()
    job.description = html_to_text(d.get("job_description", ""))


# ------------------------------------------------------------ Uber
def uber(cfg: dict) -> list[Job]:
    out = []
    for page in range(0, 10):
        r = S.post("https://www.uber.com/api/loadSearchJobsResults?localeCode=en",
                   json={"params": {"location": [{"country": cfg.get("country", "IND")}], "department": [], "team": []},
                         "page": page, "limit": 50},
                   headers={"x-csrf-token": "x", "Content-Type": "application/json"}, timeout=30)
        r.raise_for_status()
        res = (r.json().get("data") or {}).get("results") or []
        for p in res:
            loc = p.get("location") or {}
            j = Job(company="", title=p.get("title", ""),
                    location=", ".join(x for x in [loc.get("city"), loc.get("countryName")] if x),
                    url=f"https://www.uber.com/global/en/careers/list/{p.get('id')}/",
                    source="careers portal (Uber)", posted=to_iso(p.get("creationDate") or p.get("updatedDate")),
                    description=html_to_text(p.get("description", "")))
            out.append(j)
        if len(res) < 50:
            break
        nap(1, 2)
    return out


# ------------------------------------------------------------ MyNextHire (Swiggy)
def mynexthire(cfg: dict) -> list[Job]:
    t = cfg["tenant"]
    r = S.post(f"https://{t}.mynexthire.com/employer/careers/reqlist/get",
               json={"source": "careers", "code": "", "filterByBuId": -1},
               headers={"Content-Type": "application/json"}, timeout=30)
    r.raise_for_status()
    data = r.json()
    reqs = data.get("reqDetailsBOList") or data.get("reqList") or []
    out = []
    for p in reqs:
        rid = p.get("reqId") or p.get("id")
        out.append(Job(company="", title=p.get("reqTitle") or p.get("title", ""),
                       location=p.get("location") or p.get("locationName") or "",
                       url=f"https://{t}.mynexthire.com/employer/jobs?src=careers&p={rid}" if rid else f"https://{t}.mynexthire.com/",
                       source="careers portal (MyNextHire)",
                       posted=to_iso(p.get("publishedDate") or p.get("createdDate") or p.get("postedDate")),
                       description=html_to_text(p.get("jdDisplay") or p.get("jobDescription") or ""),
                       exp_text=f"{p.get('minExp','')}-{p.get('maxExp','')} years" if p.get("minExp") is not None else ""))
    return out


ATS = {
    "smartrecruiters": (smartrecruiters, smartrecruiters_detail),
    "workday": (workday, workday_detail),
    "eightfold": (eightfold, eightfold_detail),
    "uber": (uber, None),
    "mynexthire": (mynexthire, None),
}
