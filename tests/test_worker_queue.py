import time

from jobagent.worker import claim_job, next_job


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
