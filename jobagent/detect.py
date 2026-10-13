"""Page-state detection: closed, already applied, captcha, login wall, confirmation, validation errors."""
import re

CLOSED = re.compile(r"no longer (available|accepting|open)|position (has been|is) (filled|closed)|job (is )?(closed|not found|no longer)|"
                    r"posting (has )?(expired|closed)|page (could )?not (be )?found|this job (has )?expired|not currently accepting|"
                    r"(role|position) (is )?no longer", re.I)
ALREADY = re.compile(r"you('ve| have) already (applied|submitted)|already applied (to|for) this|application (was )?already (submitted|received)|"
                     r"duplicate application|you have previously applied (to|for) this", re.I)
CONFIRM = re.compile(r"thank you for (applying|your application|submitting)|application (has been |was )?(successfully )?(submitted|received|sent)|"
                     r"we('ve| have) received your application|successfully (applied|submitted)|your application is complete|application complete", re.I)
HUMAN = re.compile(r"verify (that )?you are (a )?human|are you a robot|checking your browser|unusual traffic|access denied|attention required", re.I)

CAPTCHA_JS = r"""() => {
  // Only interactive challenges count. Invisible reCAPTCHA / hCaptcha enclave / badges do not block anything.
  const big = (e, w, h) => { const r = e.getBoundingClientRect(); return r.width > w && r.height > h; };
  for (const f of document.querySelectorAll('iframe')) {
    const src = f.src || '';
    if (/recaptcha/.test(src)) {
      if (/bframe/.test(src) && big(f, 150, 150) && getComputedStyle(f).visibility !== 'hidden') return true;
      if (/anchor/.test(src) && !/size=invisible/.test(src) && big(f, 200, 50)) return true;
    }
    if (/hcaptcha/.test(src)) {
      if (/frame=challenge/.test(src) && big(f, 150, 150)) return true;
      if (/frame=checkbox/.test(src) && big(f, 100, 40)) return true;
    }
    if (/challenges\.cloudflare\.com|turnstile/.test(src) && big(f, 100, 40)) return true;
  }
  return [...document.querySelectorAll('.g-recaptcha,.h-captcha,.cf-turnstile')].some(e => big(e, 100, 40) && !e.querySelector('iframe[src*="invisible"]'));
}"""
ERRORS_JS = r"""() => {
  const out = [];
  document.querySelectorAll('[aria-invalid="true"], .error, .field-error, .invalid-feedback, [role=alert], .has-error, [class*="error-message"], [data-automation-id*="error"]').forEach(e => {
    const r = e.getBoundingClientRect(); const t = (e.innerText||'').trim();
    if (r.width > 0 && r.height > 0 && t && t.length < 200) out.push(t);
  });
  return [...new Set(out)].slice(0, 8);
}"""


def body_text(page) -> str:
    try:
        return page.inner_text("body", timeout=5000)
    except Exception:
        return ""


def has_captcha(page) -> bool:
    try:
        return bool(page.evaluate(CAPTCHA_JS))
    except Exception:
        return False


def login_wall(page) -> bool:
    """A visible password field with few other inputs means sign-in/account creation, not an application."""
    try:
        return bool(page.evaluate("""() => {
          const v = e => { const r = e.getBoundingClientRect(); return r.width>0 && r.height>0; };
          const pw = [...document.querySelectorAll('input[type=password]')].filter(v).length;
          const other = [...document.querySelectorAll('input[type=text],input[type=tel],textarea,select,input[type=file]')].filter(v).length;
          return pw > 0 && other < 4;
        }"""))
    except Exception:
        return False


def confirmation(page, adapter) -> str | None:
    t = body_text(page)
    m = CONFIRM.search(t)
    if m:
        return m.group(0)
    return None


def errors(page) -> list[str]:
    try:
        return page.evaluate(ERRORS_JS)
    except Exception:
        return []


EMAIL_CODE = re.compile(r"verification code (was|has been) sent|enter the \d+[- ]character code|security code|confirm you.?re a human", re.I)
