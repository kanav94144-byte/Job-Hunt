"""Daily job-hunt run.  python -m scraper.run  [--first-run] [--only "Company,Company"] [--no-discovery]"""
from __future__ import annotations

import argparse
import os
import json
import re
import sys
import time
import traceback
from pathlib import Path

import yaml

from . import sources as src
from .core import Evaluator, Job, dedupe_key, norm, today

ROOT = Path(__file__).resolve().parent.parent
DATA, DOCS = ROOT / "data", ROOT / "docs"


def load(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def company_matches(job_company: str, aliases: list[str]) -> bool:
    jc = norm(job_company)
    for a in aliases:
        na = norm(a)
        if not na:
            continue
        # exact, or company name starts with alias as a whole word ("Swiggy Instamart", "Walmart Global Tech India")
        if jc == na or re.match(rf"^{re.escape(na)}\b", jc):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--first-run", action="store_true")
    ap.add_argument("--only", default="")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--limit-per-search", type=int, default=40)
    args = ap.parse_args()

    profile = yaml.safe_load((ROOT / "config/profile.yml").read_text())
    cfg = yaml.safe_load((ROOT / "config/companies.yml").read_text())
    ev = Evaluator(profile)
    DATA.mkdir(exist_ok=True)
    DOCS.mkdir(exist_ok=True)

    seen = load(DATA / "seen.json", {})
    first = args.first_run or not seen or os.environ.get("FULL_SCAN") == "true"
    hours = profile["recency_hours_first_run"] if first else profile["recency_hours_daily"]
    health = {"run_date": today(), "first_run": first, "sources": {}}
    raw: list[Job] = []

    def record(name, n=None, err=None):
        h = health["sources"].setdefault(name, {"ok": 0, "failed": 0, "jobs": 0, "errors": []})
        if err:
            h["failed"] += 1
            h["errors"] = (h["errors"] + [err[:300]])[-3:]
        else:
            h["ok"] += 1
            h["jobs"] += n or 0

    only = {norm(x) for x in args.only.split(",") if x.strip()}
    companies = [c for c in cfg["companies"] if not only or norm(c["name"]) in only]

    portals_cache = load(DATA / "portals.json", {})
    for c in companies:
        name = c["name"]
        print(f"\n=== {name}", flush=True)
        aliases = c.get("aliases") or [name]
        found: list[Job] = []

        # 1) company's own careers portal (first choice)
        ats = c.get("ats") or portals_cache.get(name)
        if not ats and c.get("probe", True):
            slugs = c.get("slugs") or [re.sub(r"[^a-z0-9]", "", name.lower())]
            try:
                ats = src.probe_portals(slugs, global_ats=bool(c.get("slugs")))
            except Exception:
                ats = None
            if ats:
                portals_cache[name] = ats
                print(f"  found portal automatically: {ats}")
        portal_ok = False
        if ats and ats["type"] in src.ATS:
            fetch, _ = src.ATS[ats["type"]]
            try:
                js = fetch(ats)
                found += js
                portal_ok = True
                record(f"portal:{ats['type']}:{name}", len(js))
                print(f"  careers portal ({ats['type']}): {len(js)} open roles")
            except Exception as e:
                record(f"portal:{ats['type']}:{name}", err=f"{type(e).__name__}: {e}")
                print(f"  careers portal FAILED: {e}")

        # 2) LinkedIn by company name (fallback when there is no readable portal, or to add coverage)
        if not portal_ok or c.get("also_linkedin"):
            try:
                js = src.jobspy_search("linkedin", c.get("linkedin_query") or name, "India", hours,
                                        args.limit_per_search if c.get("group") == "target" else 25)
                js = [j for j in js if company_matches(j.company, aliases)]
                found += js
                record("linkedin", len(js))
                print(f"  linkedin: {len(js)}")
            except Exception as e:
                record("linkedin", err=f"{name}: {type(e).__name__}: {e}")
                print(f"  linkedin FAILED: {e}")
            src.nap(3, 7)

        for j in found:
            j.company, j.group, j.band = name, c.get("group", "target"), c.get("band", "unknown")
            j._ats = ats["type"] if (ats and j.source == "careers portal") else None
        raw += found

    # 3) discovery searches (other startups/companies)
    if not args.no_discovery and not only:
        known = [a for c in cfg["companies"] for a in (c.get("aliases") or [c["name"]])]
        disc = cfg.get("discovery_searches", {})
        searches = [(t, "India") for t in disc.get("india", [])] + \
                   [(d["term"], d["location"]) for d in disc.get("international", [])]
        for term, loc in searches:
            print(f"\n=== discovery: {term} @ {loc}", flush=True)
            for site in ("linkedin",):
                try:
                    js = src.jobspy_search(site, term, loc, hours, 25)
                    js = [j for j in js if j.company and not company_matches(j.company, known)]
                    for j in js:
                        j.group, j.band = "discovered", "unknown"
                    raw += js
                    record(f"discovery:{site}", len(js))
                except Exception as e:
                    record(f"discovery:{site}", err=f"{term}: {type(e).__name__}: {e}")
                src.nap(3, 7)

    print(f"\nraw jobs: {len(raw)}", flush=True)

    # 4) cheap filters first (title, location, recency) -> then fetch descriptions only for survivors
    cutoff = src.days_ago(max(1, hours // 24))
    stage = []
    for j in raw:
        if not ev.title_ok(j.title):
            continue
        if j.posted and j.posted < cutoff:
            continue
        stage.append(j)

    # dedupe (prefer careers-portal link over LinkedIn/Naukri)
    merged: dict[str, Job] = {}
    rank = lambda j: 0 if j.source == "careers portal" else 1
    for j in sorted(stage, key=rank):
        k = dedupe_key(j)
        if k in merged:
            if j.source not in merged[k].sources:
                merged[k].sources.append(j.source)
            if not merged[k].exp_text and j.exp_text:
                merged[k].exp_text = j.exp_text
            if merged[k].salary_max is None and j.salary_max:
                merged[k].salary_min, merged[k].salary_max, merged[k].currency = j.salary_min, j.salary_max, j.currency
            if len(j.description) > len(merged[k].description):
                merged[k].description = j.description
        else:
            j.sources = [j.source]
            merged[k] = j
    print(f"after title/recency/dedupe: {len(merged)}", flush=True)

    for j in merged.values():
        if j.description:
            continue
        try:
            if j.source == "linkedin":
                j.description = src.linkedin_description(j.url)
                src.nap(1.5, 3.5)
            elif getattr(j, "_ats", None) and src.ATS.get(j._ats, (None, None))[1] and getattr(j, "_detail", None):
                src.ATS[j._ats][1](j)
                src.nap(0.5, 1.5)
        except Exception as e:
            record("descriptions", err=f"{j.company} {j.title}: {e}")

    # 5) evaluate + tier
    kept = [j for j in merged.values() if ev.evaluate(j)]
    print(f"kept after evaluation: {len(kept)}", flush=True)

    # 6) merge with history, mark new
    t = today()
    prev = {dedupe_key_d(d): d for d in load(DATA / "jobs.json", {}).get("jobs", [])}
    out = {}
    for j in kept:
        k = dedupe_key(j)
        first_seen = seen.get(k) or t
        seen[k] = first_seen
        d = j.to_dict()
        d.update(key=k, first_seen=first_seen, last_seen=t, is_new=(first_seen == t))
        d["posted"] = d["posted"] or first_seen
        out[k] = d
    # keep roles found in earlier runs for up to max_age days (still visible on dashboard)
    keep_from = src.days_ago(profile.get("max_age_days_on_dashboard", 7))
    for k, d in prev.items():
        if k not in out and d.get("posted", "") >= keep_from:
            d["is_new"] = False
            out[k] = d

    jobs = sorted(out.values(), key=lambda d: (d.get("posted") or "", -{"A": 0, "B": 1, "C": 2}.get(d["tier"], 3), d.get("relevance", 0)), reverse=True)
    payload = {"updated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()), "count": len(jobs),
               "new_today": sum(1 for d in jobs if d["is_new"]), "jobs": jobs}
    health["counts"] = {"raw": len(raw), "after_filters": len(merged), "kept": len(kept), "on_dashboard": len(jobs)}

    (DATA / "jobs.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False))
    (DOCS / "jobs.json").write_text(json.dumps(payload, ensure_ascii=False))
    (DATA / "portals.json").write_text(json.dumps(portals_cache, indent=1))
    (DATA / "seen.json").write_text(json.dumps(seen, indent=0))
    (DATA / "health.json").write_text(json.dumps(health, indent=1))
    (DOCS / "health.json").write_text(json.dumps(health))
    write_summary(payload)
    print(json.dumps(health["counts"]))


def dedupe_key_d(d):
    return d.get("key", "")


def write_summary(payload):
    new = [d for d in payload["jobs"] if d["is_new"]]
    lines = [f"# Job Hunt – {payload['updated']}", "",
             f"**{payload['new_today']} new roles today** · {payload['count']} active on the dashboard", ""]
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
