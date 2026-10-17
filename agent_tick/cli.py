"""agent-tick command line.

    agent-tick prepare|agent|verify|publish --role <name>   one stage (one container each)
    agent-tick tick --role <name>                           all four in one process (local dev only)
    agent-tick setup                                        create labels, check branch, print token permissions
    agent-tick check-config                                 validate pipeline.toml and exit
"""

import argparse
import os
import sys
from pathlib import Path

from agent_tick import config, identity, log, pipeline, stages
from agent_tick.github import GitHub, GitHubError

DEFAULT_CONFIG = "/etc/agent/pipeline.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-tick")
    parser.add_argument("command", choices=["prepare", "agent", "verify", "publish", "tick", "setup", "check-config"])
    parser.add_argument("--role", help="role name from pipeline.toml")
    parser.add_argument("--config", default=os.environ.get("AGENT_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--workspace", default=os.environ.get("AGENT_WORKSPACE", "/work"))
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("AGENT_DRY_RUN") == "1",
                        help="read GitHub but print intended writes instead of making them")
    args = parser.parse_args(argv)

    try:
        cfg = config.load(args.config)
    except (OSError, config.ConfigError) as exc:
        log.event("config_error", error=str(exc))
        return 2
    if args.command == "check-config":
        log.event("config_ok", repo=cfg.repo, roles=sorted(cfg.roles), merge_mode=cfg.merge_mode)
        return 0

    if args.command == "setup":
        return setup(cfg, GitHub(cfg.repo, dry_run=args.dry_run, token=os.environ.get("GH_TOKEN")))

    if not args.role:
        parser.error("--role is required")
    role = cfg.role(args.role)
    token = None
    if args.command in ("prepare", "publish", "tick"):
        try:
            token = identity.github_token(cfg, role)
        except identity.IdentityError as exc:
            log.event("identity_error", error=str(exc))
            return 1
    gh = GitHub(cfg.repo, dry_run=args.dry_run, token=token)
    ws = stages.Workspace(
        Path(args.workspace),
        control=Path(os.environ["AGENT_CONTROL_DIR"]) if os.environ.get("AGENT_CONTROL_DIR") else None,
        verify=Path(os.environ["AGENT_VERIFY_DIR"]) if os.environ.get("AGENT_VERIFY_DIR") else None,
    )
    try:
        if args.command in ("prepare", "tick"):
            if not token:
                raise SystemExit("no GitHub credential: mount the role's App key or set GH_TOKEN")
            stages.prepare(cfg, role, gh, ws, token)
        if args.command in ("agent", "tick"):
            stages.run_agent(cfg, role, ws)
        if args.command in ("verify", "tick"):
            stages.verify(cfg, role, ws)
        if args.command in ("publish", "tick"):
            if not token:
                raise SystemExit("no GitHub credential: mount the role's App key or set GH_TOKEN")
            stages.publish(cfg, role, gh, ws, token)
    except (GitHubError, RuntimeError) as exc:
        log.event("stage_error", error=str(exc)[:1000], error_type=type(exc).__name__)
        return 1
    return 0


PAT_PERMISSIONS = {
    "writer (implementer, fixer, merger)": "Contents: read/write · Issues: read/write · Pull requests: read/write · Metadata: read · Checks: read · Commit statuses: read",
    "commenter (planner, designer, reviewers)": "Contents: read · Issues: read/write · Pull requests: read/write · Metadata: read · Checks: read",
}


def setup(cfg: config.Pipeline, gh: GitHub) -> int:
    """Idempotent: create missing labels, confirm the base branch, print token scopes."""
    try:
        meta = gh.get(gh.r())
        log.event("repo", full_name=meta.get("full_name"), fork=meta.get("fork"), has_issues=meta.get("has_issues"), visibility=meta.get("visibility"))
        if not meta.get("has_issues"):
            log.event("warning", message="Issues are disabled on this repository (default for forks): enable them in Settings")
        gh.get(gh.r(f"branches/{cfg.base_branch}"))
        log.event("base_branch_ok", branch=cfg.base_branch)
    except GitHubError as exc:
        log.event("setup_error", error=str(exc))
        return 1
    existing = {l["name"] for l in gh.get(gh.r("labels"), {"per_page": "100"}, paginate=True)}
    for name, (color, description) in pipeline.LABELS.items():
        if name in existing:
            continue
        gh.write("POST", gh.r("labels"), {"name": name, "color": color, "description": description})
        log.event("label_created", label=name)
    for who, perms in PAT_PERMISSIONS.items():
        log.event("token_permissions", token=who, repository=cfg.repo, permissions=perms, workflows="none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
