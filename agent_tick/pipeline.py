"""The state machine: what each role picks, how claims work, and how reviews aggregate.

GitHub is the only state store. Labels say where an issue or PR stands;
owner-authored comments carrying markers record claims, plans, verdicts and
round history. Losing a pod never loses state.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from agent_tick import log, markers
from agent_tick.config import Pipeline, Role
from agent_tick.github import GitHub

L_READY = "ai-ready"
L_PLANNED = "ai-planned"
L_PLAN_APPROVED = "ai-plan-approved"
L_NEEDS_DESIGN = "needs-design"
L_DESIGNED = "ai-designed"
L_PR_OPENED = "ai-pr-opened"
L_FIX_NEEDED = "ai-fix-needed"
L_AWAITING_MERGE = "ai-awaiting-merge"
L_MERGED = "ai-merged"
L_BLOCKED = "ai-blocked"
L_NEEDS_INPUT = "ai-needs-input"

LABELS = {
    L_READY: ("1d76db", "Enabled for the agent pipeline (remove to pause)"),
    L_PLANNED: ("c5def5", "Planner posted a plan"),
    L_PLAN_APPROVED: ("0e8a16", "Owner approved the plan; implementation may start"),
    L_NEEDS_DESIGN: ("d4c5f9", "Needs a design step before implementation"),
    L_DESIGNED: ("c5def5", "Designer posted a design"),
    L_PR_OPENED: ("0e8a16", "Agent opened a PR for this issue"),
    L_FIX_NEEDED: ("fbca04", "Reviewers requested changes; fixer will act"),
    L_AWAITING_MERGE: ("5319e7", "All required reviewers approved the head SHA"),
    L_MERGED: ("0e8a16", "Agent merged the reviewed SHA"),
    L_BLOCKED: ("d73a4a", "Agent stopped; needs a human"),
    L_NEEDS_INPUT: ("fbca04", "Agent asked a question; reply in a comment to continue"),
}

BRANCH_PREFIX = "ai/issue-"
_BRANCH_RE = re.compile(r"^ai/issue-(\d+)$")


def branch_for(issue: int) -> str:
    return f"{BRANCH_PREFIX}{issue}"


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def label_names(obj: dict) -> set[str]:
    return {l["name"] if isinstance(l, dict) else l for l in obj.get("labels", [])}


@dataclass
class Trusted:
    """A comment from the owner (instructions) or an agent identity (state)."""

    id: int
    created_at: datetime
    body: str
    markers: list[dict[str, Any]]
    author: str = ""

    @property
    def is_instruction(self) -> bool:
        """Owner-written text, not an agent message."""
        return not self.markers


def trusted_comments(comments: list[dict], cfg: Pipeline) -> list[Trusted]:
    """Keep agent comments (markers trusted) and owner comments (instructions).

    Anyone else's comments are dropped entirely. A marker is only honored from
    an agent login, so a human cannot forge pipeline state, and an agent's
    comment is never read as an instruction.
    """
    owner = cfg.owner.lower()
    agents = cfg.agent_logins
    out = []
    for c in comments:
        login = (c.get("user") or {}).get("login", "").lower()
        found = markers.decode(c.get("body"))
        if login in agents and found:
            out.append(Trusted(c["id"], parse_ts(c["created_at"]), c.get("body") or "", found, login))
        elif login == owner and not found:
            out.append(Trusted(c["id"], parse_ts(c["created_at"]), c.get("body") or "", [], login))
    return sorted(out, key=lambda t: t.id)


def awaiting_input(trusted: list[Trusted]) -> bool:
    """True if an agent asked a question that the owner has not answered yet."""
    asked = find(trusted, "needs_input")
    if not asked:
        return False
    last_q = asked[-1][0].id
    return not any(t.is_instruction and t.id > last_q for t in trusted)


def find(trusted: list[Trusted], kind: str, **match: Any) -> list[tuple[Trusted, dict]]:
    hits = []
    for t in trusted:
        for m in t.markers:
            if m.get("kind") == kind and all(m.get(k) == v for k, v in match.items()):
                hits.append((t, m))
    return hits


def owner_applied_label(gh: GitHub, cfg: Pipeline, number: int, label: str) -> bool:
    """True if the label's most recent 'labeled' event was by the owner."""
    events = gh.get(gh.r(f"issues/{number}/events"), {"per_page": "100"}, paginate=True)
    last = None
    for ev in events:
        if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == label:
            last = ev
    return bool(last) and (last.get("actor") or {}).get("login", "").lower() == cfg.owner.lower()


