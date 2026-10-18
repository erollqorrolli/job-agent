from jobagent import worker


class FakeChromium:
    def __init__(self):
        self.call = None

    def launch_persistent_context(self, profile, **kwargs):
        self.call = (profile, kwargs)
        return "context"


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


def test_worker_launches_installed_google_chrome_with_persistent_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    pw = FakePlaywright()

    context = worker.launch(pw, {"browser_profile": "chrome-profile", "headless": False})

    profile, options = pw.chromium.call
    assert context == "context"
    assert profile == str(tmp_path / "chrome-profile")
    assert options["channel"] == "chrome"
    assert options["headless"] is False


def test_worker_allows_an_explicit_chrome_release_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "ROOT", tmp_path)
    pw = FakePlaywright()

    worker.launch(pw, {"browser_profile": "chrome-profile", "browser_channel": "chrome-beta"})

    assert pw.chromium.call[1]["channel"] == "chrome-beta"
