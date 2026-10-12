"""Process one job: open, fill (multi-step), verify, submit (only if allowed), confirm."""
import json, re, time
from dataclasses import dataclass, field
from . import detect, fields
from .adapters import adapter_for
from .answers import write_answer, llm_configured, pick_grounded, CFG_FOR_CHOICE
from .config import SHOTS
from .profile import load_profile, get, verified, norm_key
from .config import DATA
from .tailor import tailor_fit

MAX_STEPS = 10


@dataclass
class Outcome:
    status: str
    reason: str = ""
    evidence: str = ""
    resume: str = ""
    questions: list = field(default_factory=list)
    submitted: bool = False      # the final submit button was actually clicked


class Ctx:
    def __init__(self, db, cfg, profile, resume_path, answer_fn, job_id=None):
        self.job_id = job_id
        self.db, self.cfg, self.profile, self.resume_path, self.answer_fn = db, cfg, profile, resume_path, answer_fn


def pick_resume(profile, category):
    rs = profile.get("resumes") or []
    tag = {"AI/ML/Data": ("ml", "ai", "data"), "Software": ("swe", "software"), "Quant": ("quant",)}.get(category, ())
    for r in rs:
        if r.get("path") and any(t in (r.get("tags") or []) for t in tag):
            return r["path"]
    for r in rs:
        if r.get("path"):
            return r["path"]
    return None


def maybe_tailor(db, cfg, profile, job, job_text, default_path):
    """Returns (path_to_upload, Outcome|None). Outcome is set when the user must review a tailored resume first."""
    docx = next((r.get("docx") for r in profile.get("resumes") or [] if r.get("docx")), None)
    bank = db.kv_get("skills") or {}
    if not cfg.get("tailor_resume", True) or not docx or not bank or len(job_text) < cfg.get("tailor_min_chars", 200):
        return default_path, None
    ident = profile.get("identity", {})
    out = DATA / "resumes" / job["id"][:8] / f"{ident.get('first_name','')}_{ident.get('last_name','')}_Resume.docx"
    try:
        out, info = tailor_fit(docx, job_text, bank, out)
    except Exception as e:
        db.log(job["id"], f"tailor failed, using default resume: {e}")
        return default_path, None
    key = f"resume@{job['id']}"
    approved = db.q("SELECT COUNT(*) n FROM answers WHERE qkey LIKE 'resume@%'")[0]["n"]
    limit = cfg.get("tailor_review_first", 5)
    if approved < limit and db.get_answer(key) is None:
        pending = db.q("SELECT COUNT(*) n FROM human_queue WHERE kind='resume' AND resolved_at IS NULL AND job_id!=?", (job["id"],))[0]["n"]
        if approved + pending >= limit:     # enough reviews already waiting; hold this job instead of flooding the queue
            return str(out), Outcome("NOT_STARTED", "DEFER_REVIEW")
        summary = f"added: {', '.join(info['added_used'] + info['added_course']) or 'nothing'}; projects reordered: {info['reordered']}"
        db.add_need(job["id"], key, f"Review tailored resume for {job['company']} - {job['title']}", "resume", [str(out), summary])
        return str(out), Outcome("NEEDS_USER_INPUT", "tailored resume awaiting your review")
    return str(out), None


def shot(page, job, tag):
    p = SHOTS / f"{job['id'][:8]}_{tag}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(p), full_page=True)
    except Exception:
        return ""
    return str(p)


def click_first(page, selectors, timeout=2500):
    for s in selectors:
        try:
            loc = page.locator(s).first
            if loc.count() and loc.is_visible():
                return loc
        except Exception:
            continue
    return None


def settle(page):
    try:
        page.wait_for_load_state("domcontentloaded", timeout=8000)
        page.wait_for_load_state("networkidle", timeout=6000)
    except Exception:
        pass


def open_job(page, job, adapter):
    url = adapter.prepare_url(job["url"])
    resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    settle(page)
    if resp and resp.status in (404, 410):
        return Outcome("CLOSED", f"http {resp.status}")
    if resp and resp.status >= 500:
        raise RuntimeError(f"http {resp.status}")
    adapter.prepare(page)
    settle(page)
    t = detect.body_text(page)
    if detect.HUMAN.search(t[:600]):
        return Outcome("BLOCKED", "human verification / bot check")
    nform = page.evaluate("() => [...document.querySelectorAll('input:not([type=hidden]),textarea,select')].filter(e=>e.getBoundingClientRect().width>0).length")
    if detect.ALREADY.search(t) and nform < 3:
        return Outcome("ALREADY_APPLIED", "site says already applied", evidence=detect.ALREADY.search(t).group(0))
    if detect.CLOSED.search(t[:1500]) and not page.query_selector("form input"):
        return Outcome("CLOSED", detect.CLOSED.search(t).group(0))
    return None


