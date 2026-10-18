"""Scan a page for form controls, decide an answer per control, fill it."""
import re, json
from .profile import get, norm_key

SCAN_JS = r"""
() => {
  const vis = e => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const txt = e => (e && e.innerText || '').replace(/\s+/g,' ').trim();
  const LAB = '.application-label,legend,label,[class*=label],[class*=question],h1,h2,h3,h4,h5';
  const ctrls = '(input:not([type=hidden]):not([type=radio]):not([type=checkbox]),select,textarea)';
  const qlab = el => {
    let p = el.parentElement;
    for (let i = 0; i < 5 && p; i++, p = p.parentElement) {
      if ([...p.querySelectorAll('input:not([type=hidden]):not([type=radio]):not([type=checkbox]),select,textarea')].filter(x => x !== el).length > 0) break;
      for (const l of p.querySelectorAll(LAB)) {
        if (l.contains(el) || l.querySelector('input,select,textarea')) continue;
        const t = txt(l); if (t.length > 1 && t.length < 400) return t;
      }
    }
    return '';
  };
  const optLabel = el => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if (l) t = txt(l); }
    if (!t && el.closest('label')) t = txt(el.closest('label'));
    if (!t) t = el.getAttribute('aria-label') || (el.nextSibling && el.nextSibling.textContent || '').trim() || el.value || '';
    return t;
  };
  const labelOf = el => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if (l) t = txt(l); }
    if (!t && el.closest('label')) t = txt(el.closest('label'));
    if (!t && el.getAttribute('aria-labelledby')) t = el.getAttribute('aria-labelledby').split(' ').map(i => txt(document.getElementById(i))).join(' ');
    if (!t) t = el.getAttribute('aria-label') || '';
    if (!t) { const fs = el.closest('fieldset'); const lg = fs && fs.querySelector('legend'); if (lg) t = txt(lg); }
    const genericFile = (el.type || '').toLowerCase() === 'file' && /^(upload|attach|choose|browse)( a)?( file| document)?$/i.test(t);
    if (!t || genericFile || (!/\s/.test(t) && /[\[\]_\d]|.{26,}/.test(t))) {   // empty, generic upload text, or machine name: use surrounding question
      const q = qlab(el); if (q) t = q;
    }
    if (!t) t = el.placeholder || el.name || '';
    return t;
  };
  const out = []; const groups = {}; let n = 0; let nfile = 0;
  document.querySelectorAll('input,select,textarea').forEach(el => {
    const type = (el.type || el.tagName).toLowerCase();
    if (['hidden','submit','button','image','reset'].includes(type)) return;
    if (type !== 'file' && !vis(el)) return;
    if (el.disabled) return;
    if (type === 'search') return;
    if (type !== 'file') {   // react-select etc. add invisible shadow inputs that are not real fields
      const cs = getComputedStyle(el);
      if (el.getAttribute('aria-hidden') === 'true' || (el.tabIndex === -1 && cs.opacity === '0') || cs.pointerEvents === 'none' && cs.opacity === '0') return;
    }
    let id = el.getAttribute('data-ja') ; if (!id) { id = 'ja' + (++n) + '_' + Math.random().toString(36).slice(2,6); el.setAttribute('data-ja', id); }
    const required = el.required || el.getAttribute('aria-required') === 'true' || /\*|required/i.test(labelOf(el)) ;
    if (type === 'radio' || type === 'checkbox') {
      const g = (type === 'radio' ? el.name : '') || id;
      const fs = el.closest('fieldset,[role=group],[role=radiogroup]');
      let gl = (fs && (fs.querySelector('legend,.label,label') ? txt(fs.querySelector('legend,.label,label')) : '')) || '';
      if (!gl || gl === labelOf(el)) { const q = qlab(el); if (q && q !== labelOf(el)) gl = q; }
      const optL = optLabel(el);
      groups[g] = groups[g] || {kind: type, group: g, label: type==='radio' ? (gl || optL) : labelOf(el), required, options: [], ids: [], checked: []};
      groups[g].options.push(optL); groups[g].ids.push(id); groups[g].checked.push(el.checked);
      if (required) groups[g].required = true;
      return;
    }
    const o = {kind: el.tagName === 'SELECT' ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (type === 'file' ? 'file' : ((el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || /select__input/.test(el.className)) ? 'combobox' : 'text'))),
               id, label: labelOf(el), required, value: el.value || '', maxlength: el.maxLength > 0 ? el.maxLength : null, inputType: type, name: el.name || '', domid: el.id || '', fileIndex: type === 'file' ? ++nfile : 0};
    if (o.kind === 'select') o.options = [...el.options].map(x => x.text.trim()).filter(Boolean);
    out.push(o);
  });
  // button-style Yes/No questions (Ashby): treat each pair as a radio group
  const yn = [...document.querySelectorAll('button')].filter(b => /^(yes|no)$/i.test((b.innerText||'').trim()) && vis(b));
  const byParent = new Map(); yn.forEach(b => { const p = b.parentElement; byParent.set(p, [...(byParent.get(p)||[]), b]); });
  byParent.forEach((bs, p) => {
    if (bs.length < 2) return;
    const ids = bs.map(b => { let id = b.getAttribute('data-ja'); if (!id) { id = 'jb' + (++n) + '_' + Math.random().toString(36).slice(2,6); b.setAttribute('data-ja', id); } return id; });
    let label = qlab(bs[0]); if (!label) { let e = p.previousElementSibling || p.parentElement && p.parentElement.previousElementSibling; label = e ? txt(e) : ''; }
    const box = p.closest('[class*=field],[class*=question],[class*=Question]') || p.parentElement;
    const required = /\*/.test(label) || !!(box && box.querySelector('[class*=required],[class*=Required]'));
    out.push({kind: 'btngroup', group: ids[0], label, required, options: bs.map(b => b.innerText.trim()), ids, checked: bs.map(() => false)});
  });
  Object.values(groups).forEach(g => out.push(g));
  return out;
}
"""

