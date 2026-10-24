"""Backend-neutral scorers + normalization of any backend's score objects to floats."""

from __future__ import annotations

import inspect
from typing import Any, Callable

TRUE_STR = {"yes", "true", "pass", "passed", "correct", "y"}
FALSE_STR = {"no", "false", "fail", "failed", "incorrect", "n"}


def scorer(name: str | None = None, *, llm: bool = False):
    """Decorate fn(*, inputs, outputs, expectations) -> float | bool | dict | Score-like."""

    def deco(fn: Callable):
        fn.evalops_name = name or fn.__name__
        fn.evalops_llm = llm
        return fn

    return deco


def scorer_name(s) -> str:
    return (
        getattr(s, "evalops_name", None) or getattr(s, "name", None) or getattr(s, "__name__", None) or type(s).__name__
    )


def to_float(value: Any) -> float | None:
    """Normalize MLflow Feedback, Langfuse Evaluation, LangSmith dicts, bools, 'yes'/'no'..."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in TRUE_STR:
            return 1.0
        if v in FALSE_STR:
            return 0.0
        try:
            return float(v)
        except ValueError:
            return None
    if isinstance(value, dict):
        for key in ("score", "value"):
            if key in value:
                return to_float(value[key])
        return None
    for attr in ("value", "score"):
        if hasattr(value, attr):
            return to_float(getattr(value, attr))
    return None


def _params(s):
    return inspect.signature(s if inspect.isfunction(s) else s.__call__).parameters


def _is_neutral(s) -> bool:
    # Backend scorer objects (e.g. MLflow Scorer) have their own `trace` type: never pass ours to them.
    return inspect.isfunction(s) or hasattr(s, "evalops_name")


def wants_trace(s) -> bool:
    """True if a neutral scorer explicitly takes `trace` (trajectory / step-level scorers)."""
    try:
        return _is_neutral(s) and "trace" in _params(s)
    except (TypeError, ValueError):
        return False


def call_scorer(s, *, inputs, outputs, expectations, trace=None) -> dict[str, float]:
    """Call a neutral scorer or any backend scorer that accepts inputs/outputs/expectations(/trace) kwargs.

    Returns {metric_name: value}. A scorer may return a list of scores (multi-metric).
    """
    params = _params(s)
    kwargs = {"inputs": inputs, "outputs": outputs, "expectations": expectations or {}}
    if wants_trace(s):
        kwargs["trace"] = trace
    if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in params}
    raw = s(**kwargs)
    name = scorer_name(s)
    if isinstance(raw, list):
        out = {}
        for i, item in enumerate(raw):
            n = getattr(item, "name", None) or (item.get("key") if isinstance(item, dict) else None) or f"{name}_{i}"
            if (f := to_float(item)) is not None:
                out[n] = f
        return out
    f = to_float(raw)
    return {} if f is None else {name: f}


def _p95(vals):
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]


def aggregate_rows(rows) -> dict[str, float]:
    """Per-scorer '<name>/mean'; trace metrics as 'trace.<m>/mean' and 'trace.<m>/p95'."""
    out: dict[str, float] = {}
    for attr, prefix in (("scores", ""), ("trace_metrics", "trace.")):
        sums: dict[str, list[float]] = {}
        for r in rows:
            for k, v in (getattr(r, attr, None) or {}).items():
                if isinstance(v, (int, float)):
                    sums.setdefault(k, []).append(float(v))
        for k, v in sums.items():
            out[f"{prefix}{k}/mean"] = sum(v) / len(v)
            if prefix:
                out[f"{prefix}{k}/p95"] = _p95(v)
    return out
