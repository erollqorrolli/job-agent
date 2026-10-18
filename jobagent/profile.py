"""Applicant profile. Source of truth is profile.yaml (verified by the user), never invented."""
import os
import re
from pathlib import Path

import yaml
from .config import ROOT

PROFILE_PATH = Path(os.environ.get('JOBAGENT_PROFILE', ROOT / "profile.yaml"))
TEMPLATE = ROOT / "profile.example.yaml"

PROFILE_FORM = {
    "identity": {
        "first_name": None, "last_name": None, "email": None, "phone": None, "pronouns": None,
        "address": {"line1", "city", "state", "zip", "country"},
    },
    "education": {
        "school", "degree", "degree_level", "major", "start_date",
        "graduation_date", "gpa", "currently_enrolled",
    },
    "work_authorization": {"authorized_us", "requires_sponsorship", "us_citizen", "us_person"},
    "preferences": {
        "trust_llm_answers", "over_18", "willing_to_relocate", "willing_onsite",
        "available_full_duration", "has_prior_experience", "current_employer",
        "how_did_you_hear",
    },
    "demographics": {"default", "gender", "race", "ethnicity", "veteran", "disability"},
}


def load_profile(path=None) -> dict:
    p = path or PROFILE_PATH
    if not p.exists():
        return yaml.safe_load(open(TEMPLATE)) or {}
    return yaml.safe_load(open(p)) or {}


def get(profile: dict, dotted: str):
    cur = profile
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _configured_resume(profile: dict) -> bool:
    return any(isinstance(item, dict) and (item.get("path") or item.get("docx") or item.get("tex"))
               for item in profile.get("resumes") or [])


def missing_required(profile: dict, has_resume: bool | None = None) -> list[str]:
    need = ["identity.first_name", "identity.last_name", "identity.email", "identity.phone",
            "education.school", "education.degree", "education.major", "education.graduation_date",
            "work_authorization.authorized_us", "work_authorization.requires_sponsorship"]
    out = [k for k in need if not get(profile, k)
           and not (isinstance(get(profile, k), bool))]
    if not (_configured_resume(profile) if has_resume is None else has_resume):
        out.append("resumes")
    return out


def verified(profile: dict, has_resume: bool | None = None) -> bool:
    return bool(profile.get("verified")) and not missing_required(profile, has_resume=has_resume)


def _public_section(source: dict, spec) -> dict:
    if not isinstance(source, dict):
        source = {}
    out = {}
    for key, child in spec.items() if isinstance(spec, dict) else ((key, None) for key in spec):
        value = source.get(key)
        out[key] = _public_section(value, child) if isinstance(child, (dict, set)) else value
    return out


def profile_form(profile: dict) -> dict:
    """Return only dashboard-editable fields; never expose resume paths or unrelated secrets."""
    return {"verified": bool(profile.get("verified")),
            **{section: _public_section(profile.get(section) or {}, spec)
               for section, spec in PROFILE_FORM.items()}}


def _merge_form(target: dict, incoming: dict, spec) -> None:
    if not isinstance(incoming, dict):
        return
    for key, child in spec.items() if isinstance(spec, dict) else ((key, None) for key in spec):
        if key not in incoming:
            continue
        value = incoming[key]
        if isinstance(child, (dict, set)):
            current = target.get(key)
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _merge_form(current, value, child)
        elif value is None or isinstance(value, (bool, int, float)):
            target[key] = value
        elif isinstance(value, str):
            target[key] = value.strip()[:500] or None


def save_profile_form(values: dict, *, mark_verified: bool = False, path: Path | None = None) -> dict:
    """Merge validated dashboard fields without overwriting resumes, facts, or advanced preferences."""
    current = load_profile(path)
    for section, spec in PROFILE_FORM.items():
        block = current.get(section)
        if not isinstance(block, dict):
            block = {}
            current[section] = block
        _merge_form(block, values.get(section) or {}, spec)
    current["verified"] = bool(mark_verified)
    destination = Path(path or PROFILE_PATH)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(yaml.safe_dump(current, sort_keys=False, allow_unicode=True))
    os.chmod(temporary, 0o600)
    temporary.replace(destination)
    os.chmod(destination, 0o600)
    return current


def norm_key(q: str) -> str:
    q = re.sub(r"[*]|\(required\)|\boptional\b", " ", q.lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", q).split())[:160]
