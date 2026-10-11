"""Priority tiers, platform detection, and hard-eligibility checks. Pure functions."""
import re
from urllib.parse import urlsplit, parse_qsl, urlencode

P1 = re.compile(r"software|swe\b|backend|back-end|full[- ]?stack|front[- ]?end|infrastructure|platform|cloud|devops|sre\b|developer (tools|experience)|"
                r"machine learning|\bml\b|\bai\b|artificial intelligence|llm|applied scientist|mobile|ios|android|web", re.I)
P2 = re.compile(r"data (scien|engineer|analy)|security|cyber|systems|quant|research|embedded|firmware|network|database", re.I)
TRACKING = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "source", "src", "gh_src", "lever-source", "mobile", "needsRedirect"}
BLOCK_TITLE = re.compile(r"u\.?s\.? citizen|security clearance|ts/sci|secret clearance|\bphd\b|ph\.d", re.I)
US_STATE = re.compile(r",\s*(A[KLRZ]|C[AOT]|D[CE]|FL|GA|HI|I[ADLN]|K[SY]|LA|M[ADEINOST]|N[CDEHJMVY]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[AT]|W[AIVY])\b")
NON_US = re.compile(r"canada|united kingdom|\buk\b|india|germany|china|singapore|ireland|france|israel|japan|australia|netherlands|mexico|brazil|poland|switzerland|korea|taiwan", re.I)

PLATFORMS = [
    ("greenhouse", r"greenhouse\.io"), ("lever", r"jobs\.lever\.co"), ("ashby", r"ashbyhq\.com"),
    ("workday", r"myworkdayjobs\.com|myworkdaysite\.com"), ("smartrecruiters", r"smartrecruiters\.com"),
    ("icims", r"icims\.com"), ("taleo", r"taleo\.net"), ("oracle", r"oraclecloud\.com"),
    ("successfactors", r"successfactors\.(com|eu)|sapsf\.com"), ("workable", r"workable\.com"),
    ("rippling", r"rippling\.com"), ("bamboohr", r"bamboohr\.com"),
]


def platform_of(url: str) -> str:
    for name, pat in PLATFORMS:
        if re.search(pat, url, re.I):
            return name
    return "generic"


def normalize_url(url: str) -> str:
    u = urlsplit(url.strip())
    q = [(k, v) for k, v in parse_qsl(u.query) if k.lower() not in TRACKING and not k.lower().startswith("utm_")]
    path = u.path.rstrip("/")
    return f"{u.netloc.lower()}{path}" + (("?" + urlencode(sorted(q))) if q else "")


def dedupe_key(company, title, locations) -> str:
    return "|".join([company.lower().strip(), re.sub(r"\W+", " ", title.lower()).strip(), ",".join(sorted(l.lower() for l in locations))])


def priority_of(title: str, category: str) -> int:
    t = f"{title} {category or ''}"
    if re.search(r"hardware|mechanical|electrical|manufactur|chip|silicon|asic|fpga|rtl|verification", t, re.I) and not re.search(r"software", title, re.I):
        return 3
    if re.search(r"product manage|program manage|\bpm\b|\btpm\b", title, re.I):
        return 3
    if P1.search(title):
        return 1
    if P2.search(t):
        return 2
    return 3 if (category or "") in ("Hardware", "Product") else 2


def location_ok(locations, countries) -> bool:
    if not locations:
        return True
    for l in locations:
        if re.search(r"remote", l, re.I) and not NON_US.search(l):
            return True
        if "US" in countries and (US_STATE.search(l) or re.search(r"united states|\busa?\b", l, re.I)):
            return True
        if "US" not in countries or not NON_US.search(l):
            if not US_STATE.search(l) and not NON_US.search(l) and re.search(r",", l) is None:
                return True  # bare city / unknown: don't silently drop
    return False


def eligibility(item: dict, profile: dict, cfg: dict) -> tuple[bool, str]:
    """Hard requirements only. Missing preferred quals never reject."""
    degrees = item.get("degrees") or []
    held = profile.get("education", {}).get("degree_level", "Bachelor's")
    if degrees and held not in degrees:
        return False, f"requires {'/'.join(degrees)}"
    if BLOCK_TITLE.search(item["title"]):
        return False, "title requires citizenship/clearance/PhD"
    sp = (item.get("sponsorship") or "")
    if re.search(r"citizenship", sp, re.I):
        return False, "US citizenship required"
    if re.search(r"does not offer", sp, re.I) and profile.get("work_authorization", {}).get("requires_sponsorship") is True:
        return False, "no sponsorship offered"
    if not location_ok(item.get("locations") or [], cfg.get("countries", ["US"])):
        return False, "location outside allowed countries"
    kws = [k.lower() for k in cfg.get("excluded_keywords", [])]
    if any(k in item["title"].lower() for k in kws):
        return False, "excluded keyword"
    return True, ""