YES = re.compile(r"^(yes|true|y)\b", re.I)
NO = re.compile(r"^(no|false|n)\b", re.I)
DECLINE = re.compile(r"decline|prefer not|do not wish|don'?t wish|not to (say|answer|disclose|specify)|rather not", re.I)

# (label regex, profile path | callable). Order matters: first match wins.
RULES = [
    (r"full name|first and last|legal name|your name|^name\b", lambda p: f"{get(p,'identity.first_name')} {get(p,'identity.last_name')}"),
    (r"first name|given name|legal first|preferred first", "identity.first_name"),
    (r"last name|family name|surname|preferred last", "identity.last_name"),
    (r"e-?mail", "identity.email"),
    (r"phone|mobile|telephone", "identity.phone"),
    (r"linkedin", "links.linkedin"),
    (r"github", "links.github"),
    (r"portfolio|personal (web)?site|website|\burl\b", "links.portfolio"),
    (r"street|address line 1|^address$", "identity.address.line1"),
    (r"current location|where are you (currently )?(located|based)|^location", lambda p: ", ".join(x for x in (get(p,'identity.address.city'), get(p,'identity.address.state')) if x) or None),
    (r"\bcity\b", "identity.address.city"),
    (r"\bstate\b|province", "identity.address.state"),
    (r"zip|postal", "identity.address.zip"),
    (r"country", "identity.address.country"),
    (r"university|school|college|institution", "education.school"),
    (r"degree", "education.degree"),
    (r"major|field of study|discipline", "education.major"),
    (r"gpa|grade point", "education.gpa"),
    (r"graduat|expected completion|end date", "education.graduation_date"),
    (r"salary|compensation", "preferences.salary_expectation"),
    (r"current (employer|company)", "preferences.current_employer"),
    (r"how did you (hear|find)|source", "preferences.how_did_you_hear"),
]
BOOL_RULES = [
    (r"sponsor", "work_authorization.requires_sponsorship"),
    (r"authori[sz]ed to work|legally (authori|able|eligible)|eligible to work|right to work", "work_authorization.authorized_us"),
    (r"relocat", "preferences.willing_to_relocate"),
]
# yes/no facts the user has confirmed; checked BEFORE the legal-declaration guard
PREF_BOOL = [
    (r"enrolled in|currently (a |an )?(student|enrolled)|pursuing (a |an )?(bachelor|master|phd|degree)|accredited (college|university)", "education.currently_enrolled"),
    (r"relative|family member|related to (any|an)|spouse", "preferences.has_relative_at_company"),
    (r"government (employee|official)|public official|elected official", "preferences.government_employee"),
    (r"at least 18|18 years|over 18|legal age", "preferences.over_18"),
    (r"(current(ly)?|any|other) (job )?offers?\b|offers? (from|with|pending)", "preferences.has_other_offers"),
    (r"on-?site|in-?office|in[- ]person|hybrid|days (a|per) week|work (from|in) (the )?office", "preferences.willing_onsite"),
    (r"40 hours|full[- ]time (commitment|during|for the)|(entire|full) (duration|length|term) of|commit(ted)? to (work|being|the)", "preferences.available_full_duration"),
    (r"previously completed .*internship|relevant (full-time )?(work )?experience", "preferences.has_prior_experience"),
]
EEO = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|transgender|hispanic|latino|pronoun|protected", re.I)
DEMO_RULES = [
    (r"pronoun", "identity.pronouns"),
    (r"gender|\bsex\b|transgender", "demographics.gender"),
    (r"race", "demographics.race"),
    (r"ethnic|hispanic|latino", "demographics.ethnicity"),
    (r"veteran", "demographics.veteran"),
    (r"disabilit", "demographics.disability"),
]


