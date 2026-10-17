"""Harness registry. A role picks its engine with `engine = "<name>"` in pipeline.toml.

Adding a harness: implement engines.base.Engine in a new module, register it
here, install its CLI in the stack image, and give its credential to the agent
container (the chart value for that engine's secret).
"""

from agent_tick.engines.base import Engine, SessionRequest
from agent_tick.engines.claude import ClaudeEngine

_REGISTRY: dict[str, type] = {"claude": ClaudeEngine}

NAMES = tuple(sorted(_REGISTRY))


def get(name: str) -> Engine:
    try:
        return _REGISTRY[name]()
    except KeyError:
        raise ValueError(f"unknown engine {name!r}; available: {NAMES}") from None


__all__ = ["Engine", "SessionRequest", "NAMES", "get"]
