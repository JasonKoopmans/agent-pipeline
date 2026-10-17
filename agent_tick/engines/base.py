"""The contract between the `agent` stage and whatever harness runs the model.

Everything outside the `agent` stage (claims, verify, publish, the state
machine) only sees a SessionRequest going in and a session result dict coming
out, so swapping or mixing harnesses per role touches nothing else.

Session result keys:
    status             ok | error | timeout | usage_limit
    structured_output  dict matching the request's schema (schema roles only)
    result             final free-text message (becomes the PR summary for code roles)
    num_turns, total_cost_usd, duration_ms   usage, when the harness reports it
    error              short reason when status != ok
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

USAGE_LIMIT_RE = re.compile(r"usage limit|rate limit|limit reached|quota|try again later|overloaded", re.I)


@dataclass(frozen=True)
class SessionRequest:
    role_name: str
    role_kind: str
    prompt: str                       # the task message
    system_prompt_file: Path          # role instructions, appended to the harness's own
    cwd: Path                         # the repository working copy
    scratch_dir: Path                 # harness-private files (settings, output files)
    timeout_s: int
    model: str
    max_turns: int
    max_budget_usd: float
    writes_code: bool                 # False: read-only tools only
    schema: dict[str, Any] | None = None
    effort: str | None = None
    mcp_config: Path | None = None
    sandbox: bool = True
    env: dict[str, str] = field(default_factory=dict)


class Engine(Protocol):
    name: str
    # Env vars the harness needs from the agent container (its credentials).
    # Nothing else from the container environment reaches the harness.
    secret_env: tuple[str, ...]

    def run(self, req: SessionRequest) -> dict[str, Any]: ...
