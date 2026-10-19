import time

from jobagent.runner import Outcome
from jobagent.worker import (claim_job, counts_toward_session, next_job,
                             session_limit_reached)


def test_worker_claim_does_not_override_a_manual_queue_decision(db):
    db.x("""INSERT INTO jobs(id,company,title,url,platform,priority,status,discovered_at)
            VALUES(?,?,?,?,?,?,?,?)""",
         ("queued", "Acme", "SWE Intern", "https://example.test/job", "generic", 1,
          "NOT_STARTED", time.time()))
    job = next_job(db, {"priorities": [1]}, set())
    assert job["id"] == "queued"

    db.x("UPDATE jobs SET status='SKIPPED' WHERE id='queued' AND status='NOT_STARTED'")
    claimed = claim_job(db, job["id"])

    assert claimed is False
    assert db.q("SELECT status FROM jobs WHERE id='queued'")[0]["status"] == "SKIPPED"


def test_session_limit_ignores_jobs_parked_for_user_input():
    cfg = {"max_per_session": 50}

    assert counts_toward_session(Outcome("NEEDS_USER_INPUT", "2 unanswered", keep_page=True)) is False
    assert session_limit_reached(attempted=50, completed=7, once_limit=None, cfg=cfg) is False
    assert session_limit_reached(attempted=93, completed=50, once_limit=None, cfg=cfg) is True


def test_completed_forms_and_submission_attempts_count_toward_session():
    assert counts_toward_session(Outcome("READY_TO_SUBMIT", keep_page=True)) is True
    assert counts_toward_session(Outcome("UNCERTAIN", submitted=True)) is True
    assert counts_toward_session(Outcome("NOT_STARTED", "DRYRUN_OK")) is True


def test_explicit_cli_limit_still_counts_attempts():
    cfg = {"max_per_session": 50}

    assert session_limit_reached(attempted=3, completed=0, once_limit=3, cfg=cfg) is True
