"""Repo-locked GitHub client.

Every call goes through `gh api` with an explicit `repos/<repo>/...` path. No
command ever relies on gh's repository resolution from git remotes, which in a
fork checkout can point at the upstream parent. A path outside the configured
repo raises before any network call, reads included.
"""

import json
import os
import subprocess
from collections.abc import Callable
from typing import Any

from agent_tick import log


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RepoGuardError(GitHubError):
    pass


Runner = Callable[..., subprocess.CompletedProcess]


class GitHub:
    def __init__(self, repo: str, *, dry_run: bool = False, runner: Runner = subprocess.run, token: str | None = None):
        self.repo = repo
        self._token = token
        self.dry_run = dry_run
        self._run = runner
        self.writes: list[dict[str, Any]] = []

    def _check(self, path: str) -> None:
        base = f"repos/{self.repo}"
        if path != base and not path.startswith(base + "/"):
            raise RepoGuardError(f"refusing GitHub path outside {self.repo}: {path}")
        if ".." in path.split("/"):
            raise RepoGuardError(f"refusing path traversal: {path}")

    def _gh(self, args: list[str], body: dict | None = None) -> Any:
        proc = self._run(
            ["gh", "api", *args],
            input=json.dumps(body) if body is not None else None,
            capture_output=True,
            text=True,
            check=False,
            env=os.environ | {"GH_TOKEN": self._token} if self._token else None,
        )
        if proc.returncode != 0:
            status = None
            try:
                status = int(json.loads(proc.stdout or "{}").get("status"))
            except (ValueError, TypeError, AttributeError):
                pass
            raise GitHubError(f"gh api {' '.join(args[:3])} failed: {proc.stderr.strip()[:500]}", status)
        out = proc.stdout.strip()
        return json.loads(out) if out else None

    def get(self, path: str, params: dict[str, str] | None = None, *, paginate: bool = False) -> Any:
        self._check(path)
        args = ["-X", "GET", path, "-H", "Accept: application/vnd.github+json"]
        for key, value in (params or {}).items():
            args += ["-f", f"{key}={value}"]
        if paginate:
            args += ["--paginate", "--slurp"]
            pages = self._gh(args) or []
            items: list[Any] = []
            for page in pages:
                items.extend(page if isinstance(page, list) else [page])
            return items
        return self._gh(args)

    def write(self, method: str, path: str, body: dict | None = None) -> Any:
        self._check(path)
        if method not in ("POST", "PATCH", "PUT", "DELETE"):
            raise ValueError(f"unsupported write method {method}")
        record = {"method": method, "path": path, "body": body}
        self.writes.append(record)
        if self.dry_run:
            log.event("dry_run_write", **record)
            return {"id": -len(self.writes), "dry_run": True}
        return self._gh(["-X", method, path, "--input", "-"], body or {})

    # Convenience wrappers; all stay inside self.repo.
    def r(self, suffix: str = "") -> str:
        return f"repos/{self.repo}" + (f"/{suffix}" if suffix else "")

    def comment(self, number: int, body: str) -> dict:
        return self.write("POST", self.r(f"issues/{number}/comments"), {"body": body})

    def edit_comment(self, comment_id: int, body: str) -> dict:
        return self.write("PATCH", self.r(f"issues/comments/{comment_id}"), {"body": body})

    def add_labels(self, number: int, *labels: str) -> None:
        if labels:
            self.write("POST", self.r(f"issues/{number}/labels"), {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self.write("DELETE", self.r(f"issues/{number}/labels/{label}"))
        except GitHubError as exc:
            if exc.status != 404:
                raise

    def comments(self, number: int) -> list[dict]:
        return self.get(self.r(f"issues/{number}/comments"), {"per_page": "100"}, paginate=True)