def demographic_boolean(path: str | None, value, label: str) -> bool | None:
    """Translate a saved category into a site's equivalent Yes/No EEO question."""
    if path != "demographics.ethnicity" or not re.search(r"hispanic|latin[oaex]", label, re.I):
        return None
    normalized = re.sub(r"[^a-z]+", " ", str(value or "").lower()).strip()
    if re.search(r"\b(non|not|no)\s+(hispanic|latin)", normalized):
        return False
    if re.search(r"\bhispanic\b|\blatin(?:o|a|x|e)?\b", normalized):
        return True
    return None
# legal declarations the user must approve explicitly
LEGAL = re.compile(r"criminal|convict|felony|background check|drug test|non-?compete|security clearance|export control|itar|government official|"
                   r"citizen(ship)?|visa status|immigration", re.I)


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]


def date_part(v: str, label: str):
    m = re.match(r"(\d{4})-(\d{2})", v or "")
    if not m:
        return None
    y, mo = m.groups()
    if re.search(r"month", label, re.I):
        return MONTHS[int(mo) - 1]
    if re.search(r"year", label, re.I):
        return y
    return None


def graduation_details(v: str):
    """Return safe, deterministic ways to express a YYYY-MM graduation date."""
    m = re.match(r"(\d{4})-(\d{2})", v or "")
    if not m:
        return None
    year, month_text = m.groups()
    month = int(month_text)
    if not 1 <= month <= 12:
        return None
    term = "Spring" if month <= 5 else ("Summer" if month <= 8 else "Fall")
    return {
        "year": year,
        "short_year": year[-2:],
        "month": MONTHS[month - 1],
        "month_number": month_text,
        "term": term,
        "term_long": f"{term} {year}",
        "term_short": f"{term} {year[-2:]}",
        "date": f"{month_text}/{year}",
        "month_year": f"{MONTHS[month - 1]} {year}",
    }


def graduation_candidates(v: str, label: str) -> list[str]:
    """Best-first values for matching common graduation dropdown formats."""
    info = graduation_details(v)
    if not info:
        return []
    if re.search(r"semester|season|term|when (?:do|will|are).{0,20}graduat", label, re.I):
        order = ("term_long", "term_short", "month_year", "date", "year")
    elif re.search(r"month", label, re.I) and not re.search(r"year", label, re.I):
        order = ("month", "month_number")
    elif re.search(r"year", label, re.I) and not re.search(r"month", label, re.I):
        order = ("year", "short_year", "term_long", "term_short")
    else:
        order = ("date", "month_year", "term_long", "term_short", "year")
    return [info[k] for k in order]


def academic_year(grad: str, today=None):
    import datetime
    t = today or datetime.date.today()
    m = re.match(r"(\d{4})", grad or "")
    if not m:
        return None
    start = t.year if t.month >= 8 else t.year - 1
    left = int(m.group(1)) - (start + 1)
    return {0: "Senior", 1: "Junior", 2: "Sophomore", 3: "Freshman"}.get(left)


