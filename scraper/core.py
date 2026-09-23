"""Parsing, filtering and scoring helpers (no network)."""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timezone
from typing import Optional

# ----------------------------------------------------------------- data model
@dataclass
class Job:
    company: str
    title: str
    location: str = ""
    url: str = ""
    source: str = ""                 # e.g. "linkedin", "naukri", "smartrecruiters"
    posted: Optional[str] = None     # ISO date
    description: str = ""
    exp_text: str = ""               # raw experience text if the source provides it
    salary_min: Optional[float] = None
    salary_max: Optional[float] = None
    currency: Optional[str] = None
    group: str = "target"            # target | startup | discovered
    band: str = "unknown"
    # filled by evaluate()
    exp_min: Optional[float] = None
    exp_max: Optional[float] = None
    exp_label: str = ""
    mba: str = ""
    salary_label: str = ""
    salary_kind: str = ""            # stated | estimated | unknown
    region: str = ""                 # india | international
    relevance: int = 0
    tier: str = ""
    reasons: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    links: list = field(default_factory=list)
    variants: list = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d.pop("description", None)
        return d


def html_to_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# ----------------------------------------------------------------- experience
_NUM = r"(\d{1,2}(?:\.\d)?)"
_EXP_RANGE = re.compile(_NUM + r"\s*\+?\s*(?:-|–|—|to)\s*" + _NUM + r"\s*\+?\s*(?:years?|yrs?)", re.I)
_EXP_MIN = re.compile(r"(?:minimum|min\.?|at\s*least|over|more\s*than)?\s*" + _NUM + r"\s*\+?\s*(?:years?|yrs?)(?:\s*\+)?", re.I)


def parse_experience(exp_text: str, description: str):
    """Return (min, max, label). max may be None ("5+ years")."""
    for text in (exp_text, description):
        if not text:
            continue
        # prefer sentences that talk about experience
        chunks = [c for c in re.split(r"[\n.;•]", text) if re.search(r"experience|exp\b|yrs|years", c, re.I)]
        for c in chunks or [text]:
            m = _EXP_RANGE.search(c)
            if m:
                a, b = float(m.group(1)), float(m.group(2))
                if a <= b <= 30:
                    return a, b, f"{m.group(1)}-{m.group(2)} yrs"
            m = _EXP_MIN.search(c)
            if m and re.search(r"experience|exp\b", c, re.I):
                a = float(m.group(1))
                if a <= 30:
                    return a, None, f"{m.group(1)}+ yrs"
    return None, None, "not stated"


# ----------------------------------------------------------------- salary
def parse_salary_text(text: str):
    """Find a salary range in free text. Returns (min, max, currency) annual, or (None,None,None)."""
    if not text:
        return None, None, None
    t = text.replace(",", "")
    m = re.search(r"(?:₹|INR|Rs\.?)?\s*(\d{1,3}(?:\.\d+)?)\s*(?:-|–|to)\s*(\d{1,3}(?:\.\d+)?)\s*(LPA|lakhs?|lacs?|L\b)", t, re.I)
    if m:
        return float(m.group(1)) * 1e5, float(m.group(2)) * 1e5, "INR"
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(LPA|lakhs? per annum)", t, re.I)
    if m:
        v = float(m.group(1)) * 1e5
        return v, v, "INR"
    m = re.search(r"(AED|SGD|GBP|USD|EUR|£|\$|S\$)\s*(\d{2,3})\s*[kK]?\s*(?:-|–|to)\s*(?:AED|SGD|GBP|USD|EUR|£|\$|S\$)?\s*(\d{2,3})\s*[kK]", t)
    if m:
        cur = {"£": "GBP", "$": "USD", "S$": "SGD"}.get(m.group(1), m.group(1))
        return float(m.group(2)) * 1e3, float(m.group(3)) * 1e3, cur
    return None, None, None


