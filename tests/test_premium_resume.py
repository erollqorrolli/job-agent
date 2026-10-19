import io
import json
from pathlib import Path

import pytest
from docx import Document

from jobagent.premium_resume import (apply_latex_edits, call_paid_latex_editor,
                                     prepare_latex_packet, prepare_packet,
                                     write_premium_answer)
from jobagent.resume_sources import (add_uploaded_sources, remove_uploaded_source,
                                     resume_inventory, set_primary)


def _docx(path, *lines):
    doc = Document()
    for line in lines:
        doc.add_paragraph(line)
    doc.save(path)
    return path


def _tex(path):
    path.write_text(r"""\documentclass{article}
\newcommand{\resumeItem}[1]{\item #1}
\begin{document}
\section{Skills}
\textbf{Languages}{: Java, Python}
\section{Projects}
\textbf{Course Scheduler}
\begin{itemize}
\resumeItem{Built a scheduler with Python}
\end{itemize}
\section{Education}
\textbf{Test University}
\section{Experience}
\textbf{Original Employer}
\end{document}
""")
    return path


def test_inventory_keeps_supplementary_context_out_of_submission(db, tmp_path):
    original = _docx(tmp_path / "main.docx", "Main resume")
    older = _docx(tmp_path / "older.docx", "Older resume")
    profile = {"resumes": [{"path": str(original), "tags": ["default"]}]}
    add_uploaded_sources(db, [{"id": "old", "name": "older.docx", "path": str(older)}], "supplementary")

    items = resume_inventory(db, profile)
    assert next(x for x in items if x["role"] == "primary")["path"] == str(original)
    assert next(x for x in items if x["id"] == "old")["role"] == "supplementary"

    assert set_primary(db, "old")
    items = resume_inventory(db, profile)
    assert next(x for x in items if x["role"] == "primary")["id"] == "old"
    assert next(x for x in items if x["id"] == "profile-0")["role"] == "supplementary"


def test_dashboard_uploads_multiple_resumes_and_marks_only_first_primary(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "load_profile", lambda: {})
    client = dashboard.app.test_client()
    response = client.post("/api/resumes", data={
        "role": "primary",
        "files": [(io.BytesIO(b"%PDF-main"), "main.pdf"), (io.BytesIO(b"%PDF-old"), "old.pdf")],
    }, content_type="multipart/form-data")
    assert response.status_code == 200
    uploaded = [x for x in response.get_json()["resumes"] if x["source"] == "upload"]
    assert [(x["name"], x["role"]) for x in uploaded] == [("main.pdf", "primary"), ("old.pdf", "supplementary")]
    private = [x for x in resume_inventory(db, {}) if x["source"] == "upload"]
    assert [Path(x["path"]).name for x in private] == ["main.pdf", "old.pdf"]


def test_dashboard_accepts_latex_as_primary_and_keeps_source_for_editing(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    def fake_compile(source, destination):
        pdf = Path(destination) / f"{Path(source).stem}.pdf"
        pdf.write_bytes(b"%PDF-compiled")
        return pdf

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "load_profile", lambda: {})
    monkeypatch.setattr(dashboard, "compile_latex", fake_compile)
    client = dashboard.app.test_client()
    response = client.post("/api/resumes", data={
        "role": "primary",
        "files": [(io.BytesIO(_tex(tmp_path / "source.tex").read_bytes()), "resume.tex")],
    }, content_type="multipart/form-data")

    assert response.status_code == 200
    public = response.get_json()["resumes"][0]
    assert public["name"] == "resume.tex" and public["format"] == "latex" and public["role"] == "primary"
    private = resume_inventory(db, {})[0]
    assert Path(private["path"]).name == "resume.pdf"
    assert Path(private["context_path"]).name == "resume.tex"


def test_dashboard_removes_uploaded_resume_and_promotes_next_source(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "load_profile", lambda: {})
    client = dashboard.app.test_client()
    uploaded = client.post("/api/resumes", data={
        "role": "primary",
        "files": [(io.BytesIO(b"%PDF-main"), "main.pdf"),
                  (io.BytesIO(b"%PDF-backup"), "backup.pdf")],
    }, content_type="multipart/form-data").get_json()["resumes"]
    primary = next(item for item in uploaded if item["role"] == "primary")
    source_dir = tmp_path / "resume_sources" / primary["id"]

    response = client.delete(f"/api/resumes/{primary['id']}")

    assert response.status_code == 200
    assert response.get_json()["removed"] == "main.pdf"
    assert not source_dir.exists()
    remaining = response.get_json()["resumes"]
    assert [(item["name"], item["role"]) for item in remaining] == [("backup.pdf", "primary")]
    assert all(item["id"] != primary["id"] for item in db.kv_get("resume_sources"))


