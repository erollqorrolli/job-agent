from conftest import make_job
from jobagent.runner import process
from mock_site.server import SUBMITS

GOOD = lambda s, u: "I like building stuff and figuring out how things work. I made a small web app for course schedules and want to get better at that with a team."
BAD = lambda s, u: "I am incredibly passionate about leveraging innovative technology."


def run(page, db, site, name, live=False, chat=GOOD, **cfg):
    job = make_job(db, f"{site}/{name}")
    return process(page, db, {"live_submit": live, **cfg}, job, answer_chat=chat)


def test_simple_dry_run_fills_without_submitting(page, db, site, profile):
    n = len(SUBMITS)
    out = run(page, db, site, "simple.html")
    assert out.status == "NOT_STARTED" and out.reason == "DRYRUN_OK", (out, )
    assert len(SUBMITS) == n
    assert page.input_value("#em") == "test@example.com"
    assert page.input_value("#gd") == "05/2028"
    assert page.input_value("#auth") == "Yes" or page.locator("#auth").evaluate("e=>e.selectedOptions[0].text") == "Yes"
    assert page.locator("input[name=sp][value=n]").is_checked()
    assert page.locator("#g").evaluate("e=>e.selectedOptions[0].text") == "Decline to self-identify"
    assert "building" in page.input_value("#why")
    assert page.input_value("#hx") == "Decline To Self Identify"  # combobox + container label


def test_saved_latino_ethnicity_fills_yes_on_real_combobox(page, db, site, profile):
    profile["demographics"] = {"default": "self_identify", "gender": "Male",
                               "race": "Hispanic/Latino", "ethnicity": "Hispanic/Latino"}
    out = run(page, db, site, "simple.html", handoff="always")
    assert out.status == "READY_TO_SUBMIT"
    assert page.input_value("#hx") == "Yes"


def test_simple_live_submit_confirms(page, db, site, profile):
    out = run(page, db, site, "simple.html", live=True)
    assert out.status == "APPLIED", out
    assert "Thank you" in out.evidence or "received" in out.evidence


def test_multistep(page, db, site, profile):
    out = run(page, db, site, "multistep.html", live=True)
    assert out.status == "APPLIED", out


def test_apply_button_landing(page, db, site, profile):
    out = run(page, db, site, "landing.html", live=True)
    assert out.status == "APPLIED", out


def test_closed_and_already(page, db, site, profile):
    assert run(page, db, site, "closed.html").status == "CLOSED"
    db.x("DELETE FROM jobs")
    assert run(page, db, site, "already.html").status == "ALREADY_APPLIED"


def test_captcha_blocks(page, db, site, profile):
    assert run(page, db, site, "captcha.html", live=True).status == "BLOCKED"


def test_login_wall_parks_job(page, db, site, profile):
    out = run(page, db, site, "login.html")
    assert out.status == "NEEDS_USER_INPUT"
    assert db.q("SELECT 1 FROM human_queue WHERE kind='account'")


def test_unknown_and_legal_questions_go_to_human(page, db, site, profile):
    out = run(page, db, site, "unknown_q.html", live=True)
    assert out.status == "NEEDS_USER_INPUT"
    qs = [r["question"] for r in db.q("SELECT question FROM human_queue")]
    assert any("criminal" in q for q in qs)


def test_answered_question_is_reused_and_job_requeued(page, db, site, profile):
    out = run(page, db, site, "unknown_q.html", live=True)
    for r in db.q("SELECT qkey FROM human_queue WHERE resolved_at IS NULL"):
        db.resolve_need(r["qkey"], "No" if "criminal" in r["qkey"] else "Python")
    assert db.q("SELECT status FROM jobs")[0]["status"] == "NOT_STARTED"
    job = db.q("SELECT * FROM jobs")[0]
    out = process(page, db, {"live_submit": True}, job, answer_chat=GOOD)
    assert out.status == "APPLIED", (out, db.q("SELECT question,kind,resolved_at FROM human_queue"))


def test_silent_submit_is_uncertain_not_applied(page, db, site, profile):
    assert run(page, db, site, "silent.html", live=True).status == "UNCERTAIN"


def test_bad_voice_answer_rejected(page, db, site, profile):
    out = run(page, db, site, "simple.html", live=True, chat=BAD)
    assert out.status == "NEEDS_USER_INPUT"


def test_live_requires_verified_profile(page, db, site, profile):
    profile["verified"] = False
    out = run(page, db, site, "simple.html", live=True)
    assert out.status == "NEEDS_USER_INPUT"
    assert out.reason == "complete and verify the applicant profile before processing"


