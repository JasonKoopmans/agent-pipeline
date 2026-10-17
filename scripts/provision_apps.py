#!/usr/bin/env python3
"""Create one GitHub App per agent role with GitHub's App manifest flow.

GitHub requires a logged-in human to create an App, so this is as automated as
it gets: per role you click "Create GitHub App" once in the browser, then
"Install" on the target repository. Everything else is templated here:
names, permissions, no webhook, private, and the credentials land straight in
a SOPS-encrypted Kubernetes Secret. The private key is never printed.

    python3 scripts/provision_apps.py --prefix jk-agent --out <dir> \
        --roles planner implementer fixer review-quality review-security merger

Apps are per role, not per repository: install each App on every repository
its role works on. Re-running skips roles whose secret file already holds
credentials (committed placeholders are replaced).
"""

import argparse
import html
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

API = "https://api.github.com"

WRITER = {"contents": "write", "issues": "write", "pull_requests": "write", "checks": "read", "statuses": "read", "metadata": "read"}
COMMENTER = {"contents": "read", "issues": "write", "pull_requests": "write", "checks": "read", "metadata": "read"}
KIND_PERMISSIONS = {
    "implementer": WRITER, "fixer": WRITER, "merger": WRITER,
    "planner": COMMENTER, "designer": COMMENTER, "reviewer": COMMENTER,
}


def kind_of(role: str) -> str:
    return "reviewer" if role.startswith("review") else role


def manifest(name: str, role: str, port: int, homepage: str) -> dict:
    return {
        "name": name,
        "url": homepage,
        "description": f"agent-pipeline {role} role: acts on GitHub issues and pull requests for this role only.",
        "redirect_url": f"http://127.0.0.1:{port}/callback",
        "public": False,
        "default_permissions": KIND_PERMISSIONS[kind_of(role)],
        "default_events": [],
        "hook_attributes": {"url": homepage, "active": False},
    }


def create_app(name: str, role: str, owner_org: str | None, homepage: str) -> dict:
    """Run one manifest round trip and return GitHub's conversion result."""
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def do_GET(self):
            url = urlparse(self.path)
            if url.path == "/":
                target = f"https://github.com/organizations/{owner_org}/settings/apps/new" if owner_org else "https://github.com/settings/apps/new"
                body = (
                    f'<form id="f" method="post" action="{target}?state={state}">'
                    f'<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest(name, role, port, homepage)))}">'
                    f'<button>Create {html.escape(name)}</button></form>'
                    '<script>document.getElementById("f").submit()</script>'
                )
            elif url.path == "/callback":
                q = parse_qs(url.query)
                if q.get("state", [""])[0] != state:
                    body = "State mismatch; ignored."
                else:
                    result["code"] = q.get("code", [""])[0]
                    body = f"Created. Return to the terminal (next: install {html.escape(name)})."
                    done.set()
            else:
                self.send_response(404)
                self.end_headers()
                return
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"→ {name}: opening {url} — click 'Create GitHub App' in the browser.")
    webbrowser.open(url)
    if not done.wait(timeout=900):
        server.shutdown()
        raise SystemExit(f"timed out waiting for {name}")
    server.shutdown()

    req = urllib.request.Request(f"{API}/app-manifests/{result['code']}/conversions", method="POST",
                                 headers={"Accept": "application/vnd.github+json", "User-Agent": "agent-pipeline-provision"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def bot_user_id(slug: str) -> int | None:
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{API}/users/{slug}%5Bbot%5D", headers={"User-Agent": "agent-pipeline-provision"}), timeout=20) as resp:
            return json.loads(resp.read())["id"]
    except (urllib.error.URLError, KeyError):
        return None


def write_secret(path: Path, secret_name: str, app: dict, role: str) -> bool:
    """Write the Secret, encrypt it in place with sops. Returns True if encrypted."""
    pem = "\n".join("    " + line for line in app["pem"].strip().splitlines())
    text = (
        f"# GitHub App for the agent-pipeline `{role}` role: {app['html_url']}\n"
        "# Created by scripts/provision_apps.py. Mounted only into the prepare and publish containers.\n"
        "apiVersion: v1\nkind: Secret\nmetadata:\n"
        f"  name: {secret_name}\n"
        "stringData:\n"
        f"  app-id: \"{app['id']}\"\n"
        "  private-key: |\n"
        f"{pem}\n"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    if not shutil.which("sops"):
        return False
    proc = subprocess.run(["sops", "--encrypt", "--in-place", str(path)], capture_output=True, text=True)
    return proc.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prefix", required=True, help="App name prefix, globally unique on GitHub (e.g. jk-agent)")
    ap.add_argument("--roles", nargs="+", default=["planner", "implementer", "fixer", "review-quality", "review-security", "merger"])
    ap.add_argument("--out", required=True, type=Path, help="directory for the <role> app secret files (inside the GitOps repo, so .sops.yaml applies)")
    ap.add_argument("--org", help="create the Apps under this organization instead of your user account")
    ap.add_argument("--homepage", default="https://github.com/JasonKoopmans/agent-pipeline")
    args = ap.parse_args()

    # Refuse before creating anything: a private key written where sops has no
    # rule stays plaintext on disk (a relative --out from the wrong directory).
    args.out = args.out.expanduser().resolve()
    if not shutil.which("sops"):
        raise SystemExit("sops is not installed; nothing created")
    if not any((d / ".sops.yaml").is_file() for d in [args.out, *args.out.parents]):
        raise SystemExit(f"no .sops.yaml at or above {args.out}; run from the GitOps repo or pass an absolute --out inside it. Nothing created.")
    args.out.mkdir(parents=True, exist_ok=True)
    values: dict[str, dict] = {}
    for role in args.roles:
        if kind_of(role) not in KIND_PERMISSIONS:
            print(f"skip {role}: unknown role kind", file=sys.stderr)
            continue
        secret_name = f"agent-app-{role}"
        path = args.out / f"{secret_name}.sops.yaml"
        if path.exists():
            if "PLACEHOLDER_FILL" not in path.read_text():
                print(f"skip {role}: {path} already holds credentials")
                continue
            path.unlink()  # a committed placeholder: replace it
        name = f"{args.prefix}-{role}"
        app = create_app(name, role, args.org, args.homepage)
        encrypted = write_secret(path, secret_name, app, role)
        uid = bot_user_id(app["slug"])
        values[role] = {"appSlug": app["slug"], "appUserId": uid, "appSecret": secret_name}
        print(f"✓ {app['slug']} (id {app['id']}) → {path}" + ("" if encrypted else "  ⚠ NOT ENCRYPTED: run `sops -e -i` on it before committing"))
        print(f"  install it on the target repo: https://github.com/apps/{app['slug']}/installations/new")
    if values:
        print("\nHelmRelease values (roles.<name>):")
        print(json.dumps(values, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
