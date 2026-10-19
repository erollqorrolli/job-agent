"""Process one job: open, fill (multi-step), verify, submit (only if allowed), confirm."""
import json, re, shutil, time
from dataclasses import dataclass, field
from pathlib import Path
from . import detect, fields
from .adapters import adapter_for
from .answers import (write_answer, llm_configured, pick_grounded, semantic_question_match,
                      CFG_FOR_CHOICE)
from .config import SHOTS
from .profile import load_profile, get, verified, norm_key
from .config import DATA
from .premium_resume import (call_paid_grounded_choice, paid_editor_configured, source_signature,
                             tailor_with_paid_editor, write_premium_answer)
from .resume_sources import resume_inventory
from .resume_validation import (ResumeValidationError, browser_file_metadata,
                                validate_resume_artifact, verify_browser_file)

MAX_STEPS = 10


@dataclass
class Outcome:
    status: str
    reason: str = ""
    evidence: str = ""
    resume: str = ""
    questions: list = field(default_factory=list)
    submitted: bool = False      # the final submit button was actually clicked (or handed to the user)
    keep_page: bool = False      # hand-off: leave the filled tab open for the user's click
    resume_sha256: str = ""
    resume_verification: dict = field(default_factory=dict)


class Ctx:
    def __init__(self, db, cfg, profile, resume_path, answer_fn, job_id=None, job_text=""):
        self.job_id = job_id
        self.db, self.cfg, self.profile, self.resume_path, self.answer_fn = db, cfg, profile, resume_path, answer_fn
        self.job_text = job_text
        self.question_keys = {}
        self.resume_artifact = None
        self.resume_uploads = {}
        self.resume_verification = {}


def pick_resume(profile, category, db=None):
    rs = resume_inventory(db, profile) if db is not None else profile.get("resumes") or []
    tag = {"AI/ML/Data": ("ml", "ai", "data"), "Software": ("swe", "software"), "Quant": ("quant",)}.get(category, ())
    for r in rs:
        if r.get("role", "primary") == "primary" and r.get("path") and any(t in (r.get("tags") or []) for t in tag):
            return r["path"]
    for r in rs:
        if r.get("role", "primary") == "primary" and r.get("path"):
            return r["path"]
    return None


def _resume_basename(source: dict, actual_path: str | Path) -> str:
    """Return the original human filename with the actual submission suffix."""
    actual = Path(actual_path)
    original = Path(str(source.get("name") or actual.name)).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(original).stem).strip(" ._") or "resume"
    return stem + actual.suffix.lower()


