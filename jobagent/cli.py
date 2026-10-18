import argparse, csv, sys, time, json
from .db import DB
from .config import load_config, DATA
from .ingest import ingest
from .profile import load_profile, missing_required, verified
from .resume_sources import resume_inventory
from . import worker


def export_csv(db, path=None):
    path = path or DATA / "applications.csv"
    rows = db.q("""SELECT company,title,location,url,source_url,category,priority,platform,status,status_reason,
        datetime(discovered_at,'unixepoch') discovered, datetime(attempted_at,'unixepoch') attempted,
        datetime(submitted_at,'unixepoch') submitted, resume_used, retries, last_error, evidence, questions_json FROM jobs ORDER BY priority, company""")
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["company"])
        w.writeheader(); w.writerows(rows)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(prog="jobagent")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest"); sub.add_parser("stats"); sub.add_parser("export"); sub.add_parser("login")
    sub.add_parser("profile-check"); sub.add_parser("retry-failed"); sub.add_parser("enable-live"); sub.add_parser("answer")
    d = sub.add_parser("dry-run"); d.add_argument("n", type=int, nargs="?", default=5); d.add_argument("--platform"); d.add_argument("--no-review", action="store_true", help="skip resume tailoring during this dry run")
    w = sub.add_parser("worker"); w.add_argument("--limit", type=int)
    ds = sub.add_parser("dashboard"); ds.add_argument("--port", type=int, default=5055)
    a = ap.parse_args(argv)
    db = DB(); cfg = worker.effective_cfg(db)

    if a.cmd == "ingest":
        print(ingest(db, cfg))
    elif a.cmd == "stats":
        print(json.dumps(db.counts(), indent=1))
    elif a.cmd == "export":
        print(export_csv(db))
    elif a.cmd == "profile-check":
        p = load_profile(); has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, p))
        m = missing_required(p, has_resume=has_resume)
        print("verified:", p.get("verified"), "| missing:", m or "none")
    elif a.cmd == "retry-failed":
        db.x("UPDATE jobs SET status='NOT_STARTED', retries=0, next_try_at=0 WHERE status IN ('FAILED','UNCERTAIN','BLOCKED')")
    elif a.cmd == "enable-live":
        p = load_profile(); has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, p))
        if not verified(p, has_resume=has_resume):
            sys.exit("profile not verified/complete: " + str(missing_required(p, has_resume=has_resume)))
        db.kv_set("settings", {**(db.kv_get("settings", {}) or {}), "live_submit": True})
        db.x("UPDATE jobs SET next_try_at=0, last_error=NULL WHERE last_error='DRYRUN_OK'")
        print("live submission ENABLED; dry-run jobs requeued")
    elif a.cmd == "login":  # open the persistent profile headed so you can sign in to Simplify / ATS sites
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            ctx = worker.launch(pw, {**cfg, "headless": False})
            ctx.new_page().goto("https://simplify.jobs/auth/login")
            input("Sign in to Simplify (and any ATS you want reused), then press Enter here to close... ")
            ctx.close()
    elif a.cmd == "answer":
        needs = db.q("SELECT qkey, question, kind, options, COUNT(*) n FROM human_queue WHERE resolved_at IS NULL GROUP BY qkey ORDER BY n DESC")
        for n in needs:
            print(f"\n[{n['n']} jobs] {n['question']}  ({n['kind']}) {n['options'] if n['options']!='[]' else ''}")
            v = input("answer (blank=skip): ").strip()
            if v:
                db.resolve_need(n["qkey"], v)
    elif a.cmd == "dry-run":  # never submits, regardless of saved settings
        worker.run(db, once_limit=a.n, override={"live_submit": False, "headless": True,
                                                 **({"tailor_resume": False} if a.no_review else {})}, platform=a.platform)
    elif a.cmd == "worker":
        worker.run(db, once_limit=a.limit)
    elif a.cmd == "dashboard":
        from .dashboard import app
        app.run(port=a.port, debug=False)


if __name__ == "__main__":
    main()
