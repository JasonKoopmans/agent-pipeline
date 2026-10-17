"""Claude Code engine: one bounded `claude -p` session.

Only the official, unmodified Claude Code CLI talks to the model; it
authenticates with CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`.
"""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_tick.engines.base import USAGE_LIMIT_RE, SessionRequest

# Read-only roles get no edit tools at all; code roles keep the default set
# minus network fetchers and anything that could publish.
READ_ONLY_TOOLS = "Read,Grep,Glob,Bash"
# Never available: there is no human on the other end of a headless session.
# Questions go into the structured output and are posted to GitHub instead.
ALWAYS_DENY = ["AskUserQuestion"]
CODE_DENY = ["WebFetch", "WebSearch", "Bash(git push *)", "Bash(gh *)", "Bash(curl *)", "Bash(wget *)"]
READ_ONLY_DENY = ["WebFetch", "WebSearch", "Edit", "Write", "NotebookEdit", "Bash(git push *)", "Bash(git commit *)", "Bash(gh *)"]
SECRET_DENY = [
    "Read(/proc/**)",
    "Read(/run/secrets/**)",
    "Read(/var/run/secrets/**)",
    "Bash(env)",
    "Bash(env *)",
    "Bash(printenv*)",
    "Bash(cat /proc/*)",
]

Runner = Callable[..., subprocess.CompletedProcess]


class ClaudeEngine:
    name = "claude"
    secret_env = ("CLAUDE_CODE_OAUTH_TOKEN",)

    def __init__(self, runner: Runner = subprocess.run):
        self._run = runner

    def settings(self, req: SessionRequest) -> dict[str, Any]:
        deny = ALWAYS_DENY + SECRET_DENY + (CODE_DENY if req.writes_code else READ_ONLY_DENY)
        out: dict[str, Any] = {"permissions": {"deny": deny}, "includeCoAuthoredBy": False}
        if req.sandbox:
            # Claude Code's own bubblewrap sandbox for model-run shell commands.
            out["sandbox"] = {"enabled": True, "autoAllowBashIfSandboxed": True}
        return out

    def build_command(self, req: SessionRequest, settings_path: Path) -> list[str]:
        cmd = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--model", req.model,
            "--max-turns", str(req.max_turns),
            "--max-budget-usd", f"{req.max_budget_usd:.2f}",
            "--permission-mode", "bypassPermissions",
            "--permission-prompts", "none",
            # Ignore project/local settings from the target repo (hooks, MCP, permissions).
            "--setting-sources", "user",
            "--settings", str(settings_path),
            "--strict-mcp-config",
            "--no-session-persistence",
            "--disallowedTools", *ALWAYS_DENY,
            "--append-system-prompt-file", str(req.system_prompt_file),
        ]
        if req.effort:
            cmd += ["--effort", req.effort]
        if req.mcp_config:
            cmd += ["--mcp-config", str(req.mcp_config)]
        if not req.writes_code:
            cmd += ["--tools", READ_ONLY_TOOLS]
        if req.schema:
            cmd += ["--json-schema", json.dumps(req.schema)]
        return cmd

    def run(self, req: SessionRequest) -> dict[str, Any]:
        settings_path = req.scratch_dir / "claude-settings.json"
        settings_path.write_text(json.dumps(self.settings(req)))
        cmd = self.build_command(req, settings_path)
        try:
            proc = self._run(cmd, input=req.prompt, cwd=req.cwd, capture_output=True, text=True, timeout=req.timeout_s, env=req.env, check=False)
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            summary = parse_stream(out)
            summary["status"] = "timeout"
            return summary
        summary = parse_stream(proc.stdout)
        summary["exit_code"] = proc.returncode
        if summary["status"] == "error" and proc.stderr:
            summary["stderr_tail"] = proc.stderr[-2000:]
            if USAGE_LIMIT_RE.search(proc.stderr):
                summary["status"] = "usage_limit"
        return summary


def parse_stream(text: str) -> dict[str, Any]:
    """Summarize stream-json output: the final `result` event carries the totals."""
    result: dict[str, Any] | None = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            result = obj
    if result is None:
        return {"status": "error", "error": "no result event in claude output"}
    summary = {
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "duration_ms": result.get("duration_ms"),
        "subtype": result.get("subtype"),
        "result": (result.get("result") or "")[:4000],
        "structured_output": result.get("structured_output"),
    }
    if result.get("is_error") or result.get("subtype") != "success":
        summary["status"] = "usage_limit" if USAGE_LIMIT_RE.search(json.dumps(result)) else "error"
    else:
        summary["status"] = "ok"
    return summary