def stage_resume_for_job(sources: list[dict], job: dict, resume_path: str | None) -> str | None:
    """Give browser uploads a clean filename while internal source paths stay unique."""
    if not resume_path:
        return None
    source_path = Path(resume_path)
    source = next((item for item in sources
                   if Path(item.get("path", "")) == source_path), {})
    destination = DATA / "resumes" / job["id"][:8] / _resume_basename(source, source_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source_path.resolve() != destination.resolve():
        shutil.copy2(source_path, destination)
    return str(destination)


def resume_decision_event(job, info):
    """Concise, explicit premium-model decision for the dashboard event stream."""
    edits = int(info.get("edits") or 0)
    decision = "CHANGE" if edits else "NO CHANGE"
    model_decision = str(info.get("model_decision") or info.get("decision") or "").replace("_", " ").upper()
    reason = " ".join(str(info.get("decision_reason") or "").split())[:180]
    sections = ", ".join(info.get("sections") or []) or "none"
    keywords = ", ".join(str(x) for x in (info.get("keywords") or [])[:8]) or "none"
    company = " ".join(str(job.get("company") or "").split())[:60]
    title = " ".join(str(job.get("title") or "").split())[:80]
    model_note = f" (model: {model_decision})" if model_decision and model_decision != decision else ""
    tokens = (f"; tokens={info.get('input_tokens')} in/{info.get('output_tokens')} out"
              if info.get("input_tokens") is not None else "")
    return (f"resume decision for {company} - {title}: {decision}{model_note}; "
            f"reason={reason or 'not provided'}; accepted edits={edits}; sections={sections}; "
            f"ATS keywords={keywords}; model={info.get('provider', 'model')}/{info.get('model', 'unknown')}{tokens}")


def maybe_tailor(db, cfg, profile, job, job_text, default_path, require_review=True):
    """Tailor only a primary LaTeX source, preserving its structure locally."""
    sources = resume_inventory(db, profile)
    if not cfg.get("tailor_resume", True) or len(job_text) < cfg.get("tailor_min_chars", 200):
        return default_path, None
    primary = next((x for x in sources if x.get("role") == "primary"
                    and Path(x.get("context_path") or x.get("path", "")).suffix.lower() == ".tex"), None)
    template = Path(primary.get("context_path") or primary.get("path", "")) if primary else None
    if not template or not template.is_file():
        marker = "resume tailoring unavailable: primary resume is not an editable LaTeX (.tex) source"
        if not db.q("SELECT 1 FROM events WHERE job_id=? AND msg LIKE ? LIMIT 1",
                    (job["id"], marker + "%")):
            db.log(job["id"], marker + "; original resume will be used unchanged until a .tex primary is uploaded")
        return default_path, None
    if not paid_editor_configured(cfg):
        marker = "resume tailoring unavailable: premium model is not connected"
        if not db.q("SELECT 1 FROM events WHERE job_id=? AND msg LIKE ? LIMIT 1",
                    (job["id"], marker + "%")):
            db.log(job["id"], marker + "; original resume will be used unchanged")
        return default_path, None

    clean_stem = Path(_resume_basename(primary or {}, template)).stem
    out = DATA / "resumes" / job["id"][:8] / f"{clean_stem}.tex"
    sig = source_signature(cfg, job_text, sources, profile)
    cached = db.kv_get(f"paid_resume@{job['id']}") or {}
    cached_path = Path(cached.get("path", ""))
    if cached.get("signature") == sig and cached_path.is_file():
        clean_cached = out.with_suffix(cached_path.suffix)
        if cached_path.resolve() != clean_cached.resolve():
            clean_cached.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cached_path, clean_cached)
            cached_path = clean_cached
            db.kv_set(f"paid_resume@{job['id']}", {**cached, "path": str(cached_path)})
        out, info = cached_path, cached.get("info") or {"mode": "paid-latex", "model": "cached"}
    else:
        try:
            out, info = tailor_with_paid_editor(cfg, profile, job, job_text, sources, out)
            db.kv_set(f"paid_resume@{job['id']}", {"signature": sig, "path": str(out), "info": info})
            db.log(job["id"], resume_decision_event(job, info))
        except Exception as e:
            db.log(job["id"], f"resume decision for {job.get('company', '')} - {job.get('title', '')}: ERROR; "
                                 f"primary resume left unchanged because the premium editor failed: {str(e)[:260]}")
            return default_path, None
    if int(info.get("edits") or 0) == 0:
        return default_path, None
    if not require_review:
        return str(out), None
    key = f"resume@{job['id']}"
    if db.get_answer(key) is None:
        limit = max(1, int(cfg.get("tailor_review_queue_limit", cfg.get("tailor_review_first", 5))))
        pending = db.q("SELECT COUNT(*) n FROM human_queue WHERE kind='resume' AND resolved_at IS NULL AND job_id!=?", (job["id"],))[0]["n"]
        if pending >= limit:     # hold excess jobs rather than flooding the user's mandatory review queue
            return str(out), Outcome("NOT_STARTED", "DEFER_REVIEW")
        usage = ""
        if info.get("input_tokens") is not None:
            usage = f"; tokens: {info.get('input_tokens')} in / {info.get('output_tokens')} out"
        summary = (f"premium LaTeX editor: {info.get('provider', 'model')} / {info.get('model')}; "
                   f"format-safe non-Education edits: {info.get('edits', 0)}; "
                   f"sections: {', '.join(info.get('sections') or []) or 'not reported'}; "
                   f"reason: {info.get('decision_reason') or 'not provided'}; format preserved; "
                   f"ATS keywords: {', '.join(info.get('keywords') or []) or 'none'}{usage}")
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