def test_dashboard_removes_legacy_flat_resume_files(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    source_id = "a" * 32
    root = tmp_path / "resume_sources"
    root.mkdir()
    pdf = root / f"{source_id}_resume.pdf"
    tex = root / f"{source_id}_resume.tex"
    log = root / f"{source_id}_resume.log"
    outside = tmp_path / "keep.pdf"
    for path in (pdf, tex, log, outside):
        path.write_bytes(b"content")
    add_uploaded_sources(db, [{"id": source_id, "name": "resume.tex", "path": str(pdf),
                               "context_path": str(tex), "format": "latex"}], "primary")
    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "load_profile", lambda: {})

    response = dashboard.app.test_client().delete(f"/api/resumes/{source_id}")

    assert response.status_code == 200 and response.get_json()["files_removed"] == 3
    assert not any((pdf.exists(), tex.exists(), log.exists()))
    assert outside.exists()


def test_dashboard_refuses_resume_removal_while_worker_is_running(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    source_id = "b" * 32
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF")
    add_uploaded_sources(db, [{"id": source_id, "name": source.name, "path": str(source)}], "primary")
    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "DATA", tmp_path)
    monkeypatch.setattr(dashboard, "proc_alive", lambda: True)

    response = dashboard.app.test_client().delete(f"/api/resumes/{source_id}")

    assert response.status_code == 409
    assert source.exists()
    assert db.kv_get("resume_sources")[0]["id"] == source_id


def test_remove_uploaded_source_does_not_remove_unknown_id(db, tmp_path):
    source = tmp_path / "resume.pdf"
    source.write_bytes(b"%PDF")
    add_uploaded_sources(db, [{"id": "known", "name": source.name, "path": str(source)}], "primary")

    assert remove_uploaded_source(db, "missing") is None
    assert db.kv_get("resume_sources")[0]["id"] == "known"


def test_submission_copy_uses_original_resume_name_not_internal_uuid(db, tmp_path, monkeypatch):
    from jobagent import runner

    stored = tmp_path / "8481a8fb3cd24d86bf944c64b9b32322_DanielRanaudoResume.pdf"
    stored.write_bytes(b"%PDF")
    sources = [{"id": "source", "name": "DanielRanaudoResume.tex", "path": str(stored),
                "context_path": str(stored), "role": "primary"}]
    monkeypatch.setattr(runner, "DATA", tmp_path / "data")

    staged = runner.stage_resume_for_job(sources, {"id": "job-123456"}, str(stored))

    assert Path(staged).name == "DanielRanaudoResume.pdf"
    assert Path(staged).read_bytes() == b"%PDF"


def test_dashboard_can_switch_resume_editor_provider_and_model(db, monkeypatch):
    from jobagent import dashboard
    from jobagent.worker import effective_cfg

    monkeypatch.setattr(dashboard, "db", db)
    client = dashboard.app.test_client()
    response = client.post("/api/resume-editor/config", json={
        "provider": "gemini", "model": "gemini-3.8-flash"})
    assert response.status_code == 200
    cfg = effective_cfg(db)
    assert cfg["resume_editor"]["provider"] == "gemini"
    assert cfg["resume_editor"]["models"]["gemini"] == "gemini-3.8-flash"


def test_local_preprocessor_accepts_only_exact_candidate_evidence(tmp_path):
    source = _docx(tmp_path / "main.docx", "Built a Python API used by 20 students.", "Test University, 2024")
    sources = [{"name": source.name, "path": str(source), "context_path": str(source), "role": "primary"}]
    answer = json.dumps({
        "target_role": "Backend Intern", "company": "Acme",
        "requirements": ["Python and API experience"], "keywords": ["Python", "API"],
        "candidate_evidence": ["Built a Python API used by 20 students.", "Invented AWS experience"],
    })
    packet = prepare_packet({}, {}, {"title": "Backend Intern", "company": "Acme"},
                            "We require Python and API experience.", sources, local_chat=lambda *_: answer)
    assert packet["candidate_evidence"] == ["Built a Python API used by 20 students."]
    assert packet["requirements"] == ["Python and API experience"]


