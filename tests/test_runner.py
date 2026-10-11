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
    assert out.reason == "DRYRUN_OK"


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
