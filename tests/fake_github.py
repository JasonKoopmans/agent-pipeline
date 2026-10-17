"""In-memory stand-in for the GitHub REST endpoints agent-tick uses."""

import itertools
import re
from datetime import UTC, datetime

from agent_tick.github import GitHub, GitHubError

OWNER = "owner"
REPO = "owner/app"


class FakeGitHub(GitHub):
    def __init__(self, repo: str = REPO, now: datetime | None = None):
        super().__init__(repo, opener=self._no_network)
        self.now = now or datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
        self.issues: dict[int, dict] = {}
        self.pulls: dict[int, dict] = {}
        self.comments_by: dict[int, list[dict]] = {}
        self.events: dict[int, list[dict]] = {}
        self.labels_repo: set[str] = set()
        self.check_runs: dict[str, list[dict]] = {}
        self.merged: list[dict] = []
        self._ids = itertools.count(1000)
        self._numbers = itertools.count(100)

    @staticmethod
    def _no_network(*a, **k):
        raise AssertionError("fake must not touch the network")

    # ------------------------------------------------------------ fixtures

    def add_issue(self, number: int, title="Do a thing", body="details", author=OWNER, labels=(), labeled_by=OWNER):
        self.issues[number] = {"number": number, "title": title, "body": body, "state": "open", "user": {"login": author}, "labels": [{"name": l} for l in labels]}
        self.comments_by.setdefault(number, [])
        self.events[number] = [{"event": "labeled", "label": {"name": l}, "actor": {"login": labeled_by}} for l in labels]
        return self.issues[number]

    def add_pr(self, number: int, issue: int, sha="a" * 40, author=OWNER, labels=(), base="ai-staging", head_repo=REPO):
        self.pulls[number] = {
            "number": number, "title": f"#{issue}: thing", "body": "", "state": "open",
            "user": {"login": author}, "labels": [{"name": l} for l in labels],
            "head": {"ref": f"ai/issue-{issue}", "sha": sha, "repo": {"full_name": head_repo}},
            "base": {"ref": base},
        }
        self.comments_by.setdefault(number, [])
        self.events.setdefault(number, [])
        return self.pulls[number]

    def add_comment(self, number: int, body: str, author=OWNER, created_at: datetime | None = None):
        c = {"id": next(self._ids), "body": body, "user": {"login": author}, "created_at": (created_at or self.now).isoformat().replace("+00:00", "Z"), "issue": number}
        self.comments_by.setdefault(number, []).append(c)
        return c

    def labels_of(self, number: int) -> set[str]:
        obj = self.issues.get(number) or self.pulls.get(number)
        return {l["name"] for l in obj["labels"]}

    # ------------------------------------------------------------ API

    def get(self, path, params=None, *, paginate=False):
        self._check(path)
        params = params or {}
        rel = path[len(f"repos/{self.repo}"):].lstrip("/")
        if rel == "":
            return {"full_name": self.repo, "fork": True, "has_issues": True, "visibility": "public"}
        if rel == "issues":
            want = params.get("labels")
            return [i for i in self.issues.values() if i["state"] == "open" and (not want or want in {l["name"] for l in i["labels"]})]
        if rel == "issues/comments":
            since = params.get("since")
            allc = [c for cs in self.comments_by.values() for c in cs]
            return [c for c in allc if not since or c["created_at"] >= since]
        if m := re.fullmatch(r"issues/(\d+)", rel):
            n = int(m.group(1))
            return self.issues.get(n) or self.pulls[n]
        if m := re.fullmatch(r"issues/(\d+)/events", rel):
            return list(self.events.get(int(m.group(1)), []))
        if m := re.fullmatch(r"issues/(\d+)/comments", rel):
            return list(self.comments_by.get(int(m.group(1)), []))
        if rel == "pulls":
            return [p for p in self.pulls.values() if p["state"] == "open" and p["base"]["ref"] == params.get("base", p["base"]["ref"])]
        if m := re.fullmatch(r"pulls/(\d+)", rel):
            return self.pulls[int(m.group(1))]
        if m := re.fullmatch(r"commits/(\w+)/check-runs", rel):
            return {"check_runs": self.check_runs.get(m.group(1), [])}
        if m := re.fullmatch(r"commits/(\w+)/status", rel):
            return {"state": "pending", "statuses": []}
        if m := re.fullmatch(r"branches/(.+)", rel):
            return {"name": m.group(1)}
        if rel == "labels":
            return [{"name": n} for n in self.labels_repo]
        raise GitHubError(f"fake: unhandled GET {rel}", 404)

    def write(self, method, path, body=None):
        self._check(path)
        self.writes.append({"method": method, "path": path, "body": body})
        rel = path[len(f"repos/{self.repo}"):].lstrip("/")
        if method == "POST" and (m := re.fullmatch(r"issues/(\d+)/comments", rel)):
            return self.add_comment(int(m.group(1)), body["body"])
        if method == "PATCH" and (m := re.fullmatch(r"issues/comments/(\d+)", rel)):
            for cs in self.comments_by.values():
                for c in cs:
                    if c["id"] == int(m.group(1)):
                        c["body"] = body["body"]
                        return c
            raise GitHubError("no comment", 404)
        if method == "POST" and (m := re.fullmatch(r"issues/(\d+)/labels", rel)):
            n = int(m.group(1))
            obj = self.issues.get(n) or self.pulls[n]
            for l in body["labels"]:
                if l not in {x["name"] for x in obj["labels"]}:
                    obj["labels"].append({"name": l})
                    self.events.setdefault(n, []).append({"event": "labeled", "label": {"name": l}, "actor": {"login": OWNER}})
            return obj["labels"]
        if method == "DELETE" and (m := re.fullmatch(r"issues/(\d+)/labels/(.+)", rel)):
            n = int(m.group(1))
            obj = self.issues.get(n) or self.pulls[n]
            before = len(obj["labels"])
            obj["labels"] = [x for x in obj["labels"] if x["name"] != m.group(2)]
            if len(obj["labels"]) == before:
                raise GitHubError("label not found", 404)
            return None
        if method == "POST" and rel == "pulls":
            n = next(self._numbers)
            issue = int(body["head"].rsplit("-", 1)[1])
            pr = self.add_pr(n, issue, sha="b" * 40)
            pr["title"], pr["body"] = body["title"], body["body"]
            return pr
        if method == "PUT" and (m := re.fullmatch(r"pulls/(\d+)/merge", rel)):
            self.merged.append({"pr": int(m.group(1)), **body})
            return {"merged": True}
        if method == "POST" and rel == "labels":
            self.labels_repo.add(body["name"])
            return body
        raise GitHubError(f"fake: unhandled {method} {rel}", 404)
