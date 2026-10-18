import os
import stat
from urllib.parse import parse_qs, urlsplit

from jobagent import provider_auth


def test_chatgpt_pkce_registration_and_callback_are_saved_locally(tmp_path, monkeypatch):
    credentials = tmp_path / "providers.json"
    monkeypatch.setattr(provider_auth, "CREDENTIALS_PATH", credentials)
    provider_auth._PENDING.clear()
    redirect_uri = "http://127.0.0.1:5055/auth/chatgpt/callback"
    auth_url = provider_auth.begin_chatgpt_sign_in(redirect_uri, "browser-session")
    query = parse_qs(urlsplit(auth_url).query)

    assert query["client_id"] == ["dynamic_agent_client"]
    assert query["resource"] == ["https://api.openai.com/v1"]
    assert query["code_challenge_method"] == ["S256"]
    assert "chatgpt.tokens.use.direct" in query["scope"][0]
    assert query["redirect_uri"] == [redirect_uri]

    seen = {}

    class Response:
        def raise_for_status(self): pass
        def json(self):
            return {"id_token": "id-token", "access_token": "access-token", "refresh_token": "refresh-token",
                    "expires_in": 3600, "scope": provider_auth.OPENAI_SCOPES}

    def post(url, **kwargs):
        seen.update(url=url, **kwargs)
        return Response()

    status = provider_auth.complete_chatgpt_sign_in(
        {"state": query["state"][0], "code": "authorization-code", "client_id": "oaiapp-issued"},
        "browser-session", request_fn=post,
        verify_fn=lambda token, client, nonce: {
            "iss": provider_auth.OPENAI_ISSUER, "sub": "subject-1", "email": "person@example.com"},
    )
    assert status["connected"] and status["account"] == "person@example.com"
    assert seen["data"]["client_id"] == "oaiapp-issued"
    assert seen["data"]["code_verifier"]
    assert stat.S_IMODE(os.stat(credentials).st_mode) == 0o600
    assert "access_token" not in status and "refresh_token" not in status


def test_chatgpt_callback_rejects_wrong_browser_session(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_auth, "CREDENTIALS_PATH", tmp_path / "providers.json")
    provider_auth._PENDING.clear()
    auth_url = provider_auth.begin_chatgpt_sign_in(
        "http://127.0.0.1:5055/auth/chatgpt/callback", "right-session")
    state = parse_qs(urlsplit(auth_url).query)["state"][0]
    try:
        provider_auth.complete_chatgpt_sign_in({"state": state, "code": "x", "client_id": "oaiapp-x"},
                                               "wrong-session")
        assert False, "expected callback rejection"
    except ValueError as exc:
        assert "browser session" in str(exc)
