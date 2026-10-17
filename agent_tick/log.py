"""JSON-lines logging to stdout, one object per event, always carrying run_id and role."""

import json
import sys
from datetime import UTC, datetime

_context: dict[str, str] = {}


def bind(**fields: str) -> None:
    """Attach fields (run_id, role, stage) to every later log line."""
    _context.update({k: v for k, v in fields.items() if v})


def event(name: str, **fields: object) -> None:
    line = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "event": name, **_context, **fields}
    sys.stdout.write(json.dumps(line, default=str) + "\n")
    sys.stdout.flush()
