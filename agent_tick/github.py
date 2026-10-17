"""Repo-locked GitHub client (REST over the standard library; no gh binary).

Every call uses an explicit `repos/<repo>/...` path. Nothing ever relies on a
CLI's repository resolution from git remotes, which in a fork checkout can
point at the upstream parent. A path outside the configured repo raises before
any network call, reads included.
"""

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from agent_tick import log

API = "https://api.github.com"
_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')


class GitHubError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class RepoGuardError(GitHubError):
    pass


Opener = Callable[..., Any]


class GitHub:
    def __init__(self, repo: str, *, dry_run: bool = False, token: str | None = None, opener: Opener = urllib.request.urlopen):
        self.repo = repo
        self._token = token
        self.dry_run = dry_run
        self._open = opener
        self.writes: list[dict[str, Any]] = []

    def _check(self, path: str) -> None:
        base = f"repos/{self.repo}"
        if path != base and not path.startswith(base + "/"):
            raise RepoGuardError(f"refusing GitHub path outside {self.repo}: {path}")
        if ".." in path.split("/") or "?" in path or "#" in path:
            raise RepoGuardError(f"refusing unsafe path: {path}")

    def _request(self, method: str, url: str, body: dict | None = None) -> tuple[Any, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agent-tick"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, timeout=30) as resp:
                raw = resp.read()
                link = resp.headers.get("Link", "") if resp.headers else ""
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read() or b"{}").get("message", "")
            except (ValueError, AttributeError):
                pass
            raise GitHubError(f"GitHub {method} {url.removeprefix(API)} failed: HTTP {exc.code} {detail}"[:500], exc.code) from None
        except urllib.error.URLError as exc:
            raise GitHubError(f"GitHub {method} {url.removeprefix(API)} failed: {exc.reason}") from None
        return (json.loads(raw) if raw else None), link

    def get(self, path: str, params: dict[str, str] | None = None, *, paginate: bool = False) -> Any:
        self._check(path)
        url = f"{API}/{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        if not paginate:
            return self._request("GET", url)[0]
        items: list[Any] = []
        prefix = f"{API}/{path}"
        while url:
            page, link = self._request("GET", url)
            items.extend(page if isinstance(page, list) else [page])
            match = _NEXT_RE.search(link)
            url = match.group(1) if match else ""
            if url and not url.startswith(prefix):
                raise RepoGuardError(f"pagination left {path}: {url}")
        return items

    def write(self, method: str, path: str, body: dict | None = None) -> Any:
        self._check(path)
        if method not in ("POST", "PATCH", "PUT", "DELETE"):
            raise ValueError(f"unsupported write method {method}")
        record = {"method": method, "path": path, "body": body}
        self.writes.append(record)
        if self.dry_run:
            log.event("dry_run_write", **record)
            return {"id": -len(self.writes), "dry_run": True}
        return self._request(method, f"{API}/{path}", body if method != "DELETE" else None)[0]

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
