"""LLM-judge helpers that stay backend- and model-neutral: YOU supply `judge`, a callable that calls
your judge model and returns a score (float/bool/'yes'/'no'/dict). Pin its model + prompt version in
judge_meta and calibrate it with `evalops calibrate` before trusting it in gates.
"""
from __future__ import annotations

from ..scoring import scorer, to_float


def span_judge(judge, *, name: str, kind: str = "tool", select=None, max_spans: int = 10):
    """Step-level judge: judge(span=..., inputs=..., trace=...) per selected span -> mean.

    Example: was each tool call's argument justified by the conversation so far?
    Cost: up to `max_spans` judge calls per row; account for it in judge_meta.llm_judge_count.
    """
    @scorer(name, llm=True)
    def _s(*, inputs, trace):
        if trace is None:
            return None
        spans = [s for s in trace.of_kind(kind) if select is None or select(s)][:max_spans]
        vals = [to_float(judge(span=s, inputs=inputs, trace=trace)) for s in spans]
        vals = [v for v in vals if v is not None]
        return sum(vals) / len(vals) if vals else None
    return _s


def trajectory_judge(judge, *, name: str = "trajectory_quality", max_chars: int = 300):
    """Holistic judge over a compact rendering of the whole trajectory (one call per row)."""
    @scorer(name, llm=True)
    def _s(*, inputs, outputs, expectations, trace):
        if trace is None:
            return None
        return judge(inputs=inputs, outputs=outputs, expectations=expectations,
                     trajectory=trace.render(max_chars=max_chars))
    return _s