# ---------------------------------------------------------------- claims


@dataclass
class Claim:
    comment_id: int
    marker: dict[str, Any]
    body: str


def active_claims(trusted: list[Trusted], group: str, now: datetime, stale_minutes: int) -> list[tuple[Trusted, dict]]:
    cutoff = now - timedelta(minutes=stale_minutes)
    return [
        (t, m)
        for t, m in find(trusted, "claim", group=group)
        if not m.get("released") and t.created_at >= cutoff
    ]


def reclaim_stale(gh: GitHub, cfg: Pipeline, number: int, trusted: list[Trusted], now: datetime) -> int:
    """Release claims older than the stale window (their pod died)."""
    cutoff = now - timedelta(minutes=cfg.stale_claim_minutes)
    count = 0
    for t, m in find(trusted, "claim"):
        if not m.get("released") and t.created_at < cutoff:
            release(gh, Claim(t.id, m, t.body), "stale: no result within the claim window, reclaimed")
            log.event("claim_reclaimed", target=number, stale_run_id=m.get("run_id"))
            count += 1
    return count


def claim_body(role: Role, run_id: str, marker: dict, note: str) -> str:
    return f"🤖 **{role.name}** — {note} · run `{run_id}`\n\n{markers.encode(marker)}"


def try_claim(gh: GitHub, cfg: Pipeline, role: Role, number: int, run_id: str, now: datetime, extra: dict | None = None) -> Claim | None:
    marker = {
        "kind": "claim",
        "group": role.claim_group,
        "role": role.name,
        "role_kind": role.kind,
        "run_id": run_id,
        "ts": now.isoformat(timespec="seconds"),
        **(extra or {}),
    }
    body = claim_body(role, run_id, marker, "started")
    created = gh.comment(number, body)
    claim = Claim(int(created.get("id", 0)), marker, body)
    if gh.dry_run:
        return claim
    # Re-read: the oldest active claim in the group wins. Two pods that post
    # within the same second both see the same ordering and agree.
    trusted = trusted_comments(gh.comments(number), cfg)
    actives = active_claims(trusted, role.claim_group, now + timedelta(seconds=1), cfg.stale_claim_minutes)
    if actives and actives[0][1].get("run_id") == run_id:
        return claim
    release(gh, claim, "claim lost to a concurrent run")
    log.event("claim_lost", target=number)
    return None


def release(gh: GitHub, claim: Claim, outcome: str) -> None:
    marker = dict(claim.marker, released=True, outcome=outcome[:300])
    body = claim_body_from(claim, marker, outcome)
    gh.edit_comment(claim.comment_id, body)


def claim_body_from(claim: Claim, marker: dict, outcome: str) -> str:
    first = claim.body.split("\n", 1)[0].replace("— started", f"— {outcome}")
    return f"{first}\n\n{markers.encode(marker)}"


def sessions_today(gh: GitHub, cfg: Pipeline, now: datetime) -> int:
    """Model sessions started today (UTC), counted from claim markers across the repo."""
    midnight = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    comments = gh.get(gh.r("issues/comments"), {"since": midnight.isoformat().replace("+00:00", "Z"), "per_page": "100"}, paginate=True)
    count = 0
    for t in trusted_comments(comments, cfg):
        if t.created_at < midnight:
            continue
        for m in t.markers:
            if m.get("kind") == "claim" and m.get("role_kind") != "merger" and m.get("outcome") != "claim lost to a concurrent run":
                count += 1
    return count


# ---------------------------------------------------------------- picking


@dataclass
class Unit:
    """One piece of work for a role."""

    target: int  # issue or PR number the claim is posted on
    issue: dict
    pr: dict | None = None
    trusted: list[Trusted] = field(default_factory=list)  # comments on target

    @property
    def issue_number(self) -> int:
        return self.issue["number"]

    @property
    def head_sha(self) -> str | None:
        return self.pr["head"]["sha"] if self.pr else None


def _open_ai_ready_issues(gh: GitHub, cfg: Pipeline) -> list[dict]:
    issues = gh.get(gh.r("issues"), {"state": "open", "labels": L_READY, "per_page": "100", "sort": "created", "direction": "asc"}, paginate=True)
    return [i for i in issues if "pull_request" not in i and (i.get("user") or {}).get("login", "").lower() == cfg.owner.lower()]


