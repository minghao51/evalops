"""LLM-judge helpers that stay backend- and model-neutral: YOU supply `judge`, a callable that calls
your judge model and returns a score (float/bool/'yes'/'no'/dict). Pin its model + prompt version in
judge_meta and calibrate it with `evalops calibrate` before trusting it in gates.

Two kinds of wrapper live here (WP3b.2/WP3b.3): `span_judge`/`trajectory_judge` wrap a judge
*callable* you supply, and `spec_judge` wraps the executor a declared `JudgeSpec` selects -- the
native judge of the configured evaluator backend (:mod:`evalops.scorers.native`) or the evalops
rubric fallback over LiteLLM (:mod:`evalops.scorers.litellm_judge`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..judges import JudgeSpec
from ..scoring import scorer, to_float
from .litellm_judge import litellm_judge
from .native import native_judge


def spec_judge(spec: JudgeSpec, *, evaluator: str) -> Callable[..., float | None]:
    """The scorer for one declared spec, chosen by its executor (``spec.judge``).

    ``native`` (the default, and what an absent ``judges.<name>.judge`` key means) uses the
    evaluator's own judge from :mod:`evalops.scorers.native`; ``litellm`` uses the evalops rubric
    fallback, which is evaluator-independent and therefore ignores `evaluator`. This is the single
    entry point: `judges.load` has already validated the value against the closed set, so an
    unknown executor cannot reach here from a config (a direct API caller gets the same rejection
    from `JudgeSpec.validate`).

    `evaluator` is the **plugin name** serving the `evaluator` role (`mlflow`, `langfuse`,
    `langsmith`, `core`). An evaluator without an adapter, or one whose SDK is not installed, raises
    `CapabilityError` -> exit 6 with a hint (the full spec reaches the vendor call: rubric, pinned
    model, prompt version, sampling); a missing `litellm` is the same exit 6 with
    `pip install litellm`.

    The returned scorer carries the spec's resolved identity (`evalops_judge`), which is what
    `run._evaluate` records on the run and per verdict (WP3b.5 C1); stamping here means both
    executors are covered without touching either executor module.
    """
    if spec.judge == "litellm":
        scorer_fn: Any = litellm_judge(spec)
    else:
        scorer_fn = native_judge(spec, evaluator=evaluator)
    # WP3b.5 C1: the single dispatch point stamps the spec identity on the returned function. A
    # plain function object accepts the attribute; `run._evaluate` reads it back per score key.
    scorer_fn.evalops_judge = {"model": spec.model, "prompt_version": spec.prompt_version}
    return scorer_fn


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
        return judge(
            inputs=inputs, outputs=outputs, expectations=expectations, trajectory=trace.render(max_chars=max_chars)
        )

    return _s
