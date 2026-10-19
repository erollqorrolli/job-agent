"""Persistent queue worker. One browser, one application at a time, resumable."""
import random, signal, subprocess, sys, time, traceback
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright
from .config import ROOT, load_config
from .db import DB
from .ingest import ingest
from .runner import process, Outcome
from .profile import load_profile, verified, missing_required
from .resume_sources import resume_inventory

SPEED = {"greenhouse": 0, "lever": 0, "ashby": 0, "smartrecruiters": 1, "workable": 1, "generic": 2}


def effective_cfg(db):
    cfg = load_config()
    settings = db.kv_get("settings", {}) or {}
    base_editor = cfg.get("resume_editor", {}) or {}
    editor_override = settings.get("resume_editor") or {}
    cfg.update(settings)
    if editor_override:
        base_models = base_editor.get("models", {}) or {}
        override_models = editor_override.get("models", {}) or {}
        cfg["resume_editor"] = {**base_editor, **editor_override,
                                "models": {**base_models, **override_models}}
    return cfg


def notify(msg):
    if sys.platform == "darwin":
        subprocess.run(["osascript", "-e", f'display notification "{msg}" with title "job-agent"'], capture_output=True)


def next_job(db, cfg, skip, platform=None):
    rows = db.q("SELECT * FROM jobs WHERE status='NOT_STARTED' AND next_try_at<=? ", (time.time(),))
    rows = [r for r in rows if r["priority"] in cfg.get("priorities", [1, 2, 3]) and r["id"] not in skip and (not platform or r["platform"] == platform)]
    rows.sort(key=lambda r: (r["priority"], SPEED.get(r["platform"], 1), -(r["date_posted"] or 0)))
    return rows[0] if rows else None


def claim_job(db, job_id: str) -> bool:
    """Atomically claim a queued job unless a user decision got there first."""
    cur = db.x("""UPDATE jobs SET status='IN_PROGRESS', attempted_at=?
                  WHERE id=? AND status='NOT_STARTED'""", (time.time(), job_id))
    return cur.rowcount == 1


def launch(pw, cfg):
    args = []
    ext = cfg.get("extension_path")
    if ext:
        args += [f"--disable-extensions-except={ext}", f"--load-extension={ext}"]
    return pw.chromium.launch_persistent_context(str(ROOT / cfg.get("browser_profile", "browser_profile")),
                                                 channel=cfg.get("browser_channel", "chrome"),
                                                 headless=cfg.get("headless", False), args=args,
                                                 viewport={"width": 1280, "height": 900})


def requeue_manual_resume_reviews(db, cfg):
    """Resume-only review gates are non-blocking when the final submit is manual."""
    if cfg.get("handoff", "fallback") != "always" or cfg.get("headless", False):
        return 0
    cur = db.x("""UPDATE jobs SET status='NOT_STARTED',
                  status_reason='tailored resume ready; queued for manual final review', next_try_at=0
                  WHERE status='NEEDS_USER_INPUT'
                    AND EXISTS (SELECT 1 FROM human_queue h WHERE h.job_id=jobs.id
                                AND h.resolved_at IS NULL AND h.kind='resume')
                    AND NOT EXISTS (SELECT 1 FROM human_queue h WHERE h.job_id=jobs.id
                                    AND h.resolved_at IS NULL AND h.kind!='resume')""")
    return cur.rowcount