def test_unreviewed_model_text_becomes_draft_and_approval_is_job_scoped(page, db, site, profile):
    profile["preferences"]["trust_llm_answers"] = False
    out = run(page, db, site, "simple.html", live=True)
    assert out.status == "NEEDS_USER_INPUT"
    row = db.q("SELECT * FROM human_queue WHERE kind='draft'")[0]
    assert "@j1" in row["qkey"] and "building" in row["options"]
    db.resolve_need(row["qkey"], "I like building stuff.")
    assert db.get_answer("why do you want to work here@j1") == "I like building stuff."
    assert db.get_answer("why do you want to work here") is None   # not reused for other companies
    out = process(page, db, {"live_submit": True}, db.q("SELECT * FROM jobs")[0])
    assert out.status == "APPLIED", out


def test_clean_output_strips_model_meta():
    from jobagent.answers import clean_output
    assert clean_output("Original:\nfoo bar.\n\nRewritten:\nbaz qux.") == "baz qux."


def test_ashby_style_buttons_autofill_widget_and_submit_selector(page, db, site, profile):
    profile["work_authorization"]["requires_sponsorship"] = True
    out = run(page, db, site, "ashbylike.html", live=False)
    assert out.reason == "DRYRUN_OK", out
    assert page.evaluate("window.bad") is None                      # never clicked "All Jobs"
    assert page.evaluate("document.querySelector('#b1 [data-sel=\"1\"]').innerText") == "Yes"
    assert page.evaluate("document.querySelector('#b2 [data-sel=\"1\"]').innerText") == "Yes"   # needs sponsorship
    assert page.input_value("#n") == "Test Student"                 # full-name rule; autofill widget left alone


def test_non_latex_resume_is_uploaded_unchanged(page, db, site, profile):
    job = make_job(db, f"{site}/simple.html")
    cfg = {"live_submit": False, "tailor_min_chars": 10, "tailor_review_first": 1}
    out = process(page, db, cfg, job, answer_chat=GOOD)
    assert out.reason == "DRYRUN_OK", out
    assert page.evaluate("document.querySelector('#rs').files[0].name") == "resume.pdf"
    assert not db.q("SELECT * FROM human_queue WHERE kind='resume'")


def test_review_queue_is_capped(page, db, site, profile, tmp_path, monkeypatch):
    from pathlib import Path
    from jobagent import runner

    src = tmp_path / "base.tex"
    src.write_text("\\documentclass{article}\\begin{document}\\section{Skills}Languages: Python\\section{Projects}\\item Built scheduler\\end{document}")
    profile["resumes"][0]["tex"] = str(src)

    def fake_editor(cfg, profile, job, job_text, sources, out):
        pdf = Path(out).with_suffix(".pdf")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-tailored")
        return pdf, {"mode": "paid-latex", "provider": "test", "model": "test", "source_count": len(sources),
                     "keywords": ["Python"], "edits": 1, "format_preserved": True,
                     "decision": "change", "model_decision": "change",
                     "decision_reason": "Reworded a project bullet around Python.", "sections": ["projects"]}

    monkeypatch.setattr(runner, "paid_editor_configured", lambda cfg: True)
    monkeypatch.setattr(runner, "tailor_with_paid_editor", fake_editor)
    cfg = {"live_submit": False, "tailor_min_chars": 10, "tailor_review_first": 2}
    outs = [process(page, db, cfg, make_job(db, f"{site}/simple.html", jid=f"j{i}"), answer_chat=GOOD) for i in range(4)]
    assert [o.reason for o in outs[:2]] == ["tailored resume awaiting your review"] * 2
    assert [o.reason for o in outs[2:]] == ["DEFER_REVIEW"] * 2
    assert db.q("SELECT COUNT(*) n FROM human_queue WHERE kind='resume'")[0]["n"] == 2


def test_manual_review_attaches_tailored_resume_without_a_separate_approval(page, db, site, profile, tmp_path, monkeypatch):
    from pathlib import Path
    from jobagent import runner

    src = tmp_path / "base.tex"
    src.write_text("\\documentclass{article}\\begin{document}\\section{Skills}Python\\end{document}")
    profile["resumes"][0]["tex"] = str(src)

    def fake_editor(cfg, profile, job, job_text, sources, out):
        pdf = Path(out).with_suffix(".pdf")
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-tailored")
        return pdf, {"provider": "test", "model": "test", "keywords": ["Python"], "edits": 1,
                     "decision": "change", "decision_reason": "Relevant wording.", "sections": ["skills"]}

    monkeypatch.setattr(runner, "paid_editor_configured", lambda cfg: True)
    monkeypatch.setattr(runner, "tailor_with_paid_editor", fake_editor)
    for i in range(5):
        db.add_need(f"existing-{i}", f"resume@existing-{i}", "Existing resume review", "resume", ["resume.pdf"])
    job = make_job(db, f"{site}/simple.html", jid="manual-resume")
    out = process(page, db, {"live_submit": False, "tailor_min_chars": 10,
                             "handoff": "always", "headless": False}, job, answer_chat=GOOD)

    assert out.status == "READY_TO_SUBMIT" and out.keep_page
    assert page.input_value("#em") == "test@example.com"
    assert out.resume.endswith(".pdf")
    assert not db.q("SELECT 1 FROM human_queue WHERE job_id=? AND kind='resume' AND resolved_at IS NULL",
                    (job["id"],))