# Rough annual CTC bands (INR lakhs) for people ~1-5 yrs post tier-1 MBA, by company band and title level.
_EST = {
    #               junior(analyst/associate)  mid(manager)  senior(sr mgr/lead)
    "mnc_top":        ((20, 32), (30, 50), (45, 75)),
    "big4":           ((16, 24), (26, 42), (38, 58)),   # consultant / manager / sr manager
    "listed_tech":    ((16, 26), (26, 42), (38, 60)),
    "unicorn":        ((16, 26), (25, 40), (36, 55)),
    "funded_startup": ((12, 20), (20, 32), (30, 45)),
    "d2c":            ((10, 18), (18, 30), (28, 42)),
    "large_indian":   ((10, 16), (16, 26), (24, 38)),
    "unknown":        ((10, 20), (18, 32), (28, 45)),
}


def title_level(title: str) -> int:
    t = title.lower()
    if re.search(r"senior manager|sr\.? manager|\blead\b|\bhead\b|principal|group manager|chief of staff", t):
        return 2
    if re.search(r"senior consultant|sr\.? consultant", t):
        return 1
    if re.search(r"analyst|associate(?! manager| director)|executive|specialist|consultant|coordinator|officer", t):
        return 0
    if re.search(r"manager|product owner|program", t):
        return 1
    return 1


def estimate_salary(band: str, title: str):
    lo, hi = _EST.get(band, _EST["unknown"])[title_level(title)]
    return lo * 1e5, hi * 1e5


# ----------------------------------------------------------------- location
def classify_location(loc: str, profile) -> Optional[str]:
    l = (loc or "").lower()
    if not l.strip():
        return "india"   # unknown location: assume India (searches are India-scoped)
    if any(k in l for k in profile["locations"]["international"]):
        return "international"
    return "india"   # searches are India-scoped; portal connectors already drop other countries


_INDIA_PLACES = re.compile(r"\b(india|ind|bengaluru|bangalore|gurugram|gurgaon|delhi|ncr|noida|mumbai|pune|hyderabad|chennai|"
                           r"kolkata|ahmedabad|jaipur|chandigarh|kochi|indore|coimbatore|lucknow|thane|karnataka|maharashtra|"
                           r"haryana|telangana|tamil nadu)\b", re.I)


def in_scope_location(loc: str, profile) -> bool:
    """For global ATS boards (Greenhouse, Lever, Ashby, Workable, Recruitee) that list every country:
    keep India roles and the international cities in profile.yml; drop the rest (incl. bare 'Remote')."""
    l = (loc or "").lower()
    if not l.strip():
        return True
    if _INDIA_PLACES.search(l):
        return True
    return any(k in l for k in profile["locations"]["international"])


