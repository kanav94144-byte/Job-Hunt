"""Network sources. Every function returns a list[Job] and raises on hard failure
(the runner catches errors per source and records them in data/health.json)."""
from __future__ import annotations

import json
import random
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlparse

import requests

from .core import Job, html_to_text, to_iso

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class _ThreadSession:
    """One requests.Session per thread (portals are fetched in parallel; Session isn't thread-safe)."""
    _local = threading.local()

    def _s(self) -> requests.Session:
        s = getattr(self._local, "s", None)
        if s is None:
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            self._local.s = s
        return s

    def __getattr__(self, k):
        return getattr(self._s(), k)


S = _ThreadSession()


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


# ------------------------------------------------------------ LinkedIn / Indeed / Google Jobs / Naukri (via JobSpy)
_INDEED_COUNTRY = {"india": "India", "united arab emirates": "United Arab Emirates", "dubai": "United Arab Emirates",
                   "singapore": "Singapore", "united kingdom": "UK", "london": "UK"}


def jobspy_search(site: str, term: str, location: str, hours_old: int, results: int) -> list[Job]:
    from jobspy import scrape_jobs
    loc_l = (location or "").lower()
    country = next((v for k, v in _INDEED_COUNTRY.items() if k in loc_l), "India")
    df = scrape_jobs(
        site_name=[site],
        search_term=term,
        location=location,
        hours_old=hours_old,
        results_wanted=results,
        country_indeed=country,
        linkedin_fetch_description=False,  # we fetch LinkedIn details ourselves, only for roles that survive filters
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
        j._apply_url = str(val("job_url_direct") or "")   # often the company's own ATS link -> portal discovery
        jobs.append(j)
    return jobs


# LinkedIn's guest pages rate-limit per IP. Instead of sleeping 20s+40s on every 429 (which stalls the run),
# stop asking LinkedIn for details after a few 429s in a row and let the rest of the run finish.
LINKEDIN = {"consecutive_429": 0, "blocked": False, "fetched": 0}


def linkedin_detail(job: Job) -> None:
    """Fill job.description (and job._apply_url when the role applies on the company's own site)."""
    if LINKEDIN["blocked"]:
        return
    m = re.search(r"(\d{8,})", job.url or "")
    if not m:
        return
    r = S.get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}", timeout=30)
    if r.status_code == 429:
        LINKEDIN["consecutive_429"] += 1
        if LINKEDIN["consecutive_429"] >= 4:
            LINKEDIN["blocked"] = True
        time.sleep(8)
        return
    r.raise_for_status()
    LINKEDIN["consecutive_429"] = 0
    LINKEDIN["fetched"] += 1
    mm = re.search(r'show-more-less-html__markup[^>]*>(.*?)</div>', r.text, re.S)
    job.description = html_to_text(mm.group(1)) if mm else ""
    am = re.search(r'id="applyUrl"[^>]*>\s*<!--\s*"?([^"<]+)', r.text)
    if am:
        q = parse_qs(urlparse(am.group(1)).query).get("url")
        job._apply_url = unquote(q[0]) if q else am.group(1)


def linkedin_description(job_url: str) -> str:      # kept for backwards compatibility
    j = Job(company="", title="", url=job_url)
    linkedin_detail(j)
    return j.description


