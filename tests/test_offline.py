"""Offline checks for the scanner: no network, every source is faked.
Run from the repo root:  python -m tests.test_offline
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scraper import run, sources as src          # noqa: E402
from scraper.core import Evaluator, Job, in_scope_location   # noqa: E402

PROFILE = yaml.safe_load((REPO / "config/profile.yml").read_text())
TODAY = src.days_ago(0)
JD = ("We are looking for a strategy and growth manager. MBA from a tier-1 institute preferred. 2-4 years of experience "
      "in consulting, business development, go-to-market and stakeholder management with cross-functional teams.")


# ------------------------------------------------------------------ unit checks
def test_ats_from_url():
    cases = {
        "https://jobs.lever.co/acmeindia/5f1c-uuid": ("lever", "acmeindia"),
        "https://boards.greenhouse.io/rocketco/jobs/123": ("greenhouse", "rocketco"),
        "https://job-boards.greenhouse.io/rocketco/jobs/123": ("greenhouse", "rocketco"),
        "https://boards.greenhouse.io/embed/job_app?for=rocketco&token=9": ("greenhouse", "rocketco"),
        "https://jobs.ashbyhq.com/Atlan/abc": ("ashby", "Atlan"),
        "https://zepto.darwinbox.in/ms/candidate/careers/123": ("darwinbox", "zepto"),
        "https://gokwik.keka.com/careers/jobdetails/42": ("keka", "gokwik"),
        "https://apply.workable.com/fyle/j/ABC123/": ("workable", "fyle"),
        "https://acme.recruitee.com/o/strategy-manager": ("recruitee", "acme"),
        "https://jobs.smartrecruiters.com/PHONEPELIMITED/744000": ("smartrecruiters", "PHONEPELIMITED"),
        "https://paypal.wd1.myworkdayjobs.com/en-US/jobs/job/Bangalore/Strategy_R0123": ("workday", "paypal"),
    }
    for url, (kind, tenant) in cases.items():
        a = src.ats_from_url(url)
        assert a and a["type"] == kind and a["tenant"] == tenant, (url, a)
    wd = src.ats_from_url("https://paypal.wd1.myworkdayjobs.com/en-US/jobs/job/Bangalore/X")
    assert wd["wd"] == "wd1" and wd["site"] == "jobs", wd
    for url in ["https://www.linkedin.com/jobs/view/123456789", "https://boards.greenhouse.io/embed/job_app?token=9",
                "https://www.iimjobs.com/j/strategy-123", ""]:
        assert src.ats_from_url(url) is None, url


def test_locations():
    keep = ["Bengaluru, Karnataka, India", "Remote - India", "Gurgaon", "Dubai, United Arab Emirates", "Singapore", ""]
    drop = ["San Francisco, CA", "Remote", "New York, NY, United States", "Berlin, Germany", "Indianapolis, IN"]
    for l in keep:
        assert in_scope_location(l, PROFILE), l
    for l in drop:
        assert not in_scope_location(l, PROFILE), l


def test_jsonld():
    h = ('<html><script type="application/ld+json">{"@context":"https://schema.org","@type":"JobPosting",'
         '"title":"Strategy Manager","description":"&lt;p&gt;MBA preferred. Own the GTM plan.&lt;/p&gt;",'
         '"educationRequirements":{"@type":"EducationalOccupationalCredential","credentialCategory":"postgraduate degree"},'
         '"experienceRequirements":{"@type":"OccupationalExperienceRequirements","monthsOfExperience":36}}</script></html>')
    post = src._jobposting_from_html(h)
    assert post and post["title"] == "Strategy Manager"
    old_get = src.get
    src.get = lambda url, **kw: type("R", (), {"text": h})()
    try:
        j = Job(company="X", title="Strategy Manager", url="https://www.iimjobs.com/j/x-1")
        src.jsonld_detail(j)
    finally:
        src.get = old_get
    assert "MBA preferred" in j.description and "postgraduate" in j.description and j.exp_text.startswith("3+"), j


def test_exclusions():
    ev = Evaluator(PROFILE)
    for name in ["HDFC Bank", "ICICI Prudential", "Hdfc Life Ltd", "Tata Capital Ltd", "Bajaj Finserv", "IDFC FIRST Bank",
                 "Airtel Payments Bank", "JPMorganChase", "DMI Finance Private Limited", "Acko"]:
        assert ev.company_excluded(name), name
    for name in ["Groww", "BharatPe", "Juspay", "Mastercard", "Accordion India", "Scapia", "PhonePe", "Bajaj Broking",
                 "HDFC Securities", "BankBazaar", "CarDekho", "InsuranceDekho", "Bajaj Consumer Care"]:
        assert not ev.company_excluded(name), name
    assert ev.company_excluded("Some Lender", ["Banking"])
    assert not ev.company_excluded("Some Fintech", ["Financial Services"])
    page = '<h3 class="x">\n Industries\n </h3>\n <span class="y">\n Banking and Financial Services\n </span>'
    assert src.linkedin_industries(page) == ["Banking", "Financial Services"]
    for loc, want in [("Chennai", None), ("Chennai, Tamil Nadu, India", None), ("Bengaluru · Chennai", "Bengaluru"),
                      ("India", "India")]:
        assert ev.allowed_location(loc) == want, (loc, ev.allowed_location(loc))


def test_ceo_office_titles():
    ev = Evaluator(PROFILE)
    for title in ["CEO’s Office – Strategy", "CEO Office", "CXO Office", "Chief of Staff, Office of CEO",
                  "Entrepreneur in Residence (EIR)"]:
        assert ev.title_ok(title), title
    for title in ["CEO", "Deputy CEO", "Chief Marketing Officer", "Founder's Office Trainee"]:
        assert not ev.title_ok(title), title


def test_tenant_similarity():
    ok = [("zeptonow", "Zepto"), ("PHONEPELIMITED", "PhonePe"), ("nimbussaas", "Nimbus SaaS"), ("walmart", "Walmart"),
          ("rocketcommerce", "Rocket Commerce Pvt Ltd"), ("dreamsports", "Dream Sports (Dream11)")]
    bad = [("xyzstaffing", "Acme Brands"), ("bundl", "Swiggy"), ("ab", "AB InBev")]
    for tenant, co in ok:
        assert run.tenant_looks_like(tenant, co), (tenant, co)
    for tenant, co in bad:
        assert not run.tenant_looks_like(tenant, co), (tenant, co)


def test_company_matching():
    assert run.company_matches("Swiggy Instamart", ["Swiggy"])
    assert run.company_matches("Walmart Global Tech India", ["Walmart"])
    assert not run.company_matches("Swiggyland", ["Swiggy"])
    idx = run.CompanyIndex([{"name": "Porter", "aliases": ["Porter", "SmartShift Logistics Solutions"]}])
    assert idx.find("SmartShift Logistics Solutions Pvt Ltd")["name"] == "Porter"
    assert idx.find("Porterhouse") is None


# ------------------------------------------------------------------ rate limits and refusals
class Resp:
    def __init__(self, status=200, headers=None, body=None):
        self.status_code, self.headers, self.body = status, headers or {}, body or {}

    def json(self):
        return self.body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def with_fake_http(responses, fn):
    """Run fn with the HTTP session returning `responses` in order and sleeping disabled -> (sleeps, requests made)."""
    sleeps, made, queue = [], [], list(responses)

    def send(url, **kw):
        made.append(url)
        return queue.pop(0)

    saved = src.S, src.time.sleep, dict(src.WORKDAY), dict(src.DARWINBOX_PROBES)
    try:
        src.S = type("S", (), {"get": staticmethod(send), "post": staticmethod(send)})()
        src.time.sleep = sleeps.append
        src.WORKDAY.update(gave_up_in_a_row=0, stopped=False, next_at=0.0, got_429=0)
        src.DARWINBOX_PROBES["blocked"] = 0
        fn()
    finally:
        src.S, src.time.sleep = saved[0], saved[1]
        src.WORKDAY.update(saved[2])
        src.DARWINBOX_PROBES.update(saved[3])
    return sleeps, made


def raises(fn, text):
    try:
        fn()
    except Exception as e:
        assert text in str(e), e
        return e
    raise AssertionError("expected an error")


def test_workday_waits_then_succeeds():
    def check():
        assert src._workday_request("post", "u").status_code == 200
    sleeps, made = with_fake_http([Resp(429, {"Retry-After": "7"}), Resp(429), Resp()], check)
    assert len(made) == 3 and 7 in sleeps and src.WORKDAY_RETRY_WAITS[1] in sleeps, sleeps


def test_workday_stops_after_repeated_429():
    tries = len(src.WORKDAY_RETRY_WAITS) + 1

    def check():
        for _ in range(src.WORKDAY_GIVE_UP_AFTER):
            raises(lambda: src._workday_request("post", "u"), "429")
        raises(lambda: src._workday_request("post", "u"), "skipped")
    _, made = with_fake_http([Resp(429)] * tries * src.WORKDAY_GIVE_UP_AFTER, check)
    assert len(made) == tries * src.WORKDAY_GIVE_UP_AFTER, len(made)


def test_workday_does_not_wait_out_a_long_retry_after():
    sleeps, made = with_fake_http([Resp(429, {"Retry-After": "3600"})],
                                  lambda: raises(lambda: src._workday_request("post", "u"), "429"))
    assert len(made) == 1 and 3600 not in sleeps


def test_darwinbox_block_is_reported():
    def check():
        e = raises(lambda: src.darwinbox({"tenant": "acme"}), "refused")
        assert isinstance(e, src.AccessBlocked)
    _, made = with_fake_http([Resp(403, {"server": "cloudflare"})], check)
    assert len(made) == 1
    # a 403 that isn't Cloudflare's block page stays an ordinary error and is retried next run
    def other():
        assert not isinstance(raises(lambda: src.darwinbox({"tenant": "acme"}), "403"), src.AccessBlocked)
    with_fake_http([Resp(403)], other)


def test_darwinbox_probes_stop_after_refusals():
    slugs = [f"co{i}" for i in range(src.DARWINBOX_PROBE_LIMIT + 2)]
    blocked = Resp(403, {"server": "cloudflare"})

    def send_all():
        assert src.probe_portals(slugs) is None
    # every slug is still tried on Keka; Darwinbox only until the limit
    n = src.DARWINBOX_PROBE_LIMIT
    _, made = with_fake_http([blocked, Resp(404)] * n + [Resp(404)] * (len(slugs) - n), send_all)
    assert sum("darwinbox" in u for u in made) == n and sum("keka" in u for u in made) == len(slugs), made


def test_blocked_portal_is_left_alone_for_a_while():
    calls = []

    def refuses(cfg):
        calls.append(cfg["tenant"])
        raise src.AccessBlocked("Cloudflare refused the request (403)")

    patches, ats, _ = fake_sources()
    ats["darwinbox"] = (refuses, None)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for d in ("config", "data", "docs"):
            shutil.copytree(REPO / d, root / d)
        (root / "data/blocked_portals.json").unlink(missing_ok=True)
        data = lambda f: json.loads((root / "data" / f).read_text())
        run_scan(root, ats)
        assert "porter" in calls and data("blocked_portals.json")["Porter"]["since"] == TODAY
        calls.clear()
        run_scan(root, ats)
        assert calls == [], calls                  # not asked again straight away
        h = data("health.json")["sources"]["portal:darwinbox:Porter"]
        assert h["failed"] == 1 and "skipped" in h["errors"][0], h
        # once the wait is over it is asked again, and a success clears the record
        old = {k: dict(v, since=src.days_ago(30)) for k, v in data("blocked_portals.json").items()}
        (root / "data/blocked_portals.json").write_text(json.dumps(old))
        ats["darwinbox"] = (lambda cfg: calls.append(cfg["tenant"]) or [], None)
        run_scan(root, ats)
        assert "porter" in calls and "Porter" not in data("blocked_portals.json")


# ------------------------------------------------------------------ end-to-end run with fake sources
def fake_sources():
    calls = {"linkedin_detail": 0, "probe": [], "portal": []}

    def job(company, title, source, url, loc="Bengaluru, India", desc="", apply=""):
        j = Job(company=company, title=title, location=loc, url=url, source=source, posted=TODAY, description=desc)
        j._apply_url = apply
        return j

    def jobspy_search(site, term, location, hours_old, results):
        if site == "linkedin" and term == "founder's office":
            return [job("Rocket Commerce", "Founder's Office - Strategy & Growth", "linkedin",
                        "https://www.linkedin.com/jobs/view/4000000001"),
                    job("Talent Hunt Recruiters", "Strategy Manager", "linkedin", "https://www.linkedin.com/jobs/view/4000000002")]
        if site == "indeed" and term == "strategy manager":
            return [job("Nimbus SaaS", "Strategy & Operations Manager", "indeed", "https://in.indeed.com/viewjob?jk=1",
                        desc=JD, apply="https://jobs.lever.co/nimbussaas/abc")]
        if site == "google" and term == "chief of staff":
            return [job("Swiggy", "Senior Manager - Strategy", "google", "https://careers.swiggy.com/x", desc=JD)]
        return []

    def linkedin_detail(j):
        calls["linkedin_detail"] += 1
        j.description = JD
        if "4000000001" in j.url:
            j._apply_url = "https://jobs.lever.co/rocketcommerce/xyz"

    def iimjobs(term, days):
        if term != "strategy":
            return []
        return [job("Acme Brands", "Growth Manager - D2C", "iimjobs", "https://www.iimjobs.com/j/growth-1",
                    desc="Growth Manager. growth, d2c")]

    def get(url, **kw):
        if "iimjobs.com" in url:
            body = json.dumps({"@type": "JobPosting", "description": JD})
            return type("R", (), {"text": f'<script type="application/ld+json">{body}</script>'})()
        raise AssertionError(f"unexpected network call {url}")

    def probe(slugs, global_ats=False):
        calls["probe"].append((tuple(slugs), global_ats))
        if slugs and slugs[0] == "chargebee":
            return {"type": "greenhouse", "tenant": "chargebee", "probed": True}
        return None

    def greenhouse(cfg):
        calls["portal"].append(("greenhouse", cfg["tenant"]))
        return [Job(company="", title="Strategy Manager, GTM", location="Bengaluru, India", source="careers portal",
                    url=f"https://boards.greenhouse.io/{cfg['tenant']}/jobs/1", posted=TODAY, description=JD),
                Job(company="", title="Strategy Manager, GTM (US)", location="San Francisco, CA", source="careers portal",
                    url=f"https://boards.greenhouse.io/{cfg['tenant']}/jobs/2", posted=TODAY, description=JD)]

    def lever(cfg):
        calls["portal"].append(("lever", cfg["tenant"]))
        return [Job(company="", title="Business Operations Manager", location="Mumbai", source="careers portal",
                    url=f"https://jobs.lever.co/{cfg['tenant']}/2", posted=src.days_ago(20), description=JD)]

    def quiet(cfg):
        calls["portal"].append(("other", cfg.get("tenant") or cfg.get("type")))
        return []

    patches = {"jobspy_search": jobspy_search, "linkedin_detail": linkedin_detail, "iimjobs": iimjobs, "get": get,
               "probe_portals": probe, "nap": lambda *a, **k: None,
               "naukri_inbox": lambda *a, **k: [], "hirist": lambda *a, **k: [], "instahyre": lambda *a, **k: []}
    ats = {k: (quiet, None) for k in src.ATS}
    ats["greenhouse"] = (greenhouse, None)
    ats["lever"] = (lever, None)
    return patches, ats, calls


def run_scan(root: Path, ats_override: dict | None = None):
    patches, ats, calls = fake_sources()
    ats.update(ats_override or {})
    saved = {k: getattr(src, k) for k in patches}
    saved_ats = dict(src.ATS)
    saved_paths = (run.ROOT, run.DATA, run.DOCS, run.CONFIG)
    saved_argv = sys.argv
    try:
        for k, v in patches.items():
            setattr(src, k, v)
        src.ATS.clear(); src.ATS.update(ats)
        run.ROOT, run.DATA, run.DOCS, run.CONFIG = root, root / "data", root / "docs", root / "config"
        sys.argv = ["run"]
        run.main()
    finally:
        for k, v in saved.items():
            setattr(src, k, v)
        src.ATS.clear(); src.ATS.update(saved_ats)
        run.ROOT, run.DATA, run.DOCS, run.CONFIG = saved_paths
        sys.argv = saved_argv
    return calls


def test_end_to_end():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for d in ("config", "data", "docs"):
            shutil.copytree(REPO / d, root / d)
        # start from a clean portal cache so probing / negative caching is exercised
        (root / "data/portals.json").write_text("{}")
        for f in ("companies_auto.json", "rejected.json"):
            (root / "data" / f).unlink(missing_ok=True)
        # keep 'seen' non-empty so this is a normal (not first) run
        calls1 = run_scan(root)

        data = lambda f: json.loads((root / "data" / f).read_text())
        jobs = {(j["company"], j["title"]): j for j in data("jobs.json")["jobs"]}
        health = data("health.json")
        portals = data("portals.json")
        auto = data("companies_auto.json")

        # seeds were probed with their global-ATS slugs; the failures are remembered (negative cache)
        assert any(s == ("chargebee",) and g for s, g in calls1["probe"]), calls1["probe"][:5]
        assert portals["Chargebee"]["type"] == "greenhouse"
        assert portals["Freshworks"] == {"type": None, "checked": TODAY}
        # global-ATS roles outside India / target cities are dropped, India ones kept as a SaaS seed role
        assert ("Chargebee", "Strategy Manager, GTM") in jobs
        assert ("Chargebee", "Strategy Manager, GTM (US)") not in jobs
        assert jobs[("Chargebee", "Strategy Manager, GTM")]["group"] == "saas"
        # discovered roles: LinkedIn (desc fetched), Indeed, iimjobs (JD read from JSON-LD -> MBA found -> Tier A)
        assert jobs[("Rocket Commerce", "Founder's Office - Strategy & Growth")]["tier"] == "A"
        assert jobs[("Nimbus SaaS", "Strategy & Operations Manager")]["tier"] == "A"
        assert jobs[("Acme Brands", "Growth Manager - D2C")]["tier"] == "A", jobs.get(("Acme Brands", "Growth Manager - D2C"))
        assert health["descriptions"].get("iimjobs") == 1, health["descriptions"]
        # a curated company found through open search is kept and labelled as yours (previously thrown away)
        assert jobs[("Swiggy", "Senior Manager - Strategy")]["group"] == "target"
        # auto-watchlist: real companies added, recruiters not; portals learned from apply links
        assert {"Rocket Commerce", "Nimbus SaaS", "Acme Brands"} <= set(auto), auto
        assert "Talent Hunt Recruiters" not in auto
        assert portals["Rocket Commerce"]["type"] == "lever" and portals["Rocket Commerce"]["tenant"] == "rocketcommerce"
        assert portals["Nimbus SaaS"]["tenant"] == "nimbussaas"
        assert health["watchlist"]["size"] >= 3 and health["funnel"]["linkedin"]["raw"] >= 2

        # ---- second run the same day: watchlist portals are read; known roles are not re-fetched
        calls2 = run_scan(root)
        jobs2 = {(j["company"], j["title"]): j for j in data("jobs.json")["jobs"]}
        health2 = data("health.json")
        assert ("lever", "rocketcommerce") in calls2["portal"] and ("lever", "nimbussaas") in calls2["portal"]
        # a 20-day-old role on a company portal is still shown (portal window = 30 days), under the auto-tracked group
        assert jobs2[("Rocket Commerce", "Business Operations Manager")]["group"] == "watchlist"
        assert calls2["linkedin_detail"] == 0, calls2          # already on the dashboard -> no second fetch
        assert not any(s == ("freshworks",) for s, _ in calls2["probe"])   # negative cache respected
        assert ("Rocket Commerce", "Founder's Office - Strategy & Growth") in jobs2
        assert health2["counts"]["on_dashboard"] >= health["counts"]["on_dashboard"]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all offline checks passed")