def reach_form(page, adapter):
    """If no form fields yet, click an Apply control."""
    def nfields():
        return page.evaluate("() => [...document.querySelectorAll('input:not([type=hidden]):not([type=search]),textarea,select')].filter(e=>e.getBoundingClientRect().width>0).length")
    if nfields() >= 3:
        return
    from .adapters import find_button
    btn = find_button(page, [], re.compile(r"^\s*(apply( now| for this job| for this role)?|start application)\s*$", re.I))
    if not btn:
        return
    href = btn.get_attribute("href") if btn.evaluate("e => e.tagName") == "A" else None
    if href and not href.startswith("#"):
        page.goto(page.evaluate("h => new URL(h, location.href).href", href), wait_until="domcontentloaded")
    else:
        btn.click()
    settle(page)


def process(page, db, cfg, job, answer_chat=None) -> Outcome:
    profile = load_profile()
    live = bool(cfg.get("live_submit")) and verified(profile)
    adapter = adapter_for(job["platform"])
    early = open_job(page, job, adapter)
    if early:
        early.evidence = early.evidence or shot(page, job, "early")
        return early
    reach_form(page, adapter)
    if detect.login_wall(page):
        db.add_need(job["id"], norm_key(f"account:{adapter.name}:{job['url_key'].split('/')[0]}"),
                    f"{job['company']} needs sign-in / account on {adapter.name}. Sign in once via `python -m jobagent login`, then retry.", "account", [])
        return Outcome("NEEDS_USER_INPUT", "account/sign-in required", evidence=shot(page, job, "login"))
    page_text = detect.body_text(page)
    resume_path = pick_resume(profile, job.get("category"))
    resume_path, review = maybe_tailor(db, cfg, profile, job, page_text, resume_path)
    if review:
        review.evidence = shot(page, job, "review")
        return review
    ans_fn = lambda q, limit=None: write_answer(cfg, q, job["company"], job["title"], page_text, limit, chat=answer_chat) \
        if (answer_chat or llm_configured()) else None
    ctx = Ctx(db, cfg, profile, resume_path, ans_fn, job["id"])
    CFG_FOR_CHOICE[0] = cfg
    if answer_chat or llm_configured():
        ctx.choice_fn = lambda q, opts: pick_grounded(cfg, q, opts, profile, chat=answer_chat)
    asked = []

    for step in range(MAX_STEPS):
        if detect.has_captcha(page):
            db.add_need(job["id"], norm_key(f"captcha:{job['id']}"), f"CAPTCHA on {job['company']} - {job['title']}. Complete it in the browser.", "captcha", [])
            return Outcome("BLOCKED", "captcha", evidence=shot(page, job, "captcha"), questions=asked)
        flds = page.evaluate(fields.SCAN_JS)
        needs, filled = [], 0
        for f in sorted(flds, key=lambda x: x["kind"] == "file"):   # uploads last: some sites re-render on upload
            if f["kind"] == "combobox" and not f.get("options"):
                f["options"] = fields.read_combo_options(page, f)
            label = (f.get("label") or "").strip()
            try:
                d = fields.decide(f, ctx)
                if d.ineligible:
                    return Outcome("INELIGIBLE", d.ineligible, evidence=shot(page, job, "inelig"), questions=asked)
                if d.need:
                    needs.append((f, d.need))
                    continue
                if d.skip or d.value is None:
                    continue
                if f["kind"] == "combobox":
                    f["fallback"] = None
                if fields.apply(page, f, d):
                    filled += 1
                    asked.append({"q": label[:120], "src": d.source})
            except Exception as e:  # one bad field must not kill the application
                if f.get("required"):
                    needs.append((f, (label or "field", f"fill error: {str(e)[:80]}")))
        if needs:
            for f, need in needs:
                q, kind = need[0], need[1]
                if kind == "draft":   # job-specific: never reused for other companies
                    db.add_need(job["id"], norm_key(q) + "@" + job["id"], q, "draft", [need[2]])
                else:
                    db.add_need(job["id"], norm_key(q), q, kind, f.get("options"))
            return Outcome("NEEDS_USER_INPUT", f"{len(needs)} unanswered", evidence=shot(page, job, "needs"), questions=asked)

        sub = adapter.find_submit(page)
        nxt = adapter.find_next(page) if not sub else None
        if sub and not nxt:
            problems = pre_submit_check(page, profile, flds)
            if problems:
                return Outcome("FAILED", "pre-submit check: " + "; ".join(problems)[:300], evidence=shot(page, job, "precheck"), questions=asked)
            if not live:
                return Outcome("NOT_STARTED", "DRYRUN_OK", evidence=shot(page, job, "dryrun"), resume=resume_path or "", questions=asked)
            before = page.url
            sub.click()
            settle(page)
            return confirm_submission(page, job, adapter, before, resume_path or "", asked)
        if nxt:
            nxt.click()
            settle(page)
            errs = detect.errors(page)
            if errs and not page.evaluate(fields.SCAN_JS) == flds:
                pass
            if errs and step > 0 and errs == getattr(process, "_last", None):
                return Outcome("FAILED", "validation: " + "; ".join(errs)[:300], evidence=shot(page, job, "valid"), questions=asked)
            process._last = errs
            continue
        return Outcome("FAILED", "no submit/next control found", evidence=shot(page, job, "nobutton"), questions=asked)
    return Outcome("FAILED", "too many steps", evidence=shot(page, job, "steps"), questions=asked)


