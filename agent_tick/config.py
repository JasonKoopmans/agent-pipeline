"""Pipeline configuration (pipeline.toml).

This file is the trusted side of the system: it lives in the cluster's GitOps
repo (rendered into a ConfigMap by the chart), never in the target repo, so a
pull request written by the agent cannot change which roles run, the review
cap, the merge mode or the test gate.
"""

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agent_tick.engines import NAMES as ENGINE_NAMES

KINDS = ("planner", "designer", "implementer", "fixer", "reviewer", "merger")
MERGE_MODES = ("off", "staging")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ROLE_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Role:
    name: str
    kind: str
    engine: str = "claude"
    model: str = "sonnet"
    effort: str | None = None
    max_turns: int = 40
    max_budget_usd: float = 3.0
    timeout_minutes: int = 30
    required: bool = True
    prompt: str = ""
    mcp: str | None = None
    # GitHub App that acts for this role (None: the PAT owner acts, legacy mode).
    app_slug: str | None = None
    app_user_id: int | None = None

    @property
    def login(self) -> str | None:
        return f"{self.app_slug}[bot]" if self.app_slug else None

    @property
    def claim_group(self) -> str:
        # Implementer, fixer and merger all write to the same branch/PR, so
        # they exclude each other. Each reviewer lens only excludes itself,
        # which is what lets lenses run in parallel on one head SHA.
        if self.kind in ("implementer", "fixer", "merger"):
            return "writer"
        if self.kind == "reviewer":
            return f"review:{self.name}"
        return self.kind

    @property
    def uses_model(self) -> bool:
        return self.kind != "merger"

    @property
    def writes_code(self) -> bool:
        return self.kind in ("implementer", "fixer")


@dataclass(frozen=True)
class Pipeline:
    repo: str
    owner: str
    base_branch: str
    roles: dict[str, Role]
    review_cap: int = 3
    daily_session_cap: int = 8
    merge_mode: str = "off"
    plan_gate: bool = True
    stale_claim_minutes: int = 75
    # Claude Code's bubblewrap sandbox needs unprivileged user namespaces, which
    # Talos blocks; when it cannot start, the model just runs commands outside it.
    sandbox: bool = False
    forbidden_paths: tuple[str, ...] = (".github/", "CLAUDE.md", ".claude/", ".agent/", ".mcp.json")
    gate_commands: tuple[tuple[str, ...], ...] = ()
    git_author_name: str = "agent-pipeline"
    git_author_email: str = "agent-pipeline@users.noreply.github.com"
    prompts_dir: str = "/opt/agent/prompts"
    mcp_dir: str = "/opt/agent/mcp"
    extra: dict = field(default_factory=dict)

    def role(self, name: str) -> Role:
        try:
            return self.roles[name]
        except KeyError:
            raise ConfigError(f"unknown role {name!r}; configured: {sorted(self.roles)}") from None

    @property
    def required_reviewers(self) -> list[str]:
        return sorted(r.name for r in self.roles.values() if r.kind == "reviewer" and r.required)

    @property
    def agent_logins(self) -> frozenset[str]:
        """Logins whose comment markers are trusted as pipeline state.

        Instructions are a separate question: only the owner's own comments
        (without markers) are ever treated as instructions.
        """
        logins = {r.login.lower() for r in self.roles.values() if r.login}
        if any(not r.login for r in self.roles.values()):
            logins.add(self.owner.lower())  # PAT mode: the owner's token writes state
        return frozenset(logins)

    def git_identity(self, role: "Role") -> tuple[str, str]:
        if role.app_slug:
            email = f"{role.app_user_id}+{role.login}@users.noreply.github.com" if role.app_user_id else f"{role.login}@users.noreply.github.com"
            return role.login, email
        return self.git_author_name, self.git_author_email

    @property
    def reviewers(self) -> list[str]:
        return sorted(r.name for r in self.roles.values() if r.kind == "reviewer")


def load(path: str | Path) -> Pipeline:
    with open(path, "rb") as fh:
        return parse(tomllib.load(fh))


def parse(data: dict) -> Pipeline:
    def need(key: str):
        if key not in data:
            raise ConfigError(f"missing required key {key!r}")
        return data[key]

    repo = need("repo")
    if not isinstance(repo, str) or not _REPO_RE.match(repo):
        raise ConfigError(f"repo must be owner/name, got {repo!r}")
    owner = need("owner")
    base_branch = need("base_branch")
    if not isinstance(base_branch, str) or not base_branch or base_branch.startswith("-"):
        raise ConfigError("base_branch must be a branch name")

    raw_roles = need("roles")
    if not isinstance(raw_roles, dict) or not raw_roles:
        raise ConfigError("roles must be a non-empty table")
    roles: dict[str, Role] = {}
    for name, spec in raw_roles.items():
        if not _ROLE_RE.match(name):
            raise ConfigError(f"role name {name!r} must be lowercase, digits and hyphens")
        spec = dict(spec)
        kind = spec.pop("kind", None)
        if kind not in KINDS:
            raise ConfigError(f"role {name}: kind must be one of {KINDS}, got {kind!r}")
        spec.setdefault("prompt", name)
        allowed = set(Role.__dataclass_fields__) - {"name", "kind"}
        unknown = set(spec) - allowed
        if unknown:
            raise ConfigError(f"role {name}: unknown keys {sorted(unknown)}")
        role = Role(name=name, kind=kind, **spec)
        if role.engine not in ENGINE_NAMES:
            raise ConfigError(f"role {name}: engine must be one of {ENGINE_NAMES}, got {role.engine!r}")
        roles[name] = role

    kinds = [r.kind for r in roles.values()]
    for single in ("planner", "designer", "implementer", "fixer", "merger"):
        if kinds.count(single) > 1:
            raise ConfigError(f"only one {single} role is allowed")

    merge_mode = data.get("merge_mode", "off")
    if merge_mode not in MERGE_MODES:
        raise ConfigError(f"merge_mode must be one of {MERGE_MODES}")

    review_cap = int(data.get("review_cap", 3))
    if review_cap < 1:
        raise ConfigError("review_cap must be >= 1")

    gates = data.get("gate_commands", [])
    if not all(isinstance(c, list) and c and all(isinstance(a, str) for a in c) for c in gates):
        raise ConfigError("gate_commands must be a list of argv lists, e.g. [[\"dotnet\", \"test\"]]")

    known = set(Pipeline.__dataclass_fields__) - {"roles", "extra", "gate_commands", "forbidden_paths"}
    simple = {k: v for k, v in data.items() if k in known}
    simple.update(merge_mode=merge_mode, review_cap=review_cap)
    extra = {k: v for k, v in data.items() if k not in known | {"roles", "gate_commands", "forbidden_paths"}}
    kwargs = dict(simple, roles=roles, gate_commands=tuple(tuple(c) for c in gates), extra=extra)
    if "forbidden_paths" in data:
        kwargs["forbidden_paths"] = tuple(data["forbidden_paths"])
    return Pipeline(**kwargs)
