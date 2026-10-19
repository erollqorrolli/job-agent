import hashlib, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import pytest
from playwright.sync_api import sync_playwright
from mock_site.server import start, SUBMITS
from jobagent.db import DB


@pytest.fixture(scope="session")
def site():
    srv, url = start()
    yield url
    srv.shutdown()


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


@pytest.fixture
def page(browser):
    pg = browser.new_page(); pg.set_default_timeout(8000)
    yield pg
    pg.close()


@pytest.fixture
def db(tmp_path):
    return DB(tmp_path / "t.db")


@pytest.fixture
def profile(tmp_path, monkeypatch):
    resume = tmp_path / "resume.pdf"; resume.write_bytes(b"%PDF-1.4 test")
    prof = {"verified": True, "identity": {"first_name": "Test", "last_name": "Student", "email": "test@example.com", "phone": "2155550100",
            "address": {"city": "Philadelphia", "state": "PA", "country": "United States"}},
            "links": {"linkedin": "https://linkedin.com/in/test"},
            "education": {"school": "Test University", "degree": "BSE", "degree_level": "Bachelor's", "major": "Computer Science", "graduation_date": "2028-05"},
            "work_authorization": {"authorized_us": True, "requires_sponsorship": False},
            "preferences": {"trust_llm_answers": True}, "demographics": {"default": "decline"},
            "resumes": [{"path": str(resume), "tags": ["default"]}],
            "facts": {"projects": ["Built a small web app for tracking course schedules"], "skills": ["Python"]}}
    for mod in ("jobagent.runner", "jobagent.answers", "jobagent.ingest"):
        monkeypatch.setattr(f"{mod}.load_profile", lambda: prof)
    from jobagent.resume_validation import ResumeArtifact
    def valid_test_resume(path, _profile):
        value = pathlib.Path(path)
        data = value.read_bytes()
        return ResumeArtifact(str(value.resolve()), value.name, len(data), hashlib.sha256(data).hexdigest(),
                              value.suffix.lstrip("."), 1, 500)
    monkeypatch.setattr("jobagent.runner.validate_resume_artifact", valid_test_resume)
    return prof


def make_job(db, url, platform="generic", jid="j1", category="Software"):
    db.x("INSERT INTO jobs(id,company,title,location,url,url_key,platform,category,priority,status,discovered_at) VALUES(?,?,?,?,?,?,?,?,?,?,0)",
         (jid, "Acme", "SWE Intern", "NYC, NY", url, url, platform, category, 1, "NOT_STARTED"))
    return db.q("SELECT * FROM jobs WHERE id=?", (jid,))[0]