def _verify_resume_at_checkpoint(page, ctx: Ctx) -> dict:
    """Recheck any still-present inputs; retain an exact upload-time match across form steps."""
    if not ctx.resume_artifact:
        raise ResumeValidationError("resume artifact was not validated")
    if not ctx.resume_uploads:
        result = {
            "status": "not-requested",
            "name": ctx.resume_artifact.name,
            "size": ctx.resume_artifact.size,
            "sha256": ctx.resume_artifact.sha256,
            "pages": ctx.resume_artifact.pages,
            "extracted_chars": ctx.resume_artifact.extracted_chars,
            "warnings": [*ctx.resume_artifact.warnings,
                         "this application did not expose a resume file input"],
            "method": "validated-artifact-only",
            "uploads": 0,
            "rechecked_at_final_step": 0,
        }
        ctx.resume_verification = result
        return result
    visible_rechecks = 0
    for field_id, upload_check in list(ctx.resume_uploads.items()):
        metadata = browser_file_metadata(page, field_id)
        if metadata is None:
            continue  # a prior multi-step page is gone; the exact upload-time match remains evidence
        ctx.resume_uploads[field_id] = verify_browser_file(metadata, ctx.resume_artifact)
        visible_rechecks += 1
    result = {
        "status": "verified",
        "name": ctx.resume_artifact.name,
        "size": ctx.resume_artifact.size,
        "sha256": ctx.resume_artifact.sha256,
        "pages": ctx.resume_artifact.pages,
        "extracted_chars": ctx.resume_artifact.extracted_chars,
        "warnings": list(ctx.resume_artifact.warnings),
        "method": "browser-file-byte-match",
        "uploads": len(ctx.resume_uploads),
        "rechecked_at_final_step": visible_rechecks,
    }
    ctx.resume_verification = result
    return result


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
    sources = resume_inventory(db, profile)
    default_resume = stage_resume_for_job(
        sources, job, pick_resume(profile, job.get("category"), db))
    if not verified(profile, has_resume=bool(default_resume)):
        return Outcome("NEEDS_USER_INPUT", "complete and verify the applicant profile before processing")
    live = bool(cfg.get("live_submit"))
    keep_for_review = cfg.get("handoff", "fallback") == "always" and not cfg.get("headless", False)
    adapter = adapter_for(job["platform"])
    early = open_job(page, job, adapter)
    if early:
        early.evidence = early.evidence or shot(page, job, "early")
        return early
    reach_form(page, adapter)
    if detect.login_wall(page):
        db.add_need(job["id"], norm_key(f"account:{adapter.name}:{job['url_key'].split('/')[0]}"),
                    f"{job['company']} needs sign-in / account on {adapter.name}. Sign in once via `python -m jobagent login`, then retry.", "account", [])
        return Outcome("NEEDS_USER_INPUT", "account/sign-in required", evidence=shot(page, job, "login"),
                       keep_page=keep_for_review)
    page_text = detect.body_text(page)
    resume_path = default_resume
    resume_path, review = maybe_tailor(
        db, cfg, profile, job, page_text, resume_path,
        require_review=not keep_for_review)
    if review:
        review.evidence = shot(page, job, "review")
        if not keep_for_review or review.reason == "DEFER_REVIEW":
            return review
        # Manual handoff already guarantees that the user reviews the completed
        # application before submitting it. Continue with the generated resume
        # in the same pass so the employer tab is actually filled; its dashboard
        # review remains available, and the final manual click is approval.
        db.log(job["id"], "tailored resume ready for review; continuing to fill because manual website handoff is enabled")
    try:
        artifact = validate_resume_artifact(resume_path, profile)
    except ResumeValidationError as exc:
        reason = f"resume validation failed: {str(exc)[:220]}"
        db.add_need(job["id"], f"resume-validity@{job['id']}",
                    f"Fix the resume for {job['company']} - {job['title']}: {reason}", "file", [resume_path or ""])
        return Outcome("NEEDS_USER_INPUT", reason, evidence=shot(page, job, "resume_invalid"),
                       resume=resume_path or "")
    if answer_chat:
        ans_fn = lambda q, limit=None: write_answer(cfg, q, job["company"], job["title"], page_text, limit, chat=answer_chat)
    elif paid_editor_configured(cfg):
        ans_fn = lambda q, limit=None: write_premium_answer(
            cfg, profile, job, page_text, q, sources, limit,
            event_fn=lambda msg: db.log(job["id"], msg))
    elif llm_configured(cfg):
        ans_fn = lambda q, limit=None: write_answer(cfg, q, job["company"], job["title"], page_text, limit)
    else:
        ans_fn = lambda q, limit=None: None
    ctx = Ctx(db, cfg, profile, resume_path, ans_fn, job["id"], page_text)
    ctx.resume_artifact = artifact
    ctx.resume_verification = {
        "status": "artifact-valid", "name": artifact.name, "size": artifact.size,
        "sha256": artifact.sha256, "pages": artifact.pages,
        "extracted_chars": artifact.extracted_chars, "warnings": list(artifact.warnings),
    }
    CFG_FOR_CHOICE[0] = cfg
    if answer_chat or llm_configured(cfg):
        ctx.choice_fn = lambda q, opts: pick_grounded(cfg, q, opts, profile, chat=answer_chat)
        ctx.reuse_fn = lambda q, kind, opts: semantic_question_match(cfg, db, q, kind, opts, chat=answer_chat)
    elif paid_editor_configured(cfg):
        def premium_choice(q, opts):
            from .answers import facts_sheet
            try:
                result, usage = call_paid_grounded_choice(cfg, facts_sheet(profile), q, opts)
            except Exception as exc:
                db.log(job["id"], f"grounded field reasoner failed: {str(exc)[:220]}")
                return None
            # Reuse the same strict evidence and exact-option validator as the local model.
            chat = lambda _system, _user: json.dumps(result)
            picked = pick_grounded(cfg, q, opts, profile, chat=chat)
            if picked is not None:
                tokens = (f"; tokens={usage.get('input_tokens')} in/{usage.get('output_tokens')} out"
                          if usage.get("input_tokens") is not None else "")
                db.log(job["id"], f"premium grounded field inference accepted for: {' '.join(q.split())[:140]}{tokens}")
            return picked
        ctx.choice_fn = premium_choice
    asked = []

    for step in range(MAX_STEPS):
        if detect.has_captcha(page):
            db.add_need(job["id"], norm_key(f"captcha:{job['id']}"), f"CAPTCHA on {job['company']} - {job['title']}. Complete it in the browser.", "captcha", [])
            return Outcome("BLOCKED", "captcha", evidence=shot(page, job, "captcha"), questions=asked,
                           keep_page=keep_for_review)
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
                applied = fields.apply(page, f, d)
                if applied and f["kind"] == "file" and d.source == "resume":
                    try:
                        metadata = browser_file_metadata(page, f["id"])
                        ctx.resume_uploads[f["id"]] = verify_browser_file(metadata, artifact)
                        ctx.resume_verification = {
                            **ctx.resume_uploads[f["id"]], "pages": artifact.pages,
                            "extracted_chars": artifact.extracted_chars,
                            "warnings": list(artifact.warnings),
                        }
                    except ResumeValidationError as exc:
                        needs.append((f, (f"Resume attachment verification failed: {str(exc)[:180]}", "file")))
                        continue
                if applied:
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
                    base_key = norm_key(q)
                    db.add_need(job["id"], ctx.question_keys.get(base_key, base_key), q, kind, f.get("options"))
            return Outcome("NEEDS_USER_INPUT", f"{len(needs)} unanswered", evidence=shot(page, job, "needs"),
                           resume=resume_path or "", questions=asked, keep_page=keep_for_review,
                           resume_sha256=artifact.sha256, resume_verification=ctx.resume_verification)

        sub = adapter.find_submit(page)
        nxt = adapter.find_next(page) if not sub else None
        if sub and not nxt:
            try:
                attachment = _verify_resume_at_checkpoint(page, ctx)
            except ResumeValidationError as exc:
                reason = f"resume attachment unverified: {str(exc)[:220]}"
                db.add_need(job["id"], f"resume-attachment@{job['id']}",
                            f"Verify the resume attachment for {job['company']} - {job['title']}: {reason}",
                            "file", [resume_path or ""])
                return Outcome("NEEDS_USER_INPUT", reason, evidence=shot(page, job, "resume_unverified"),
                               resume=resume_path or "", questions=asked, keep_page=keep_for_review,
                               resume_sha256=artifact.sha256, resume_verification=ctx.resume_verification)
            problems = pre_submit_check(page, profile, flds)
            if problems:
                return Outcome("FAILED", "pre-submit check: " + "; ".join(problems)[:300], evidence=shot(page, job, "precheck"),
                               resume=resume_path or "", questions=asked, resume_sha256=artifact.sha256,
                               resume_verification=attachment)
            if not live:
                if keep_for_review:
                    try:
                        page.bring_to_front()
                        sub.scroll_into_view_if_needed(timeout=2000)
                    except Exception:
                        pass
                    return Outcome("READY_TO_SUBMIT", "dry-run form filled: review the open tab; only you can click Submit",
                                   evidence=shot(page, job, "ready"), resume=resume_path or "", questions=asked,
                                   keep_page=True, resume_sha256=artifact.sha256,
                                   resume_verification=attachment)
                return Outcome("NOT_STARTED", "DRYRUN_OK", evidence=shot(page, job, "dryrun"), resume=resume_path or "",
                               questions=asked, resume_sha256=artifact.sha256, resume_verification=attachment)
            if cfg.get("handoff", "fallback") == "always":   # the human makes the final click; invisible reCAPTCHA rejects automated ones
                try:
                    page.bring_to_front()
                    sub.scroll_into_view_if_needed(timeout=2000)
                except Exception:
                    pass
                return Outcome("READY_TO_SUBMIT", "form filled: click Submit in the open tab", evidence=shot(page, job, "ready"),
                               resume=resume_path or "", questions=asked, submitted=True, keep_page=True,
                               resume_sha256=artifact.sha256, resume_verification=attachment)
            before = page.url
            sub.click()
            settle(page)
            out = confirm_submission(page, job, adapter, before, resume_path or "", asked)
            out.resume_sha256 = artifact.sha256
            out.resume_verification = attachment
            if out.status == "BLOCKED" and "anti-bot" in out.reason and cfg.get("handoff", "fallback") != "never":
                # the agent tried on its own and the site rejected it; the filled form is still on screen, so hand it to the user
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                out.status, out.reason, out.keep_page = "READY_TO_SUBMIT", "auto-submit was rejected by the site: click Submit in the open tab", True
            return out
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
    out = _confirm(page, job, adapter, before_url, resume, asked)
    out.submitted = True            # the submit button was clicked, whatever the site did with it
    return out


