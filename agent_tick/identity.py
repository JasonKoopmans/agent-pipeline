"""GitHub credentials for the token-holding stages (prepare, publish).

Preferred: one GitHub App per role. The App's private key is mounted only
into `prepare` and `publish`; each stage mints its own installation token,
scoped to the single target repository and valid for one hour, so nothing
long-lived ever reaches a stage that runs model-written code.

Fallback (legacy / bootstrap): a fine-grained PAT in GH_TOKEN.
"""

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from agent_tick import log
from agent_tick.config import Pipeline, Role

API = "https://api.github.com"


class IdentityError(RuntimeError):
    pass


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, key_file: Path, now: float | None = None) -> str:
    """RS256 JWT for the App, signed with openssl (no third-party crypto)."""
    now = int(now if now is not None else time.time())
    header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    # iat backdated for clock skew; GitHub caps exp at 10 minutes.
    payload = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": str(app_id)}, separators=(",", ":")).encode())
    signing_input = f"{header}.{payload}".encode()
    proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(key_file)], input=signing_input, capture_output=True, check=False)
    if proc.returncode != 0:
        raise IdentityError("could not sign the App JWT (is the private key a PEM file?)")
    return f"{header}.{payload}.{_b64url(proc.stdout)}"


def _api(method: str, path: str, bearer: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}/{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agent-tick",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        hint = " (is the App installed on this repository?)" if exc.code == 404 and path.endswith("/installation") else ""
        raise IdentityError(f"GitHub App API {method} {path} failed: HTTP {exc.code}{hint}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise IdentityError(f"GitHub App API {method} {path} unreachable: {reason} (check the egress policy)") from None


def installation_token(cfg: Pipeline, role: Role, app_id: str, key_file: Path) -> str:
    jwt = app_jwt(app_id, key_file)
    app = _api("GET", "app", jwt)
    if role.app_slug and app.get("slug") != role.app_slug:
        # Guards against mounting the wrong role's key.
        raise IdentityError(f"App key belongs to {app.get('slug')!r}, role {role.name} expects {role.app_slug!r}")
    inst = _api("GET", f"repos/{cfg.repo}/installation", jwt)
    token = _api("POST", f"app/installations/{inst['id']}/access_tokens", jwt, {"repositories": [cfg.repo.split("/", 1)[1]]})
    log.event("app_token_minted", app=app.get("slug"), expires_at=token.get("expires_at"))
    return token["token"]


def github_token(cfg: Pipeline, role: Role) -> str | None:
    app_id = os.environ.get("GITHUB_APP_ID")
    key_file = os.environ.get("GITHUB_APP_PRIVATE_KEY_FILE")
    if app_id and key_file:
        return installation_token(cfg, role, app_id, Path(key_file))
    if role.app_slug:
        raise IdentityError(f"role {role.name} is configured for App {role.app_slug} but no App credentials are mounted")
    return os.environ.get("GH_TOKEN") or None