# ----------------------------------------------------------------- evaluation
class Evaluator:
    def __init__(self, profile: dict):
        self.p = profile
        self.kw = re.compile("|".join(profile["title_keywords"]), re.I)
        self.ex = re.compile("|".join(profile["title_exclude"]), re.I)
        self.mba = re.compile("|".join(profile["mba_patterns"]), re.I)
        self.cv = {k.lower(): v for k, v in profile["cv_keywords"].items()}
        self.strong_title = re.compile(
            r"strateg|growth|founder|chief of staff|program|business operations|biz ?ops|partnership|alliances|"
            r"business development|consult|corporate development|go[- ]to[- ]market|\bgtm\b|\bai\b|genai|"
            r"product owner|product manager|key account|b2b|generalist|transformation|category", re.I)

    def title_ok(self, title: str) -> bool:
        return bool(self.kw.search(title or "")) and not self.ex.search(title or "")

    @staticmethod
    def gate(job) -> str:
        return getattr(job, "_gate", None) or ("strict" if job.group == "discovered" else "lenient")

    def worth_details(self, job) -> bool:
        """Cheap pre-check before spending a request on the job description:
        strict/medium roles are dropped later anyway unless the title is strong."""
        return self.gate(job) == "lenient" or bool(self.strong_title.search(job.title or ""))

    def evaluate(self, job: Job) -> bool:
        """Fill derived fields. Returns False if the job should be dropped."""
        p = self.p
        if not self.title_ok(job.title):
            return False
        job.region = classify_location(job.location, p) or ""
        if not job.region:
            return False

        text = f"{job.title}\n{job.description}"
        # experience
        job.exp_min, job.exp_max, job.exp_label = parse_experience(job.exp_text, job.description)
        emin, emax = job.exp_min, job.exp_max
        stretch = False
        if emin is not None:
            if emin > p["experience"]["stretch_max_min"]:
                return False
            if emax is not None and emax < p["experience"]["min"]:
                return False          # fresher-only
            if emin >= p["experience"]["max"]:
                stretch = True
                job.reasons.append(f"stretch: asks {job.exp_label}")

        # MBA
        m = self.mba.search(text)
        if m:
            snippet = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
            job.mba = "Yes: …" + snippet.strip() + "…"
        else:
            job.mba = "Not mentioned"

        # salary
        floor = p["salary_floor"]
        if job.salary_max is None:
            smin, smax, cur = parse_salary_text(job.description)
            if smax:
                job.salary_min, job.salary_max, job.currency = smin, smax, cur
        low_salary = False
        if job.salary_max:
            job.salary_kind = "stated"
            cur = job.currency or "INR"
            f = floor.get(cur)
            if f and job.salary_max < f:
                return False
            job.salary_label = fmt_money(job.salary_min, job.salary_max, cur)
        elif job.region == "india":
            lo, hi = estimate_salary(job.band, job.title)
            job.salary_kind = "estimated"
            job.salary_label = "~" + fmt_money(lo, hi, "INR") + " (est.)"
            if hi < 0.9 * floor["INR"]:
                if self.gate(job) != "lenient":
                    return False
                low_salary = True
                job.reasons.append("estimated pay may be below 20 LPA")
        else:
            job.salary_kind = "unknown"
            job.salary_label = "not stated"

        # relevance vs CV
        tl = text.lower()
        score = sum(w for k, w in self.cv.items() if k in tl)
        if self.strong_title.search(job.title):
            score += 5
        job.relevance = min(score, 40)

        # How strict to be depends on how much we already trust the company:
        #   lenient = your curated list (config/companies.yml)
        #   medium  = seed list / auto-watchlist companies (vetted company, but their whole portal is read)
        #   strict  = any other company found through open searches
        gate = self.gate(job)
        if gate in ("strict", "medium"):
            if not self.strong_title.search(job.title) or job.relevance < 8:
                return False
        if gate == "strict":
            if job.source == "naukri":
                # Naukri has many small-company / mass-hiring posts: keep only if MBA is asked or pay (>= 20 LPA) is stated
                if not (m or job.salary_kind == "stated"):
                    return False
            elif not (m or job.salary_kind == "stated" or job.source == "iimjobs"):
                return False

        # tiering
        in_range = not stretch
        if m and in_range and not low_salary:
            job.tier = "A"
            job.reasons.insert(0, "MBA/PG asked or preferred")
        elif in_range and not low_salary and (job.relevance >= 12 or self.strong_title.search(job.title)):
            job.tier = "B"
            job.reasons.insert(0, "relevant to CV (MBA not mentioned)")
        else:
            job.tier = "C"
        if emin is None:
            job.reasons.append("experience not stated")
        return True


def fmt_money(lo, hi, cur):
    if cur == "INR":
        a, b = (lo or hi) / 1e5, hi / 1e5
        return f"₹{a:.0f}–{b:.0f} LPA" if round(a) != round(b) else f"₹{b:.0f} LPA"
    a, b = (lo or hi) / 1e3, hi / 1e3
    return f"{cur} {a:.0f}k–{b:.0f}k"


_CITY_WORDS = (r"bengaluru|bangalore|gurugram|gurgaon|delhi|new delhi|ncr|noida|mumbai|navi mumbai|thane|pune|hyderabad|chennai|kolkata|"
               r"ahmedabad|jaipur|chandigarh|kochi|cochin|lucknow|indore|surat|siliguri|kanyakumari|coimbatore|nagpur|ludhiana|"
               r"rourkela|jorhat|guwahati|bhubaneswar|vizag|visakhapatnam|vadodara|nashik|mysore|mysuru|trivandrum|madurai|"
               r"karnataka|maharashtra|haryana|telangana|tamil nadu|west bengal|uttar pradesh|gujarat|kerala|rajasthan|punjab|"
               r"bhopal|patna|india|apac|emea|remote|hybrid|onsite|on site|pan india|multiple locations|division|city|west|east|north|south|central")