def fmt_date(v: str, label: str):
    m = re.match(r"(\d{4})-(\d{2})", v or "")
    if not m:
        return v
    y, mo = m.groups()
    if re.search(r"year", label, re.I) and not re.search(r"month", label, re.I):
        return y
    return f"{mo}/{y}"


class Decision:
    def __init__(self, value=None, need=None, skip=False, source="", ineligible=None):
        self.value, self.need, self.skip, self.source, self.ineligible = value, need, skip, source, ineligible


def pick_option(options, want) -> int | None:
    """Fuzzy choose an option for a wanted string or bool."""
    low = [o.lower() for o in options]
    if isinstance(want, bool):
        for i, o in enumerate(options):
            if (YES if want else NO).match(o.strip()):
                return i
        return None
    w = str(want).lower()
    norm = lambda t: re.sub(r"[\s/.\-]", "", t)
    for i, o in enumerate(low):
        if o == w or norm(o) == norm(w):
            return i
    for i, o in enumerate(low):
        if w and (w in o or (len(o) > 2 and o in w)):
            return i
    return None


def pick_range(options, x: float):
    """Choose the option whose numeric range contains x ("3.0 - 3.49", "3.5+", "Below 3.0")."""
    for i, o in enumerate(options):
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", o)]
        if len(nums) >= 2 and nums[0] <= x <= nums[1] + (0.0 if nums[1] - nums[0] > 0.5 else 0.0):
            return i
        if len(nums) == 1 and re.search(r"\+|or (higher|above|more)|above|over", o, re.I) and x >= nums[0]:
            return i
        if len(nums) == 1 and re.search(r"below|under|less|<", o, re.I) and x < nums[0]:
            return i
    return None


PAY_RANGE = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*(?:-|–|—|to)\s*\$?\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)",
    re.I)
PAY_SINGLE = re.compile(r"\$\s*(\d{1,3}(?:,\d{3})*(?:\.\d+)?)", re.I)


def salary_from_posting(job_text: str, label: str, options=None, input_type="text"):
    """Use the midpoint of an explicit posted compensation range; never invent one."""
    text = " ".join((job_text or "").split())
    candidates = []
    for match in PAY_RANGE.finditer(text):
        low, high = (float(x.replace(",", "")) for x in match.groups())
        if low <= 0 or high < low:
            continue
        context = text[max(0, match.start() - 80):match.end() + 100].lower()
        unit = "hour" if re.search(r"/\s*(hr|hour)|per\s+(hr|hour)|hourly", context) else (
            "year" if re.search(r"/\s*(yr|year)|per\s+(yr|year)|annual|salary", context) else
            ("year" if high >= 1000 else "hour"))
        candidates.append((low, high, unit))
    if not candidates:
        for match in PAY_SINGLE.finditer(text):
            value = float(match.group(1).replace(",", ""))
            context = text[max(0, match.start() - 80):match.end() + 100].lower()
            if not re.search(r"compensation|pay|salary|wage|hourly|per\s+(hour|year)|/\s*(hr|yr)", context):
                continue
            unit = "hour" if re.search(r"/\s*(hr|hour)|per\s+(hr|hour)|hourly", context) else (
                "year" if value >= 1000 else "hour")
            candidates.append((value, value, unit))
            break
    if not candidates:
        return None
    low, high, source_unit = candidates[0]
    target = (low + high) / 2
    requested_unit = ("hour" if re.search(r"hour|hourly|/\s*hr", label, re.I) else
                      "year" if re.search(r"annual|yearly|per year|salary", label, re.I) else source_unit)
    if source_unit == "hour" and requested_unit == "year":
        target *= 2080
    elif source_unit == "year" and requested_unit == "hour":
        target /= 2080
    if options:
        return pick_range(options, target)
    rounded = round(target, 2)
    if input_type == "number":
        return str(int(rounded)) if rounded.is_integer() else f"{rounded:.2f}".rstrip("0").rstrip(".")
    if requested_unit == "year":
        return f"${rounded:,.0f} per year"
    return f"${rounded:.2f}".rstrip("0").rstrip(".") + " per hour"


