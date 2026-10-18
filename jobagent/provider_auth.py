"""Local credentials for premium resume-editor providers.

ChatGPT plan access uses OpenAI's public-client OAuth + PKCE flow. Gemini uses
an API key from the environment or this protected local credential file. No
secret from this module is ever returned to the dashboard browser.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode

import requests

from .config import DATA


OPENAI_ISSUER = "https://auth.openai.com"
OPENAI_AUTHORIZE = f"{OPENAI_ISSUER}/api/accounts/authorize"
OPENAI_TOKEN = f"{OPENAI_ISSUER}/api/accounts/oauth/token"
OPENAI_REVOKE = f"{OPENAI_ISSUER}/api/accounts/oauth/revoke"
OPENAI_DISCOVERY = f"{OPENAI_ISSUER}/.well-known/openid-configuration"
OPENAI_RESOURCE = "https://api.openai.com/v1"
OPENAI_SCOPES = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CHATGPT_USAGE_URL = "https://chatgpt.com/#settings/Usage"
GEMINI_MODELS_URL = "https://generativelanguage.googleapis.com/v1beta/models"

CREDENTIALS_PATH = DATA / "provider_credentials.json"
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.RLock()


@contextmanager
def _credential_lock():
    """Serialize token rotation across dashboard and worker processes."""
    import fcntl

    lock_path = CREDENTIALS_PATH.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
            fcntl.flock(handle, fcntl.LOCK_UN)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_unlocked() -> dict:
    try:
        value = json.loads(CREDENTIALS_PATH.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_unlocked(value: dict) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CREDENTIALS_PATH.with_name(f".{CREDENTIALS_PATH.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, CREDENTIALS_PATH)
        os.chmod(CREDENTIALS_PATH, 0o600)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_credentials() -> dict:
    with _credential_lock():
        return _read_unlocked()


def _host_id_unlocked(data: dict) -> str:
    chatgpt = data.setdefault("chatgpt", {})
    host_id = chatgpt.get("host_id")
    if not host_id:
        host_id = f"urn:uuid:{uuid.uuid4()}"
        chatgpt["host_id"] = host_id
    return host_id


def _active_profile_unlocked(data: dict) -> tuple[str | None, dict | None]:
    chatgpt = data.get("chatgpt") or {}
    profile_id = chatgpt.get("active_profile")
    profile = (chatgpt.get("profiles") or {}).get(profile_id) if profile_id else None
    return profile_id, profile


def chatgpt_status() -> dict:
    data = _read_credentials()
    _, profile = _active_profile_unlocked(data)
    if not profile:
        return {"connected": False, "sharing": False, "account": None, "models": []}
    scopes = set(profile.get("scopes") or [])
    sharing = "chatgpt.tokens.use.direct" in scopes
    connected = bool(sharing and (profile.get("access_token") or profile.get("refresh_token")))
    return {
        "connected": connected,
        "sharing": sharing,
        "account": profile.get("email") or profile.get("name") or "ChatGPT account",
        "models": list(profile.get("models") or []),
        "client_registered": bool(profile.get("client_id")),
    }


def begin_chatgpt_sign_in(redirect_uri: str, browser_session: str, app_name: str = "Job Agent") -> str:
    """Create a one-use OAuth transaction and return the OpenAI authorization URL."""
    with _credential_lock():
        data = _read_unlocked()
        host_id = _host_id_unlocked(data)
        profile_id, profile = _active_profile_unlocked(data)
        _write_unlocked(data)

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    client_id = profile.get("client_id") if profile else "dynamic_agent_client"
    transaction = {
        "state": state,
        "nonce": nonce,
        "verifier": verifier,
        "redirect_uri": redirect_uri,
        "browser_session": browser_session,
        "client_id": client_id,
        "profile_id": profile_id,
        "expires_at": time.time() + 600,
    }
    with _PENDING_LOCK:
        now = time.time()
        for key in [k for k, v in _PENDING.items() if v.get("expires_at", 0) <= now]:
            _PENDING.pop(key, None)
        _PENDING[state] = transaction

    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": OPENAI_SCOPES,
        "resource": OPENAI_RESOURCE,
        "state": state,
        "nonce": nonce,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "ext_agent_host_id": host_id,
    }
    if profile:
        if profile.get("id_token"):
            params["id_token_hint"] = profile["id_token"]
        elif profile.get("email"):
            params["login_hint"] = profile["email"]
    else:
        params["agent_name_hint"] = app_name
    return f"{OPENAI_AUTHORIZE}?{urlencode(params)}"


def verify_openai_id_token(id_token: str, client_id: str, nonce: str) -> dict:
    """Verify signature and OIDC claims using OpenAI discovery metadata."""
    try:
        import jwt
    except ImportError as exc:  # pragma: no cover - exercised only in a real OAuth callback
        raise RuntimeError("PyJWT is required for ChatGPT sign-in; run pip install -r requirements.txt") from exc

    discovery_response = requests.get(OPENAI_DISCOVERY, timeout=20)
    discovery_response.raise_for_status()
    discovery = discovery_response.json()
    issuer = discovery.get("issuer")
    jwks_uri = discovery.get("jwks_uri")
    supported = set(discovery.get("id_token_signing_alg_values_supported") or [])
    algorithm = jwt.get_unverified_header(id_token).get("alg")
    if not issuer or not jwks_uri or not algorithm or algorithm == "none" or (supported and algorithm not in supported):
        raise ValueError("OpenAI returned unsupported identity-token metadata")
    signing_key = jwt.PyJWKClient(jwks_uri).get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=[algorithm],
        audience=client_id,
        issuer=issuer,
        leeway=5,
        options={"require": ["sub", "exp", "iat"]},
    )
    if claims.get("nonce") != nonce:
        raise ValueError("ChatGPT identity-token nonce did not match")
    if not claims.get("sub"):
        raise ValueError("ChatGPT identity token did not contain a subject")
    return claims


def complete_chatgpt_sign_in(params, browser_session: str | None, request_fn=requests.post,
                             verify_fn=verify_openai_id_token) -> dict:
    """Consume a callback, exchange its code, validate identity, and save credentials."""
    state = params.get("state", "")
    with _PENDING_LOCK:
        transaction = _PENDING.pop(state, None)
    if not transaction or transaction.get("expires_at", 0) <= time.time():
        raise ValueError("ChatGPT sign-in expired or had invalid state")
    if not browser_session or not secrets.compare_digest(browser_session, transaction["browser_session"]):
        raise ValueError("ChatGPT sign-in browser session did not match")
    if params.get("error"):
        raise ValueError(f"ChatGPT sign-in was not completed: {params.get('error')}")
    code = params.get("code")
    if not code:
        raise ValueError("ChatGPT sign-in returned no authorization code")

    expected_client = transaction["client_id"]
    callback_client = params.get("client_id")
    if expected_client == "dynamic_agent_client":
        if not callback_client or callback_client == "dynamic_agent_client":
            raise ValueError("ChatGPT did not return an issued client ID")
        client_id = callback_client
    else:
        if callback_client and callback_client != expected_client:
            raise ValueError("ChatGPT returned a different client registration")
        client_id = expected_client

    response = request_fn(
        OPENAI_TOKEN,
        timeout=30,
        headers={"Accept": "application/json"},
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": transaction["verifier"],
            "redirect_uri": transaction["redirect_uri"],
            "resource": OPENAI_RESOURCE,
        },
    )
    response.raise_for_status()
    tokens = response.json()
    if not all(isinstance(tokens.get(k), str) and tokens.get(k) for k in ("id_token", "access_token", "refresh_token")):
        raise ValueError("ChatGPT token response was incomplete")
    identity = verify_fn(tokens["id_token"], client_id, transaction["nonce"])
    scopes = str(tokens.get("scope") or params.get("scope") or "").split()
    now = time.time()
    profile_key = hashlib.sha256(f"{identity.get('iss', OPENAI_ISSUER)}|{identity['sub']}|{client_id}".encode()).hexdigest()

    with _credential_lock():
        data = _read_unlocked()
        host_id = _host_id_unlocked(data)
        chatgpt = data.setdefault("chatgpt", {})
        profiles = chatgpt.setdefault("profiles", {})
        old_id = transaction.get("profile_id")
        if old_id and old_id in profiles:
            old = profiles[old_id]
            if old.get("subject") != identity["sub"] or old.get("client_id") != client_id:
                raise ValueError("ChatGPT account did not match the selected registration")
        previous = profiles.get(profile_key) or {}
        profiles[profile_key] = {
            "email": identity.get("email"),
            "name": identity.get("name"),
            "issuer": identity.get("iss", OPENAI_ISSUER),
            "subject": identity["sub"],
            "client_id": client_id,
            "ext_agent_host_id": host_id,
            "id_token": tokens["id_token"],
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_at": now + int(tokens.get("expires_in") or 3600),
            "earliest_refresh_at": tokens.get("earliest_refresh_at"),
            "scopes": scopes,
            "saved_at": now,
            "models": previous.get("models") or [],
        }
        if old_id and old_id != profile_key:
            profiles.pop(old_id, None)
        chatgpt["active_profile"] = profile_key
        _write_unlocked(data)
    return chatgpt_status()


def chatgpt_access_token(request_fn=requests.post) -> str:
    """Return a current access token, rotating the refresh token when necessary."""
    with _credential_lock():
        data = _read_unlocked()
        _, profile = _active_profile_unlocked(data)
        if not profile or "chatgpt.tokens.use.direct" not in set(profile.get("scopes") or []):
            raise RuntimeError("ChatGPT plan usage is not connected")
        if profile.get("access_token") and float(profile.get("expires_at") or 0) > time.time() + 90:
            return profile["access_token"]
        if not profile.get("refresh_token") or not profile.get("client_id"):
            raise RuntimeError("ChatGPT sign-in needs to be renewed")
        response = request_fn(
            OPENAI_TOKEN,
            timeout=30,
            headers={"Accept": "application/json"},
            data={
                "grant_type": "refresh_token",
                "client_id": profile["client_id"],
                "refresh_token": profile["refresh_token"],
                "resource": OPENAI_RESOURCE,
            },
        )
        response.raise_for_status()
        tokens = response.json()
        if not tokens.get("access_token") or not tokens.get("refresh_token"):
            raise RuntimeError("ChatGPT token refresh was incomplete; reconnect the account")
        profile["access_token"] = tokens["access_token"]
        profile["refresh_token"] = tokens["refresh_token"]
        profile["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
        profile["earliest_refresh_at"] = tokens.get("earliest_refresh_at")
        if tokens.get("scope"):
            profile["scopes"] = str(tokens["scope"]).split()
        profile["saved_at"] = time.time()
        _write_unlocked(data)
        return profile["access_token"]


def refresh_chatgpt_models(request_fn=requests.get, force: bool = True) -> list[dict]:
    status = chatgpt_status()
    if status["models"] and not force:
        return status["models"]
    token = chatgpt_access_token()
    response = request_fn(
        f"{OPENAI_RESOURCE}/models",
        timeout=30,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    response.raise_for_status()
    raw = response.json()
    rows = raw.get("models") or raw.get("data") or []
    models = []
    for item in rows:
        if item.get("visibility") not in (None, "list"):
            continue
        slug = item.get("slug") or item.get("id")
        if slug:
            models.append({"slug": slug, "display_name": item.get("display_name") or slug})
    with _credential_lock():
        data = _read_unlocked()
        _, profile = _active_profile_unlocked(data)
        if profile is not None:
            profile["models"] = models
            _write_unlocked(data)
    return models


def disconnect_chatgpt(revoke_fn=requests.post, discovery_fn=requests.get) -> bool:
    """Revoke renewable access when possible, then clear local tokens."""
    with _credential_lock():
        data = _read_unlocked()
        _, profile = _active_profile_unlocked(data)
        confirmed = False
        if profile and profile.get("refresh_token") and profile.get("client_id"):
            endpoint = OPENAI_REVOKE
            try:
                discovery = discovery_fn(OPENAI_DISCOVERY, timeout=20)
                discovery.raise_for_status()
                endpoint = discovery.json().get("revocation_endpoint") or endpoint
            except Exception:
                pass
            try:
                response = revoke_fn(
                    endpoint,
                    timeout=20,
                    data={"token": profile["refresh_token"], "token_type_hint": "refresh_token",
                          "client_id": profile["client_id"]},
                )
                response.raise_for_status()
                confirmed = True
            except Exception:
                confirmed = False
        if profile:
            for key in ("access_token", "refresh_token", "id_token", "expires_at", "earliest_refresh_at"):
                profile.pop(key, None)
            _write_unlocked(data)
    return confirmed


def gemini_api_key() -> str | None:
    env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if env:
        return env
    return (_read_credentials().get("gemini") or {}).get("api_key")


def gemini_key_source() -> str | None:
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "environment"
    return "saved locally" if (_read_credentials().get("gemini") or {}).get("api_key") else None


def validate_gemini_key(api_key: str, request_fn=requests.get) -> None:
    response = request_fn(GEMINI_MODELS_URL, timeout=30,
                          headers={"x-goog-api-key": api_key, "Accept": "application/json"})
    response.raise_for_status()


def save_gemini_key(api_key: str) -> None:
    if not api_key or len(api_key.strip()) < 12:
        raise ValueError("enter a valid Gemini API key")
    with _credential_lock():
        data = _read_unlocked()
        data["gemini"] = {"api_key": api_key.strip(), "saved_at": time.time()}
        _write_unlocked(data)


def clear_gemini_key() -> bool:
    with _credential_lock():
        data = _read_unlocked()
        removed = bool((data.get("gemini") or {}).get("api_key"))
        data.pop("gemini", None)
        _write_unlocked(data)
    return removed
