import json, os, re, secrets, shutil, signal, subprocess, sys, time, uuid
from pathlib import Path
from urllib.parse import quote, urlsplit
from flask import Flask, jsonify, redirect, request, Response
from werkzeug.utils import secure_filename
from .db import DB
from .config import ROOT, DATA, load_config
from .cli import export_csv
from .ingest import ingest
from .worker import effective_cfg
from .profile import (load_profile, missing_required, profile_form, save_profile_form,
                      verified)
from .premium_resume import ALLOWED_SUFFIXES, compile_latex, editor_status
from .provider_auth import (CHATGPT_USAGE_URL, begin_chatgpt_sign_in, clear_gemini_key,
                            complete_chatgpt_sign_in, disconnect_chatgpt,
                            refresh_chatgpt_models, save_gemini_key, validate_gemini_key)
from .resume_sources import (add_uploaded_sources, public_inventory, resume_inventory,
                             remove_uploaded_source, set_primary)
from .answers import llm_configured, semantic_equivalent_pending_keys

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 60 * 1024 * 1024
db = DB()
SETTINGS = ["max_per_session", "max_ready_tabs", "priorities", "countries", "excluded_keywords",
            "live_submit", "handoff", "poll_minutes", "headless", "max_retries"]
HANDOFF_MODES = {"always", "fallback", "never"}
MANUAL_JOB_DECISIONS = {
    "already_applied": ("ALREADY_APPLIED", "marked manually: already applied"),
    "skip": ("SKIPPED", "marked manually: not applying to this internship"),
}


def proc_alive():
    pid = db.kv_get("worker_pid")
    if not pid:
        return False
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)      # reap if it is our finished child (a zombie still answers kill(pid, 0))
        if done:
            return False
    except ChildProcessError:
        pass
    try:
        os.kill(pid, 0); return True
    except OSError:
        return False


@app.get("/api/state")
def state():
    c = db.counts()
    total = sum(c.values())
    hour = db.q("SELECT COUNT(*) n FROM jobs WHERE status='APPLIED' AND submitted_at>?", (time.time() - 3600,))[0]["n"]
    elig = total - c["INELIGIBLE"] - c["CLOSED"] - c["SKIPPED"]
    cfg = effective_cfg(db)
    p = load_profile()
    resumes = resume_inventory(db, p)
    has_resume = any(x.get("role") == "primary" for x in resumes)
    primary = next((x for x in resumes if x.get("role") == "primary"), None)
    primary_format = str((primary or {}).get("format") or Path(
        (primary or {}).get("context_path") or (primary or {}).get("path") or "").suffix.lstrip(".")).lower()
    editor = editor_status(cfg)
    editor["essay_ready"] = editor["ready"]
    editor["tailoring_ready"] = bool(editor["ready"] and primary_format in ("tex", "latex"))
    if not primary:
        editor["tailoring_blocker"] = "Upload a primary LaTeX (.tex) resume to enable resume tailoring."
    elif primary_format not in ("tex", "latex"):
        editor["tailoring_blocker"] = (f"Resume tailoring is inactive because the primary resume is {primary_format.upper() or 'not LaTeX'}. "
                                         "Upload the original .tex file as primary; essays can still be autofilled.")
    elif not editor["ready"]:
        editor["tailoring_blocker"] = "Connect and select a premium model to enable resume tailoring."
    else:
        editor["tailoring_blocker"] = ""
    needs = db.q("""SELECT qkey,question,kind,options,COUNT(*) n FROM human_queue
                    WHERE resolved_at IS NULL GROUP BY qkey ORDER BY n DESC LIMIT 50""")
    for need in needs:
        need["jobs"] = db.q("""SELECT DISTINCT j.id,j.company,j.title,j.status
                               FROM human_queue h JOIN jobs j ON j.id=h.job_id
                               WHERE h.qkey=? AND h.resolved_at IS NULL
                                 AND j.status IN ('NOT_STARTED','NEEDS_USER_INPUT')
                               ORDER BY j.company,j.title""", (need["qkey"],))
    return jsonify(
        counts=c, total=total, eligible=elig, remaining=c["NOT_STARTED"] + c["IN_PROGRESS"], per_hour=hour,
        dryrun_ok=db.q("SELECT COUNT(*) n FROM jobs WHERE last_error='DRYRUN_OK'")[0]["n"],
        current=db.kv_get("current"), control=db.kv_get("control", "stop"), running=proc_alive(),
        settings={k: cfg.get(k) for k in SETTINGS},
        profile_ok=verified(p, has_resume=has_resume),
        profile_missing=missing_required(p, has_resume=has_resume), profile=profile_form(p),
        resumes=public_inventory(db, p), resume_editor=editor,
        latest=db.q("SELECT company,title,status,status_reason,datetime(COALESCE(submitted_at,attempted_at),'unixepoch','localtime') t FROM jobs WHERE attempted_at IS NOT NULL ORDER BY attempted_at DESC LIMIT 15"),
        needs=needs,
        events=db.q("SELECT datetime(ts,'unixepoch','localtime') t, msg FROM events ORDER BY id DESC LIMIT 8"))


