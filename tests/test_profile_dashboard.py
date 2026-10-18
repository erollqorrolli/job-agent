import stat

from jobagent.resume_sources import add_uploaded_sources


def _complete_profile():
    return {
        "verify": True,
        "identity": {"first_name": "Test", "last_name": "Student", "email": "test@example.com",
                     "phone": "2155550100", "address": {"city": "Philadelphia", "state": "PA",
                                                               "country": "United States"}},
        "education": {"school": "Test University", "degree": "BSE", "major": "Computer Science",
                      "graduation_date": "2028-05", "currently_enrolled": True},
        "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
        "preferences": {"trust_llm_answers": False},
        "demographics": {"default": "decline", "gender": None, "race": None},
    }


def test_dashboard_requires_and_saves_verified_preflight(db, tmp_path, monkeypatch):
    from jobagent import dashboard, profile as profile_module

    profile_path = tmp_path / "profile.yaml"
    monkeypatch.setattr(profile_module, "PROFILE_PATH", profile_path)
    monkeypatch.setattr(dashboard, "db", db)
    client = dashboard.app.test_client()

    blocked = client.post("/api/action/start", json={})
    assert blocked.status_code == 400
    assert "applicant profile" in blocked.get_json()["error"]

    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-test")
    add_uploaded_sources(db, [{"id": "main", "name": "resume.pdf", "path": str(resume)}], "primary")
    saved = client.post("/api/profile", json=_complete_profile())
    assert saved.status_code == 200 and saved.get_json()["ok"] is True
    assert stat.S_IMODE(profile_path.stat().st_mode) == 0o600

    public = client.get("/api/profile").get_json()["profile"]
    assert public["identity"]["first_name"] == "Test"
    assert "resumes" not in public and "facts" not in public


def test_revert_jobs_clears_unanswered_work_but_protects_possible_submissions(db):
    db.x("INSERT INTO jobs(id,company,title,status,attempted_at,resume_used,retries,evidence,last_error,next_try_at,questions_json) "
         "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
         ("pending", "Acme", "Intern", "NEEDS_USER_INPUT", 10, "/tmp/tailored.pdf", 2,
          "screenshot", "old error", 99, '[{"q":"question"}]'))
    db.x("INSERT INTO jobs(id,company,title,status,submitted_at) VALUES(?,?,?,?,?)",
         ("applied", "Safe", "Intern", "APPLIED", 20))
    db.add_need("pending", "question@pending", "Question?", "draft", [])
    db.add_need("applied", "question@applied", "Question?", "text", [])
    db.kv_set("paid_resume@pending", {"path": "/tmp/tailored.pdf"})
    db.save_answer("resume@pending", "Review", "ok")

    result = db.revert_jobs(["pending", "applied"])

    assert result["reverted"] == 1 and result["questions_cleared"] == 1
    assert result["protected"] == [{"id": "applied", "status": "APPLIED"}]
    pending = db.q("SELECT * FROM jobs WHERE id='pending'")[0]
    assert pending["status"] == "NOT_STARTED" and pending["attempted_at"] is None
    assert pending["resume_used"] is None and pending["questions_json"] == "[]"
    assert not db.q("SELECT 1 FROM human_queue WHERE job_id='pending' AND resolved_at IS NULL")
    assert db.q("SELECT 1 FROM human_queue WHERE job_id='applied' AND resolved_at IS NULL")
    assert db.kv_get("paid_resume@pending") is None
    assert db.get_answer("resume@pending") is None
    assert db.q("SELECT status FROM jobs WHERE id='applied'")[0]["status"] == "APPLIED"


def test_dashboard_bulk_revert_resets_question_jobs(db, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "proc_alive", lambda: False)
    db.x("INSERT INTO jobs(id,company,title,status) VALUES(?,?,?,?)",
         ("waiting", "Acme", "Intern", "NEEDS_USER_INPUT"))
    db.add_need("waiting", "why@waiting", "Why?", "draft", [])

    response = dashboard.app.test_client().post("/api/jobs/revert-pending", json={})

    assert response.status_code == 200
    assert response.get_json()["reverted"] == 1
    assert response.get_json()["questions_cleared"] == 1
    assert db.q("SELECT status FROM jobs WHERE id='waiting'")[0]["status"] == "NOT_STARTED"


def test_dashboard_revert_waits_for_worker_to_stop(db, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "proc_alive", lambda: True)
    db.x("INSERT INTO jobs(id,company,title,status) VALUES(?,?,?,?)",
         ("active", "Acme", "Intern", "IN_PROGRESS"))

    response = dashboard.app.test_client().post("/api/jobs/active/revert", json={})

    assert response.status_code == 409
    assert db.kv_get("control") == "stop"
    assert db.q("SELECT status FROM jobs WHERE id='active'")[0]["status"] == "IN_PROGRESS"


def test_dashboard_saves_submission_handoff_mode(db, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    client = dashboard.app.test_client()

    response = client.post("/api/settings", json={"handoff": "always"})
    assert response.status_code == 200
    assert client.get("/api/state").get_json()["settings"]["handoff"] == "always"

    bad = client.post("/api/settings", json={"handoff": "surprise"})
    assert bad.status_code == 400
    assert client.get("/api/state").get_json()["settings"]["handoff"] == "always"

    hidden = client.post("/api/settings", json={"handoff": "always", "headless": True})
    assert hidden.status_code == 400
    assert "visible browser" in hidden.get_json()["error"]


def test_answer_resolves_semantically_equivalent_existing_queue_items(db, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "llm_configured", lambda: True)
    q1, q2 = "available to start in June", "able to begin work in June"
    db.add_need("j1", q1, "Are you available to start in June?", "select", ["Yes", "No"])
    db.add_need("j2", q2, "Are you able to begin work in June?", "select", ["Yes", "No"])
    monkeypatch.setattr(dashboard, "semantic_equivalent_pending_keys", lambda *args, **kwargs: [q2])

    response = dashboard.app.test_client().post("/api/answer", json={"qkey": q1, "answer": "Yes"})

    assert response.status_code == 200 and response.get_json()["equivalents_resolved"] == 1
    assert not db.q("SELECT 1 FROM human_queue WHERE resolved_at IS NULL")
    assert db.get_answer(q1) == "Yes" and db.get_answer(q2) == "Yes"