# ------------------------------------------------------------ any job page with schema.org JobPosting (iimjobs, Google Jobs links …)
def jsonld_detail(job: Job) -> None:
    """Read the JobPosting JSON-LD block most job pages embed for Google for Jobs.
    Deliberately does NOT fall back to the whole page text: site menus often say "MBA jobs",
    which would falsely mark every role as MBA-asked."""
    url = getattr(job, "_detail", None) or job.url
    if not url:
        return
    h = get(url).text
    post = _jobposting_from_html(h)
    if post:
        desc = html_to_text(html_unescape(str(post.get("description") or "")))
        edu = post.get("educationRequirements")
        if isinstance(edu, dict):
            edu = edu.get("credentialCategory") or edu.get("name")
        exp = post.get("experienceRequirements")
        if isinstance(exp, dict):
            months = exp.get("monthsOfExperience")
            exp = f"{float(months) / 12:g}+ years experience" if months else exp.get("description")
        job.description = "\n".join(x for x in [desc, f"Education: {edu}" if edu else "", str(exp or "")] if x)
        if not job.exp_text and exp:
            job.exp_text = str(exp)
        return
    # Next.js pages: look for the longest "...description" string in the page data
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h, re.S)
    if m:
        try:
            best = max(_strings_under(json.loads(m.group(1)), re.compile(r"desc|^jd$", re.I)), key=len, default="")
            if len(best) > 200:
                job.description = html_to_text(html_unescape(best))
        except ValueError:
            pass


def _jobposting_from_html(h: str) -> dict | None:
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', h, re.S | re.I):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        for item in (d if isinstance(d, list) else d.get("@graph", [d]) if isinstance(d, dict) else []):
            if isinstance(item, dict) and "JobPosting" in str(item.get("@type", "")):
                return item
    return None


def _strings_under(o, key_re, depth=0):
    if depth > 12:
        return
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(v, str) and key_re.search(k):
                yield v
            else:
                yield from _strings_under(v, key_re, depth + 1)
    elif isinstance(o, list):
        for v in o:
            yield from _strings_under(v, key_re, depth + 1)


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
GLOBAL_ATS = ("greenhouse", "lever", "ashby", "workable", "recruitee", "smartrecruiters")


def probe_portals(slugs: list[str], global_ats: bool = False) -> dict | None:
    """Try common ATS hosts for a company slug. Returns an ats cfg or None.
    Indian ATSs (Darwinbox, Keka) are always tried; global ones only when the company has explicit slugs,
    because a generic slug on Lever/Greenhouse often belongs to a different company abroad."""
    for slug in slugs:
        tries = [
            ("darwinbox", lambda: S.post(f"https://{slug}.darwinbox.in/ms/candidateapi/job/alljobs?companyId=main",
                                         json={"page": 1, "limit": 1}, timeout=10)),
            ("keka", lambda: S.get(f"https://{slug}.keka.com/careers/api/jobs/default/active", timeout=10)),
            ("greenhouse", lambda: S.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=10)),
            ("lever", lambda: S.get(f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=1", timeout=10)),
            ("ashby", lambda: S.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=10)),
            ("workable", lambda: S.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", timeout=10)),
            ("recruitee", lambda: S.get(f"https://{slug}.recruitee.com/api/offers/", timeout=10)),
            ("smartrecruiters", lambda: S.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                                              params={"limit": 1}, timeout=10)),
        ]
        for kind, call in tries:
            if kind in GLOBAL_ATS and not global_ats:
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
                     (kind == "ashby" and d.get("jobs")) or \
                     (kind == "workable" and d.get("jobs")) or \
                     (kind == "recruitee" and d.get("offers")) or \
                     (kind == "smartrecruiters" and d.get("totalFound"))
                if ok:
                    cfg = {"type": kind, "tenant": slug, "probed": True}
                    if kind == "smartrecruiters":
                        cfg.update(company_id=slug, country="in")
                    return cfg
            except Exception:
                pass
    return None


# ------------------------------------------------------------ recognise a company's ATS from any job/apply link
_ATS_URL = [
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9._-]+)", re.I)),
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_app\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9._%-]+)", re.I)),
    ("darwinbox", re.compile(r"https?://([a-z0-9-]+)\.darwinbox\.in/", re.I)),
    ("keka", re.compile(r"https?://([a-z0-9-]+)\.keka\.com/careers", re.I)),
    ("workable", re.compile(r"https?://apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com/", re.I)),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("mynexthire", re.compile(r"https?://([a-z0-9-]+)\.mynexthire\.com/", re.I)),
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)", re.I)),
]
_NOT_TENANT = {"embed", "api", "v1", "jobs", "job", "careers", "www", "app", "apply", "boards", "wday", "o", "j"}