@app.get("/api/jobs")
def jobs():
    st = request.args.get("status")
    rows = db.q("SELECT id,company,title,location,platform,priority,status,status_reason,url FROM jobs"
                + (" WHERE status=?" if st else "") + " ORDER BY priority, company LIMIT 500", (st,) if st else ())
    return jsonify(rows)


def _revert_jobs(job_ids):
    if proc_alive():
        db.kv_set("control", "stop")
        return jsonify(error="worker is stopping; wait a few seconds and try revert again"), 409
    return jsonify(ok=True, **db.revert_jobs(job_ids))


@app.post("/api/jobs/<job_id>/revert")
def job_revert(job_id):
    if not db.q("SELECT 1 FROM jobs WHERE id=?", (job_id,)):
        return jsonify(error="job not found"), 404
    return _revert_jobs([job_id])


@app.post("/api/jobs/<job_id>/decision")
def job_decision(job_id):
    decision = str((request.json or {}).get("decision") or "")
    if decision not in MANUAL_JOB_DECISIONS:
        return jsonify(error="invalid manual job decision"), 400
    if not db.q("SELECT 1 FROM jobs WHERE id=?", (job_id,)):
        return jsonify(error="job not found"), 404
    status, reason = MANUAL_JOB_DECISIONS[decision]
    changed = db.x("""UPDATE jobs SET status=?, status_reason=?, next_try_at=0, questions_json='[]'
                      WHERE id=? AND status IN ('NOT_STARTED','NEEDS_USER_INPUT')""",
                   (status, reason, job_id))
    if changed.rowcount != 1:
        return jsonify(error="only queued or awaiting-input jobs can be marked manually"), 409
    cleared = db.x("DELETE FROM human_queue WHERE job_id=? AND resolved_at IS NULL", (job_id,)).rowcount
    db.log(job_id, reason)
    return jsonify(ok=True, status=status, status_reason=reason, questions_cleared=cleared)


@app.post("/api/jobs/revert-pending")
def jobs_revert_pending():
    rows = db.q("""SELECT DISTINCT j.id FROM jobs j
                   LEFT JOIN human_queue h ON h.job_id=j.id AND h.resolved_at IS NULL
                   WHERE h.id IS NOT NULL OR j.status IN
                   ('IN_PROGRESS','NEEDS_USER_INPUT','BLOCKED','READY_TO_SUBMIT')""")
    return _revert_jobs([row["id"] for row in rows])