def decide(f: dict, ctx) -> Decision:
    """ctx: profile, db, resume_path, answer_fn(question, limit)->str|None"""
    p = ctx.profile
    label = re.sub(r"\s+", " ", (f.get("label") or "")).strip()
    key = norm_key(label)
    kind = f["kind"]
    opts = f.get("options") or []
    if kind == "file" and re.search(r"autofill|auto-fill|parse", label, re.I):
        return Decision(skip=True)   # these widgets re-render the whole form on upload
    if kind == "file":
        hay = f"{label} {f.get('name','')} {f.get('domid','')}"
        if re.search(r"cover", hay, re.I):
            return Decision(skip=not f.get("required"), need=("Cover letter file required", "file") if f.get("required") else None)
        if re.search(r"resume|\bcv\b|curriculum", hay, re.I) or not label or f.get("fileIndex") == 1 or label.lower() in ("attach", "upload", "choose file", "browse"):
            return Decision(value=ctx.resume_path, source="resume") if ctx.resume_path else Decision(need=("Resume file path missing in profile", "file"))
        return Decision(skip=not f.get("required"), need=("Document required: " + label, "file") if f.get("required") else None)
    saved = (ctx.db.get_answer(f"{key}@{ctx.job_id}") or ctx.db.get_answer(key)) if key else None
    if re.search(r"u\.?s\.? ?(person|citizen)|citizenship|itar|export control", label, re.I):
        person = get(p, "work_authorization.us_person")
        citizen = get(p, "work_authorization.us_citizen")
        v = person if re.search(r"person|itar|export", label, re.I) else citizen
        if v is None:
            return Decision(need=(label, kind))
        if v is False and re.search(r"must be|required|only|eligible (candidates|applicants)|restricted to", label, re.I):
            return Decision(ineligible="requires US person/citizen")
        i = pick_option(opts, bool(v)) if opts else None
        if opts and i is None:
            return Decision(need=(label, kind))
        return Decision(value=i if opts else ("Yes" if v else "No"), source="profile")
    if saved is not None and kind != "checkbox":
        if kind in ("select", "radio", "combobox", "btngroup"):
            i = pick_option(opts, saved)
            if i is not None:
                return Decision(value=i, source="library")
        else:
            return Decision(value=saved, source="library")
    if re.search(r"pronoun", label, re.I) and get(p, "identity.pronouns"):
        if opts:
            i = pick_option(opts, get(p, "identity.pronouns"))
            return Decision(value=i, source="profile") if i is not None else Decision(need=(label, kind))
        return Decision(value=get(p, "identity.pronouns"), source="profile")
    if re.search(r"gpa|grade point", label, re.I):
        g = get(p, "education.gpa")
        if not f.get("required"):
            return Decision(skip=True)     # only disclosed when the form requires it
        if g is None:
            return Decision(need=(label, kind))
        if opts:
            i = pick_range(opts, float(g))
            if i is None:
                i = pick_option(opts, str(g))
            return Decision(value=i, source="profile") if i is not None else Decision(need=(label, kind))
        return Decision(value=str(g), source="profile")
    if re.search(r"salary|compensation|pay expectation|desired pay|expected pay|hourly rate|wage", label, re.I):
        grounded = salary_from_posting(getattr(ctx, "job_text", ""), label, opts, f.get("inputType", "text"))
        if grounded is not None:
            return Decision(value=grounded, source="job-posting-pay")
        configured = get(p, "preferences.salary_expectation")
        if configured not in (None, ""):
            i = pick_option(opts, configured) if opts else None
            if opts and i is None:
                return Decision(need=(label, kind))
            return Decision(value=i if opts else str(configured), source="profile")
        return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
    if EEO.search(label) and not re.search(r"auth|sponsor", label, re.I):
        pref = get(p, "demographics.default") or "decline"
        wanted = demo_path = None
        if pref != "decline":
            matched = next(((path, get(p, path)) for pat, path in DEMO_RULES
                            if re.search(pat, label, re.I) and get(p, path)), None)
            if matched:
                demo_path, wanted = matched
        boolean_wanted = demographic_boolean(demo_path, wanted, label)
        if kind in ("select", "radio", "combobox", "btngroup"):
            i = pick_option(opts, wanted) if wanted else None
            if i is None and boolean_wanted is not None:
                i = pick_option(opts, boolean_wanted)
            if i is not None:
                return Decision(value=i, source="profile-eeo")
            if pref == "decline":
                i = next((i for i, o in enumerate(opts) if DECLINE.search(o)), None)
                if i is not None:
                    return Decision(value=i, source="eeo")
        elif kind == "checkbox" and boolean_wanted is not None:
            return Decision(value=True, source="profile-eeo") if boolean_wanted else Decision(skip=True)
        elif wanted:
            return Decision(value=str(wanted), source="profile-eeo")
        if getattr(ctx, "choice_fn", None) and kind in ("select", "radio", "combobox", "btngroup"):
            inferred = ctx.choice_fn(label, opts)
            if inferred is not None:
                return Decision(value=inferred, source="llm-grounded")
        return Decision(skip=not f.get("required"), need=(label, kind) if f.get("required") else None)
    for pat, path in PREF_BOOL:
        if re.search(pat, label, re.I) and kind != "file":
            v = get(p, path)
            if v is None:
                break
            if kind in ("radio", "select", "combobox", "btngroup"):
                i = pick_option(opts, bool(v))
                if i is not None:
                    return Decision(value=i, source="profile")
            elif kind in ("text", "textarea") and not f.get("required") and len(label) < 60:
                break
            else:
                return Decision(value="Yes" if v else "No", source="profile")
            break
    if LEGAL.search(label) and not re.search(r"sponsor|authori[sz]ed to work|eligible to work", label, re.I):
        return Decision(need=(label, kind))
    for pat, path in BOOL_RULES:
        if re.search(pat, label, re.I):
            v = get(p, path)
            if v is None:
                return Decision(need=(label, kind))
            if kind in ("radio", "select", "combobox", "btngroup"):
                i = pick_option(opts, bool(v))
                return Decision(value=i, source="profile") if i is not None else Decision(need=(label, kind))
            return Decision(value="Yes" if v else "No", source="profile")
    if re.search(r"previous(ly)? (employ|work)|worked (for|at|here|there).*before|former employee|"
                 r"have you (ever )?(worked|been employed) (at|for|with|here)|prior employment (at|with)", label, re.I):
        prior = [c.lower() for c in (get(p, "preferences.previously_worked_for_companies") or [])]
        if any(c and c in label.lower() for c in prior):
            return Decision(need=(label, kind))
        i = pick_option(opts, False) if opts else None
        if i is None and opts:   # "I have never worked for X" style answers
            i = next((k for k, o in enumerate(opts) if re.search(r"\bnever\b|\bnone\b|\bno\b|have not|not (a )?(previous|current|former)", o, re.I)
                      and not re.search(r"\b(previous|current|former) (employee|contractor)", o, re.I)), None)
        return Decision(value=i if opts else "No", source="profile") if (i is not None or not opts) else Decision(need=(label, kind))
    if re.search(r"(employee|internal|personal) referral|referred (by|through)|do you have (a )?referr", label, re.I):
        if re.search(r"name|e-?mail|who|provide|specify|details", label, re.I):
            return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
        i = pick_option(opts, False) if opts else None
        if i is None and opts:
            i = next((k for k, o in enumerate(opts)
                      if re.search(r"not referred|no referral|without (a )?referral", o, re.I)), None)
        if i is not None or not opts:
            return Decision(value=i if opts else "No", source="profile-default")
        return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
    if kind == "checkbox":
        if saved is not None:
            return Decision(value=bool(YES.match(saved.strip())), source="library")
        if re.search(r"certify|accurate|true and (correct|complete)|acknowledge", label, re.I):
            return Decision(value=True, source="ack")
        if re.search(r"terms|agree|consent|privacy|policy|authori[sz]e", label, re.I):
            if get(p, "preferences.accept_standard_terms") is True:
                return Decision(value=True, source="profile")
            return Decision(need=(label, kind))
        return Decision(skip=True)
    if re.search(r"graduat|expected completion", label, re.I) and not re.search(r"high school|sat|act", label, re.I):
        grad = get(p, "education.graduation_date") or ""
        candidates = graduation_candidates(grad, label)
        if opts:
            for candidate in candidates:
                i = pick_option(opts, candidate)
                if i is not None:
                    return Decision(value=i, source="profile-derived")
        elif re.search(r"semester|season|term|when (?:do|will|are).{0,20}graduat", label, re.I):
            info = graduation_details(grad)
            if info:
                return Decision(value=info["term_long"], source="profile-derived")
    if re.search(r"(start|end|graduat|completion)[^?]*(month|year)|(month|year)[^?]*(start|end|graduat)", label, re.I) and not re.search(r"high school|sat|act", label, re.I):
        src = "education.start_date" if re.search(r"start", label, re.I) else "education.graduation_date"
        part = date_part(get(p, src) or "", label)
        if part is None:
            return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
        i = pick_option(opts, part) if opts else None
        return Decision(value=i if i is not None else part, source="profile")
    if re.search(r"academic year|year in school|class (standing|year)|current year of study", label, re.I):
        ay = academic_year(get(p, "education.graduation_date"))
        if ay:
            i = pick_option(opts, ay) if opts else None
            return Decision(value=i if i is not None else ay, source="profile")
    if re.search(r"(university|school|college|\.edu) e-?mail", label, re.I):
        em = get(p, "identity.email") or ""
        if em.endswith(".edu"):
            return Decision(value=em, source="profile")
    if kind in ("select", "combobox") and re.search(r"privacy|code of conduct|acknowledg|terms|consent", label, re.I):
        if get(p, "preferences.accept_standard_terms") is True:
            i = next((i for i, o in enumerate(opts) if re.search(r"acknowledge|agree|accept|i have read|yes|confirm", o, re.I)), None)
            if i is not None:
                return Decision(value=i, source="profile")
        return Decision(need=(label, kind))
    for pat, path in RULES:
        if re.search(pat, label, re.I):
            v = path(p) if callable(path) else get(p, path)
            if v in (None, ""):
                return Decision(skip=not f.get("required"), need=(label, kind) if f.get("required") else None)
            if path == "education.graduation_date":
                v = fmt_date(v, label)
            if kind in ("select", "radio", "combobox", "btngroup"):
                i = pick_option(opts, v)
                if i is None and path == "education.degree":
                    i = pick_option(opts, get(p, "education.degree_level") or "")
                if i is None and kind == "combobox":
                    return Decision(value=str(v), source="profile")
                if i is None:
                    return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
                return Decision(value=i, source="profile")
            return Decision(value=str(v), source="profile")
    # Let the inexpensive/local model connect differently worded versions of the same question.
    # It may select only an existing answer or unresolved queue key; it cannot invent a response.
    if label and getattr(ctx, "reuse_fn", None):
        match = ctx.reuse_fn(label, kind, opts)
        if match:
            answer = match.get("answer")
            if answer is None:
                ctx.question_keys[key] = match["qkey"]
            elif kind in ("select", "radio", "combobox", "btngroup"):
                i = pick_option(opts, answer)
                if i is not None:
                    ctx.db.save_answer(key, label, answer, "semantic-reuse")
                    return Decision(value=i, source="library-semantic")
            else:
                ctx.db.save_answer(key, label, answer, "semantic-reuse")
                return Decision(value=answer, source="library-semantic")
    # factual question the profile may settle: let the local model pick, but only with grounded evidence
    if label and getattr(ctx, "choice_fn", None) and (opts or (kind == "text" and len(label) <= 120)):
        got = ctx.choice_fn(label, opts)
        if got is not None and opts:
            ctx.db.save_answer(key, label, opts[got], "llm")
            return Decision(value=got, source="llm-grounded")
        if got is not None:      # free text from a small model: show it to the user as a draft instead of using it
            return Decision(need=(label, "draft", got))
    # open question
    if kind in ("textarea", "text") and label:
        if kind == "textarea" or len(label) > 40:
            ans = ctx.answer_fn(label, f.get("maxlength"))
            if ans:
                if get(p, "preferences.trust_llm_answers") is True:
                    return Decision(value=ans, source="llm")
                # unreviewed model text is never submitted: park it as a draft for one-click approval
                return Decision(need=(label, "draft", ans))
        return Decision(skip=not f.get("required"), need=(label, kind) if f.get("required") else None)
    if f.get("required"):
        return Decision(need=(label or f.get("name", "unlabeled field"), kind))
    return Decision(skip=True)