def pre_submit_check(page, profile, flds) -> list[str]:
    """Re-read what is actually in the form and compare with the verified profile."""
    problems = []
    vals = {f["id"]: f for f in page.evaluate(fields.SCAN_JS) if "id" in f}
    ident = profile.get("identity", {})
    checks = [(r"e-?mail", ident.get("email")), (r"first name", ident.get("first_name")), (r"last name", ident.get("last_name"))]
    for f in vals.values():
        if re.search(r"first and last|full name|legal name|\band\b", f.get("label", ""), re.I):
            continue   # combined name fields are checked by their own rule
        for pat, want in checks:
            if want and re.search(pat, f.get("label", ""), re.I) and f["kind"] == "text" and f.get("value") \
                    and f["value"].strip().lower() != str(want).lower():
                problems.append(f"{pat} mismatch")
        if f.get("required") and f["kind"] in ("text", "textarea", "select") and not (f.get("value") or "").strip():
            if f["kind"] == "select" and False:
                continue
            problems.append(f"required empty: {f.get('label','')[:40]}")
    for g in [f for f in flds if f["kind"] == "radio" and f.get("required")]:
        cur = [x for x in page.evaluate(fields.SCAN_JS) if x["kind"] == "radio" and x.get("group") == g["group"]]
        if cur and not any(cur[0]["checked"]):
            problems.append(f"required radio unset: {g['label'][:40]}")
    return problems


def confirm_submission(page, job, adapter, before_url, resume, asked) -> Outcome:
    for _ in range(6):
        time.sleep(1.5)
        c = detect.confirmation(page, adapter)
        if c:
            return Outcome(submitted=True, "APPLIED", "confirmed", evidence=f"{c} | {shot(page, job, 'confirm')}", resume=resume, questions=asked)
        t = detect.body_text(page)
        if detect.ALREADY.search(t):
            return Outcome(submitted=True, "ALREADY_APPLIED", "site says already applied", evidence=shot(page, job, "already"), resume=resume, questions=asked)
    errs = detect.errors(page)
    if detect.has_captcha(page):
        return Outcome(submitted=True, "BLOCKED", "captcha at submit", evidence=shot(page, job, "captcha"), resume=resume, questions=asked)
    if errs:
        return Outcome(submitted=True, "FAILED", "validation after submit: " + "; ".join(errs)[:300], evidence=shot(page, job, "err"), resume=resume, questions=asked)
    if page.query_selector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha'):
        # the form is still on screen and an anti-bot widget is present: the site most likely rejected the automated submit silently.
        return Outcome(submitted=True, "BLOCKED", "no confirmation and anti-bot widget present: likely rejected as automated. Open the link and submit by hand",
                       evidence=shot(page, job, "silent_reject"), resume=resume, questions=asked)
    changed = page.url != before_url and adapter.confirm_url.search(page.url)
    return Outcome(submitted=True, "UNCERTAIN", "submit clicked, no confirmation text" + (" (url changed)" if changed else ""),
                   evidence=shot(page, job, "uncertain"), resume=resume, questions=asked)