def _agent_prs(gh: GitHub, cfg: Pipeline) -> list[dict]:
    pulls = gh.get(gh.r("pulls"), {"state": "open", "base": cfg.base_branch, "per_page": "100", "sort": "created", "direction": "asc"}, paginate=True)
    out = []
    for p in pulls:
        if not _BRANCH_RE.match(p["head"]["ref"]):
            continue
        if (p.get("user") or {}).get("login", "").lower() not in cfg.agent_logins:
            continue  # only PRs the implementer opened
        if (p["head"].get("repo") or {}).get("full_name", "").lower() != cfg.repo.lower():
            continue  # never act on a PR from another fork
        out.append(p)
    return out


def issue_for_pr(gh: GitHub, cfg: Pipeline, pr: dict) -> dict | None:
    match = _BRANCH_RE.match(pr["head"]["ref"])
    if not match:
        return None
    issue = gh.get(gh.r(f"issues/{match.group(1)}"))
    if issue.get("state") != "open" or L_READY not in label_names(issue):
        return None  # removing ai-ready from the issue pauses its PR too
    return issue


def verdicts(trusted: list[Trusted], sha: str) -> dict[str, dict]:
    """Latest verdict per reviewer role for one head SHA."""
    latest: dict[str, dict] = {}
    for _, m in find(trusted, "verdict", sha=sha):
        latest[m["role"]] = m
    return latest


def reviewed_shas(trusted: list[Trusted], cfg: Pipeline) -> list[str]:
    """SHAs that got a complete set of required verdicts, oldest first: the round history."""
    order: list[str] = []
    for _, m in find(trusted, "verdict"):
        if m["sha"] not in order:
            order.append(m["sha"])
    required = set(cfg.required_reviewers)
    return [s for s in order if required <= set(verdicts(trusted, s))]


def candidates(gh: GitHub, cfg: Pipeline, role: Role) -> list[Unit]:
    if role.kind in ("planner", "designer", "implementer"):
        return _issue_candidates(gh, cfg, role)
    return _pr_candidates(gh, cfg, role)


def _issue_candidates(gh: GitHub, cfg: Pipeline, role: Role) -> list[Unit]:
    out = []
    open_heads = {p["head"]["ref"] for p in _agent_prs(gh, cfg)} if role.kind == "implementer" else set()
    for issue in _open_ai_ready_issues(gh, cfg):
        labels = label_names(issue)
        if L_BLOCKED in labels or L_PR_OPENED in labels or L_MERGED in labels:
            continue
        if role.kind == "planner" and L_PLANNED in labels:
            continue
        if role.kind == "designer" and not (L_PLANNED in labels and L_NEEDS_DESIGN in labels and L_DESIGNED not in labels):
            continue
        if role.kind == "implementer":
            if L_PLANNED not in labels and "planner" in {r.kind for r in cfg.roles.values()}:
                continue
            if L_NEEDS_DESIGN in labels and L_DESIGNED not in labels:
                continue
            if cfg.plan_gate and L_PLAN_APPROVED not in labels:
                continue
            if branch_for(issue["number"]) in open_heads:
                continue
        # Only issues whose trigger label the owner applied: on a public repo
        # this is the main defense against injected work.
        if not owner_applied_label(gh, cfg, issue["number"], L_READY):
            continue
        if role.kind == "implementer" and cfg.plan_gate and not owner_applied_label(gh, cfg, issue["number"], L_PLAN_APPROVED):
            continue
        trusted: list[Trusted] = []
        if L_NEEDS_INPUT in labels:
            trusted = trusted_comments(gh.comments(issue["number"]), cfg)
            if awaiting_input(trusted):
                continue  # waiting for the owner's reply
        out.append(Unit(target=issue["number"], issue=issue, trusted=trusted))
    return out