def apply(page, f: dict, d: Decision):
    """Fill one control. Returns True if something was set."""
    kind = f["kind"]
    if kind == "btngroup":
        if not isinstance(d.value, int):
            return False
        page.locator(f'[data-ja="{f["ids"][d.value]}"]').click(force=True, timeout=3000)
        return True
    if kind in ("radio", "checkbox"):
        ids = f["ids"]
        if kind == "checkbox" and d.value is True:
            tgt = ids[0]
        elif kind == "radio" and isinstance(d.value, int):
            tgt = ids[d.value]
        else:
            return False
        loc = page.locator(f'[data-ja="{tgt}"]')
        if not loc.is_checked():
            try:
                loc.check(timeout=3000, force=True)
            except Exception:
                loc.evaluate("e => e.click()")
        return True
    loc = page.locator(f'[data-ja="{f["id"]}"]')
    if kind == "combobox":
        text = f["options"][d.value] if isinstance(d.value, int) else str(d.value)
        q = re.split(r"[,(]", text)[0].strip()[:20] or text[:20]   # "Philadelphia, PA" -> "Philadelphia"
        loc.click(timeout=4000); loc.fill("", timeout=4000); loc.type(q, delay=15); page.wait_for_timeout(700)
        opts = page.locator('[role=option]:visible')
        texts = opts.all_inner_texts()
        i = pick_option(texts, text) if texts else None
        if i is None and texts:
            i = pick_option(texts, q)
        if i is None and texts and f.get("fallback"):
            i = pick_option(texts, f["fallback"])
        if i is None and len(texts) == 1:
            i = 0
        if i is None:
            loc.press("Escape"); loc.fill("")
            raise ValueError("no matching option")
        opts.nth(i).click()
        return True
    if kind == "file":
        loc.set_input_files(d.value, timeout=8000)
    elif kind == "select":
        loc.select_option(index=d.value) if isinstance(d.value, int) else loc.select_option(label=str(d.value))
    else:
        if (f.get("value") or "").strip():  # never overwrite what Simplify/autofill already put there
            return False
        val = str(d.value)
        if f.get("maxlength"):
            val = val[: f["maxlength"]]
        if re.search(r"location|city", f.get("label", ""), re.I) and f["kind"] == "text":
            return fill_autocomplete(page, loc, val)
        try:
            loc.fill(val, timeout=4000)
        except Exception:   # overlays / phone widgets: type it instead
            loc.click(force=True, timeout=3000); loc.press_sequentially(val, delay=10)
    return True


