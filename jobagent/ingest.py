"""Fetch the Simplify Summer2027 listings.json, normalize, dedupe, queue."""
import time, requests
from .classify import platform_of, normalize_url, dedupe_key, priority_of, eligibility
from .profile import load_profile

REPO = "https://github.com/SimplifyJobs/Summer2027-Internships"


def fetch(url: str) -> list[dict]:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, list):
        raise ValueError("unexpected listings format: not a list")
    return data


def ingest(db, cfg, items=None) -> dict:
    items = items if items is not None else fetch(cfg["source_url"])
    term = cfg.get("term", "Summer 2027")
    profile = load_profile()
    stats = dict(seen=0, new=0, changed_url=0, closed=0, inelig=0, dup=0)
    now = time.time()
    existing = {r["id"]: r for r in db.q("SELECT id,url_key,status,dedupe_key FROM jobs")}
    seen_keys = {r["dedupe_key"]: r["id"] for r in existing.values()}
    for it in items:
        if not any(term in t for t in it.get("terms", [])):
            continue
        stats["seen"] += 1
        jid = it["id"]
        url = it.get("url") or ""
        active = bool(it.get("active")) and it.get("is_visible", True) and url
        row = existing.get(jid)
        if row:
            if not active and row["status"] in ("NOT_STARTED", "NEEDS_USER_INPUT", "FAILED", "UNCERTAIN"):
                db.update(jid, status="CLOSED", status_reason="closed in source"); stats["closed"] += 1
            elif active and normalize_url(url) != row["url_key"]:
                # link changed: only requeue if we never completed it
                db.update(jid, url=url, url_key=normalize_url(url), platform=platform_of(url))
                if row["status"] in ("FAILED", "BLOCKED", "CLOSED"):
                    db.update(jid, status="NOT_STARTED", retries=0, status_reason="link changed")
                stats["changed_url"] += 1
            continue
        if not active:
            continue  # never insert closed listings
        dk = dedupe_key(it["company_name"], it["title"], it.get("locations", []))
        uk = normalize_url(url)
        status, reason = "NOT_STARTED", None
        dup_uk = db.q("SELECT id FROM jobs WHERE url_key=?", (uk,))
        if dk in seen_keys or dup_uk:
            status, reason = "CLOSED", "duplicate of " + (seen_keys.get(dk) or dup_uk[0]["id"])
            stats["dup"] += 1
        else:
            ok, why = eligibility(it, profile, cfg)
            if not ok:
                status, reason = "INELIGIBLE", why; stats["inelig"] += 1
        seen_keys[dk] = jid
        db.x("""INSERT INTO jobs(id,company,title,location,url,url_key,dedupe_key,source_url,category,priority,platform,
                status,status_reason,date_posted,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (jid, it["company_name"], it["title"], "; ".join(it.get("locations", [])), url, uk, dk, REPO,
              it.get("category"), priority_of(it["title"], it.get("category")), platform_of(url),
              status, reason, it.get("date_posted"), now))
        stats["new"] += 1
    db.kv_set("last_ingest", now)
    db.log("-", f"ingest {stats}")
    return stats
