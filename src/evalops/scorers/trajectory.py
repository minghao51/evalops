"""Deterministic trajectory scorers over the normalized AgentTrace.

Expectations schema (all optional, per dataset record):
  expected_tools:  ["search_orders", {"name": "refund", "args": {"order_id": "A1"}}]
  tool_match:      exact | in_order | any_order | subset        (default from the scorer factory)
  forbidden_tools: ["delete_account"]
  max_steps:       8                                            (LLM + tool calls)
  expected_agents: ["supervisor", "refund_agent"]               (sub-agents / graph nodes / handoffs)
  agent_match:     exact | in_order | any_order
  expected_state:  {"status": "refunded"}                       (partial match on dict outputs)
A scorer returns None (= not applicable, excluded from means) when its expectation is absent.
"""

from __future__ import annotations

import json
from collections import Counter

from ..scoring import scorer


def _names(expected) -> list[str]:
    return [e["name"] if isinstance(e, dict) else str(e) for e in expected]


def _is_subsequence(needle: list, hay: list) -> bool:
    it = iter(hay)
    return all(any(x == y for y in it) for x in needle)


def sequence_match(actual: list[str], expected: list[str], mode: str) -> bool:
    if mode == "exact":
        return actual == expected
    if mode == "in_order":
        return _is_subsequence(expected, actual)
    if mode == "any_order":
        return not (Counter(expected) - Counter(actual))
    if mode == "subset":  # every actual call is an expected one (no unexpected tools)
        return set(actual) <= set(expected)
    raise ValueError(f"unknown match mode {mode}")


def _norm(v):
    if isinstance(v, str):
        try:
            return _norm(json.loads(v))
        except ValueError:
            return v.strip().lower()
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    return v


def _partial(expected: dict, actual) -> bool:
    actual = _norm(actual)
    if not isinstance(actual, dict):
        return False
    return all(_norm(v) == actual.get(k) for k, v in expected.items())


def tool_trajectory(match: str = "in_order", name: str = "tool_trajectory"):
    """1.0 if the tool-call sequence matches expected_tools under `match` (per-record tool_match overrides)."""

    @scorer(name)
    def _s(*, expectations, trace):
        exp = (expectations or {}).get("expected_tools")
        if exp is None or trace is None:
            return None
        mode = (expectations or {}).get("tool_match", match)
        return sequence_match(trace.tool_names(), _names(exp), mode)

    return _s


def tool_recall(name: str = "tool_recall"):
    """Fraction of expected tool calls that happened (order-insensitive, multiset)."""

    @scorer(name)
    def _s(*, expectations, trace):
        exp = (expectations or {}).get("expected_tools")
        if not exp or trace is None:
            return None
        want, got = Counter(_names(exp)), Counter(trace.tool_names())
        return sum(min(c, got[k]) for k, c in want.items()) / sum(want.values())

    return _s


def tool_precision(name: str = "tool_precision"):
    """Fraction of actual tool calls that were expected: penalizes wasted / unnecessary calls."""

    @scorer(name)
    def _s(*, expectations, trace):
        exp = (expectations or {}).get("expected_tools")
        if exp is None or trace is None:
            return None
        got = Counter(trace.tool_names())
        if not got:
            return 1.0 if not exp else 0.0
        want = Counter(_names(exp))
        return sum(min(c, want[k]) for k, c in got.items()) / sum(got.values())

    return _s


def tool_args(name: str = "tool_args"):
    """For expected tools with `args`, fraction whose call (matched by name, in order) has those args."""

    @scorer(name)
    def _s(*, expectations, trace):
        exp = [e for e in (expectations or {}).get("expected_tools") or [] if isinstance(e, dict) and "args" in e]
        if not exp or trace is None:
            return None
        calls, used, hits = trace.tool_calls(), set(), 0
        for e in exp:
            for i, c in enumerate(calls):
                if i not in used and (c.tool_name or c.name) == e["name"] and _partial(e["args"], c.inputs):
                    used.add(i)
                    hits += 1
                    break
        return hits / len(exp)

    return _s


def forbidden_tools(name: str = "forbidden_tools"):
    @scorer(name)
    def _s(*, expectations, trace):
        bad = (expectations or {}).get("forbidden_tools")
        if not bad or trace is None:
            return None
        return not (set(trace.tool_names()) & set(bad))

    return _s


def step_budget(max_steps: int | None = None, name: str = "step_budget"):
    """1.0 if LLM+tool steps <= expectations.max_steps (or the factory default)."""

    @scorer(name)
    def _s(*, expectations, trace):
        limit = (expectations or {}).get("max_steps", max_steps)
        if limit is None or trace is None:
            return None
        return trace.steps <= int(limit)

    return _s


def tool_errors(name: str = "tool_success"):
    """Fraction of tool calls that did not error (None if no tool calls)."""

    @scorer(name)
    def _s(*, trace):
        calls = trace.tool_calls() if trace else []
        return None if not calls else sum(1 for c in calls if not c.error) / len(calls)

    return _s


def no_loops(max_identical: int = 2, name: str = "no_loops"):
    """0.0 if any identical tool call (same name + args) occurs more than `max_identical` times."""

    @scorer(name)
    def _s(*, trace):
        if trace is None:
            return None
        keys = Counter(
            f"{c.tool_name or c.name}:{json.dumps(_norm(c.inputs), sort_keys=True, default=str)}"
            for c in trace.tool_calls()
        )
        return all(v <= max_identical for v in keys.values())

    return _s


def agent_route(match: str = "in_order", exclude: tuple[str, ...] = (), name: str = "agent_route"):
    """Sub-agent / graph-node / handoff route vs expected_agents (e.g. LangGraph supervisor routing)."""

    @scorer(name)
    def _s(*, expectations, trace):
        exp = (expectations or {}).get("expected_agents")
        if exp is None or trace is None:
            return None
        route = [a for a in trace.agent_route() if a not in exclude]
        return sequence_match(route, list(exp), (expectations or {}).get("agent_match", match))

    return _s


def expected_state(name: str = "expected_state"):
    """Fraction of expected_state keys matched in dict outputs (agents that act rather than answer)."""

    @scorer(name)
    def _s(*, outputs, expectations):
        exp = (expectations or {}).get("expected_state")
        if not exp:
            return None
        out = _norm(outputs)
        if not isinstance(out, dict):
            return 0.0
        return sum(1 for k, v in exp.items() if out.get(k) == _norm(v)) / len(exp)

    return _s
