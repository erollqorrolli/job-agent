"""Persistent queue worker. One browser, one application at a time, resumable."""
import random, signal, subprocess, sys, time, traceback
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
from .config import ROOT, load_config
from .db import DB
from .ingest import ingest
from .runner import process, Outcome
from .profile import load_profile, verified, missing_required

SPEED = {"greenhouse": 0, "lever": 0, "ashby": 0, "smartrecruiters": 1, "workable": 1, "generic": 2}


def effective_cfg(db):
    cfg = load_config()
    cfg.update(db.kv_get("settings", {}) or {})
    return cfg


def notify(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "job-agent"'], capture_output=True)


def next_job(db, cfg, skip, platform=None):
    rows = db.q("SELECT * FROM jobs WHERE status='NOT_STARTED' AND next_try_at<=? ", (time.time(),))
    rows = [r for r in rows if r["priority"] in cfg.get("priorities", [1, 2, 3]) and r["id"] not in skip and (not platform or r["platform"] == platform)]
    rows.sort(key=lambda r: (r["priority"], SPEED.get(r["platform"], 1), -(r["date_posted"] or 0)))
    return rows[0] if rows else None


def launch(pw, cfg):
    args = []
    ext = cfg.get("extension_path")
    if ext:
        args += [f"--disable-extensions-except={ext}", f"--load-extension={ext}"]
    return pw.chromium.launch_persistent_context(str(ROOT / cfg.get("browser_profile", "browser_profile")),
                                                 headless=cfg.get("headless", False), args=args,
                                                 viewport={"width": 1280, "height": 900})


def run(db=None, once_limit=None, answer_chat=None, log=print, override=None, platform=None):
    db = db or DB()
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    db.x("UPDATE jobs SET status='NOT_STARTED' WHERE status='IN_PROGRESS'")  # crash/restart recovery
    db.kv_set("control", "run")
    db.kv_set("current", None)
    done, tried, host_last = 0, set(), {}
    pw = sync_playwright().start()
    ctx = None
    try:
        while not stop["flag"]:
            cfg = {**effective_cfg(db), **(override or {})}
            control = db.kv_get("control", "run")
            if control == "stop":
                break
            if control == "pause":
                time.sleep(2); continue
            if time.time() - (db.kv_get("last_ingest", 0) or 0) > cfg.get("poll_minutes", 120) * 60:
                try:
                    log("ingest", ingest(db, cfg))
                except Exception as e:
                    log("ingest failed", e)
                    db.kv_set("last_ingest", time.time() - cfg.get("poll_minutes", 120) * 60 + 600)
            if done >= (once_limit or cfg.get("max_per_session", 50)):
                log("session limit reached"); break
            job = next_job(db, cfg, tried, platform)
            if not job:
                if once_limit:
                    break
                time.sleep(15); continue
            if cfg.get("live_submit") and not verified(load_profile()):
                log("live_submit on but profile unverified/incomplete:", missing_required(load_profile()))
                db.kv_set("control", "pause"); continue
            host = urlsplit(job["url"]).netloc
            wait = 8 - (time.time() - host_last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            if ctx is None:
                ctx = launch(pw, cfg)
            db.update(job["id"], status="IN_PROGRESS", attempted_at=time.time())
            db.kv_set("current", f"{job['company']} - {job['title']}")
            page = None
            try:
                page = ctx.new_page()
                page.set_default_timeout(15000)
                out = process(page, db, cfg, job, answer_chat)
            except Exception as e:
                msg = str(e).splitlines()[0][:200] if str(e) else type(e).__name__
                out = Outcome("FAILED", msg)
                if "closed" in msg.lower() and "target" in msg.lower():  # browser died
                    try: ctx.close()
                    except Exception: pass
                    ctx = None
                db.log(job["id"], traceback.format_exc()[-400:])
            finally:
                try: page and page.close()
                except Exception: pass
            host_last[host] = time.time()
            apply_outcome(db, cfg, job, out)
            tried.add(job["id"])
            done += 1
            log(f"{out.status:18} {job['company'][:24]:24} {job['title'][:40]:40} {out.reason}")
            if out.status == "NEEDS_USER_INPUT":
                notify(f"Input needed: {job['company']}")
            lo, hi = cfg.get("delay_seconds", [20, 45])
            if not once_limit:
                time.sleep(random.uniform(lo, hi))
    finally:
        db.kv_set("current", None)
        db.kv_set("control", "stop")
        try:
            ctx and ctx.close()
        except Exception:
            pass
        pw.stop()
    return done


def apply_outcome(db, cfg, job, out: Outcome):
    f = dict(status=out.status, status_reason=out.reason, evidence=out.evidence, resume_used=out.resume or None,
             questions_json=__import__("json").dumps(out.questions))
    if out.status == "APPLIED":
        f["submitted_at"] = time.time()
    if out.status == "NOT_STARTED" and out.reason == "DEFER_REVIEW":
        f["next_try_at"] = time.time() + 900     # try again after the user has had a chance to review
        f["status_reason"] = "waiting for resume reviews"
    if out.status == "NOT_STARTED" and out.reason == "DRYRUN_OK":
        f["next_try_at"] = time.time() + 86400 * 3650  # parked until live mode is enabled
        f["last_error"] = "DRYRUN_OK"
    if out.status == "FAILED":
        n = job["retries"] + 1
        f["retries"] = n; f["last_error"] = out.reason
        if n < cfg.get("max_retries", 3):
            f["status"] = "NOT_STARTED"; f["next_try_at"] = time.time() + 300 * (2 ** n)  # backoff
    db.update(job["id"], **f)