def test_paid_latex_call_requests_exact_patch_schema(monkeypatch):
    monkeypatch.setenv("RESUME_LLM_API_KEY", "paid-test-key")
    result = {"edits": [], "ats_keywords": ["Python"], "omitted_keywords": [],
              "decision": "no_change", "decision_reason": "The existing resume already emphasizes Python."}
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(result)}]}], "usage": {}}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    got, _ = call_paid_latex_editor(
        {"resume_editor": {"provider": "openai", "model": "gpt-test"}},
        {"candidate_sources": [], "latex_editable_sections": {}}, request_fn=post)
    assert got == result
    assert seen["json"]["store"] is False
    assert seen["json"]["text"]["format"]["name"] == "latex_resume_edits"
    assert {"decision", "decision_reason"}.issubset(seen["json"]["text"]["format"]["schema"]["required"])
    assert "Do not return a complete LaTeX document" in seen["json"]["instructions"]
    assert "small, useful tailoring pass" in seen["json"]["instructions"]


def test_chatgpt_plan_call_uses_streamed_nonstored_contract(monkeypatch):
    from jobagent import premium_resume

    result = {"edits": [], "ats_keywords": ["Python"], "omitted_keywords": [],
              "decision": "no_change", "decision_reason": "The existing resume already emphasizes Python."}
    monkeypatch.setattr(premium_resume, "chatgpt_status", lambda: {
        "connected": True, "models": [{"slug": "gpt-plan", "display_name": "GPT Plan"}]})
    monkeypatch.setattr(premium_resume, "chatgpt_access_token", lambda: "oauth-access-token")
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def iter_lines(self):
            text = json.dumps(result)
            yield f'data: {json.dumps({"type": "response.output_text.delta", "delta": text})}'.encode()
            yield b'data: {"type":"response.completed","response":{"usage":{"input_tokens":44,"output_tokens":22}}}'

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    got, usage = call_paid_latex_editor({"resume_editor": {"provider": "chatgpt", "model": "gpt-plan"}},
                                        {"candidate_sources": [{"text": "Test University, 2024"}]}, request_fn=post)
    assert got == result and usage["input_tokens"] == 44
    assert seen["stream"] is True and seen["json"]["stream"] is True
    assert seen["json"]["store"] is False
    assert isinstance(seen["json"]["input"], list)
    assert "max_output_tokens" not in seen["json"]
    assert seen["headers"]["Authorization"] == "Bearer oauth-access-token"


def test_gemini_call_uses_interactions_structured_output(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    result = {"edits": [], "ats_keywords": ["Python"], "omitted_keywords": [],
              "decision": "no_change", "decision_reason": "The existing resume already emphasizes Python."}
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"status": "completed", "steps": [{"type": "model_output", "content": [
                {"type": "text", "text": json.dumps(result)}]}],
                    "usage": {"input_tokens": 30, "output_tokens": 20}}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    got, usage = call_paid_latex_editor({"resume_editor": {"provider": "gemini", "model": "gemini-test"}},
                                        {"candidate_sources": [{"text": "Test University, 2024"}]}, request_fn=post)
    assert got == result and usage["output_tokens"] == 20
    assert seen["url"].endswith("/interactions")
    assert seen["headers"]["x-goog-api-key"] == "gemini-test-key"
    assert seen["json"]["store"] is False
    assert seen["json"]["response_format"]["mime_type"] == "application/json"
    assert "additionalProperties" not in json.dumps(seen["json"]["response_format"]["schema"])


def test_premium_model_writes_grounded_application_essay(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_LLM_API_KEY", "paid-test-key")
    source = _docx(tmp_path / "main.docx", "test@example.com", "Built a Python scheduler used by 20 students.")
    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            result = {"answer": "I built a Python scheduler used by 20 students. That work made me want to keep building practical backend tools.",
                      "insufficient_facts": False}
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(result)}]}], "usage": {}}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    cfg = {"resume_editor": {"provider": "openai", "model": "gpt-test"}}
    answer = write_premium_answer(
        cfg, {"education": {}, "facts": {}}, {"company": "Acme", "title": "Backend Intern"},
        "Build backend services in Python.", "Why are you a fit for this role?",
        [{"name": source.name, "path": str(source), "context_path": str(source)}], 300, request_fn=post)

    assert answer.startswith("I built a Python scheduler")
    assert seen["json"]["store"] is False
    assert seen["json"]["text"]["format"]["name"] == "application_essay"
    assert "test@example.com" not in seen["json"]["input"]


def test_premium_essay_rejects_unsupported_numbers(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_LLM_API_KEY", "paid-test-key")
    source = _docx(tmp_path / "main.docx", "Built a Python scheduler.")

    class Response:
        def raise_for_status(self): pass
        def json(self):
            result = {"answer": "I improved performance by 80%.", "insufficient_facts": False}
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(result)}]}]}

    cfg = {"resume_editor": {"provider": "openai", "model": "gpt-test"}}
    answer = write_premium_answer(
        cfg, {}, {"company": "Acme", "title": "Intern"}, "Python role", "Why you?",
        [{"name": source.name, "path": str(source), "context_path": str(source)}], request_fn=lambda *a, **k: Response())
    assert answer is None


