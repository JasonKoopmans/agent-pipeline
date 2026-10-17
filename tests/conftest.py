import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from agent_tick import config  # noqa: E402
from fake_github import OWNER, REPO, FakeGitHub  # noqa: E402

BASE = {
    "repo": REPO,
    "owner": OWNER,
    "base_branch": "ai-staging",
    "review_cap": 3,
    "daily_session_cap": 8,
    "merge_mode": "staging",
    "plan_gate": True,
    "roles": {
        "planner": {"kind": "planner"},
        "implementer": {"kind": "implementer"},
        "fixer": {"kind": "fixer"},
        "review-quality": {"kind": "reviewer"},
        "review-security": {"kind": "reviewer"},
        "review-perf": {"kind": "reviewer", "required": False},
        "merger": {"kind": "merger"},
    },
}


@pytest.fixture
def cfg():
    return config.parse(BASE)


@pytest.fixture
def gh():
    return FakeGitHub()
