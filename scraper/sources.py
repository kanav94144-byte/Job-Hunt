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
                source="careers portal",
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
                if not re.search(r"^/job/IN-|\bIndia\b|\bIN[ -]+(KA|TN|HR|MH|TS|DL|UP|GJ|WB)\b|Bengaluru|Bangalore|Chennai|Gurugram|Gurgaon|Hyderabad|Mumbai|Pune|Noida", loc + " " + path, re.I) \
                        or re.search(r"\(USA\)|, IN$|Indianapolis|Indiana", loc):
                    continue
                j = Job(company="", title=p.get("title", ""), location=loc if re.search(r"[a-z]{4}", loc.lower()) and "locations" not in loc.lower() else path.split("/")[2].replace("-", " "),
                        url=f"https://{t}.{wd}.myworkdayjobs.com/{site}{path}", source="careers portal",
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
                    source="careers portal", posted=to_iso(p.get("creationDate") or p.get("updatedDate")),
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
               headers={"Content-Type": "application/json"}, timeout=40)
    r.raise_for_status()
    out = []
    for p in r.json().get("reqDetailsBOList") or []:
        emin, emax = p.get("expMin"), p.get("expMax")
        out.append(Job(company="", title=p.get("reqTitle") or p.get("designation") or "",
                       location=p.get("location") or "",
                       url=cfg.get("careers_url", f"https://{t}.mynexthire.com/employer/jobs/careers#?src=careers&page=careers"),
                       source="careers portal", posted=to_iso(p.get("approvedOn")),
                       description=html_to_text(p.get("jdDisplay") or ""),
                       exp_text=f"{emin:g}-{emax:g} years experience" if emin is not None and emax else ""))
    return out


# ------------------------------------------------------------ Oracle HCM Candidate Experience (American Express)
def oracle_hcm(cfg: dict) -> list[Job]:
    host, site = cfg["api_host"], cfg["site"]
    out = []
    for offset in range(0, 500, 50):
        finder = (f"findReqs;siteNumber={site},facetsList=NONE,limit=50,offset={offset},"
                  f"sortBy=POSTING_DATES_DESC" + (f",locationId={cfg['location_id']}" if cfg.get("location_id") else ""))
        r = get(f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                params={"onlyData": "true", "expand": "requisitionList.secondaryLocations", "finder": finder})
        items = (r.json().get("items") or [{}])[0].get("requisitionList") or []
        for p in items:
            j = Job(company="", title=p.get("Title", ""), location=p.get("PrimaryLocation", ""),
                    url=f"{cfg['public_base']}/job/{p.get('Id')}", source="careers portal",
                    posted=to_iso(p.get("PostedDate")))
            j._detail = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
                         f"?expand=all&onlyData=true&finder=ById;Id=%22{p.get('Id')}%22,siteNumber={site}")
            out.append(j)
        if len(items) < 50 or (items and to_iso(items[-1].get("PostedDate")) and to_iso(items[-1].get("PostedDate")) < days_ago(10)):
            break
        nap(1, 2)
    return out