def read_combo_options(page, f) -> list[str]:
    """Open a custom dropdown to learn its options (react-select etc.), then close it."""
    loc = page.locator(f'[data-ja="{f["id"]}"]')
    try:
        loc.click(timeout=3000); page.wait_for_timeout(350)
        texts = [t.strip() for t in page.locator('[role=option]:visible').all_inner_texts()][:200]
        if not texts:   # some widgets only render the list after a key press
            loc.press("ArrowDown"); page.wait_for_timeout(400)
            texts = [t.strip() for t in page.locator('[role=option]:visible').all_inner_texts()][:200]
        loc.press("Escape")
        return texts
    except Exception:
        return []


SUGGEST = '.dropdown-location, .dropdown-results li, [role=option], .pac-item, li[class*=suggest], [class*=autocomplete] li'


def fill_autocomplete(page, loc, val):
    """Location boxes (Lever etc.) discard free text unless a suggestion is clicked."""
    city = re.split(r"[,(]", val)[0].strip() or val
    loc.click(timeout=4000); loc.fill("", timeout=3000); loc.press_sequentially(city, delay=70)
    page.wait_for_timeout(2200)
    opts = page.locator(SUGGEST + ":visible")
    texts = [t.strip() for t in opts.all_inner_texts()]
    if not texts:
        loc.fill(val)           # plain text field after all
        return True
    want = city.lower()
    best = next((i for i, t in enumerate(texts) if want in t.lower() and re.search(r"\\bUSA?\\b|United States|,\\s*[A-Z]{2}\\b", t)), 0)
    opts.nth(best).click()
    page.wait_for_timeout(400)
    return True
