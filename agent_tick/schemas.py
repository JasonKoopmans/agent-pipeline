"""Structured outputs each role kind must return, independent of the harness.

Engines that can enforce a JSON schema natively pass these through; others are
asked to write the object to a file. `publish` validates the result either way.
"""

# Every role ends with one of these. `needs_input` turns the session's
# questions into a GitHub comment and waits for the owner's reply; no session
# ever waits on a human directly.
OUTCOME = {
    "outcome": {"type": "string", "enum": ["done", "needs_input", "cannot_proceed"],
                "description": "done: finished. needs_input: blocked on a question only the owner can answer. cannot_proceed: impossible as specified (explain in reason)."},
    "questions": {"type": "array", "items": {"type": "string"}, "description": "Questions for the owner (needs_input), each answerable in a sentence or two."},
    "reason": {"type": "string", "description": "Why the outcome is not done; empty otherwise."},
}


def _with_outcome(schema: dict) -> dict:
    props = {k: v for k, v in schema["properties"].items() if k not in OUTCOME}
    return {
        "type": "object",
        "properties": {**props, **OUTCOME},
        "required": sorted(set(schema["required"]) - set(OUTCOME) | {"outcome", "questions", "reason"}),
    }


PLAN = {
    "type": "object",
    "properties": {
        "plan_markdown": {"type": "string", "description": "The plan: scope, files to touch, test strategy, risks, out of scope."},
        "needs_design": {"type": "boolean", "description": "True only if UI/UX or public API design must be decided before coding."},
        "questions": {"type": "array", "items": {"type": "string"}, "description": "Blocking questions for the owner, if any."},
    },
    "required": ["plan_markdown", "needs_design", "questions"],
}

DESIGN = {
    "type": "object",
    "properties": {"design_markdown": {"type": "string"}},
    "required": ["design_markdown"],
}

VERDICT = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "request_changes"]},
        "reviewed_sha": {"type": "string", "description": "The full head SHA you reviewed (from the task)."},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "detail": {"type": "string"},
                },
                "required": ["severity", "title", "detail"],
            },
        },
    },
    "required": ["decision", "reviewed_sha", "summary", "findings"],
}

CODE = {
    "type": "object",
    "properties": {
        "summary": {"type": "string", "description": "What changed, why, and how it was tested (becomes the PR description)."},
        "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Reasonable defaults you chose instead of asking."},
        "commit_message": {"type": "string", "description": "One-line conventional commit summary."},
    },
    "required": ["summary", "assumptions", "commit_message"],
}

PLAN, DESIGN, VERDICT, CODE = (_with_outcome(s) for s in (PLAN, DESIGN, VERDICT, CODE))

BY_KIND = {"planner": PLAN, "designer": DESIGN, "reviewer": VERDICT, "implementer": CODE, "fixer": CODE}


def for_kind(kind: str) -> dict | None:
    return BY_KIND.get(kind)