def oracle_hcm_detail(job: Job) -> None:
    d = (get(job._detail).json().get("items") or [{}])[0]
    job.description = html_to_text(" ".join(str(d.get(k) or "") for k in
                                            ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")))


# ------------------------------------------------------------ Darwinbox (Porter, Rapido, Tata 1mg …)
def darwinbox(cfg: dict) -> list[Job]:
    t = cfg["tenant"]
    base = f"https://{t}.darwinbox.in"
    out = []
    for page in range(1, 11):
        r = S.post(f"{base}/ms/candidateapi/job/alljobs?companyId=main", json={"page": page, "limit": 100},
                   headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=40)
        r.raise_for_status()
        d = r.json()
        rows = d.get("data") or []
        for p in rows:
            ts = p.get("posted_on") or 0
            loc = (p.get("officelocations_without_area") or [p.get("locations") or ""])[0]
            sal_min = _num(p.get("salary_min")); sal_max = _num(p.get("salary_max"))
            out.append(Job(company="", title=p.get("title") or p.get("designation_name") or "",
                           location=re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", loc)).strip(),
                           url=f"{base}/ms/candidatev2/main/careers/jobDetails/{p.get('id')}",
                           source="careers portal",
                           posted=datetime.fromtimestamp(int(ts), timezone.utc).date().isoformat() if ts else None,
                           description=html_to_text(html_unescape(p.get("jd") or "")),
                           exp_text=(p.get("experience") or "") + " experience" if p.get("experience") else "",
                           salary_min=sal_min, salary_max=sal_max,
                           currency=(p.get("salary_currency") or "INR") if sal_max else None))
        if len(rows) < 100 or page * 100 >= (d.get("job_counts") or 0):
            break
        nap(1, 2)
    return out


def _num(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if v > 0 else None
    except Exception:
        return None


def html_unescape(s):
    import html as _h
    return _h.unescape(s)


# ------------------------------------------------------------ Keka (SolarSquare …)
def keka(cfg: dict) -> list[Job]:
    t = cfg["tenant"]
    r = get(f"https://{t}.keka.com/careers/api/jobs/default/active")
    out = []
    for p in r.json() if isinstance(r.json(), list) else []:
        sr = p.get("salaryRange") or {}
        lo, hi = _num(sr.get("minimum")), _num(sr.get("maximum"))
        if hi and hi < 300000:          # monthly figures
            lo, hi = (lo or 0) * 12 or None, hi * 12
        locs = p.get("jobLocations") or []
        loc = ", ".join(filter(None, [(l.get("city") or l.get("name") or "") for l in locs if isinstance(l, dict)])) or "India"
        out.append(Job(company="", title=p.get("title", ""), location=loc,
                       url=f"https://{t}.keka.com/careers/jobdetails/{p.get('id')}", source="careers portal",
                       posted=to_iso(p.get("publishedOn")), description=html_to_text(p.get("description") or ""),
                       exp_text=(p.get("experience") or "") + " experience" if p.get("experience") else "",
                       salary_min=lo, salary_max=hi, currency=sr.get("currency") if hi else None))
    return out


# ------------------------------------------------------------ MakeMyTrip careers API
def makemytrip(cfg: dict) -> list[Job]:
    d = get("https://careers.makemytrip.com/api/jobs").json()
    out = []
    for p in d.get("allJobs") or []:
        created = p.get("job_created_timestamp") or ""
        m = re.match(r"(\d{2})-(\d{2})-(\d{4})", created)
        slug = re.sub(r"\s+", "-", (p.get("job_title") or "").strip().lower())
        out.append(Job(company="", title=p.get("job_title", ""), location=", ".join(p.get("location_city") or []),
                       url=f"https://careers.makemytrip.com/prod/opportunity/{p.get('job_id')}/{slug}",
                       source="careers portal", posted=f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else None,
                       exp_text=f"{p.get('experience_from')}-{p.get('experience_to')} years experience" if p.get("experience_to") else ""))
    return out


# ------------------------------------------------------------ Urban Company careers API
def urbancompany(cfg: dict) -> list[Job]:
    r = S.post("https://www.urbanclap.com/api/v2/platform-gateway/getAllJobs", json={},
               headers={"Content-Type": "application/json", "Origin": "https://careers.urbancompany.com"}, timeout=30)
    r.raise_for_status()
    return [Job(company="", title=p.get("job_title", ""), location=", ".join(p.get("location_city") or []),
                url=p.get("apply_url") or f"https://careers.urbancompany.com/jobDetail?id={p.get('job_id')}",
                source="careers portal", description=html_to_text(p.get("job_description") or ""))
            for p in r.json().get("jobs") or []]


# ------------------------------------------------------------ Avature (Deloitte USI)
def avature(cfg: dict) -> list[Job]:
    base = cfg["base"]           # e.g. https://usijobs.deloitte.com/en_US/careersUSI
    out, seen = [], set()
    for offset in range(0, cfg.get("max_jobs", 200), 50):
        h = get(f"{base}/SearchJobs/?jobRecordsPerPage=50&jobOffset={offset}").text
        found = 0
        for m in re.finditer(r'<a href="(https://[^"]+/JobDetail/[^"]+/(\d+))"[^>]*>\s*([^<]+?)\s*</a>', h):
            url, jid, title = m.group(1), m.group(2), html_to_text(m.group(3))
            if jid in seen:
                continue
            seen.add(jid); found += 1
            tail = h[m.end(): m.end() + 1500]
            spans = [html_to_text(x) for x in re.findall(r"<span>\s*([^<]{2,120}?)\s*</span>", tail)]
            loc = next((x for x in spans if re.search(r"India|Multiple Locations", x) and not re.search(r"Deloitte|Private Limited|LLP", x)), "India")
            j = Job(company="", title=title, location=loc, url=url, source="careers portal")
            j._detail = url
            out.append(j)
        if found == 0:
            break
        nap(1, 2)
    return out


def html_detail(job: Job) -> None:
    text = html_to_text(get(job._detail).text)
    if job.location in ("India", "Multiple Locations", ""):
        m = re.search(r"([A-Z][A-Za-z]+(?: [A-Z][a-z]+)?, [A-Z][A-Za-z]+(?: [A-Z][a-z]+)?, India)", text)
        if m:
            job.location = m.group(1)
    k = text.find("Position Summary")
    job.description = (text[k:] if k >= 0 else text)[:20000]


# ------------------------------------------------------------ Google Careers
def google_careers(cfg: dict) -> list[Job]:
    out, seen = [], set()
    for page in range(1, cfg.get("pages", 6) + 1):
        h = get("https://www.google.com/about/careers/applications/jobs/results/",
                params={"location": "India", "sort_by": "date", "page": page}).text
        titles = [html_to_text(t) for t in re.findall(r"<h3[^>]*>(.*?)</h3>", h, re.S)]
        ids = []
        for m in re.finditer(r'jobs/results/(\d{6,})-([a-z0-9-]+)', h):
            if m.group(1) not in seen and m.group(1) not in [i for i, _ in ids]:
                ids.append((m.group(1), m.group(2)))
        if not ids:
            break
        for n, (jid, slug) in enumerate(ids):
            seen.add(jid)
            title = titles[n] if n < len(titles) and titles[n] else slug.replace("-", " ").title()
            j = Job(company="", title=title, location="India",
                    url=f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}", source="careers portal")
            j._detail = j.url
            out.append(j)
        nap(1, 3)
    return out


def google_detail(job: Job) -> None:
    t = html_to_text(get(job._detail).text)
    m = re.search(r"(Bengaluru|Bangalore|Gurugram|Gurgaon|Hyderabad|Mumbai|Pune|Chennai|New Delhi)[^.\n]{0,40}India", t)
    if m:
        job.location = m.group(0)
    k = t.find("Minimum qualifications")
    job.description = t[k:k + 6000] if k >= 0 else t[:6000]


# ------------------------------------------------------------ auto-discovery of portals by slug
def probe_portals(slugs: list[str], global_ats: bool = False) -> dict | None:
    """Try common Indian-startup ATS hosts for a company slug. Returns an ats cfg or None."""
    for slug in slugs:
        tries = [
            ("darwinbox", lambda: S.post(f"https://{slug}.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main",
                                         json={"page": 1, "limit": 1}, timeout=15)),
            ("keka", lambda: S.get(f"https://{slug}.keka.com/careers/api/jobs/default/active", timeout=15)),
            ("greenhouse", lambda: S.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=15)),
            ("lever", lambda: S.get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", timeout=15)),
            ("ashby", lambda: S.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=15)),
        ]
        for kind, call in tries:
            if kind in ("greenhouse", "lever", "ashby") and not global_ats:
                continue
            try:
                r = call()
                if r.status_code != 200 or "json" not in r.headers.get("content-type", ""):
                    continue
                d = r.json()
                ok = (kind == "darwinbox" and d.get("status") == "success") or \
                     (kind == "keka" and isinstance(d, list) and d) or \
                     (kind == "greenhouse" and d.get("jobs")) or \
                     (kind == "lever" and isinstance(d, list) and d) or \
                     (kind == "ashby" and d.get("jobs"))
                if ok:
                    return {"type": kind, "tenant": slug, "probed": True}
            except Exception:
                pass
    return None


# ------------------------------------------------------------ Greenhouse / Lever / Ashby (generic)
def greenhouse(cfg: dict) -> list[Job]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['tenant']}/jobs", params={"content": "true"}).json()
    return [Job(company="", title=p.get("title", ""), location=(p.get("location") or {}).get("name", ""),
                url=p.get("absolute_url", ""), source="careers portal", posted=to_iso(p.get("updated_at")),
                description=html_to_text(html_unescape(p.get("content") or ""))) for p in d.get("jobs", [])]


def lever(cfg: dict) -> list[Job]:
    d = get(f"https://api.lever.co/v0/postings/{cfg['tenant']}", params={"mode": "json"}).json()
    return [Job(company="", title=p.get("text", ""), location=(p.get("categories") or {}).get("location", ""),
                url=p.get("hostedUrl", ""), source="careers portal",
                posted=datetime.fromtimestamp(p["createdAt"] / 1000, timezone.utc).date().isoformat() if p.get("createdAt") else None,
                description=html_to_text((p.get("descriptionPlain") or "") + " " + " ".join(
                    (l.get("content") or "") for l in p.get("lists") or [])))
            for p in d]


def ashby(cfg: dict) -> list[Job]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{cfg['tenant']}").json()
    return [Job(company="", title=p.get("title", ""), location=p.get("location", ""), url=p.get("jobUrl", ""),
                source="careers portal", posted=to_iso(p.get("publishedAt")),
                description=p.get("descriptionPlain") or "") for p in d.get("jobs", [])]


# ------------------------------------------------------------ iimjobs (public search)
def iimjobs(term: str, days: int) -> list[Job]:
    posting = 1 if days <= 1 else (3 if days <= 3 else 7)
    out = []
    for page in range(0, 5):
        d = get("https://gladiator.iimjobs.com/job/search",
                params={"query": term, "page": page, "posting": posting, "industry": ""},
                headers={"Origin": "https://www.iimjobs.com", "Referer": "https://www.iimjobs.com/"}).json()
        for p in d.get("data") or []:
            co = ((p.get("companyData") or {}).get("companyName") or "").strip()
            title = (p.get("jobdesignation") or p.get("title") or "").strip()
            locs = [l.get("name", "") for l in p.get("locations") or [] if l.get("name") not in ("Others", "Anywhere in India")]
            ts = p.get("createdTime") or p.get("createdTimeMs")
            j = Job(company=co or (p.get("title", "").split(" - ")[0]), title=title,
                    location=", ".join(locs[:1]) if locs else "India", url=p.get("jobDetailUrl", ""), source="iimjobs",
                    posted=datetime.fromtimestamp(ts / 1000, timezone.utc).date().isoformat() if ts else None,
                    description=(p.get("title", "") + ". " + ", ".join(t.get("name", "") for t in p.get("tags") or [])),
                    exp_text=f"{p.get('min')}-{p.get('max')} years experience" if p.get("max") else "")
            if not p.get("hideSal") and p.get("maxSal"):
                j.salary_min, j.salary_max, j.currency = float(p.get("minSal") or 0) * 1e5, float(p["maxSal"]) * 1e5, "INR"
            j._all_locations = locs
            out.append(j)
        if not d.get("hasMore"):
            break
        nap(1, 2)
    return out


# ------------------------------------------------------------ Naukri (collected in the browser, saved as data/naukri_inbox.json)
def naukri_inbox(path, max_age_days: int = 3) -> list[Job]:
    import json
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return []
    d = json.loads(p.read_text())
    if (d.get("collected") or "") < days_ago(max_age_days):
        return []
    out = []
    for r in d.get("jobs", []):
        u = r.get("u", "")
        if u.startswith("~"):
            u = "https://www.naukri.com/job-listings-" + u[1:]
        j = Job(company=r.get("c", ""), title=r.get("t", ""), location=r.get("l", ""),
                url=u, source="naukri", posted=r.get("d"), exp_text=r.get("e", ""),
                description=html_to_text(r.get("j", "")) + " " + (r.get("k") or ""))
        if r.get("smax"):
            j.salary_min, j.salary_max, j.currency = r.get("smin"), r["smax"], "INR"
        out.append(j)
    return out


ATS = {
    "smartrecruiters": (smartrecruiters, smartrecruiters_detail),
    "workday": (workday, workday_detail),
    "oracle_hcm": (oracle_hcm, oracle_hcm_detail),
    "uber": (uber, None),
    "mynexthire": (mynexthire, None),
    "darwinbox": (darwinbox, None),
    "keka": (keka, None),
    "makemytrip": (makemytrip, None),
    "urbancompany": (urbancompany, None),
    "avature": (avature, html_detail),
    "google": (google_careers, google_detail),
    "greenhouse": (greenhouse, None),
    "lever": (lever, None),
    "ashby": (ashby, None),
}
