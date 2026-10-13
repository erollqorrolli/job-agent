import json, os, signal, subprocess, sys, time
from flask import Flask, jsonify, request, Response
from .db import DB
from .config import ROOT, load_config
from .cli import export_csv
from .ingest import ingest
from .worker import effective_cfg
from .profile import load_profile, missing_required, verified

app = Flask(__name__)
db = DB()
SETTINGS = ["max_per_session", "priorities", "countries", "excluded_keywords", "live_submit", "poll_minutes", "headless", "max_retries"]


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
    db.x("""DELETE FROM human_queue WHERE resolved_at IS NULL AND job_id IN
            (SELECT id FROM jobs WHERE status IN ('APPLIED','ALREADY_APPLIED','INELIGIBLE','CLOSED'))""")   # questions for jobs that no longer matter
    c = db.counts()
    total = sum(c.values())
    hour = db.q("SELECT COUNT(*) n FROM jobs WHERE status='APPLIED' AND submitted_at>?", (time.time() - 3600,))[0]["n"]
    elig = total - c["INELIGIBLE"] - c["CLOSED"]
    cfg = effective_cfg(db)
    p = load_profile()
    return jsonify(
        counts=c, total=total, eligible=elig, remaining=c["NOT_STARTED"] + c["IN_PROGRESS"], per_hour=hour,
        dryrun_ok=db.q("SELECT COUNT(*) n FROM jobs WHERE last_error='DRYRUN_OK'")[0]["n"],
        current=db.kv_get("current"), control=db.kv_get("control", "stop"), running=proc_alive(),
        settings={k: cfg.get(k) for k in SETTINGS}, profile_ok=verified(p), profile_missing=missing_required(p),
        latest=db.q("SELECT company,title,status,status_reason,resume_used,datetime(COALESCE(submitted_at,attempted_at),'unixepoch','localtime') t FROM jobs WHERE attempted_at IS NOT NULL ORDER BY attempted_at DESC LIMIT 15"),
        needs=db.q("SELECT qkey,question,kind,options,COUNT(*) n FROM human_queue WHERE resolved_at IS NULL GROUP BY qkey ORDER BY n DESC LIMIT 50"),
        events=db.q("SELECT datetime(ts,'unixepoch','localtime') t, msg FROM events ORDER BY id DESC LIMIT 8"))


@app.get("/api/jobs")
def jobs():
    st = request.args.get("status")
    rows = db.q("SELECT id,company,title,location,platform,priority,status,status_reason,url FROM jobs"
                + (" WHERE status=?" if st else "") + " ORDER BY priority, company LIMIT 500", (st,) if st else ())
    return jsonify(rows)


@app.post("/api/action/<name>")
def action(name):
    if name == "start":
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
        if on and not verified(load_profile()):
            return jsonify(error="profile not verified/complete", missing=missing_required(load_profile())), 400
        db.kv_set("settings", {**(db.kv_get("settings", {}) or {}), "live_submit": on})
        if on:
            db.x("UPDATE jobs SET next_try_at=0, last_error=NULL WHERE last_error='DRYRUN_OK'")
    return jsonify(ok=True)


@app.post("/api/settings")
def settings():
    cur = db.kv_get("settings", {}) or {}
    for k, v in (request.json or {}).items():
        if k in SETTINGS and k != "live_submit":
            cur[k] = v
    db.kv_set("settings", cur)
    return jsonify(ok=True)


@app.post("/api/answer")
def answer():
    d = request.json
    db.resolve_need(d["qkey"], d["answer"], save=d.get("save", True))
    return jsonify(ok=True)


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


@app.post("/api/mark")
def mark():
    d = request.json or {}
    if d.get("status") not in ("APPLIED", "ALREADY_APPLIED", "INELIGIBLE", "NOT_STARTED"):
        return jsonify(error="bad status"), 400
    db.x("UPDATE jobs SET status=?, status_reason='set by you', next_try_at=? WHERE id=?",
         (d["status"], 0 if d["status"] == "NOT_STARTED" else 9999999999, d["id"]))
    return jsonify(ok=True)


@app.get("/api/resume")
def resume_info():
    import datetime, hashlib
    from pathlib import Path
    out = []
    for r in load_profile().get("resumes") or []:
        item = {"tags": r.get("tags"), "files": []}
        for kind in ("docx", "path"):
            f = r.get(kind)
            if not f:
                continue
            p = Path(f)
            if p.exists():
                st = p.stat()
                item["files"].append({"role": "source (tailored for each job)" if kind == "docx" else "fallback PDF", "path": str(p),
                                      "modified": datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"), "mtime": st.st_mtime,
                                      "size_kb": round(st.st_size / 1024), "sha": hashlib.sha1(p.read_bytes()).hexdigest()[:8]})
            else:
                item["files"].append({"role": kind, "path": str(p), "missing": True})
        files = {f["role"].split(" ")[0]: f for f in item["files"] if not f.get("missing")}
        item["warning"] = None
        if "source" in files and "fallback" in files and files["fallback"]["mtime"] < files["source"]["mtime"]:
            item["warning"] = "The fallback PDF is older than the docx. The agent re-exports it automatically before using it."
        if any(f.get("missing") for f in item["files"]):
            item["warning"] = "A resume file is missing."
        out.append(item)
    recent = db.q("SELECT company, resume_used, datetime(COALESCE(submitted_at,attempted_at),'unixepoch','localtime') t FROM jobs WHERE resume_used IS NOT NULL AND resume_used != '' ORDER BY attempted_at DESC LIMIT 5")
    return jsonify(resumes=out, recent=recent, tailoring=effective_cfg(db).get("tailor_resume", True))


@app.post("/api/ollama")
def ask_ollama():
    """On-demand: the user clicked 'Ask Ollama' on one question. Returns a draft (text) or a suggested option index."""
    import json as _json
    from . import answers
    qkey = (request.json or {}).get("qkey")
    rows = db.q("""SELECT h.question, h.kind, h.options, j.company, j.title FROM human_queue h JOIN jobs j ON j.id = h.job_id
                   WHERE h.qkey = ? AND h.resolved_at IS NULL LIMIT 1""", (qkey,))
    if not rows:
        return jsonify(error="question not found")
    r = rows[0]
    if not answers.llm_configured():
        return jsonify(error="Ollama isn't configured for the dashboard. Start it from a terminal that has LLM_BASE_URL set.")
    cfg = effective_cfg(db)
    answers.CFG_FOR_CHOICE[0] = cfg
    profile = load_profile()
    opts = _json.loads(r["options"] or "[]")
    try:
        if opts and r["kind"] not in ("draft", "resume"):
            return jsonify(suggest=answers.pick_grounded(cfg, r["question"], opts, profile))
        text, problems = answers.draft_answer(cfg, r["question"], r["company"], r["title"])
        if not text:
            return jsonify(error="Ollama couldn't write an answer from your profile (" + "; ".join(problems) + "). Write it yourself.")
        return jsonify(draft=text, warnings=problems)
    except Exception as e:
        return jsonify(error=f"Ollama isn't responding ({type(e).__name__}). Is it running?")