_CO_NOISE = r"\b(pvt|private|ltd|limited|llp|inc|corp|corporation|co|company|the|india|in|technologies|technology|solutions|services|global|group|labs)\b"
_TITLE_STOP = {"of", "and", "the", "for", "to", "a", "an", "in", "at", "with", "on", "job", "role", "opening", "opportunity",
               "hiring", "urgent", "requirement", "immediate", "joiner", "joiners", "wfo", "work", "from", "office", "home"}
_LEVELS = {"i", "ii", "iii", "iv", "v", "1", "2", "3", "4", "l1", "l2", "l3", "l4", "senior", "sr", "snr", "junior", "jr",
           "associate_level", "grade", "band"}


def norm_company(name: str) -> str:
    c = (name or "").lower().replace("&", " and ")
    c = re.sub(r"\(.*?\)", " ", c)
    c = re.sub(r"[^a-z0-9 ]+", " ", c)
    c = re.sub(_CO_NOISE, " ", c)
    return " ".join(c.split())


def title_tokens(title: str, location: str = "", company: str = "") -> frozenset:
    """Role signature: words of the title without cities, levels, codes and filler."""
    t = (title or "").lower()
    t = re.sub(r"\(.*?\)|\[.*?\]|\{.*?\}", " ", t)
    t = re.sub(r"^in[_ -]+", " ", t)
    t = t.replace("&", " and ")
    t = re.sub(rf"\b({_CITY_WORDS})\b", " ", t)
    words = re.findall(r"[a-z0-9]+", t)
    loc_words = {w for w in re.findall(r"[a-z]+", (location or "").lower()) if len(w) >= 4}
    co_words = set(norm_company(company).split()) if company else set()
    out = [w for w in words if w not in _TITLE_STOP and w not in _LEVELS and w not in loc_words and w not in co_words
           and not re.fullmatch(r"\d+|[a-z]\d+|\d+[a-z]", w)]
    return frozenset(out)


def norm_title(title: str, company: str = "") -> str:
    return " ".join(sorted(title_tokens(title, "", company)))


def same_role(a: frozenset, b: frozenset) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    inter, union = len(a & b), len(a | b)
    # near-identical wording (e.g. "Integrated Marketing Lead" vs "Lead, Integrated Marketing APAC")
    return union >= 4 and inter / union >= 0.75


_REGION_ONLY = re.compile(r"^(india|in|ind|remote|hybrid|anywhere|asia|apac|emea|united states|usa|us|uk|united kingdom|uae|"
                          r"karnataka|maharashtra|haryana|telangana|tamil nadu|tn|west bengal|wb|uttar pradesh|up|gujarat|kerala|"
                          r"rajasthan|punjab|delhi ncr|ncr|madhya pradesh|mp|odisha|assam|bihar|andhra pradesh|ap|goa|"
                          r"hr|ka|mh|dl|tg|ts|wb|rj|gj|kl|pb|chhattisgarh|jharkhand|uttarakhand|himachal pradesh|jammu and kashmir|j and k|tripura|meghalaya|manipur|nagaland|sikkim|mizoram|arunachal pradesh|puducherry|chandigarh ut|"
                          r"multiple locations|pan india|other|others)$")
_CITY_FIX = {"bangalore": "Bengaluru", "bengaluru urban": "Bengaluru", "gurgaon": "Gurugram", "new delhi": "Delhi",
             "bombay": "Mumbai", "navi mumbai": "Navi Mumbai", "cochin": "Kochi"}


def cities_of(loc: str) -> list:
    out = []
    for part in re.split(r"\s*[,;/·|]\s*|\s+or\s+", loc or ""):
        c = norm(re.sub(r"\(.*?\)", " ", part))
        c = re.sub(r"\b(division|city|district|urban|rural|area|all areas|region|mandal|taluk|tehsil)\b", "", c).strip()
        if not c or _REGION_ONLY.match(c):
            continue
        c = _CITY_FIX.get(c, c.title())
        if c not in out:
            out.append(c)
    if not out and re.search(r"remote", loc or "", re.I):
        out = ["Remote"]
    return out