@app.post("/api/action/<name>")
def action(name):
    if name == "start":
        profile = load_profile()
        has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, profile))
        missing = missing_required(profile, has_resume=has_resume)
        if not verified(profile, has_resume=has_resume):
            return jsonify(error="complete and verify the applicant profile before starting", missing=missing), 400
        if proc_alive():
            db.kv_set("control", "run")
        else:
            proc = subprocess.Popen([sys.executable, "-m", "jobagent", "worker"], cwd=ROOT,
                                    stdout=open(ROOT / "data" / "worker.log", "a"), stderr=subprocess.STDOUT)
            db.kv_set("worker_pid", proc.pid)
    elif name in ("pause", "resume", "stop"):
        db.kv_set("control", {"pause": "pause", "resume": "run", "stop": "stop"}[name])
    elif name == "retry":
        db.x("UPDATE jobs SET status='NOT_STARTED', retries=0, next_try_at=0 WHERE status IN ('FAILED','UNCERTAIN','BLOCKED')")
    elif name == "check":
        return jsonify(ingest(db, effective_cfg(db)))
    elif name == "export":
        return jsonify(path=str(export_csv(db)))
    elif name == "live":
        on = bool((request.json or {}).get("on"))
        profile = load_profile()
        has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, profile))
        if on and not verified(profile, has_resume=has_resume):
            return jsonify(error="profile not verified/complete",
                           missing=missing_required(profile, has_resume=has_resume)), 400
        db.kv_set("settings", {**(db.kv_get("settings", {}) or {}), "live_submit": on})
        if on:
            db.x("UPDATE jobs SET next_try_at=0, last_error=NULL WHERE last_error='DRYRUN_OK'")
    return jsonify(ok=True)


@app.post("/api/settings")
def settings():
    cur = db.kv_get("settings", {}) or {}
    updates = {}
    for k, v in (request.json or {}).items():
        if k in SETTINGS and k != "live_submit":
            if k == "handoff" and v not in HANDOFF_MODES:
                return jsonify(error="invalid handoff mode"), 400
            if k == "max_ready_tabs" and (isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 100):
                return jsonify(error="max review tabs must be a whole number from 1 to 100"), 400
            updates[k] = v
    candidate = {**effective_cfg(db), **updates}
    if candidate.get("handoff") == "always" and candidate.get("headless") is True:
        return jsonify(error="manual website handoff requires a visible browser"), 400
    db.kv_set("settings", {**cur, **updates})
    return jsonify(ok=True)


@app.get("/api/profile")
def applicant_profile_get():
    profile = load_profile()
    has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, profile))
    return jsonify(profile=profile_form(profile), verified=verified(profile, has_resume=has_resume),
                   missing=missing_required(profile, has_resume=has_resume))


@app.post("/api/profile")
def applicant_profile_save():
    data = request.json or {}
    profile = save_profile_form(data, mark_verified=False)
    has_resume = any(x.get("role") == "primary" for x in resume_inventory(db, profile))
    missing = missing_required(profile, has_resume=has_resume)
    if bool(data.get("verify")) and not missing:
        profile = save_profile_form({}, mark_verified=True)
    ok = verified(profile, has_resume=has_resume)
    status = 200 if ok or not data.get("verify") else 400
    return jsonify(ok=ok, profile=profile_form(profile), missing=missing,
                   error=None if ok or not data.get("verify") else "complete the required fields before starting"), status


def _save_editor_choice(provider: str, model: str | None = None) -> dict:
    current = db.kv_get("settings", {}) or {}
    configured = effective_cfg(db).get("resume_editor", {}) or {}
    models = dict(configured.get("models") or {})
    if model is not None:
        models[provider] = model
    current["resume_editor"] = {**configured, "provider": provider, "models": models}
    db.kv_set("settings", current)
    return effective_cfg(db)


@app.post("/api/resume-editor/config")
def resume_editor_config():
    data = request.json or {}
    provider = str(data.get("provider") or "").lower()
    model = str(data.get("model") or "").strip() if "model" in data else None
    if provider not in ("chatgpt", "gemini", "openai"):
        return jsonify(error="choose ChatGPT, Gemini, or OpenAI API"), 400
    if model is not None and len(model) > 160:
        return jsonify(error="model name is too long"), 400
    cfg = _save_editor_choice(provider, model)
    return jsonify(ok=True, editor=editor_status(cfg))


def _loopback_base() -> str:
    parsed = urlsplit(f"//{request.host}")
    port = parsed.port or 5055
    return f"http://127.0.0.1:{port}"


@app.get("/auth/chatgpt/start")
def chatgpt_start():
    base = _loopback_base()
    if urlsplit(f"//{request.host}").hostname != "127.0.0.1":
        return redirect(f"{base}/auth/chatgpt/start")
    browser_session = secrets.token_urlsafe(32)
    redirect_uri = f"{base}/auth/chatgpt/callback"
    authorize_url = begin_chatgpt_sign_in(redirect_uri, browser_session)
    response = redirect(authorize_url)
    response.set_cookie("jobagent_siwc", browser_session, max_age=600, httponly=True,
                        samesite="Lax", secure=False)
    return response


