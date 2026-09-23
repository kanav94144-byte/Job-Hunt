"""Job-hunt scan.  python -m scraper.run  [--first-run] [--only "Company,Company"] [--no-discovery]

Where roles come from (in this order):
  1. Company careers portals, read in parallel, for three kinds of company:
       curated  - config/companies.yml (your list, competitors, startups)
       seed     - config/seed_companies.yml (wider SaaS / B2B / consumer list; portal-only, no LinkedIn search each)
       auto     - data/companies_auto.json (companies the scan itself found posting good roles; grows every run)
  2. LinkedIn search by company name, only for curated companies without a readable portal.
  3. Open searches (LinkedIn, Indeed, Google Jobs) for role keywords, any company.
  4. Job boards for MBA hiring: iimjobs (public search) + Naukri (collected in the browser).
Every job/apply link seen is checked for a known ATS (Lever, Greenhouse, Darwinbox, Workday …). When a company's
portal is recognised this way, the next scan reads that company's whole portal, not just what search surfaced.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from . import sources as src
from .core import (Evaluator, Job, SOURCE_RANK, collapse, dedupe_key, in_scope_location, merge_locations, norm,
                   norm_company, today)

ROOT = Path(__file__).resolve().parent.parent
DATA, DOCS, CONFIG = ROOT / "data", ROOT / "docs", ROOT / "config"

GATE = {"curated": "lenient", "seed": "medium", "auto": "medium"}
# portals that list every country: keep only India + the international cities in profile.yml
LOCATION_FILTERED_ATS = {"greenhouse", "lever", "ashby", "workable", "recruitee"}
# names that are recruiters / placeholders, never worth adding to the auto-watchlist
NOT_A_COMPANY = re.compile(r"recruit|staffing|placement|manpower|consultan(?:cy|ts)\b|\bhr\b|talent|hiring|\bjobs?\b|"
                           r"headhunt|search\b|confidential|^mnc$|independent|stealth|^company$|client of|people\b|"
                           r"careers?\b|jobgether|executive search|outsourc", re.I)


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_yaml(path, default):
    try:
        return yaml.safe_load(Path(path).read_text()) or default
    except FileNotFoundError:
        return default


def company_matches(job_company: str, aliases: list[str]) -> bool:
    jc = norm(job_company)
    for a in aliases:
        na = norm(a)
        if not na:
            continue
        # exact, or company name starts with alias as a whole word ("Swiggy Instamart", "Walmart Global Tech India")
        if jc == na or jc.startswith(na + " "):
            return True
    return False


class CompanyIndex:
    """Fast 'which known company is this?' lookup (thousands of raw jobs x hundreds of companies)."""

    def __init__(self, companies: list[dict]):
        self.rows = [(c, [n for n in (norm(a) for a in (c.get("aliases") or [c["name"]])) if n]) for c in companies]
        self.cache: dict[str, dict | None] = {}

    def find(self, name: str) -> dict | None:
        jc = norm(name)
        if jc not in self.cache:
            self.cache[jc] = next((c for c, al in self.rows if any(jc == a or jc.startswith(a + " ") for a in al)), None)
        return self.cache[jc]


def tenant_looks_like(tenant: str, company: str) -> bool:
    """'zeptonow' ~ 'Zepto', 'PHONEPELIMITED' ~ 'PhonePe', 'nimbussaas' ~ 'Nimbus SaaS'; 'xyzstaffing' !~ 'Acme'."""
    t = re.sub(r"[^a-z0-9]", "", (tenant or "").lower())
    words = norm_company(company).split()
    c = "".join(words)
    if len(t) < 3 or not c:
        return False
    return t in c or c in t or (len(words[0]) >= 4 and words[0] in t) or (len(t) >= 5 and t[:5] == c[:5])


def build_universe(cfg: dict, seeds: list[dict], auto: dict) -> list[dict]:
    universe, names, aliases = [], set(), []
    for c in cfg.get("companies", []):
        c = dict(c, _kind="curated")
        universe.append(c)
        names.add(norm(c["name"]))
        aliases += c.get("aliases") or [c["name"]]
    for c in seeds:
        if norm(c["name"]) in names or company_matches(c["name"], aliases):
            continue
        c = dict(c, _kind="seed")
        c.setdefault("group", "startup")
        universe.append(c)
        names.add(norm(c["name"]))
    for name, rec in auto.items():
        if norm(name) in names:
            continue
        universe.append({"name": name, "aliases": [name], "group": "watchlist", "band": rec.get("band", "unknown"),
                         "_kind": "auto", "probe_global": False})
        names.add(norm(name))
    return universe


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--first-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--limit-per-search", type=int, default=40)
    args = ap.parse_args()

    profile = yaml.safe_load((CONFIG / "profile.yml").read_text())
    cfg = yaml.safe_load((CONFIG / "companies.yml").read_text())
    seeds = load_yaml(CONFIG / "seed_companies.yml", {}).get("companies", []) or []
    ev = Evaluator(profile)
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)
    t = today()

    seen = load(DATA / "seen.json", {})
    auto = load(DATA / "companies_auto.json", {})
    rejected = load(DATA / "rejected.json", {})
    portals_cache = load(DATA / "portals.json", {})
    first = args.first_run or not seen or os.environ.get("FULL_SCAN") == "true"
    hours = profile["recency_hours_first_run"] if first else profile["recency_hours_daily"]
    health = {"run_date": t, "first_run": first, "sources": {}}
    raw: list[Job] = []

    def record(name, n=None, err=None):
        h = health["sources"].setdefault(name, {"ok": 0, "failed": 0, "jobs": 0, "errors": []})
        if err:
            h["failed"] += 1
            h["errors"] = (h["errors"] + [err[:300]])[-3:]
        else:
            h["ok"] += 1
            h["jobs"] += n or 0

    universe = build_universe(cfg, seeds, auto)
    only = {norm(x) for x in args.only.split(",") if x.strip()}
    if only:
        universe = [c for c in universe if norm(c["name"]) in only]
    index = CompanyIndex(universe)
    kinds = Counter(c["_kind"] for c in universe)
    print(f"companies: {dict(kinds)}", flush=True)

    # ---------------------------------------------------------------- 1) careers portals, in parallel
    recheck_from = src.days_ago(profile.get("portal_recheck_days", 14))

    def resolve_ats(c):
        """-> (ats cfg or None, probed_now)"""
        if c.get("ats"):
            return c["ats"], False
        hit = portals_cache.get(c["name"])
        if hit and hit.get("type"):
            return hit, False
        if not c.get("probe", True):
            return None, False
        if hit and (hit.get("checked") or "") >= recheck_from:
            return None, False                      # checked recently, nothing found: don't ask again yet
        slugs = c.get("slugs") or [re.sub(r"[^a-z0-9]", "", c["name"].lower())]
        try:
            return src.probe_portals(slugs, global_ats=c.get("probe_global", bool(c.get("slugs")))), True
        except Exception:
            return None, True

    def portal_work(c):
        ats, probed = resolve_ats(c)
        res = {"c": c, "ats": ats, "probed": probed, "jobs": [], "err": None}
        if ats and ats.get("type") in src.ATS:
            try:
                js = src.ATS[ats["type"]][0](ats)
                if ats["type"] in LOCATION_FILTERED_ATS:
                    js = [j for j in js if in_scope_location(j.location, profile)]
                res["jobs"] = js
            except Exception as e:
                res["err"] = f"{type(e).__name__}: {e}"
        return res

    with ThreadPoolExecutor(max_workers=profile.get("portal_workers", 8)) as pool:
        results = list(pool.map(portal_work, universe))

    linkedin_fallback = []
    new_portals = 0
    for res in results:
        c, ats, name = res["c"], res["ats"], res["c"]["name"]
        if res["probed"]:
            portals_cache[name] = dict(ats, checked=t) if ats else {"type": None, "checked": t}
            if ats:
                new_portals += 1
                print(f"  {name}: found careers portal automatically: {ats['type']}:{ats.get('tenant')}")
        portal_ok = False
        if ats and ats.get("type") in src.ATS:
            key = f"portal:{ats['type']}:{name}"
            if res["err"]:
                record(key, err=res["err"])
                print(f"  {name}: careers portal FAILED: {res['err'][:120]}")
            else:
                record(key, len(res["jobs"]))
                portal_ok = len(res["jobs"]) > 0
        for j in res["jobs"]:
            j.company, j.group, j.band = name, c.get("group", "target"), c.get("band", "unknown")
            j._ats, j._gate = ats["type"], GATE[c["_kind"]]
        raw += res["jobs"]
        wants_linkedin = c["_kind"] == "curated" or c.get("linkedin")
        if wants_linkedin and (not portal_ok or c.get("also_linkedin")):
            linkedin_fallback.append(c)
    print(f"portals: {sum(1 for r in results if r['jobs'])} with roles, {new_portals} newly found; "
          f"portal roles: {sum(len(r['jobs']) for r in results)}", flush=True)

    # ---------------------------------------------------------------- 2) LinkedIn by company name (curated only)
    for c in linkedin_fallback:
        name = c["name"]
        aliases = c.get("aliases") or [name]
        try:
            js = src.jobspy_search("linkedin", c.get("linkedin_query") or name, "India", hours,
                                    args.limit_per_search if c.get("group") == "target" else 25)
            js = [j for j in js if company_matches(j.company, aliases)]
            for j in js:
                j.company, j.group, j.band = name, c.get("group", "target"), c.get("band", "unknown")
                j._ats, j._gate = None, GATE[c["_kind"]]
            raw += js
            record("linkedin", len(js))
            print(f"  linkedin {name}: {len(js)}")
        except Exception as e:
            record("linkedin", err=f"{name}: {type(e).__name__}: {e}")
            print(f"  linkedin {name} FAILED: {e}")
        src.nap(3, 7)

    def assign(j):
        c = index.find(j.company)
        if c:
            j.company, j.group, j.band = c["name"], c.get("group", "target"), c.get("band", "unknown")
            j._gate = GATE[c["_kind"]]
        else:
            j.group, j.band, j._gate = "discovered", "unknown", "strict"
        return j

    # ---------------------------------------------------------------- 3) open searches on several sites
    if not args.no_discovery and not only:
        disc = cfg.get("discovery_searches", {})
        searches = [(s, "India") for s in disc.get("india", [])] + \
                   [(d["term"], d["location"]) for d in disc.get("international", [])]
        sites = cfg.get("discovery_sites") or {"linkedin": 25}
        for site, n in sites.items():
            for term, loc in searches:
                print(f"=== discovery [{site}]: {term} @ {loc}", flush=True)
                try:
                    js = [assign(j) for j in src.jobspy_search(site, term, loc, hours, n) if j.company]
                    raw += js
                    record(f"discovery:{site}", len(js))
                except Exception as e:
                    record(f"discovery:{site}", err=f"{term}: {type(e).__name__}: {e}")
                src.nap(3, 7) if site == "linkedin" else src.nap(1, 3)

    # ---------------------------------------------------------------- 4) job boards aimed at MBA hiring
    if not only:
        days = max(1, hours // 24)
        for term in cfg.get("board_searches", []):
            try:
                js = [assign(j) for j in src.iimjobs(term, days)]
                raw += js
                record("iimjobs", len(js))
            except Exception as e:
                record("iimjobs", err=f"{term}: {type(e).__name__}: {e}")
            src.nap(1, 3)
        try:
            js = [assign(j) for j in src.naukri_inbox(DATA / "naukri_inbox.json", max_age_days=3)]
            raw += js
            record("naukri (browser)", len(js))
            print(f"naukri inbox: {len(js)}")
        except Exception as e:
            record("naukri (browser)", err=f"{type(e).__name__}: {e}")

    print(f"\nraw jobs: {len(raw)}", flush=True)
    funnel = {s: {"raw": n} for s, n in Counter(j.source for j in raw).items()}

    # ---------------------------------------------------------------- 5) cheap filters first (title, company rule, recency)
    cutoff = src.days_ago(max(1, hours // 24))
    # a portal only lists roles that are still open, so a role posted up to N days ago is still worth showing
    portal_cutoff = src.days_ago(profile.get("portal_max_age_days", 30))
    only_rules = [(c["name"], c.get("aliases") or [c["name"]], re.compile(c["title_only"], re.I))
                  for c in cfg["companies"] if c.get("title_only")]

    def company_rule_ok(company, title):
        for name, aliases, rule in only_rules:
            if company == name or company_matches(company, aliases):
                return bool(rule.search(title or ""))
        return True

    stage = []
    for j in raw:
        if not ev.title_ok(j.title) or not company_rule_ok(j.company, j.title):
            continue
        if j.posted and j.posted < (portal_cutoff if j.source == "careers portal" else cutoff):
            continue
        stage.append(j)

    # dedupe (prefer careers-portal link over LinkedIn/iimjobs/Naukri/Indeed/Google)
    merged: dict[str, Job] = {}
    for j in sorted(stage, key=lambda j: SOURCE_RANK.get(j.source, 9)):
        k = dedupe_key(j)
        if k in merged:
            m = merged[k]
            m.location = merge_locations(m.location, j.location)
            if (j.posted or "") > (m.posted or ""):
                m.posted = j.posted
            if j.source not in m.sources:
                m.sources.append(j.source)
            if j.title not in m.variants:
                m.variants.append(j.title)
            if j.url and j.url not in [x["url"] for x in m.links]:
                m.links.append({"source": j.source, "url": j.url})
            if not m.exp_text and j.exp_text:
                m.exp_text = j.exp_text
            if m.salary_max is None and j.salary_max:
                m.salary_min, m.salary_max, m.currency = j.salary_min, j.salary_max, j.currency
            if len(j.description) > len(m.description):
                m.description = j.description
        else:
            j.sources = [j.source]
            j.links = [{"source": j.source, "url": j.url}] if j.url else []
            j.variants = [j.title]
            j.location = merge_locations("", j.location)
            merged[k] = j
    for s, n in Counter(j.source for j in merged.values()).items():
        funnel.setdefault(s, {})["after_filters"] = n
    print(f"after title/recency/dedupe: {len(merged)}", flush=True)

    # roles already on the dashboard (or rejected in the last few days) are not fetched / judged again
    prev = {}
    for d in load(DATA / "jobs.json", {}).get("jobs", []):
        prev[dedupe_key(Job(company=d.get("company", ""), title=d.get("title", "")))] = d
    fresh = {k: j for k, j in merged.items() if k not in prev and k not in rejected}
    again = {k: j for k, j in merged.items() if k in prev}
    print(f"new to judge: {len(fresh)} · already on dashboard: {len(again)} · "
          f"rejected recently: {len(merged) - len(fresh) - len(again)}", flush=True)

    # ---------------------------------------------------------------- 6) descriptions, only for roles that can still make it
    detail_counts = Counter()
    for j in fresh.values():
        # iimjobs search results only carry title + tags, so their full JD is still worth reading
        has_full_text = bool(j.description) and j.source != "iimjobs"
        j._detailed = has_full_text
        if has_full_text or not ev.worth_details(j):
            continue
        snippet = j.description
        try:
            if j.source == "linkedin":
                src.linkedin_detail(j)
                src.nap(1.5, 3.5)
            elif getattr(j, "_ats", None) and src.ATS.get(j._ats, (None, None))[1] and getattr(j, "_detail", None):
                src.ATS[j._ats][1](j)
                src.nap(0.5, 1.5)
            elif j.source in ("iimjobs", "google") and j.url:
                src.jsonld_detail(j)
                src.nap(0.5, 1.5)
            else:
                continue
            j._detailed = bool(j.description) and j.description != snippet
            if j._detailed and snippet and snippet not in j.description:
                j.description += "\n" + snippet
            detail_counts[j.source] += j._detailed
        except Exception as e:
            record("descriptions", err=f"{j.company} {j.title}: {e}")
    health["descriptions"] = dict(detail_counts)
    health["linkedin_detail_blocked"] = src.LINKEDIN["blocked"]

    # ---------------------------------------------------------------- 7) learn portals from job / apply links
    harvested: dict[str, dict] = {}
    for j in raw:
        for u in [j.url, getattr(j, "_apply_url", "")] + [l.get("url", "") for l in (j.links or [])]:
            a = src.ats_from_url(u)
            # only trust the link if the portal's name looks like the company's (not a recruiter's portal)
            if a and j.company and tenant_looks_like(a["tenant"], j.company):
                harvested.setdefault(j.company, a)
                break
    names_in_universe = {c["name"] for c in universe}
    learned = 0
    for name, a in harvested.items():
        if name in names_in_universe and not any(c.get("ats") for c in universe if c["name"] == name) \
                and not (portals_cache.get(name) or {}).get("type"):
            portals_cache[name] = dict(a, checked=t)
            learned += 1
    health["portals_learned_from_links"] = learned

    # ---------------------------------------------------------------- 8) evaluate + tier
    kept = []
    for k, j in fresh.items():
        if ev.evaluate(j):
            kept.append(j)
        elif getattr(j, "_detailed", False) or not ev.worth_details(j):
            rejected[k] = t          # judged with full information; don't fetch / judge it again for a while
    for s, n in Counter(j.source for j in kept).items():
        funnel.setdefault(s, {})["kept_new"] = n
    health["funnel"] = funnel
    print(f"kept after evaluation: {len(kept)}", flush=True)

    # ---------------------------------------------------------------- 9) auto-watchlist: companies worth reading every run
    wl = profile.get("watchlist", {}) or {}
    added = []
    if wl.get("enabled", True) and not only:
        add_tiers = set(wl.get("add_tiers", ["A", "B"]))
        for j in kept:
            name = (j.company or "").strip()
            if j.group == "watchlist" and name in auto:
                auto[name]["last_hit"], auto[name]["hits"] = t, auto[name].get("hits", 0) + 1
                continue
            if j.group != "discovered" or j.tier not in add_tiers:
                continue
            if len(norm(name)) < 3 or NOT_A_COMPANY.search(name):
                continue
            rec = auto.setdefault(name, {"first_seen": t, "hits": 0, "band": "unknown", "titles": []})
            if rec["hits"] == 0:
                added.append(name)
            rec["hits"] += 1
            rec["last_hit"] = t
            rec["titles"] = (rec.get("titles", []) + [j.title])[-3:]
            if name in harvested and not (portals_cache.get(name) or {}).get("type"):
                portals_cache[name] = dict(harvested[name], checked=t)
        expire = src.days_ago(wl.get("expire_days", 45))
        auto = {n: r for n, r in auto.items() if (r.get("last_hit") or "") >= expire}
        cap = wl.get("max_companies", 400)
        if len(auto) > cap:
            auto = dict(sorted(auto.items(), key=lambda kv: (kv[1].get("last_hit", ""), kv[1].get("hits", 0)), reverse=True)[:cap])
    health["watchlist"] = {"size": len(auto), "added_this_run": added[:50],
                           "with_portal": sum(1 for n in auto if (portals_cache.get(n) or {}).get("type"))}

    # ---------------------------------------------------------------- 10) merge with history, mark new
    out = {}
    for j in kept:
        k = dedupe_key(j)
        first_seen = seen.get(k) or t
        seen[k] = first_seen
        d = j.to_dict()
        d.update(key=k, first_seen=first_seen, last_seen=t, is_new=(first_seen == t))
        d["posted"] = d["posted"] or first_seen
        out[k] = d
    # roles seen again today: refresh cities / links / last_seen on the existing row
    for k, j in again.items():
        d = prev[k]
        d["last_seen"] = t
        d["location"] = merge_locations(d.get("location", ""), j.location)
        if (j.posted or "") > (d.get("posted") or ""):
            d["posted"] = j.posted
        for s in j.sources:
            if s not in (d.get("sources") or []):
                d["sources"] = (d.get("sources") or []) + [s]
        for l in j.links:
            if l["url"] not in [x.get("url") for x in d.get("links") or []]:
                d["links"] = (d.get("links") or []) + [l]
    # keep earlier roles for max_age days (portal roles: as long as still open, up to portal_max_age_days)
    keep_from = src.days_ago(profile.get("max_age_days_on_dashboard", 7))
    for k, d in prev.items():
        if k in out:
            out[k]["location"] = merge_locations(out[k]["location"], d.get("location", ""))
            continue
        still_valid = ev.title_ok(d.get("title", "")) and company_rule_ok(d.get("company", ""), d.get("title", "")) and \
            (d.get("exp_min") is None or d["exp_min"] <= profile["experience"]["stretch_max_min"])
        posted = d.get("posted") or ""
        if not still_valid or not (posted >= keep_from or (k in again and posted >= portal_cutoff)):
            continue
        d["is_new"] = False
        d["key"] = k
        d["location"] = merge_locations("", d.get("location", ""))
        out[k] = d

    # final cross-check for repeats: same opening on several websites / cities / slightly different titles
    before = len(out)
    rows = collapse(list(out.values()))
    for d in rows:
        seen[d["key"]] = d.get("first_seen") or t
    print(f"cross-source duplicates folded: {before - len(rows)}", flush=True)
    jobs = sorted(rows, key=lambda d: (d.get("posted") or "", -{"A": 0, "B": 1, "C": 2}.get(d["tier"], 3), d.get("relevance", 0)), reverse=True)
    payload = {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "count": len(jobs),
               "new_today": sum(1 for d in jobs if d["is_new"]), "jobs": jobs}
    health["counts"] = {"raw": len(raw), "after_filters": len(merged), "judged": len(fresh), "kept": len(kept),
                        "duplicates_folded": before - len(rows), "on_dashboard": len(jobs),
                        "companies_scanned": dict(kinds)}

    rej_from = src.days_ago(profile.get("rejected_memory_days", 5))
    rejected = {k: d for k, d in rejected.items() if d >= rej_from}
    (DATA / "jobs.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    (DOCS / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False))
    (DATA / "portals.json").write_text(json.dumps(dict(sorted(portals_cache.items())), indent=1))
    (DATA / "companies_auto.json").write_text(json.dumps(dict(sorted(auto.items())), indent=1, ensure_ascii=False))
    (DATA / "rejected.json").write_text(json.dumps(rejected, indent=0))
    (DATA / "seen.json").write_text(json.dumps(seen, indent=0))
    (DATA / "health.json").write_text(json.dumps(health, indent=1))
    (DOCS / "health.json").write_text(json.dumps(health))
    write_summary(payload, health)
    print(json.dumps(health["counts"]))


def write_summary(payload, health=None):
    new = [d for d in payload["jobs"] if d["is_new"]]
    lines = [f"# Job Hunt – {payload['updated']}", "",
             f"**{payload['new_today']} new roles today** · {payload['count']} active on the dashboard", ""]
    if health:
        f = health.get("funnel", {})
        lines += ["| Source | Raw | After filters | New kept |", "|---|---|---|---|"]
        lines += [f"| {s} | {v.get('raw', 0)} | {v.get('after_filters', 0)} | {v.get('kept_new', 0)} |" for s, v in sorted(f.items())]
        w = health.get("watchlist", {})
        lines += ["", f"Auto-watchlist: {w.get('size', 0)} companies ({w.get('with_portal', 0)} with a readable portal)"
                  + (f" · added: {', '.join(w.get('added_this_run', [])[:15])}" if w.get("added_this_run") else ""), ""]
    for tier in "ABC":
        rows = [d for d in new if d["tier"] == tier]
        if not rows:
            continue
        lines += [f"## Tier {tier} ({len(rows)})", "", "| Company | Role | Location | Exp | MBA | Pay | Link |", "|---|---|---|---|---|---|---|"]
        for d in rows:
            mba = "Yes" if d["mba"].startswith("Yes") else "–"
            lines.append(f"| {d['company']} | {d['title']} | {d['location']} | {d['exp_label']} | {mba} | {d['salary_label']} | [open]({d['url']}) |")
        lines.append("")
    (DATA / "SUMMARY.md").write_text("\n".join(lines))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
