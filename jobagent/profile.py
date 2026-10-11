"""Applicant profile. Source of truth is profile.yaml (verified by the user), never invented."""
import re
import yaml
from .config import ROOT

import os
PROFILE_PATH = __import__('pathlib').Path(os.environ.get('JOBAGENT_PROFILE', ROOT / "profile.yaml"))
TEMPLATE = ROOT / "profile.example.yaml"


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


def missing_required(profile: dict) -> list[str]:
    need = ["identity.first_name", "identity.last_name", "identity.email", "identity.phone",
            "education.school", "education.degree", "education.major", "education.graduation_date",
            "work_authorization.authorized_us", "work_authorization.requires_sponsorship", "resumes"]
    out = [k for k in need if not get(profile, k)
           and not (isinstance(get(profile, k), bool))]
    return out


def verified(profile: dict) -> bool:
    return bool(profile.get("verified")) and not missing_required(profile)


def norm_key(q: str) -> str:
    q = re.sub(r"[*]|\(required\)|\boptional\b", " ", q.lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", q).split())[:160]
