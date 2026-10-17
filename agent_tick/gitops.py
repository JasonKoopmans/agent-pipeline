"""git operations, split so that credentials never touch the agent's workspace config.

- Clones pass the token as a one-shot `-c http.extraheader`, never stored.
- The workspace is treated as untrusted after the agent ran: publishing copies
  the branch into a fresh repository (whose config and hooks are ours) and
  pushes from there, so hooks, credential helpers or `url.insteadOf`
  rewrites planted in the workspace cannot run with the token.
"""

import base64
import shutil
import subprocess
import tempfile
from pathlib import Path

from agent_tick import log

NO_HOOKS = ["-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never"]


class GitError(RuntimeError):
    pass


def _auth(token: str) -> list[str]:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]


def git(*args: str, cwd: str | Path | None = None, token: str | None = None, check: bool = True) -> str:
    cmd = ["git", *NO_HOOKS, *(_auth(token) if token else []), *args]
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        safe = " ".join(a for a in args if "extraheader" not in a)
        raise GitError(f"git {safe} failed: {proc.stderr.strip()[:800]}")
    return proc.stdout.strip()


def url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def clone(repo: str, branch: str, dest: Path, token: str | None) -> None:
    git("clone", "--no-tags", "--branch", branch, url(repo), str(dest), token=token)
    strip_credentials(dest)


def fetch_branch(repo_dir: Path, repo: str, branch: str, token: str | None) -> None:
    git("fetch", "--no-tags", url(repo), f"+refs/heads/{branch}:refs/remotes/origin/{branch}", cwd=repo_dir, token=token)


def strip_credentials(repo_dir: Path) -> None:
    for key in ("http.extraheader", "http.https://github.com/.extraheader", "credential.helper"):
        git("config", "--unset-all", key, cwd=repo_dir, check=False)
    text = (repo_dir / ".git" / "config").read_text()
    if "AUTHORIZATION" in text or "x-access-token" in text:
        raise GitError("credential material left in .git/config")


def head(repo_dir: Path) -> str:
    return git("rev-parse", "HEAD", cwd=repo_dir)


def changed_files(repo_dir: Path, base: str, tip: str = "HEAD") -> list[str]:
    out = git("diff", "--name-only", f"{base}...{tip}", cwd=repo_dir)
    return [l for l in out.splitlines() if l]


def commit_all(repo_dir: Path, message: str, name: str, email: str) -> bool:
    """Commit anything the agent left uncommitted. Returns True if a commit was made."""
    git("add", "-A", cwd=repo_dir)
    if not git("status", "--porcelain", cwd=repo_dir):
        return False
    git("-c", f"user.name={name}", "-c", f"user.email={email}", "commit", "--no-verify", "-q", "-m", message, cwd=repo_dir)
    return True


def push_from_workspace(workspace_repo: Path, branch: str, expected_sha: str, repo: str, token: str, lease: str | None) -> str:
    """Push `expected_sha` to `branch` on GitHub without trusting the workspace.

    `lease` is the SHA the remote branch must currently have ("" = must not
    exist), so a push never clobbers commits made elsewhere.
    """
    clean = Path(tempfile.mkdtemp(prefix="publish-"))
    try:
        git("init", "-q", str(clean))
        # Fetching from a local path reads objects only; the source repo's
        # config and hooks are not used.
        git("fetch", "--no-tags", "-q", f"file://{workspace_repo}", f"+refs/heads/{branch}:refs/heads/{branch}", cwd=clean)
        got = git("rev-parse", f"refs/heads/{branch}", cwd=clean)
        if got != expected_sha:
            raise GitError(f"workspace branch moved: expected {expected_sha}, found {got}")
        lease_arg = f"--force-with-lease=refs/heads/{branch}:{lease or ''}"
        git("push", "-q", lease_arg, url(repo), f"refs/heads/{branch}:refs/heads/{branch}", cwd=clean, token=token)
        log.event("pushed", branch=branch, sha=expected_sha)
        return expected_sha
    finally:
        shutil.rmtree(clean, ignore_errors=True)