@app.get("/auth/chatgpt/callback")
def chatgpt_callback():
    base = _loopback_base()
    try:
        complete_chatgpt_sign_in(request.args, request.cookies.get("jobagent_siwc"))
        try:
            models = refresh_chatgpt_models(force=True)
            current = (effective_cfg(db).get("resume_editor", {}).get("models") or {}).get("chatgpt")
            slugs = [m.get("slug") for m in models]
            selected = current if current in slugs else (slugs[0] if slugs else "")
            _save_editor_choice("chatgpt", selected)
            response = redirect(f"{base}/?chatgpt=connected")
        except Exception as model_exc:
            _save_editor_choice("chatgpt")
            response = redirect(f"{base}/?chatgpt=connected&chatgpt_model_error={quote(str(model_exc)[:200])}")
    except Exception as exc:
        response = redirect(f"{base}/?chatgpt_error={quote(str(exc)[:240])}")
    response.delete_cookie("jobagent_siwc")
    return response


@app.post("/api/resume-editor/chatgpt/models")
def chatgpt_models_refresh():
    try:
        models = refresh_chatgpt_models(force=True)
        return jsonify(ok=True, models=models)
    except Exception as exc:
        return jsonify(error=str(exc)[:240]), 400


@app.post("/api/resume-editor/chatgpt/disconnect")
def chatgpt_disconnect():
    confirmed = disconnect_chatgpt()
    return jsonify(ok=True, revocation_confirmed=confirmed,
                   message="Disconnected locally and remotely." if confirmed else
                           "Disconnected locally; remote revocation could not be confirmed.")


@app.post("/api/resume-editor/gemini-key")
def gemini_key_save():
    key = str((request.json or {}).get("api_key") or "").strip()
    try:
        validate_gemini_key(key)
        save_gemini_key(key)
        cfg = _save_editor_choice("gemini", (effective_cfg(db).get("resume_editor", {}).get("models") or {})
                                  .get("gemini", "gemini-3.8-flash"))
        return jsonify(ok=True, editor=editor_status(cfg))
    except Exception as exc:
        return jsonify(error=f"Gemini key could not be verified: {str(exc)[:200]}"), 400


@app.post("/api/resume-editor/gemini-disconnect")
def gemini_key_clear():
    removed = clear_gemini_key()
    return jsonify(ok=True, removed=removed,
                   message="Saved Gemini key removed." if removed else
                           "No saved key was present. An environment key may still be active.")


@app.get("/api/resume-editor/usage-link")
def resume_editor_usage_link():
    return redirect(CHATGPT_USAGE_URL)


@app.post("/api/answer")
def answer():
    d = request.json
    rows = db.q("SELECT job_id,question,kind,options FROM human_queue WHERE qkey=? AND resolved_at IS NULL", (d["qkey"],))
    equivalent_keys = []
    if rows and llm_configured():
        try:
            opts = json.loads(rows[0].get("options") or "[]")
        except (TypeError, ValueError):
            opts = []
        equivalent_keys = semantic_equivalent_pending_keys(
            effective_cfg(db), db, d["qkey"], rows[0]["question"], rows[0]["kind"], opts, str(d["answer"]))
    db.resolve_need(d["qkey"], d["answer"], save=d.get("save", True))
    for key in equivalent_keys:
        db.resolve_need(key, d["answer"], save=d.get("save", True))
    if equivalent_keys:
        db.log("-", f"one answer resolved {len(equivalent_keys) + 1} semantically equivalent question variants")
    return jsonify(ok=True, equivalents_resolved=len(equivalent_keys))


@app.get("/")
def index():
    return Response(open(ROOT / "jobagent" / "dashboard.html").read(), mimetype="text/html")


from .skills_catalog import CATALOG, FROM_RESUME


@app.get("/api/skills")
def skills_get():
    saved = db.kv_get("skills")
    if saved is None:
        saved = {k: "used" for k in FROM_RESUME}
    return jsonify(catalog={c: v.split("|") for c, v in CATALOG.items()}, marks=saved, seeded=db.kv_get("skills") is None)


