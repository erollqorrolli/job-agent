import hashlib
import json
from dataclasses import replace

import pytest

from jobagent.resume_validation import (ResumeArtifact, ResumeValidationError,
                                        browser_file_metadata,
                                        validate_resume_artifact,
                                        verify_browser_file)
from jobagent.runner import process
from jobagent.worker import apply_outcome
from conftest import make_job


class _Box:
    width = 612
    height = 792


class _Page:
    mediabox = _Box()

    def __init__(self, text):
        self.text = text

    def extract_text(self):
        return self.text


class _Reader:
    is_encrypted = False

    def __init__(self, _path, strict=True):
        self.pages = [_Page(
            "Daniel Ranaudo ranaudo.daniel@gmail.com University of Pennsylvania "
            "Computer Science Experience Projects Python Java TypeScript " * 4
        )]


def _profile():
    return {
        "identity": {"first_name": "Daniel", "last_name": "Ranaudo",
                     "email": "ranaudo.daniel@gmail.com"},
        "education": {"school": "University of Pennsylvania"},
    }


def test_pdf_resume_validation_records_identity_pages_and_fingerprint(tmp_path, monkeypatch):
    from jobagent import resume_validation

    monkeypatch.setattr(resume_validation, "PdfReader", _Reader)
    resume = tmp_path / "DanielRanaudoResume.pdf"
    resume.write_bytes(b"%PDF-" + b"valid-resume" * 20)

    result = validate_resume_artifact(resume, _profile())

    assert result.pages == 1 and result.extracted_chars >= 120
    assert result.sha256 == hashlib.sha256(resume.read_bytes()).hexdigest()


def test_pdf_resume_validation_rejects_the_wrong_identity(tmp_path, monkeypatch):
    from jobagent import resume_validation

    monkeypatch.setattr(resume_validation, "PdfReader", _Reader)
    resume = tmp_path / "wrong.pdf"
    resume.write_bytes(b"%PDF-" + b"valid-resume" * 20)
    profile = _profile()
    profile["identity"]["last_name"] = "Different"

    with pytest.raises(ResumeValidationError, match="last name"):
        validate_resume_artifact(resume, profile)


def test_browser_attachment_hash_must_match_exact_file(page, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-" + b"browser-byte-check" * 20)
    data = resume.read_bytes()
    artifact = ResumeArtifact(str(resume), resume.name, len(data), hashlib.sha256(data).hexdigest(),
                              "pdf", 1, 500)
    page.set_content('<input data-ja="resume-field" type="file">')
    page.locator('[data-ja="resume-field"]').set_input_files(str(resume))

    metadata = browser_file_metadata(page, "resume-field")

    assert verify_browser_file(metadata, artifact)["status"] == "verified"
    with pytest.raises(ResumeValidationError, match="fingerprint"):
        verify_browser_file(metadata, replace(artifact, sha256="0" * 64))


def test_runner_marks_ready_only_after_exact_resume_verification(page, db, site, profile):
    job = make_job(db, f"{site}/simple.html", jid="verified-resume")

    out = process(page, db, {"live_submit": False}, job,
                  answer_chat=lambda *_: '{"answer":"I built and tested a course scheduling application."}')
    apply_outcome(db, {"max_retries": 3}, job, out)

    assert out.reason == "DRYRUN_OK"
    assert out.resume_verification["status"] == "verified"
    stored = db.q("SELECT resume_sha256,resume_verification FROM jobs WHERE id=?", (job["id"],))[0]
    assert stored["resume_sha256"] == out.resume_sha256
    assert json.loads(stored["resume_verification"])["method"] == "browser-file-byte-match"


def test_runner_stops_before_filling_when_resume_is_invalid(page, db, site, profile, monkeypatch):
    from jobagent import runner

    monkeypatch.setattr(runner, "validate_resume_artifact",
                        lambda *_: (_ for _ in ()).throw(ResumeValidationError("wrong applicant")))
    job = make_job(db, f"{site}/simple.html", jid="invalid-resume")

    out = process(page, db, {"live_submit": False}, job)

    assert out.status == "NEEDS_USER_INPUT" and "wrong applicant" in out.reason
    assert db.q("SELECT kind FROM human_queue WHERE job_id=?", (job["id"],)) == [{"kind": "file"}]


def test_dashboard_exposes_exact_file_verification(db, tmp_path, monkeypatch):
    from jobagent import dashboard

    monkeypatch.setattr(dashboard, "db", db)
    monkeypatch.setattr(dashboard, "ROOT", tmp_path)
    resume = tmp_path / "data" / "resumes" / "verified" / "resume.pdf"
    resume.parent.mkdir(parents=True)
    resume.write_bytes(b"%PDF-verified")
    sha256 = hashlib.sha256(resume.read_bytes()).hexdigest()
    verification = json.dumps({"status": "verified", "sha256": sha256,
                               "method": "browser-file-byte-match"})
    db.x("""INSERT INTO jobs(id,company,title,status,resume_used,resume_sha256,resume_verification)
            VALUES(?,?,?,?,?,?,?)""",
         ("verified", "Acme", "SWE Intern", "READY_TO_SUBMIT", str(resume), sha256, verification))

    row = dashboard.app.test_client().get("/api/jobs?status=READY_TO_SUBMIT").get_json()[0]

    assert row["resume_verified"] is True
    assert row["resume_sha256_short"] == sha256[:12]
    assert "resume_verification" not in row and "resume_sha256" not in row