def _confirm(page, job, adapter, before_url, resume, asked) -> Outcome:
    for _ in range(6):
        time.sleep(1.5)
        c = detect.confirmation(page, adapter)
        if c:
            return Outcome("APPLIED", "confirmed", evidence=f"{c} | {shot(page, job, 'confirm')}", resume=resume, questions=asked)
        t = detect.body_text(page)
        if detect.ALREADY.search(t):
            return Outcome("ALREADY_APPLIED", "site says already applied", evidence=shot(page, job, "already"), resume=resume, questions=asked)
    errs = detect.errors(page)
    if detect.has_captcha(page):
        return Outcome("BLOCKED", "captcha at submit", evidence=shot(page, job, "captcha"), resume=resume, questions=asked)
    if errs:
        return Outcome("FAILED", "validation after submit: " + "; ".join(errs)[:300], evidence=shot(page, job, "err"), resume=resume, questions=asked)
    if page.query_selector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], .g-recaptcha, .h-captcha'):
        # the form is still on screen and an anti-bot widget is present: the site most likely rejected the automated submit silently.
        return Outcome("BLOCKED", "no confirmation and anti-bot widget present: likely rejected as automated. Open the link and submit by hand",
                       evidence=shot(page, job, "silent_reject"), resume=resume, questions=asked)
    changed = page.url != before_url and adapter.confirm_url.search(page.url)
    return Outcome("UNCERTAIN", "submit clicked, no confirmation text" + (" (url changed)" if changed else ""),
                   evidence=shot(page, job, "uncertain"), resume=resume, questions=asked)