def ats_from_url(url: str) -> dict | None:
    """'https://jobs.lever.co/acme/123…' -> {'type': 'lever', 'tenant': 'acme'}. None if not a known ATS link."""
    if not url:
        return None
    for kind, rx in _ATS_URL:
        m = rx.search(url)
        if not m:
            continue
        tenant = m.group(1)
        if tenant.lower() in _NOT_TENANT:
            return None
        if kind == "workday":
            site = m.group(3)
            if site.lower() in ("job", "wday", "api"):     # "jobs" is a real site name (e.g. PayPal)
                return None
            return {"type": "workday", "tenant": tenant.lower(), "wd": m.group(2).lower(), "site": site,
                    "search": ["India", "Bengaluru", "Gurugram", "Mumbai", "Hyderabad"], "harvested": True}
        cfg = {"type": kind, "tenant": tenant if kind in ("smartrecruiters", "ashby") else tenant.lower(), "harvested": True}
        if kind == "smartrecruiters":
            cfg.update(company_id=tenant, country="in")
        if kind == "mynexthire":
            cfg["careers_url"] = f"https://{tenant.lower()}.mynexthire.com/employer/jobs/careers"
        return cfg
    return None


# ------------------------------------------------------------ Workable / Recruitee (generic; common with Indian SaaS startups)
def workable(cfg: dict) -> list[Job]:
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{cfg['tenant']}", params={"details": "true"}).json()
    out = []
    for p in d.get("jobs", []):
        locs = p.get("locations") or [{"city": p.get("city"), "country": p.get("country")}]
        loc = " / ".join(", ".join(x for x in [l.get("city"), l.get("country")] if x) for l in locs if isinstance(l, dict))
        out.append(Job(company="", title=p.get("title", ""), location=loc,
                       url=p.get("url") or p.get("shortlink") or f"https://apply.workable.com/{cfg['tenant']}/j/{p.get('shortcode')}/",
                       source="careers portal", posted=to_iso(p.get("published_on") or p.get("created_at")),
                       description=html_to_text(p.get("description") or ""),
                       exp_text=p.get("experience") or ""))
    return out


def recruitee(cfg: dict) -> list[Job]:
    d = get(f"https://{cfg['tenant']}.recruitee.com/api/offers/").json()
    return [Job(company="", title=p.get("title", ""),
                location=p.get("location") or ", ".join(x for x in [p.get("city"), p.get("country")] if x),
                url=p.get("careers_url") or "", source="careers portal", posted=to_iso(p.get("published_at") or p.get("created_at")),
                description=html_to_text((p.get("description") or "") + " " + (p.get("requirements") or "")))
            for p in d.get("offers", [])]


# ------------------------------------------------------------ Amazon (amazon.jobs public search; India only)
def amazon_jobs(cfg: dict) -> list[Job]:
    out = []
    for offset in range(0, cfg.get("max_jobs", 500), 100):
        d = get("https://www.amazon.jobs/en/search.json",
                params={"normalized_country_code[]": "IND", "result_limit": 100, "offset": offset, "sort": "recent",
                        "base_query": cfg.get("query", "")}).json()
        rows = d.get("jobs") or []
        for p in rows:
            try:
                posted = datetime.strptime(p.get("posted_date", ""), "%B %d, %Y").date().isoformat()
            except ValueError:
                posted = None
            out.append(Job(company="", title=p.get("title", ""), location=p.get("normalized_location") or p.get("location", ""),
                           url="https://www.amazon.jobs" + (p.get("job_path") or ""), source="careers portal", posted=posted,
                           description=html_to_text(" ".join(str(p.get(k) or "") for k in
                                                             ("description", "basic_qualifications", "preferred_qualifications")))))
        if len(rows) < 100:
            break
        nap(1, 2)
    return out


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
    "workable": (workable, None),
    "recruitee": (recruitee, None),
    "amazon": (amazon_jobs, None),
}
