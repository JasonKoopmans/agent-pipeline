"""Render the chart and assert the isolation invariants (skipped without helm)."""

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")
from agent_tick import config  # noqa: E402

CHART = Path(__file__).parent.parent / "chart" / "agent-pipeline"
pytestmark = pytest.mark.skipif(not shutil.which("helm"), reason="helm not installed")

BASE = ["--set", "pipeline.repo=owner/app", "--set", "pipeline.owner=owner"]


def render(*sets: str) -> list[dict]:
    out = subprocess.run(["helm", "template", "t", str(CHART), *BASE, *sets], capture_output=True, text=True, check=True).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def pods(docs):
    for d in docs:
        if d["kind"] == "CronJob":
            spec = d["spec"]["jobTemplate"]["spec"]["template"]["spec"]
            yield d["metadata"]["labels"]["agent-pipeline/role"], spec, {c["name"]: c for c in spec["initContainers"] + spec["containers"]}


def env(c):
    return {e["name"] for e in c.get("env", [])}


def mounts(c):
    return {m["name"]: m.get("readOnly", False) for m in c.get("volumeMounts", [])}


PAT = ["--set", "secrets.githubWriter.name=w", "--set", "secrets.githubCommenter.name=c"]
APPS = [
    "--set", "secrets.githubWriter.name=w", "--set", "secrets.githubCommenter.name=c",
    "--set", "roles.planner.appSlug=jk-agent-planner", "--set", "roles.planner.appSecret=agent-app-planner", "--set", "roles.planner.appUserId=11",
    "--set", "roles.review-e2e.enabled=true", "--set", "roles.merger.enabled=true",
]


@pytest.mark.parametrize("sets", [PAT, APPS], ids=["pat", "apps"])
def test_credentials_never_reach_model_containers(sets):
    for role, spec, cs in pods(render(*sets)):
        for name in ("agent", "verify"):
            assert not env(cs[name]) & {"GH_TOKEN", "GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_FILE"}, (role, name)
            assert "github-app" not in mounts(cs[name]), (role, name)
        for name in ("prepare", "verify", "publish"):
            assert "CLAUDE_CODE_OAUTH_TOKEN" not in env(cs[name]), (role, name)
        assert mounts(cs["agent"])["control"] is True and "verify" not in mounts(cs["agent"])
        assert mounts(cs["publish"])["work"] is True
        assert spec["automountServiceAccountToken"] is False
        assert {"name": "ndots", "value": "1"} in spec["dnsConfig"]["options"], role


def test_images_core_for_token_stages_stack_for_model(sets=APPS):
    for role, _, cs in pods(render(*sets)):
        assert cs["prepare"]["image"].endswith("-core") and cs["publish"]["image"].endswith("-core"), role
        expected = {"planner": "core", "merger": "core", "review-e2e": "tester-dotnet10"}.get(role, "dotnet10")
        assert cs["agent"]["image"].endswith("-" + expected), (role, cs["agent"]["image"])
        assert cs["verify"]["image"] == cs["agent"]["image"]


def test_app_role_uses_app_key_not_pat():
    roles = {r: cs for r, _, cs in pods(render(*APPS))}
    planner = roles["planner"]
    assert {"GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY_FILE"} <= env(planner["prepare"]) and "GH_TOKEN" not in env(planner["prepare"])
    assert "GH_TOKEN" in env(roles["implementer"]["publish"])  # PAT fallback


def test_config_carries_identity_and_parses():
    cm = [d for d in render(*APPS) if d["kind"] == "ConfigMap"][0]
    cfg = config.parse(tomllib.loads(cm["data"]["pipeline.toml"]))
    assert cfg.role("planner").login == "jk-agent-planner[bot]"
    assert cfg.role("planner").app_user_id == 11
    assert "jk-agent-planner[bot]" in cfg.agent_logins and "owner" in cfg.agent_logins  # mixed mode


def test_egress_is_per_role():
    policies = {d["metadata"]["labels"]["agent-pipeline/role"]: d for d in render(*APPS) if d["kind"] == "CiliumNetworkPolicy"}

    def hosts(role):
        rules = [r for r in policies[role]["spec"]["egress"] if "toFQDNs" in r]
        return {next(iter(h.values())) for r in rules for h in r["toFQDNs"]}

    assert "api.nuget.org" in hosts("implementer")
    assert "api.nuget.org" not in hosts("planner") and "api.anthropic.com" in hosts("planner")
    assert hosts("merger") == {"github.com", "api.github.com", "codeload.github.com", "objects.githubusercontent.com"}
    assert policies["planner"]["spec"]["endpointSelector"]["matchLabels"]["agent-pipeline/role"] == "planner"


def test_role_without_any_github_credential_fails():
    with pytest.raises(subprocess.CalledProcessError):
        render()
