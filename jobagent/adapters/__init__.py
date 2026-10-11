import re
from urllib.parse import urlsplit, urlunsplit


class Adapter:
    name = "generic"
    needs_account = False
    confirm_url = re.compile(r"confirm|thank|thanks|success|submitted|complete", re.I)

    def prepare_url(self, url: str) -> str:
        return url

    def prepare(self, page):
        """Platform-specific navigation after first load (e.g. leave an embed iframe)."""

    submit_css = []     # exact ids/data-qa hooks for a platform
    submit_re = re.compile(r"^\s*(submit( application)?|send application|finish|complete application|apply( now)?)\s*$", re.I)
    next_re = re.compile(r"^\s*(next|continue|save (and|&) continue|next step|proceed)\s*$", re.I)

    def find_submit(self, page):
        return find_button(page, self.submit_css, self.submit_re)

    def find_next(self, page):
        return find_button(page, [], self.next_re)


def find_button(page, css, name_re):
    """Role-based lookup (text may sit in a child <span>). Never falls back to a bare type=submit match,
    which also matches 'Yes', 'Upload file', 'All Jobs' ..."""
    for sel in css:
        loc = page.locator(sel).first
        if loc.count() and loc.is_visible():
            return loc
    for role in ("button", "link"):
        loc = page.get_by_role(role, name=name_re)
        for i in range(min(loc.count(), 6)):
            if loc.nth(i).is_visible():
                return loc.nth(i)
    loc = page.locator("input[type=submit]")
    for i in range(min(loc.count(), 4)):
        v = loc.nth(i).get_attribute("value") or ""
        if name_re.match(v) and loc.nth(i).is_visible():
            return loc.nth(i)
    return None


class Greenhouse(Adapter):
    name = "greenhouse"

    def prepare(self, page):
        fr = page.query_selector('iframe[src*="greenhouse.io/embed"], iframe#grnhse_iframe')
        if fr and fr.get_attribute("src"):
            page.goto(fr.get_attribute("src"), wait_until="domcontentloaded")

    submit_css = ["#submit_app"]


class Lever(Adapter):
    name = "lever"

    def prepare_url(self, url):
        u = urlsplit(url)
        return url if u.path.rstrip("/").endswith("/apply") else urlunsplit((u.scheme, u.netloc, u.path.rstrip("/") + "/apply", "", ""))

    submit_css = ["#btn-submit", 'button[data-qa="btn-submit"]']


class Ashby(Adapter):
    name = "ashby"

    def prepare_url(self, url):
        u = urlsplit(url)
        return url if u.path.rstrip("/").endswith("/application") else urlunsplit((u.scheme, u.netloc, u.path.rstrip("/") + "/application", "", ""))


class AccountRequired(Adapter):
    """Workday/iCIMS/Taleo/Oracle/SuccessFactors: usually need a candidate account.
    We never create accounts or type passwords. If a signed-in session exists in the
    persistent browser profile the generic engine proceeds; otherwise the job is parked."""
    needs_account = True


class Workday(AccountRequired):
    name = "workday"

    def find_next(self, page):
        return find_button(page, ['button[data-automation-id="bottom-navigation-next-button"]', 'button[data-automation-id="pageFooterNextButton"]'], self.next_re)


def _acct(n):
    return type(n.title(), (AccountRequired,), {"name": n})


REGISTRY = {"greenhouse": Greenhouse, "lever": Lever, "ashby": Ashby, "workday": Workday,
            "icims": _acct("icims"), "taleo": _acct("taleo"), "oracle": _acct("oracle"),
            "successfactors": _acct("successfactors")}


def adapter_for(platform: str) -> Adapter:
    return REGISTRY.get(platform, Adapter)()
