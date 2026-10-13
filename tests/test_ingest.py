from jobagent.ingest import ingest
from jobagent.classify import normalize_url, priority_of, platform_of, eligibility
import time


def item(i, title="Software Engineer Intern", url=None, active=True, **k):
    d = dict(id=f"id{i}", company_name="Acme", title=title, active=active, is_visible=True, terms=["Summer 2027"],
             url=url or f"https://boards.greenhouse.io/acme/jobs/{i}?utm_source=x", locations=["New York, NY"], category="Software",
             degrees=["Bachelor's"], sponsorship="Other", date_posted=int(time.time()))
    d.update(k); return d


def test_ingest_dedupe_ineligible_closed(db, profile):
    items = [item(1), item(2, url="https://boards.greenhouse.io/acme/jobs/1"), item(3, active=False),
             item(4, title="Compilers Intern", degrees=["PhD"]), item(5, title="Cleared SWE Intern, Security Clearance Required"),
             item(6, terms=["Summer 2026"]), item(7, title="Platform Intern", locations=["London, UK"]), item(8, title="ML Intern", url="https://x.com/8")]
    s = ingest(db, {"term": "Summer 2027", "countries": ["US"]}, items)
    st = {r["id"]: r["status"] for r in db.q("SELECT id,status FROM jobs")}
    assert st["id1"] == "NOT_STARTED" and st["id2"] == "CLOSED"
    assert "id3" not in st and "id6" not in st
    assert st["id4"] == st["id5"] == st["id7"] == "INELIGIBLE"
    assert st["id8"] == "NOT_STARTED"
    # idempotent
    assert ingest(db, {"term": "Summer 2027", "countries": ["US"]}, items)["new"] == 0


def test_close_and_link_change_detected(db, profile):
    cfg = {"term": "Summer 2027", "countries": ["US"]}
    ingest(db, cfg, [item(1)])
    ingest(db, cfg, [item(1, url="https://boards.greenhouse.io/acme/jobs/999")])
    assert db.q("SELECT url_key FROM jobs")[0]["url_key"].endswith("/999")
    ingest(db, cfg, [item(1, active=False)])
    assert db.q("SELECT status FROM jobs")[0]["status"] == "CLOSED"


def test_never_reopen_applied(db, profile):
    cfg = {"term": "Summer 2027", "countries": ["US"]}
    ingest(db, cfg, [item(1)])
    db.update("id1", status="APPLIED")
    ingest(db, cfg, [item(1, active=False)])
    assert db.q("SELECT status FROM jobs")[0]["status"] == "APPLIED"


def test_priority_and_platform():
    assert priority_of("Backend Software Engineer Intern", "Software") == 1
    assert priority_of("Data Engineer Intern", "AI/ML/Data") == 2
    assert priority_of("Product Manager Intern", "Product") == 3
    assert platform_of("https://acme.wd5.myworkdayjobs.com/x") == "workday"
    assert normalize_url("https://A.com/x/?utm_source=1&id=2") == "a.com/x?id=2"


def test_ready_jobs_are_never_refilled_after_restart(db, profile):
    from jobagent.worker import recover_on_start
    cfg = {"term": "Summer 2027", "countries": ["US"]}
    ingest(db, cfg, [item(1), item(2)])
    db.update("id1", status="READY_TO_SUBMIT"); db.update("id2", status="IN_PROGRESS")
    recover_on_start(db)
    st = {r["id"]: r["status"] for r in db.q("SELECT id,status FROM jobs")}
    assert st["id1"] == "UNCERTAIN"          # may already have been submitted by hand: never auto-requeued
    assert st["id2"] == "NOT_STARTED"        # a crash mid-fill is safe to retry
