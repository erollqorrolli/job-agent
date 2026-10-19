"""Resume source inventory shared by the dashboard and tailoring pipeline.

The first/primary source is the file that may be submitted unchanged. Supplementary
sources are context only; they are never selected for an application upload.
"""
from pathlib import Path
import time


def _profile_entries(profile: dict) -> list[dict]:
    out = []
    for i, item in enumerate(profile.get("resumes") or []):
        submit = item.get("path") or item.get("docx")
        context = item.get("tex") or item.get("docx") or item.get("path")
        if not submit and not context:
            continue
        p = Path(context or submit).expanduser()
        out.append({
            "id": f"profile-{i}",
            "name": p.name,
            "path": str(Path(submit).expanduser()) if submit else str(p),
            "context_path": str(p),
            "role": "primary" if i == 0 else "supplementary",
            "tags": item.get("tags") or [],
            "source": "profile",
            "format": "latex" if p.suffix.lower() == ".tex" else p.suffix.lstrip(".").lower(),
        })
    return out


def resume_inventory(db, profile: dict) -> list[dict]:
    """Return uploaded + profile resume sources with exactly one effective primary."""
    uploaded = []
    for item in db.kv_get("resume_sources", []) or []:
        submit = Path(item.get("path", "")).expanduser()
        context = Path(item.get("context_path") or item.get("path", "")).expanduser()
        if submit.exists() and submit.is_file() and context.exists() and context.is_file():
            uploaded.append({**item, "path": str(submit), "context_path": str(context), "source": "upload"})

    inherited = _profile_entries(profile)
    uploaded_primary = next((x for x in uploaded if x.get("role") == "primary"), None)
    if uploaded_primary:
        for item in uploaded + inherited:
            item["role"] = "primary" if item.get("id") == uploaded_primary.get("id") else "supplementary"
    elif not any(x.get("role") == "primary" for x in inherited) and uploaded:
        uploaded[0]["role"] = "primary"

    seen, result = set(), []
    for item in uploaded + inherited:
        key = str(Path(item.get("context_path") or item.get("path", "")).resolve())
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    result.sort(key=lambda x: x.get("role") != "primary")
    return result


def public_inventory(db, profile: dict) -> list[dict]:
    """Browser-safe metadata (no local filesystem paths)."""
    return [{k: item.get(k) for k in ("id", "name", "role", "source", "tags", "format")}
            for item in resume_inventory(db, profile)]


def add_uploaded_sources(db, files: list[dict], role: str = "supplementary") -> list[dict]:
    """Persist metadata after the dashboard has safely written uploaded files."""
    current = db.kv_get("resume_sources", []) or []
    if role == "primary":
        for item in current:
            item["role"] = "supplementary"
    now = time.time()
    added = []
    for i, item in enumerate(files):
        record = {
            "id": item["id"],
            "name": item["name"],
            "path": item["path"],
            "context_path": item.get("context_path") or item["path"],
            "format": item.get("format") or Path(item.get("context_path") or item["path"]).suffix.lstrip("."),
            "role": "primary" if role == "primary" and i == 0 else "supplementary",
            "uploaded_at": now,
        }
        current.append(record)
        added.append(record)
    db.kv_set("resume_sources", current)
    return added


def set_primary(db, source_id: str) -> bool:
    items = db.kv_get("resume_sources", []) or []
    if not any(x.get("id") == source_id for x in items):
        return False
    for item in items:
        item["role"] = "primary" if item.get("id") == source_id else "supplementary"
    db.kv_set("resume_sources", items)
    return True


def remove_uploaded_source(db, source_id: str) -> dict | None:
    """Remove one dashboard-uploaded source from the inventory and return its metadata."""
    items = db.kv_get("resume_sources", []) or []
    removed = next((item for item in items if item.get("id") == source_id), None)
    if not removed:
        return None
    db.kv_set("resume_sources", [item for item in items if item.get("id") != source_id])
    return removed