def test_manual_mode_requeues_existing_resume_only_waits(db):
    from jobagent.worker import requeue_manual_resume_reviews

    job = make_job(db, "https://example.test/job", jid="resume-wait")
    deferred = make_job(db, "https://example.test/deferred", jid="resume-deferred")
    db.update(job["id"], status="NEEDS_USER_INPUT", status_reason="tailored resume awaiting your review")
    db.update(deferred["id"], status="NOT_STARTED", status_reason="waiting for resume reviews", next_try_at=99)
    db.add_need(job["id"], "resume@resume-wait", "Review tailored resume", "resume", ["resume.pdf"])

    assert requeue_manual_resume_reviews(db, {"handoff": "always", "headless": False}) == 2
    row = db.q("SELECT status,status_reason FROM jobs WHERE id=?", (job["id"],))[0]
    assert row["status"] == "NOT_STARTED"
    assert "manual final review" in row["status_reason"]
    assert not db.q("SELECT 1 FROM human_queue WHERE job_id=? AND resolved_at IS NULL", (job["id"],))
    row = db.q("SELECT status,status_reason,next_try_at FROM jobs WHERE id=?", (deferred["id"],))[0]
    assert row["status"] == "NOT_STARTED" and row["next_try_at"] == 0
    assert "manual final review" in row["status_reason"]


def test_silent_submit_with_antibot_widget_falls_back_to_handoff(page, db, site, profile):
    out = run(page, db, site, "silent_recaptcha.html", live=True)                    # default: try itself first
    assert out.status == "READY_TO_SUBMIT" and out.keep_page and "rejected" in out.reason
    db.x("DELETE FROM jobs")
    out = run(page, db, site, "silent_recaptcha.html", live=True, handoff="never")
    assert out.status == "BLOCKED" and "submit by hand" in out.reason


def test_handoff_leaves_tab_open_and_detects_the_users_click(page, db, site, profile):
    from jobagent.worker import poll_ready
    job = make_job(db, f"{site}/simple.html")
    out = process(page, db, {"live_submit": True, "handoff": "always"}, job, answer_chat=GOOD)
    assert out.status == "READY_TO_SUBMIT" and out.keep_page and out.submitted
    n = len(SUBMITS)
    assert len(SUBMITS) == n and page.input_value("#em") == "test@example.com"    # filled, not submitted
    ready = {job["id"]: (page, job)}
    db.add_need(job["id"], "manual-file", "Upload supporting document", "file", [])
    poll_ready(db, ready, log=lambda *a: None)
    assert job["id"] in ready                                                      # no confirmation yet: keep waiting
    page.click("button:has-text('Submit Application')")                            # the human click
    page.wait_for_timeout(500)
    poll_ready(db, ready, log=lambda *a: None)
    assert not ready and db.q("SELECT status FROM jobs")[0]["status"] == "APPLIED"
    assert not db.q("SELECT 1 FROM human_queue WHERE job_id=? AND resolved_at IS NULL", (job["id"],))


def test_manual_review_keeps_unanswered_and_dry_run_tabs_open(page, db, site, profile):
    unanswered = run(page, db, site, "unknown_q.html", live=False, handoff="always")
    assert unanswered.status == "NEEDS_USER_INPUT" and unanswered.keep_page and not page.is_closed()

    db.x("DELETE FROM jobs")
    db.x("DELETE FROM human_queue")
    completed = run(page, db, site, "simple.html", live=False, handoff="always")
    assert completed.status == "READY_TO_SUBMIT" and completed.keep_page
    assert "only you can click Submit" in completed.reason


def test_generic_upload_button_uses_surrounding_document_label(page, db, site, profile):
    outcome = run(page, db, site, "generic_file_label.html", live=False, handoff="always")
    assert outcome.status == "NEEDS_USER_INPUT" and outcome.keep_page
    questions = [row["question"] for row in db.q("SELECT question FROM human_queue")]
    assert questions == ["Document required: Undergraduate Transcript *"]