@app.post("/api/skills")
def skills_set():
    marks = {k: v for k, v in (request.json or {}).items() if v in ("used", "course")}
    db.kv_set("skills", marks)
    return jsonify(ok=True, n=len(marks))


@app.get("/skills")
def skills_page():
    return Response(open(ROOT / "jobagent" / "skills.html").read(), mimetype="text/html")


@app.get("/resume-file")
def resume_file():
    from flask import send_file, abort
    from pathlib import Path
    pth = Path(request.args.get("p", "")).resolve()
    if not str(pth).startswith(str((ROOT / "data" / "resumes").resolve())) or not pth.exists():
        abort(404)
    return send_file(pth, as_attachment=True)


@app.get("/api/resumes")
def resumes_get():
    return jsonify(resumes=public_inventory(db, load_profile()), editor=editor_status(effective_cfg(db)))


@app.post("/api/resumes")
def resumes_upload():
    uploads = request.files.getlist("files")
    if not uploads:
        return jsonify(error="choose at least one resume"), 400
    role = request.form.get("role", "supplementary")
    if role not in ("primary", "supplementary"):
        return jsonify(error="invalid resume role"), 400
    target = DATA / "resume_sources"
    target.mkdir(parents=True, exist_ok=True)
    written = []
    validated = []
    for upload in uploads[:12]:
        original = secure_filename(upload.filename or "")
        suffix = Path(original).suffix.lower()
        if not original or suffix not in ALLOWED_SUFFIXES:
            return jsonify(error=f"unsupported file: {upload.filename}; use PDF, DOCX, TXT, or LaTeX (.tex)"), 400
        validated.append((upload, original, suffix))
    for upload, original, suffix in validated:
        rid = uuid.uuid4().hex
        # The UUID directory prevents collisions without leaking random digits
        # into the filename later presented to an employer.
        source_dir = target / rid
        source_dir.mkdir(parents=True, exist_ok=True)
        path = source_dir / original
        upload.save(path)
        if suffix == ".tex":
            try:
                submit_path = compile_latex(path, source_dir)
            except (ValueError, RuntimeError) as exc:
                return jsonify(error=f"invalid LaTeX resume {original}: {exc}"), 400
            written.append({"id": rid, "name": original, "path": str(submit_path),
                            "context_path": str(path), "format": "latex"})
        else:
            written.append({"id": rid, "name": original, "path": str(path),
                            "context_path": str(path), "format": suffix.lstrip(".")})
    add_uploaded_sources(db, written, role=role)
    return jsonify(ok=True, resumes=public_inventory(db, load_profile()))


@app.post("/api/resumes/<source_id>/primary")
def resumes_primary(source_id):
    if not set_primary(db, source_id):
        return jsonify(error="uploaded resume not found"), 404
    return jsonify(ok=True, resumes=public_inventory(db, load_profile()))


def _remove_uploaded_resume_files(source_id: str) -> int:
    """Delete only files stored under a server-generated resume-source ID."""
    if not re.fullmatch(r"[0-9a-f]{32}", source_id):
        raise ValueError("invalid uploaded resume ID")
    root = (DATA / "resume_sources").resolve()
    targets = []
    source_dir = root / source_id
    if source_dir.exists() or source_dir.is_symlink():
        targets.append(source_dir)
    if root.exists():
        targets.extend(root.glob(f"{source_id}_*"))  # legacy flat-file upload layout
    for target in targets:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
    return len(targets)


@app.delete("/api/resumes/<source_id>")
def resumes_remove(source_id):
    if proc_alive():
        return jsonify(error="stop the worker before removing a resume"), 409
    items = db.kv_get("resume_sources", []) or []
    record = next((item for item in items if item.get("id") == source_id), None)
    if not record:
        return jsonify(error="uploaded resume not found"), 404
    try:
        files_removed = _remove_uploaded_resume_files(source_id)
    except (OSError, ValueError) as exc:
        return jsonify(error=f"could not remove uploaded resume: {exc}"), 400
    remove_uploaded_source(db, source_id)
    return jsonify(ok=True, removed=record.get("name"), files_removed=files_removed,
                   resumes=public_inventory(db, load_profile()))
