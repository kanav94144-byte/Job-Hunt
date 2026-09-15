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
    if any(k in l for k in profile["locations"]["india"]):
        return "india"
    return None


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
                if job.group == "discovered":
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

        # discovery results must carry a strong signal
        if job.group == "discovered" and (not (m or job.salary_kind == "stated")
                                          or not self.strong_title.search(job.title) or job.relevance < 10):
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


def dedupe_key(job: Job) -> str:
    city = norm(job.location.split(",")[0]) if job.location else ""
    city = re.sub(r"\b(division|city|district|urban|rural|area)\b", "", city).strip()
    city = {"bangalore": "bengaluru", "gurgaon": "gurugram", "new delhi": "delhi", "bombay": "mumbai"}.get(city, city)
    return f"{norm(job.company)}|{norm(job.title)}|{city}"


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
