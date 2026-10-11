"""Written answers in the user's voice. Grounded only in profile facts; hard style filter."""
import os, re, requests
from .profile import load_profile

BANNED = [r"passionate", r"thrilled", r"eager to", r"leverage", r"honored", r"fascinated", r"aligns? (perfectly )?with",
          r"thrive", r"unique blend", r"innovative", r"intersection of", r"dreamed", r"commitment to excellence",
          r"excited to contribute", r"cutting[- ]edge", r"dynamic", r"synerg", r"delve", r"tapestry", r"spearhead", r"—", r"–",
          r"deeply", r"incredibly", r"i am writing to"]
BANNED_RE = re.compile("|".join(BANNED), re.I)

STYLE = """You write job application answers as a normal college student.
Casual, simple, direct, short, personal. No corporate language, no fake excitement, no adjectives that sell.
Never use em dashes. Do not force the company name in. Do not open with 'I am'.
Use ONLY the facts provided. Do not invent projects, metrics, employers, tools or stories.
If the facts are not enough to answer honestly, reply with exactly: INSUFFICIENT_FACTS
Length: short question 1-3 sentences, medium 3-5, essay as needed but no filler. Respect any limit given.
Example of the right tone: "I like building stuff and figuring out how things work. I've worked on a few projects where I got to solve real problems with code, and I want to keep getting better at that while working with a team."
"""


def style_violations(text: str) -> list[str]:
    return sorted({m.group(0).lower() for m in BANNED_RE.finditer(text)})


META = re.compile(r"^(original|rewritten|rewrite|answer|response|here('s| is)[^\n]*):\s*", re.I | re.M)


def clean_output(t: str) -> str:
    t = t.strip().strip('"')
    if re.search(r"^\s*rewritten:", t, re.I | re.M):          # small models echo draft + rewrite; keep the rewrite
        t = re.split(r"^\s*rewritten:\s*", t, flags=re.I | re.M)[-1]
    return META.sub("", t).strip().strip('"')


def llm_configured() -> bool:
    base = os.environ.get("LLM_BASE_URL", "")
    return bool(os.environ.get("LLM_API_KEY") or "localhost" in base or "127.0.0.1" in base)


def _chat(cfg, system, user) -> str:
    llm = cfg.get("llm", {})
    base = os.environ.get("LLM_BASE_URL", llm.get("base_url", "")).rstrip("/")
    r = requests.post(f"{base}/chat/completions", timeout=90,
                      headers={"Authorization": f"Bearer {os.environ.get('LLM_API_KEY', 'none')}"},
                      json={"model": os.environ.get("LLM_MODEL", llm.get("model")), "temperature": 0.7,
                            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def facts_block(profile) -> str:
    f = profile.get("facts", {}) or {}
    ed = profile.get("education", {})
    lines = [f"Student: {ed.get('degree')} in {ed.get('major')} at {ed.get('school')}, graduating {ed.get('graduation_date')}."]
    for k in ("projects", "experience", "skills", "interests", "goals"):
        for v in f.get(k) or []:
            lines.append(f"{k[:-1] if k.endswith('s') else k}: {v}")
    return "\n".join(lines)


def has_enough_facts(profile) -> bool:
    f = profile.get("facts", {}) or {}
    return any(f.get(k) for k in ("projects", "experience"))


def write_answer(cfg, question, company, role, job_text, limit=None, chat=None) -> str | None:
    """Returns text or None (=> human queue). `chat` injectable for tests."""
    profile = load_profile()
    if not has_enough_facts(profile):
        return None
    chat = chat or (lambda s, u: _chat(cfg, s, u))
    user = (f"Company: {company}\nRole: {role}\nAbout the role (from the posting, may be partial):\n{job_text[:2500]}\n\n"
            f"My real background (only use this):\n{facts_block(profile)}\n\nQuestion: {question}\n"
            + (f"Hard limit: {limit} characters.\n" if limit else "") + "Write my answer.")
    for attempt in range(2):
        try:
            out = chat(STYLE, user if attempt == 0 else user + f"\nRewrite. Do not use: {', '.join(bad)}")
        except Exception:
            return None
        if out.strip() == "INSUFFICIENT_FACTS":
            return None
        out = clean_output(out)
        bad = style_violations(out)
        if not bad and (not limit or len(out) <= limit):
            return out.strip()
        if limit and len(out) > limit and not bad:
            bad = [f"over {limit} characters"]
    return None
