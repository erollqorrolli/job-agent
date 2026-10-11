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

    def submit_selectors(self):
        return ['button[type=submit]:visible', 'input[type=submit]:visible',
                'button:visible:text-matches("^\\\\s*(submit( application)?|send application|apply)\\\\s*$", "i")']

    def next_selectors(self):
        return ['button:visible:text-matches("^\\\\s*(next|continue|save (and|&) continue|next step|proceed)\\\\s*$", "i")']


class Greenhouse(Adapter):
    name = "greenhouse"

    def prepare(self, page):
        fr = page.query_selector('iframe[src*="greenhouse.io/embed"], iframe#grnhse_iframe')
        if fr and fr.get_attribute("src"):
            page.goto(fr.get_attribute("src"), wait_until="domcontentloaded")

    def submit_selectors(self):
        return ['#submit_app', 'button:visible:text-matches("^\\\\s*submit application\\\\s*$", "i")'] + super().submit_selectors()


class Lever(Adapter):
    name = "lever"

    def prepare_url(self, url):
        u = urlsplit(url)
        return url if u.path.rstrip("/").endswith("/apply") else urlunsplit((u.scheme, u.netloc, u.path.rstrip("/") + "/apply", "", ""))

    def submit_selectors(self):
        return ['#btn-submit', 'button[data-qa="btn-submit"]'] + super().submit_selectors()


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

    def next_selectors(self):
        return ['button[data-automation-id="bottom-navigation-next-button"]', 'button[data-automation-id="pageFooterNextButton"]'] + super().next_selectors()

    def submit_selectors(self):
        return ['button[data-automation-id="bottom-navigation-next-button"]:text-matches("submit", "i")'] + super().submit_selectors()


def _acct(n):
    return type(n.title(), (AccountRequired,), {"name": n})


REGISTRY = {"greenhouse": Greenhouse, "lever": Lever, "ashby": Ashby, "workday": Workday,
            "icims": _acct("icims"), "taleo": _acct("taleo"), "oracle": _acct("oracle"),
            "successfactors": _acct("successfactors")}


def adapter_for(platform: str) -> Adapter:
    return REGISTRY.get(platform, Adapter)()
