"""Native judge adapters: one per evaluator backend (WP3b.2).

Phase 3b is native-first (map decision D-judges): each preset wraps the judges its backend already
has, and :mod:`evalops.scorers.judges` becomes the neutral wrapper layer over them. This module owns
the four adapters and the registry that says which evaluator plugin has one:

=====================  ================  ==================================================
``key``                ``evaluator``     vendor surface
=====================  ================  ==================================================
``mlflow-make-judge``  ``mlflow``        ``mlflow.genai.make_judge(...)``
``langfuse-managed``   ``langfuse``      managed evaluator config + score read-back
``openevals``          ``langsmith``     ``openevals.create_llm_as_judge(...)``
``pydantic-evals``     ``core``          the pydantic-evals ``LLMJudge`` metric
=====================  ================  ==================================================

``databricks`` needs no entry of its own (its evaluator role *is* ``mlflow``), and ``local`` /
``logfire`` / ``mixed`` are all served by ``core``. Which preset uses which evaluator lives in
:mod:`evalops.presets` (``PRESETS[...].roles``) and is never restated here.

Three rules this module keeps
-----------------------------
* **Vendor-free core (D4).** Every SDK import is *inside* a factory function body, so importing this
  module never needs a vendor package; ``tests/unit/test_no_vendor_imports.py`` exempts exactly that
  lazy shape and flags a module-level import of any of the four SDKs.
* **A missing SDK is exit 6, not exit 5 (CONTRACT §3.0/§7.4).** ``ImportError`` is translated into
  ``CapabilityError(evaluator, "native judge '<name>'", hint="pip install <package>")``, chained
  ``from`` the original error, so an agent gets the install line instead of a traceback. The hint
  names the **vendor package** (``mlflow``, ``langfuse``, ``openevals``, ``pydantic-evals``), never
  an evalops extra.
* **The rubric is never emitted.** It is handed to the vendor (that is its only purpose) and may not
  appear in an envelope, message, hint or ``repr``: identity is ``judges.rubric_hash``.

The return value of every adapter is a *neutral* scorer -- ``scorer(spec.name, llm=True)`` over
``(*, inputs, outputs, expectations, trace=None) -> float | None`` -- so ``scoring.call_scorer`` and
the core evaluator consume it unchanged and the vendor object never escapes. Every vendor result is
normalized through :func:`evalops.scoring.to_float`; a missing or unscorable result is ``None``
(excluded from the aggregate), never ``0.0``.

Unverified vendor details carry ``# VERIFY(...)`` markers, the same posture as
``evalops/plugins/``: the call shapes below encode the vendor documentation as read for this WP, the
unit tests pin *evalops'* behaviour (what we pass, what we normalize) against fake SDK modules, and
phase 3 resolves each marker against a pinned SDK version.

A static analyzer pointed at a checkout of this repo will report the four imports below as
unresolvable (``reportMissingImports``/``import-not-found``). That is the intended state, not a
defect: no vendor SDK is installed in the offline environment (the doctor pins assert the vendor
presets stay unreachable), so the imports cannot resolve until a user installs one extra. The
repo's gates -- ruff, mypy with ``ignore_missing_imports``, and the AST guard -- are the authority,
and the guard exists to keep exactly this shape (a *function-local* import) rather than a
module-level one.

Deliberately **not** wired in here: choosing a scorer from a declared spec lives in
:mod:`evalops.scorers.judges` (``spec_judge`` dispatches on ``JudgeSpec.judge``, WP3b.3), and
recording a resolved model per verdict is WP3b.5. Nothing in this module is reachable from
``plan``/``evaluate``/``calibrate`` except through that dispatch.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from ..judges import JudgeSpec, rubric_hash
from ..scoring import scorer, to_float
from ..util import CapabilityError

#: A neutral scorer: what ``scoring.call_scorer`` accepts and the core evaluator aggregates.
JudgeFn = Callable[..., float | None]


@dataclass(frozen=True)
class NativeJudge:
    """One evaluator's native judge adapter: a row of :data:`ADAPTERS` (and of the matrix).

    ``key`` is the adapter label (it names the vendor call shape), ``evaluator`` is the **plugin
    name** the adapter serves, ``import_name`` is the SDK root the factory imports, ``package`` is
    the pip-install name the ``CapabilityError`` hint shows, and ``description`` is the one line the
    capability matrix renders.

    ``server_side`` is the estimate's honesty bit (WP3b.3 D2): ``True`` when the vendor runs the
    judge itself and the row costs no local model tokens (Langfuse managed evaluators), so
    ``plan``/``_evaluate`` must not add a client-side judge call for it. It renders nowhere.
    """

    key: str
    evaluator: str
    import_name: str
    package: str
    description: str
    server_side: bool = False


#: The registry, keyed by evaluator plugin name, in the packet's table order. Insertion order is
#: kept for readers; :func:`adapters` hands out a sorted copy, which is what the matrix renders.
ADAPTERS: dict[str, NativeJudge] = {
    "mlflow": NativeJudge(
        key="mlflow-make-judge",
        evaluator="mlflow",
        import_name="mlflow",
        package="mlflow",
        description="mlflow.genai.make_judge runs the rubric as a native MLflow judge",
    ),
    "langfuse": NativeJudge(
        key="langfuse-managed",
        evaluator="langfuse",
        import_name="langfuse",
        package="langfuse",
        description="a Langfuse managed evaluator scores server-side; evalops reads the score back",
        # The evaluator runs on Langfuse's side: the row costs no client-side model call, so the
        # cost estimate must not add one (WP3b.3 D2).
        server_side=True,
    ),
    "langsmith": NativeJudge(
        key="openevals",
        evaluator="langsmith",
        import_name="openevals",
        package="openevals",
        description="openevals create_llm_as_judge, the judge LangSmith's native evaluation runs",
    ),
    "core": NativeJudge(
        key="pydantic-evals",
        evaluator="core",
        import_name="pydantic_evals",
        package="pydantic-evals",
        description="the pydantic-evals LLMJudge metric, run by the core evaluator",
    ),
}


def adapters() -> dict[str, NativeJudge]:
    """A defensive, evaluator-sorted copy of :data:`ADAPTERS` (the capability matrix reads this).

    Sorted so ``evalops capabilities --json`` is byte-deterministic; a copy so a caller cannot edit
    the registry behind the matrix's back.
    """
    return {evaluator: ADAPTERS[evaluator] for evaluator in sorted(ADAPTERS)}


def has_native_judge(evaluator: str) -> bool:
    """True iff the evaluator plugin has a native judge adapter (WP3b.3's wiring probe)."""
    return evaluator in ADAPTERS


def native_judge(spec: JudgeSpec, *, evaluator: str) -> JudgeFn:
    """Build ``evaluator``'s native judge for the validated ``spec`` and return it as a neutral scorer.

    An evaluator without an adapter is ``CapabilityError`` -> exit 6 whose hint names the adapters
    that do exist (never exit 5, never a silent fallback: WP3b.3 owns the rubric fallback). A
    missing SDK is ``CapabilityError`` -> exit 6 whose hint is ``pip install <package>``.
    """
    adapter = ADAPTERS.get(evaluator)
    if adapter is None:
        raise CapabilityError(evaluator, "native judge", hint=_no_adapter_hint())
    return _FACTORIES[adapter.key](spec, adapter)


def _no_adapter_hint() -> str:
    """The registry-miss hint: which evaluators have an adapter, and where the gap is filled."""
    return (
        "native judges exist for the evaluator plugins "
        + ", ".join(sorted(ADAPTERS))
        + "; every other evaluator uses the rubric fallback (WP3b.3)"
    )


def _unavailable(adapter: NativeJudge, spec: JudgeSpec) -> CapabilityError:
    """Exit 6 for a missing SDK: the operation names the spec, the hint names the pip package."""
    return CapabilityError(
        adapter.evaluator,
        f"native judge {spec.name!r}",
        hint=f"pip install {adapter.package}",
    )


def _model_parameters(spec: JudgeSpec) -> dict[str, Any]:
    """The spec's sampling as a plain settings dict (the shape every vendor factory takes).

    ``seed`` is omitted when the spec leaves it ``None``: a literal ``seed=None`` means "let the
    vendor pick" in some SDKs and "no seed" in others, so it is not sent.
    """
    parameters: dict[str, Any] = {
        "temperature": spec.sampling.temperature,
        "max_tokens": spec.sampling.max_tokens,
    }
    if spec.sampling.seed is not None:
        parameters["seed"] = spec.sampling.seed
    return parameters


# ---------------------------------------------------------------------------
# the four adapters
# ---------------------------------------------------------------------------
def _mlflow_judge(spec: JudgeSpec, adapter: NativeJudge) -> JudgeFn:
    """``mlflow.genai.make_judge``: MLflow owns the judge prompt and the model call.

    Imports *inside* the body (D4); ``from mlflow.genai import ...`` makes an SDK that is present but
    too old (no ``genai``) an ``ImportError`` too, so both miss the package and miss the module
    become the same exit 6.
    """
    try:
        # VERIFY(native-mlflow): mlflow.genai.make_judge, mlflow >= 3.2
        from mlflow.genai import make_judge  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise _unavailable(adapter, spec) from exc

    judge = make_judge(
        name=spec.name,
        instructions=spec.rubric,  # VERIFY(native-mlflow): the rubric is make_judge's `instructions`
        model=spec.model,
        # VERIFY(native-mlflow): make_judge documents no kwarg for the judge's prompt version or its
        # sampling; both are passed explicitly so nothing is dropped silently. Phase 3 maps them onto
        # MLflow's own judge-prompt version/model settings, or records the decision to drop them.
        prompt_version=spec.prompt_version,
        model_parameters=_model_parameters(spec),
    )

    @scorer(spec.name, llm=True)
    def _judge(*, inputs, outputs, expectations, trace=None):
        # VERIFY(native-mlflow): a make_judge judge is called like a scorer and returns a Feedback
        # whose `.value` is the score (to_float reads it).
        feedback = judge(inputs=inputs, outputs=outputs, expectations=expectations or {})
        return to_float(feedback)

    return _judge


def _langfuse_key(spec: JudgeSpec) -> str:
    """The deterministic managed-evaluator key: judge name + rubric hash + prompt version.

    The key is what makes a *server-side* evaluator pinned: a different rubric (``rubric_hash``) or
    a bumped ``prompt_version`` names a different evaluator instead of silently reading the old
    evaluator's scores back, and it is the identity the nightly settings check re-checks in WP3b.5.
    It carries identity, never rubric text.
    """
    return f"{spec.name}:{rubric_hash(spec.rubric)}:{spec.prompt_version}"


def _first_score(read_back: Any) -> Any:
    """One score out of a read-back that may be a single score or a list of them (VERIFY).

    No score yet is ``None`` (and therefore ``None`` from the scorer), never ``0.0``.
    """
    if isinstance(read_back, (list, tuple)):
        return read_back[0] if read_back else None
    return read_back


def _langfuse_judge(spec: JudgeSpec, adapter: NativeJudge) -> JudgeFn:
    """Langfuse managed evaluator: evalops submits the row and reads the score back (server-side).

    Only the *configuration* happens at build time; execution is Langfuse's, so the judge call is a
    submit + read-back pair. Both routes are VERIFY-marked until phase 3 pins an SDK version.
    """
    try:
        # VERIFY(native-langfuse): the v3 client on the package root
        from langfuse import Langfuse  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise _unavailable(adapter, spec) from exc

    key = _langfuse_key(spec)
    # VERIFY(native-langfuse): the constructor reads LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST from the
    # environment and performs no request; the host is the project's, like every other role.
    client = Langfuse()
    # VERIFY(native-langfuse): `client.api` is the generated public-API client; `evaluators`/`scores`
    # are this adapter's names for the managed-evaluator config and score-read routes.
    api = client.api
    config = api.evaluators.upsert(  # VERIFY(native-langfuse): upsert the managed LLM-as-a-judge config
        name=key,
        prompt=spec.rubric,  # VERIFY(native-langfuse): the evaluator's prompt is the rubric text
        model=spec.model,
        modelParameters=_model_parameters(spec),  # VERIFY(native-langfuse): the sampling channel
        # VERIFY(native-langfuse): which trace fields the evaluator reads.
        variableMapping={"input": "input", "output": "output", "expected_output": "expected_output"},
    )

    @scorer(spec.name, llm=True)
    def _judge(*, inputs, outputs, expectations, trace=None):
        row = {"input": inputs, "output": outputs, "expected_output": expectations or {}}
        # VERIFY(native-langfuse): submitting a row runs the evaluator server-side and returns a
        # handle whose `.id` names the submission.
        submission = api.evaluators.trigger(evaluator=config.name, data=row)
        read_back = api.scores.list(evaluator=key, submission_id=submission.id)  # VERIFY: score read-back
        return to_float(_first_score(read_back))

    return _judge


def _openevals_judge(spec: JudgeSpec, adapter: NativeJudge) -> JudgeFn:
    """openevals ``create_llm_as_judge``: the judge LangSmith's native evaluation calls."""
    try:
        # VERIFY(native-openevals): openevals.llm.create_llm_as_judge
        from openevals.llm import create_llm_as_judge  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise _unavailable(adapter, spec) from exc

    evaluator = create_llm_as_judge(
        model=spec.model,
        prompt=spec.rubric,  # VERIFY(native-openevals): `prompt` accepts the rubric text (str or PromptTemplate)
        feedback_key=spec.name,  # VERIFY(native-openevals): `feedback_key` names the emitted score
        # VERIFY(native-openevals): no documented kwarg for the prompt version or the sampling; passed
        # explicitly so nothing is dropped silently (phase 3 maps them or records the decision).
        prompt_version=spec.prompt_version,
        model_parameters=_model_parameters(spec),
    )

    @scorer(spec.name, llm=True)
    def _judge(*, inputs, outputs, expectations, trace=None):
        # VERIFY(native-openevals): the evaluator is called with the row and returns a
        # {"key", "score", ...} dict (to_float reads the "score" entry).
        result = evaluator(inputs=inputs, outputs=outputs, reference_outputs=expectations or {})
        return to_float(result)

    return _judge


async def _awaited(value: Any) -> Any:
    """Await any awaitable (``asyncio.run`` insists on a coroutine, not a bare awaitable)."""
    return await value


def _pydantic_value(result: Any) -> Any:
    """pydantic-evals returns ``{score_name: EvaluationResult}``; a single entry is unwrapped (VERIFY)."""
    if isinstance(result, Mapping) and len(result) == 1:
        return next(iter(result.values()))
    return result


def _pydantic_evals_judge(spec: JudgeSpec, adapter: NativeJudge) -> JudgeFn:
    """The pydantic-evals ``LLMJudge`` metric, run by the core evaluator (``local``/``logfire``)."""
    try:
        # VERIFY(native-pydantic-evals): both names are importable from pydantic_evals.evaluators
        from pydantic_evals.evaluators import EvaluatorContext, LLMJudge  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise _unavailable(adapter, spec) from exc

    judge = LLMJudge(
        rubric=spec.rubric,
        model=spec.model,
        # VERIFY(native-pydantic-evals): LLMJudge documents no kwarg for the prompt version or the
        # sampling; `model_settings` is pydantic-ai's spelling for the sampling settings.
        prompt_version=spec.prompt_version,
        model_settings=_model_parameters(spec),
    )

    @scorer(spec.name, llm=True)
    def _judge(*, inputs, outputs, expectations, trace=None):
        # VERIFY(native-pydantic-evals): the evaluator protocol takes an EvaluatorContext whose
        # `output`/`expected_output`/`inputs` are the row (duration/metadata are not used by a judge).
        context = EvaluatorContext(
            name=spec.name,
            inputs=inputs,
            output=outputs,
            expected_output=expectations or {},
            metadata=None,
            duration=0.0,
        )
        result = judge.evaluate(context)
        if inspect.isawaitable(result):
            # VERIFY(native-pydantic-evals): LLMJudge.evaluate may be async; the core evaluator is
            # sync, so a coroutine is awaited on a private loop rather than silently returning None.
            result = asyncio.run(_awaited(result))
        return to_float(_pydantic_value(result))

    return _judge


#: adapter key -> factory. Kept separate from :data:`ADAPTERS` so the table stays pure data.
_FACTORIES: dict[str, Callable[[JudgeSpec, NativeJudge], JudgeFn]] = {
    "mlflow-make-judge": _mlflow_judge,
    "langfuse-managed": _langfuse_judge,
    "openevals": _openevals_judge,
    "pydantic-evals": _pydantic_evals_judge,
}
