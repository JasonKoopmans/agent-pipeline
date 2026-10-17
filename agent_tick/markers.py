"""Hidden machine-readable markers embedded in GitHub comments.

Every comment the wrapper writes carries one marker. Markers are only trusted
when the comment's author is the configured owner, because anyone can type
the same text into a comment on a public repository.
"""

import json
import re
from typing import Any

PREFIX = "<!-- agent:v1 "
_MARKER_RE = re.compile(r"<!-- agent:v1 (\{.*?\}) -->", re.S)


def encode(data: dict[str, Any]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"))
    # A value containing "-->" would end the HTML comment early.
    payload = payload.replace("-->", "--\\u003e")
    return f"{PREFIX}{payload} -->"


def decode(body: str | None) -> list[dict[str, Any]]:
    if not body:
        return []
    found = []
    for raw in _MARKER_RE.findall(body):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def strip(body: str | None) -> str:
    """Comment text without its markers (what a human wrote)."""
    return _MARKER_RE.sub("", body or "").strip()