def test_premium_essay_retries_rejected_draft_with_feedback(monkeypatch, tmp_path):
    monkeypatch.setenv("RESUME_LLM_API_KEY", "paid-test-key")
    source = _docx(tmp_path / "main.docx", "Built and tested a Python scheduler for students.")
    calls, events = [], []

    class Response:
        def __init__(self, result): self.result = result
        def raise_for_status(self): pass
        def json(self):
            return {"output": [{"type": "message", "content": [
                {"type": "output_text", "text": json.dumps(self.result)}]}],
                "usage": {"input_tokens": 10, "output_tokens": 5}}

    results = [
        {"answer": "", "insufficient_facts": True},
        {"answer": "I built and tested a Python scheduler for students.", "insufficient_facts": False},
    ]

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return Response(results.pop(0))

    answer = write_premium_answer(
        {"resume_editor": {"provider": "openai", "model": "gpt-test"}}, {},
        {"company": "Acme", "title": "Intern"}, "Python role",
        "Tell us about a technical project and what you owned.",
        [{"name": source.name, "path": str(source), "context_path": str(source)}],
        request_fn=post, event_fn=events.append)

    assert answer == "I built and tested a Python scheduler for students."
    assert len(calls) == 2
    assert "revision_feedback" in calls[1]["input"]
    assert events == ["premium essay generated and autofilled after 2 attempt(s); tokens=20 in/10 out"]


def test_latex_packet_gives_premium_model_complete_resume_background(tmp_path):
    primary = _tex(tmp_path / "resume.tex")
    submit = tmp_path / "resume.pdf"
    submit.write_bytes(b"%PDF")
    old = _docx(tmp_path / "old.docx", "INVENTORY_BACKGROUND_ONLY: Used PostgreSQL in an inventory project.")
    sources = [
        {"name": "resume.tex", "path": str(submit), "context_path": str(primary), "role": "primary"},
        {"name": "old.docx", "path": str(old), "context_path": str(old), "role": "supplementary"},
    ]

    local_inputs = []
    def local_chat(system, user):
        local_inputs.append(system + "\n" + user)
        return json.dumps({"target_role": "Backend Intern", "company": "Acme",
                           "requirements": ["PostgreSQL backend role"], "keywords": ["PostgreSQL"]})

    packet, _, _ = prepare_latex_packet(
        {}, {}, {"title": "Backend Intern", "company": "Acme"}, "PostgreSQL backend role", sources,
        local_chat=local_chat)
    assert any("PostgreSQL" in item["text"] for item in packet["candidate_sources"])
    assert "candidate_evidence" not in packet
    assert "INVENTORY_BACKGROUND_ONLY" not in "\n".join(local_inputs)
    assert "education" not in packet["latex_editable_sections"]
    assert packet["locked_sections"] == ["education"]


def test_latex_patches_any_non_education_content(tmp_path):
    source = _tex(tmp_path / "resume.tex").read_text()
    result = {"edits": [
        {"section": "skills", "original_text": "Java, Python", "replacement_text": "Python, PostgreSQL"},
        {"section": "projects", "original_text": "Built a scheduler with Python",
         "replacement_text": "Built a Python scheduler handling 421,000 records"},
        {"section": "experience", "original_text": "Original Employer", "replacement_text": "Relevant Employer"},
    ], "ats_keywords": ["Python"], "omitted_keywords": [],
        "decision": "change", "decision_reason": "The edits foreground relevant backend skills."}

    tailored, count = apply_latex_edits(source, result, {})
    assert count == 3
    assert "{Relevant Employer}" in tailored
    assert "{Course Scheduler}" in tailored
    assert "{Languages}" in tailored
    assert "{: Python, PostgreSQL}" in tailored
    assert "\\resumeItem{Built a Python scheduler handling 421,000 records}" in tailored
    assert "{Test University}" in tailored
    assert source.split("\\section{Skills}")[0] == tailored.split("\\section{Skills}")[0]


@pytest.mark.parametrize("edit,match", [
    ({"section": "education", "original_text": "Test University", "replacement_text": "Changed University"},
     "Education section is locked"),
    ({"section": "projects", "original_text": "Projects", "replacement_text": "Selected Projects"},
     "section header"),
    ({"section": "skills", "original_text": "Java, Python", "replacement_text": r"\textbf{Python}"},
     "LaTeX structure"),
])
def test_latex_patch_rejects_changes_outside_contract(tmp_path, edit, match):
    source = _tex(tmp_path / "resume.tex").read_text()
    with pytest.raises(ValueError, match=match):
        apply_latex_edits(source, {"edits": [edit]}, {})