def run(db=None, once_limit=None, answer_chat=None, log=print, override=None, platform=None):
    db = db or DB()
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *a: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *a: stop.update(flag=True))
    db.x("UPDATE jobs SET status='NOT_STARTED' WHERE status='IN_PROGRESS'")  # crash/restart recovery
    db.kv_set("control", "run")
    db.kv_set("current", None)
    done, tried, host_last = 0, set(), {}
    ready = {}    # job id -> (page, job, held status) kept open for review or the user's Submit click
    db.x("UPDATE jobs SET status='NOT_STARTED' WHERE status='READY_TO_SUBMIT'")   # tabs from a previous run are gone
    requeue_manual_resume_reviews(db, {**effective_cfg(db), **(override or {})})
    pw = sync_playwright().start()
    ctx = None
    try:
        while not stop["flag"]:
            cfg = {**effective_cfg(db), **(override or {})}
            control = db.kv_get("control", "run")
            if control == "stop":
                break
            if control == "pause":
                # Pause must keep the browser session alive. Still observe held tabs so a
                # manual submission made while paused can be recorded as confirmed.
                poll_ready(db, ready, log)
                time.sleep(2); continue
            if time.time() - (db.kv_get("last_ingest", 0) or 0) > cfg.get("poll_minutes", 120) * 60:
                try:
                    log("ingest", ingest(db, cfg))
                except Exception as e:
                    log("ingest failed", e)
                    db.kv_set("last_ingest", time.time() - cfg.get("poll_minutes", 120) * 60 + 600)
            if done >= (once_limit or cfg.get("max_per_session", 50)):
                if ready:
                    db.kv_set("control", "pause")
                    db.kv_set("current", f"session limit reached; {len(ready)} review tab(s) kept open")
                    log("session limit reached; paused with review tabs open")
                    continue
                log("session limit reached"); break
            poll_ready(db, ready, log)
            if len(ready) >= cfg.get("max_ready_tabs", 20):  # don't pile up unlimited tabs; keep polling until some are submitted
                db.kv_set("current", f"{len(ready)} forms waiting for review in the browser tabs")
                time.sleep(5); continue
            # jobs the user just answered are eligible again within this session
            tried &= {r["id"] for r in db.q("SELECT id FROM jobs WHERE status IN ('APPLIED','UNCERTAIN','ALREADY_APPLIED','INELIGIBLE','SKIPPED','CLOSED','FAILED','BLOCKED','READY_TO_SUBMIT')")}
            job = next_job(db, cfg, tried, platform)
            if not job:
                if once_limit:
                    break
                time.sleep(15); continue
            profile = load_profile()
            has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, profile))
            if cfg.get("live_submit") and not verified(profile, has_resume=has_resume):
                log("live_submit on but profile unverified/incomplete:",
                    missing_required(profile, has_resume=has_resume))
                db.kv_set("control", "pause"); continue
            host = urlsplit(job["url"]).netloc
            wait = 8 - (time.time() - host_last.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            if ctx is None:
                ctx = launch(pw, cfg)
            if not claim_job(db, job["id"]):  # manually removed from the queue while this worker was waiting
                tried.add(job["id"])
                continue
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
                try:
                    if not (locals().get("out") is not None and getattr(out, "keep_page", False)):
                        page and page.close()
                except Exception: pass
            host_last[host] = time.time()
            apply_outcome(db, cfg, job, out)
            if getattr(out, "keep_page", False):
                ready[job["id"]] = (page, job, out.status)
            tried.add(job["id"])
            # the session cap counts real submissions in live mode; jobs parked for your input don't use it up
            if not cfg.get("live_submit") or out.submitted:
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


def poll_ready(db, ready: dict, log=print):
    """Keep review tabs alive and detect a user's manual submission."""
    from . import detect
    from .adapters import adapter_for
    from .runner import shot
    for jid, entry in list(ready.items()):
        page, job = entry[:2]
        held_status = entry[2] if len(entry) > 2 else "READY_TO_SUBMIT"
        try:
            if page.is_closed():
                if held_status == "READY_TO_SUBMIT":
                    db.update(jid, status="UNCERTAIN", status_reason="tab closed before a confirmation was seen. Check your email")
                ready.pop(jid); continue
            c = detect.confirmation(page, adapter_for(job["platform"]))
            if c:
                db.x("""UPDATE human_queue SET answer='completed manually in browser', resolved_at=?
                        WHERE job_id=? AND resolved_at IS NULL""", (time.time(), jid))
                db.update(jid, status="APPLIED", status_reason="confirmed", submitted_at=time.time(), evidence=f"{c} | {shot(page, job, 'confirm')}")
                log(f"APPLIED (confirmed)  {job['company']} - {job['title']}")
                page.close(); ready.pop(jid)
                continue
            current = db.q("SELECT status FROM jobs WHERE id=?", (jid,))
            if held_status != "READY_TO_SUBMIT" and current and current[0]["status"] != held_status:
                page.close(); ready.pop(jid)
        except Exception:
            ready.pop(jid, None)


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