def city_of(loc: str) -> str:
    cs = cities_of(loc)
    return cs[0] if cs else ""


def dedupe_key(job: Job) -> str:
    """Same company + same role = one row, whatever the city or website (cities and sources are merged)."""
    return f"{norm_company(job.company)}|{norm_title(job.title, job.company)}"


def merge_locations(a: str, b: str) -> str:
    cities = []
    for loc in (a, b):
        for c in cities_of(loc):
            if c not in cities:
                cities.append(c)
    if not cities and re.search(r"india", f"{a} {b}", re.I):
        return "India"
    return " · ".join(cities)


SOURCE_RANK = {"careers portal": 0, "linkedin": 1, "iimjobs": 2, "naukri": 3, "indeed": 4, "google": 5}


def _src_rank(d: dict) -> int:
    s = d.get("source", "")
    return 0 if "portal" in s else SOURCE_RANK.get(s, 4)


def collapse(rows: list[dict]) -> list[dict]:
    """Final cross-check: fold together rows that are the same opening at the same company,
    even if found on different websites, in different cities, or with slightly different titles/levels.
    Keeps the best copy (company site first) and records every website + link it was found on."""
    groups: dict[str, list[dict]] = {}
    for d in rows:
        groups.setdefault(norm_company(d.get("company", "")), []).append(d)
    out = []
    tier_rank = {"A": 0, "B": 1, "C": 2}
    for _, items in groups.items():
        items.sort(key=lambda d: (_src_rank(d), tier_rank.get(d.get("tier"), 3), -(d.get("relevance") or 0)))
        clusters: list[list] = []   # [rep_tokens, rep_dict, members]
        for d in items:
            tok = title_tokens(d.get("title", ""), d.get("location", ""), d.get("company", ""))
            for cl in clusters:
                if same_role(tok, cl[0]):
                    cl[2].append(d)
                    break
            else:
                clusters.append([tok, d, [d]])
        for tok, rep, members in clusters:
            rep = dict(rep)
            rep["location"] = merge_locations("", rep.get("location", ""))
            titles, links, srcs = [], [], []
            for m in members:
                for tt in (m.get("variants") or []) + [m.get("title") or ""]:
                    if tt.strip() and tt.strip() not in titles:
                        titles.append(tt.strip())
                for s in (m.get("sources") or [m.get("source")]):
                    if s and s not in srcs:
                        srcs.append(s)
                for l in (m.get("links") or [{"source": m.get("source"), "url": m.get("url")}]):
                    if l.get("url") and l["url"] not in [x["url"] for x in links]:
                        links.append(l)
                if m is not members[0]:
                    rep["location"] = merge_locations(rep.get("location", ""), m.get("location", ""))
                    if (m.get("posted") or "") > (rep.get("posted") or ""):
                        rep["posted"] = m["posted"]
                    if (m.get("first_seen") or "9") < (rep.get("first_seen") or "9"):
                        rep["first_seen"] = m["first_seen"]
                    rep["is_new"] = bool(rep.get("is_new")) and bool(m.get("is_new"))
                    if rep.get("salary_kind") != "stated" and m.get("salary_kind") == "stated":
                        rep["salary_label"], rep["salary_kind"] = m.get("salary_label"), "stated"
                    if (rep.get("mba") or "").startswith("Not") and (m.get("mba") or "").startswith("Yes"):
                        rep["mba"] = m["mba"]
                    if tier_rank.get(m.get("tier"), 3) < tier_rank.get(rep.get("tier"), 3):
                        rep["tier"] = m["tier"]
            if len(titles) > 1:
                rep["variants"] = titles
            rep["sources"], rep["links"] = srcs, links
            out.append(rep)
    return out


def to_iso(d) -> Optional[str]:
    if d is None or d != d:  # NaN
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    s = str(d)[:10]
    return s if re.match(r"\d{4}-\d{2}-\d{2}", s) else None


def today() -> str:
    return datetime.now(timezone.utc).date().isoformat()