def test_paid_resume_is_cached_per_job_and_source_version(db, tmp_path, monkeypatch):
    from jobagent import runner

    source = _tex(tmp_path / "main.tex")
    submit = tmp_path / "main.pdf"
    submit.write_bytes(b"%PDF-main")
    profile = {"identity": {"first_name": "Test", "last_name": "Student"},
               "resumes": [{"path": str(submit), "tex": str(source)}]}
    job = {"id": "job-123456", "title": "Backend Intern", "company": "Acme"}
    calls = []

    def fake_editor(cfg, profile, job, job_text, sources, out):
        calls.append(job["id"])
        pdf = Path(out).with_suffix(".pdf")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"tailored")
        return pdf, {"mode": "paid-latex", "model": "test", "source_count": len(sources),
                     "keywords": ["Python"], "edits": 1, "format_preserved": True,
                     "decision": "change", "model_decision": "change",
                     "decision_reason": "Reworded a project bullet around Python.", "sections": ["projects"]}

    monkeypatch.setattr(runner, "DATA", tmp_path)
    monkeypatch.setattr(runner, "paid_editor_configured", lambda cfg: True)
    monkeypatch.setattr(runner, "tailor_with_paid_editor", fake_editor)
    cfg = {"tailor_min_chars": 1, "tailor_review_first": 5, "resume_editor": {"model": "test"}}
    first, review = runner.maybe_tailor(db, cfg, profile, job, "Python backend role", str(submit))
    assert review is not None and review.status == "NEEDS_USER_INPUT"
    db.save_answer("resume@job-123456", "review", "ok")
    second, review2 = runner.maybe_tailor(db, cfg, profile, job, "Python backend role", str(submit))
    assert first == second and review2 is None
    assert calls == [job["id"]]
    event = db.q("SELECT msg FROM events WHERE job_id=? ORDER BY id DESC LIMIT 1", (job["id"],))[0]["msg"]
    assert "resume decision" in event and "CHANGE" in event and "sections=projects" in event


def test_paid_resume_no_change_is_logged_without_review(db, tmp_path, monkeypatch):
    from jobagent import runner

    source = _tex(tmp_path / "main.tex")
    submit = tmp_path / "main.pdf"
    submit.write_bytes(b"%PDF-main")
    profile = {"resumes": [{"path": str(submit), "tex": str(source)}]}
    job = {"id": "job-no-change", "title": "Backend Intern", "company": "Acme"}

    def fake_editor(cfg, profile, job, job_text, sources, out):
        return Path(submit), {"mode": "paid-latex", "provider": "test", "model": "test",
                              "keywords": ["Python"], "edits": 0, "format_preserved": True,
                              "decision": "no_change", "model_decision": "no_change",
                              "decision_reason": "The existing bullets already match the supported requirements.",
                              "sections": []}

    monkeypatch.setattr(runner, "paid_editor_configured", lambda cfg: True)
    monkeypatch.setattr(runner, "tailor_with_paid_editor", fake_editor)
    path, review = runner.maybe_tailor(
        db, {"tailor_min_chars": 1}, profile, job, "Python backend role", str(submit))

    assert path == str(submit) and review is None
    assert not db.q("SELECT 1 FROM human_queue WHERE job_id=?", (job["id"],))
    event = db.q("SELECT msg FROM events WHERE job_id=?", (job["id"],))[0]["msg"]
    assert "NO CHANGE" in event and "existing bullets" in event


def test_missing_primary_latex_is_logged_once(db, tmp_path, monkeypatch):
    from jobagent import runner

    submit = tmp_path / "main.pdf"
    submit.write_bytes(b"%PDF-main")
    profile = {"resumes": [{"path": str(submit)}]}
    job = {"id": "job-no-tex", "title": "Backend Intern", "company": "Acme"}
    monkeypatch.setattr(runner, "paid_editor_configured", lambda cfg: True)

    for _ in range(2):
        path, review = runner.maybe_tailor(
            db, {"tailor_min_chars": 1}, profile, job, "Python backend role", str(submit))

    assert path == str(submit) and review is None
    events = db.q("SELECT msg FROM events WHERE job_id=?", (job["id"],))
    assert len(events) == 1
    assert "primary resume is not an editable LaTeX" in events[0]["msg"]
