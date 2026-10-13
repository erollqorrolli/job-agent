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
    if (!t || (!/\s/.test(t) && /[\[\]_\d]|.{26,}/.test(t))) {   // empty or machine name: look at the surrounding question container
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
    (r"first name|given name|legal first|preferred first|preferred name", "identity.first_name"),
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
    (r"only interested in a summer|confirm that you are only interested|summer (internship )?(only|term)", "preferences.summer_2027_only"),
    (r"returning to (an? )?(undergraduate|graduate|school|degree|program)|return to (school|your (studies|program))", "education.currently_enrolled"),
    (r"enrolled in|currently (a |an )?(student|enrolled)|pursuing (a |an )?(bachelor|master|phd|degree)|accredited (college|university)", "education.currently_enrolled"),
    (r"relative|family member|related to (any|an)|spouse", "preferences.has_relative_at_company"),
    (r"government (employee|official)|public official|elected official", "preferences.government_employee"),
    (r"at least 18|18 years|over 18|legal age", "preferences.over_18"),
    (r"(current(ly)?|any|other) (job )?offers?\b|offers? (from|with|pending)", "preferences.has_other_offers"),
    (r"on-?site|in-?office|in[- ]person|hybrid|days (a|per) week|work (from|in) (the )?office", "preferences.willing_onsite"),
    (r"40 hours|full[- ]time (commitment|during|for the)|(entire|full) (duration|length|term) of|commit(ted)? to (work|being|the)", "preferences.available_full_duration"),
    (r"(previously |already )?(completed|had|done|held|have)( at least)? (an?|any|one|1|prior|previous)( software( engineering)?| technical)? internship|previously completed .*internship|relevant (full-time )?(work )?experience|prior (internship|work) experience", "preferences.has_prior_experience"),
]
EEO = re.compile(r"gender|race|ethnic|veteran|disabilit|sexual orientation|transgender|hispanic|latino|pronoun|protected", re.I)
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
        # sentences instead of Yes/No: "I understand and I am only interested in...", "I have not previously been..."
        pos = re.compile(r"\b(understand|confirm|agree|acknowledge|only interested|i am (currently )?(a|an|enrolled)|i do)\b", re.I)
        neg = re.compile(r"\b(not|never|none|other terms|do not|don't)\b", re.I)
        hits = [i for i, o in enumerate(options) if (pos.search(o) and not neg.search(o)) == bool(want) and (pos.search(o) or neg.search(o))]
        return hits[0] if len(hits) == 1 else None
    w = str(want).lower()
    norm = lambda t: re.sub(r"[\s/.\-]", "", t)
    for i, o in enumerate(low):
        if o == w or norm(o) == norm(w):
            return i
    for i, o in enumerate(low):
        if w and (w in o or (len(o) > 2 and o in w)):
            return i
    return None


STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
          "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
          "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri",
          "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
          "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin",
          "WY": "Wyoming", "DC": "District of Columbia"}


def variants_for(label: str, value: str, p) -> list:
    """Alternative spellings of a confirmed fact, tried in order against a dropdown's options."""
    v = str(value)
    out = [v]
    if v.upper() in STATES:
        out += [STATES[v.upper()]]
    for ab, nm in STATES.items():
        if v.lower() == nm.lower():
            out += [ab]
    m = re.match(r"(\d{2})/(\d{4})$", v)
    if m:
        mo, y = int(m.group(1)), m.group(2)
        out += [f"{MONTHS[mo-1]} {y}", f"{MONTHS[mo-1][:3]} {y}", f"{mo}/{y}", y, f"Spring {y}" if mo <= 6 else f"Fall {y}", f"{MONTHS[mo-1]}, {y}"]
    return out


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
    if EEO.search(label) and not re.search(r"auth|sponsor", label, re.I):
        pref = get(p, "demographics.default") or "decline"
        if kind in ("select", "radio", "combobox", "btngroup") and pref == "decline":
            i = next((i for i, o in enumerate(opts) if DECLINE.search(o)), None)
            if i is not None:
                return Decision(value=i, source="eeo")
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
                if kind == "combobox" and not opts:    # option list could not be read: type Yes/No and let the widget match it
                    return Decision(value="Yes" if v else "No", source="profile")
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
    if re.search(r"previous(ly)? (been )?(employ|work)|worked (for|at) .* before|former employee|have you (ever )?(worked|been employed) (at|for|with)", label, re.I):
        prior = [c.lower() for c in (get(p, "preferences.previously_worked_for_companies") or [])]
        if any(c and c in label.lower() for c in prior):
            return Decision(need=(label, kind))
        i = pick_option(opts, False) if opts else None
        if i is None and opts:   # "I have never worked for X" style answers
            i = next((k for k, o in enumerate(opts) if re.search(r"\bnever\b|\bnone\b|\bno\b|have not|not (a )?(previous|current|former)", o, re.I)
                      and not re.search(r"\b(previous|current|former) (employee|contractor)", o, re.I)), None)
        return Decision(value=i if opts else "No", source="profile") if (i is not None or not opts) else Decision(need=(label, kind))
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
    if re.search(r"how did you (first )?(hear|find|learn)", label, re.I) and opts:
        for alt in (get(p, "preferences.how_did_you_hear_options") or ["Simplify", "GitHub", "Job board", "Online", "Website", "Other"]):
            i = pick_option(opts, alt)
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
                i = next((k for k in (pick_option(opts, alt) for alt in variants_for(label, v, p)) if k is not None), None)
                if i is None and path == "education.degree":
                    i = pick_option(opts, get(p, "education.degree_level") or "")
                if i is None and kind == "combobox":
                    return Decision(value=str(v), source="profile")
                if i is None:
                    return Decision(need=(label, kind)) if f.get("required") else Decision(skip=True)
                return Decision(value=i, source="profile")
            return Decision(value=str(v), source="profile")
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
        if (kind == "textarea" or len(label) > 40) and f.get("required"):
            ans = ctx.answer_fn(label, f.get("maxlength"))
            if ans:
                from .answers import question_needs_the_user
                if getattr(ctx, "company", None) and question_needs_the_user(label, ctx.company, p):
                    return Decision(need=(label, "draft", ans))      # contains a personal-history question: the user edits it
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