def _pr_candidates(gh: GitHub, cfg: Pipeline, role: Role) -> list[Unit]:
    if role.kind == "merger" and cfg.merge_mode != "staging":
        return []
    out = []
    for pr in _agent_prs(gh, cfg):
        labels = label_names(pr)
        if L_BLOCKED in labels or L_MERGED in labels:
            continue
        if role.kind == "fixer" and L_FIX_NEEDED not in labels:
            continue
        if role.kind == "merger" and L_AWAITING_MERGE not in labels:
            continue
        if role.kind == "reviewer" and (L_FIX_NEEDED in labels or L_AWAITING_MERGE in labels):
            continue
        issue = issue_for_pr(gh, cfg, pr)
        if issue is None:
            continue
        trusted = trusted_comments(gh.comments(pr["number"]), cfg)
        if L_NEEDS_INPUT in labels and awaiting_input(trusted):
            continue
        sha = pr["head"]["sha"]
        if role.kind == "reviewer":
            if role.name in verdicts(trusted, sha):
                continue
            if len(reviewed_shas(trusted, cfg)) >= cfg.review_cap:
                continue
        out.append(Unit(target=pr["number"], issue=issue, pr=pr, trusted=trusted))
    return out


def pick_and_claim(gh: GitHub, cfg: Pipeline, role: Role, run_id: str, now: datetime) -> tuple[Unit, Claim] | None:
    for unit in candidates(gh, cfg, role):
        trusted = unit.trusted or trusted_comments(gh.comments(unit.target), cfg)
        reclaim_stale(gh, cfg, unit.target, trusted, now)
        if active_claims(trusted, role.claim_group, now, cfg.stale_claim_minutes):
            continue
        extra = {"sha": unit.head_sha} if unit.head_sha else {}
        claim = try_claim(gh, cfg, role, unit.target, run_id, now, extra)
        if claim:
            unit.trusted = trusted
            return unit, claim
    return None


# ---------------------------------------------------------------- aggregation


def aggregate(gh: GitHub, cfg: Pipeline, pr: dict, issue_number: int, run_id: str) -> str:
    """After a verdict lands: decide fix / blocked / awaiting-merge for the PR's head SHA.

    Idempotent: a second reviewer finishing at the same moment finds the
    aggregate marker and does nothing.
    """
    trusted = trusted_comments(gh.comments(pr["number"]), cfg)
    sha = pr["head"]["sha"]
    vs = verdicts(trusted, sha)
    required = cfg.required_reviewers
    if any(r not in vs for r in required):
        return "pending"
    if find(trusted, "aggregate", sha=sha):
        return "already-aggregated"

    rejected = [r for r in required if vs[r]["decision"] != "approve"]
    advisory = [r for r in cfg.reviewers if r not in required and r in vs and vs[r]["decision"] != "approve"]
    rounds = len(reviewed_shas(trusted, cfg))

    if not rejected:
        outcome = "approved"
        gh.add_labels(pr["number"], L_AWAITING_MERGE)
        text = f"✅ All required reviewers approved `{sha[:12]}`."
        if cfg.merge_mode == "off":
            text += " Merge mode is **off**: a human merges."
    elif rounds >= cfg.review_cap:
        outcome = "blocked"
        gh.add_labels(pr["number"], L_BLOCKED)
        gh.add_labels(issue_number, L_BLOCKED)
        text = f"⛔ Review cap reached ({rounds}/{cfg.review_cap} rounds) with changes still requested by {', '.join(rejected)}. Needs a human."
    else:
        outcome = "fix-needed"
        gh.add_labels(pr["number"], L_FIX_NEEDED)
        text = f"🔁 Round {rounds}/{cfg.review_cap}: changes requested by {', '.join(rejected)}. The fixer will address the findings below."

    lines = [text, ""]
    for name in rejected + advisory:
        tag = "required" if name in rejected else "advisory"
        lines.append(f"**{name}** ({tag}): {vs[name].get('summary', '')}")
        for f in vs[name].get("findings", [])[:20]:
            where = f"`{f.get('file', '?')}:{f.get('line', '?')}`" if f.get("file") else ""
            lines.append(f"- [{f.get('severity', '?')}] {where} {f.get('title', '')}")
    marker = {"kind": "aggregate", "sha": sha, "outcome": outcome, "round": rounds, "run_id": run_id}
    gh.comment(pr["number"], "\n".join(lines) + "\n\n" + markers.encode(marker))
    log.event("aggregate", pr=pr["number"], sha=sha, outcome=outcome, round=rounds)
    return outcome


def findings_for_fix(cfg: Pipeline, trusted: list[Trusted], sha: str) -> list[dict]:
    vs = verdicts(trusted, sha)
    out = []
    for name, v in sorted(vs.items()):
        if v.get("decision") == "approve":
            continue
        for f in v.get("findings", []):
            out.append({"reviewer": name, "required": name in cfg.required_reviewers, **f})
    return out
