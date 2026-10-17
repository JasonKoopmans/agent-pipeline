"""verify stage against a real local git repository (no network)."""

import subprocess

import pytest

from agent_tick import config, gitops, stages
from fake_github import OWNER, REPO


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ws(tmp_path):
    ws = stages.Workspace(tmp_path / "work")
    ws.repo.mkdir(parents=True)
    ws.out.mkdir()
    _git(ws.repo, "init", "-q", "-b", "ai-staging")
    (ws.repo / "app.txt").write_text("v1\n")
    _git(ws.repo, "add", "-A")
    _git(ws.repo, "commit", "-q", "-m", "init")
    start = gitops.head(ws.repo)
    _git(ws.repo, "checkout", "-q", "-b", "ai/issue-1")
    ws.write("task.json", {"action": "work", "run_id": "r", "kind": "implementer", "branch": "ai/issue-1", "start_sha": start, "issue": {"number": 1}})
    ws.write("session.json", {"status": "ok"})
    return ws


def _cfg(gates=()):
    return config.parse({"repo": REPO, "owner": OWNER, "base_branch": "ai-staging", "gate_commands": [list(g) for g in gates], "roles": {"implementer": {"kind": "implementer"}}})


def test_commits_leftovers_and_passes_gate(ws):
    (ws.repo / "app.txt").write_text("v2\n")
    ws.write("session.json", {"status": "ok", "structured_output": {"outcome": "done", "commit_message": "feat: v2\nmore"}})
    cfg = _cfg(gates=[["true"]])
    result = stages.verify(cfg, cfg.role("implementer"), ws)
    assert result["ok"], result
    assert result["changed_files"] == ["app.txt"]
    assert gitops.git("log", "-1", "--format=%s", cwd=ws.repo) == "feat: v2"
    assert "Run-Id: r" in gitops.git("log", "-1", "--format=%b", cwd=ws.repo)


def test_needs_input_skips_gate(ws):
    ws.write("session.json", {"status": "ok", "structured_output": {"outcome": "needs_input", "questions": ["which?"]}})
    cfg = _cfg(gates=[["false"]])
    result = stages.verify(cfg, cfg.role("implementer"), ws)
    assert result == {"ok": False, "reason": "outcome needs_input", "outcome": "needs_input"}


def test_app_role_commits_as_bot(ws):
    (ws.repo / "app.txt").write_text("v3\n")
    cfg = config.parse({"repo": REPO, "owner": OWNER, "base_branch": "ai-staging",
                        "roles": {"implementer": {"kind": "implementer", "app_slug": "jk-agent-implementer", "app_user_id": 42}}})
    assert stages.verify(cfg, cfg.role("implementer"), ws)["ok"]
    assert gitops.git("log", "-1", "--format=%an <%ae>", cwd=ws.repo) == "jk-agent-implementer[bot] <42+jk-agent-implementer[bot]@users.noreply.github.com>"


def test_no_changes_fails(ws):
    cfg = _cfg()
    assert stages.verify(cfg, cfg.role("implementer"), ws)["reason"] == "no changes were made"


def test_forbidden_paths_fail(ws):
    (ws.repo / ".github").mkdir()
    (ws.repo / ".github" / "ci.yml").write_text("x")
    cfg = _cfg()
    result = stages.verify(cfg, cfg.role("implementer"), ws)
    assert not result["ok"] and "forbidden" in result["reason"]


def test_gate_failure_fails(ws):
    (ws.repo / "app.txt").write_text("v2\n")
    cfg = _cfg(gates=[["false"]])
    result = stages.verify(cfg, cfg.role("implementer"), ws)
    assert not result["ok"] and result["reason"].startswith("gate failed")


def test_prewritten_verify_file_is_not_trusted(ws):
    (ws.root / "verify.json").write_text('{"ok": true}')
    ws.write("session.json", {"status": "error"})
    cfg = _cfg()
    assert stages.verify(cfg, cfg.role("implementer"), ws)["ok"] is False


def test_rewritten_history_fails(ws):
    _git(ws.repo, "checkout", "-q", "--orphan", "tmp")
    (ws.repo / "other.txt").write_text("x")
    _git(ws.repo, "add", "-A")
    _git(ws.repo, "commit", "-q", "-m", "orphan")
    _git(ws.repo, "branch", "-f", "ai/issue-1", "tmp")
    cfg = _cfg()
    assert "history rewritten" in stages.verify(cfg, cfg.role("implementer"), ws)["reason"]


def test_push_from_workspace_ignores_planted_hooks_and_config(ws, tmp_path):
    """A pre-push hook and an insteadOf rewrite in the workspace must not run/apply."""
    (ws.repo / "app.txt").write_text("v2\n")
    _git(ws.repo, "commit", "-qam", "change")
    tip = gitops.head(ws.repo)
    marker = tmp_path / "hook-ran"
    hook = ws.repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    _git(ws.repo, "config", "url.https://evil.example/.insteadOf", "https://github.com/")
    # Point the publish URL at a local bare repo for the test.
    orig = gitops.url
    gitops.url = lambda repo: f"file://{remote}"
    try:
        gitops.push_from_workspace(ws.repo, "ai/issue-1", tip, REPO, token="", lease="")
    finally:
        gitops.url = orig
    assert not marker.exists()
    assert subprocess.run(["git", "rev-parse", "refs/heads/ai/issue-1"], cwd=remote, capture_output=True, text=True).stdout.strip() == tip


def test_control_and_verify_files_live_outside_the_agent_workspace(tmp_path):
    ws = stages.Workspace(tmp_path / "work", control=tmp_path / "control", verify=tmp_path / "verify")
    for d in ws.dirs.values():
        d.mkdir(parents=True, exist_ok=True)
    ws.write("task.json", {"action": "none"})
    ws.write("verify.json", {"ok": True})
    ws.write("session.json", {"status": "ok"})
    assert (tmp_path / "control" / "task.json").exists()
    assert (tmp_path / "verify" / "verify.json").exists()
    assert (tmp_path / "work" / "session.json").exists()
    assert not (tmp_path / "work" / "task.json").exists()
