"""WP3b.2: the four native judge adapters, tested offline against fake SDK modules.

What this module falsifies, one claim per group:

(a) the adapter registry is exactly the packet's four rows (key / evaluator / import name / pip
    package), it hands out a sorted defensive copy, and ``has_native_judge`` is its membership probe;
(b) with a **fake** SDK installed, each adapter sends the *whole* spec to the vendor call -- the
    rubric, ``spec.model`` exactly (never a literal: a second spec with another pin sends that pin),
    ``prompt_version`` and ``sampling`` -- normalizes every result through ``to_float`` (bool,
    ``"yes"``, a dict with ``score``, an object with ``value``) and returns ``None`` (never ``0.0``)
    for an unscorable row, so ``call_scorer`` excludes it from the aggregate;
(c) with **no** fake installed (the real venv genuinely lacks all four SDKs) each adapter raises
    ``CapabilityError`` -> exit 6, chained from ``ImportError``, whose hint is ``pip install
    <package>``, and neither the message nor the hint ever contains rubric text;
(d) an evaluator without an adapter (``sql``) is exit 6 whose hint names the adapters that exist and
    points at the WP3b.3 rubric fallback;
(e) ``scorers.judges.spec_judge`` is a wrapper over that registry (it honours the ``evaluator``
    kwarg), it is re-exported from ``evalops.scorers``, and ``span_judge``/``trajectory_judge`` keep
    the signature and behaviour ``docs/AGENT_TRACES.md`` documents;
(f) the Langfuse managed evaluator's config key is deterministic (judge name + ``rubric_hash`` +
    ``prompt_version``), a not-yet-scored row reads back as ``None``, and a bumped rubric or prompt
    version names a *different* server-side evaluator;
(g) no adapter surface (registry metadata, capability matrix, envelope payload) emits the rubric.

Fakes are installed with :func:`fake_modules` **inside each test** and removed in its ``finally``.
They are never module-level: ``tests/unit/test_presets.py`` imports the real vendor packages (and
the real ``importlib`` failure) in the same process to pin the offline ``doctor`` outcome, so a
leaked ``sys.modules["mlflow"]`` fake would corrupt those pins.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import contextlib
import inspect
import json
import pathlib
import sys
import types
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import evalops.scorers as scorers  # noqa: E402
from evalops import judges, presets  # noqa: E402
from evalops.scorers import judges as judge_wrappers  # noqa: E402
from evalops.scorers import native  # noqa: E402
from evalops.scoring import call_scorer, scorer_name  # noqa: E402
from evalops.util import EXIT_CAPABILITY, CapabilityError  # noqa: E402

#: A rubric distinctive enough that "is it leaking?" is a real question (never asserted on text).
RUBRIC = "Give 1 when the reply refunds the order and 0 otherwise; judge only the reply, not the ask."


# ---------------------------------------------------------------------------
# helpers: a validated spec, fake SDK modules, a normalization matrix
# ---------------------------------------------------------------------------
def a_spec(
    *,
    name: str = "refund_correctness",
    model: str = "openai:/gpt-4o-2024-08-06",
    prompt_version: str = "judge-v3",
    rubric: str = RUBRIC,
    sampling: dict | None = None,
) -> judges.JudgeSpec:
    """A real (validated) ``JudgeSpec``; ``sampling`` is the non-default one when a test passes it."""
    rule: dict[str, Any] = {"rubric": rubric, "model": model, "prompt_version": prompt_version}
    if sampling is not None:
        rule["sampling"] = sampling
    return judges.JudgeSpec.from_rule(name, rule)


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    """A ``types.ModuleType`` standing in for one vendor module."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@contextlib.contextmanager
def fake_modules(modules: dict[str, types.ModuleType]):
    """Install fake SDK modules for exactly one test, restoring ``sys.modules`` afterwards (E1).

    ``sys.modules`` is saved per name and restored in ``finally``, so no fake survives the test even
    when the assertion inside fails.
    """
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


#: Vendor result shapes and the float each must normalize to; ``None`` means "unscorable".
SCORE_SHAPES: tuple[tuple[Any, float | None], ...] = (
    (True, 1.0),
    (False, 0.0),
    ("yes", 1.0),
    ({"key": "k", "score": 0.25}, 0.25),  # LangSmith/openevals dict
    (types.SimpleNamespace(value=0.5), 0.5),  # MLflow Feedback / Langfuse Evaluation / pydantic result
    (None, None),
    ({"key": "k"}, None),  # a dict with no score/value
    (types.SimpleNamespace(comment="not scored"), None),  # an object with no score/value
)


def check_normalization(judge, state: dict, *, wrap=None) -> None:
    """Feed every vendor shape through the scorer: ``to_float`` normalizes, unscorable is ``None``.

    ``call_scorer`` returning ``{}`` (rather than ``{name: 0.0}``) is the headline assertion: a judge
    that produced no score must be excluded from the aggregate, not counted as a failure.
    """
    for raw, expected in SCORE_SHAPES:
        state["result"] = wrap(raw) if wrap is not None else raw
        got = call_scorer(judge, inputs={"q": "refund?"}, outputs="refunded", expectations={})
        if expected is None:
            assert got == {}, f"unscorable {raw!r} normalized to {got!r}, not excluded"
        else:
            assert got == {judge.evalops_name: expected}, (raw, got)


def check_vendor_call(call: dict, expected: dict, adapter: str) -> None:
    """The vendor call carries exactly the spec fields the adapter promises (nothing invented)."""
    for key, value in expected.items():
        assert key in call, f"{adapter}: the vendor call never received {key!r}: {sorted(call)}"
        assert call[key] == value, (adapter, key, call[key], value)


def check_neutral_scorer(judge, spec: judges.JudgeSpec) -> None:
    """A neutral scorer: named ``spec.name``, marked ``llm``, callable by ``call_scorer``."""
    assert scorer_name(judge) == spec.name
    assert judge.evalops_name == spec.name and judge.evalops_llm is True
    assert callable(judge)


# ---------------------------------------------------------------------------
# per-adapter fakes: each mirrors the documented vendor shape it stands in for
# ---------------------------------------------------------------------------
def mlflow_fake() -> tuple[dict[str, types.ModuleType], dict]:
    state: dict[str, Any] = {"calls": [], "result": None}

    def make_judge(**kwargs):
        state["calls"].append(kwargs)

        def judge(*, inputs, outputs, expectations):  # an mlflow Judge is called like a scorer
            return state["result"]

        return judge

    genai = fake_module("mlflow.genai", make_judge=make_judge)
    return {"mlflow": fake_module("mlflow", genai=genai), "mlflow.genai": genai}, state


def langfuse_fake() -> tuple[dict[str, types.ModuleType], dict]:
    state: dict[str, Any] = {
        "client_kwargs": [],
        "configs": [],
        "triggers": [],
        "reads": [],
        "scores": [],
        "result": None,
    }

    class FakeEvaluators:
        def upsert(self, **kwargs):
            state["configs"].append(kwargs)
            return types.SimpleNamespace(name=kwargs["name"])

        def trigger(self, **kwargs):
            state["triggers"].append(kwargs)
            return types.SimpleNamespace(id=f"sub-{len(state['triggers'])}")

    class FakeScores:
        def list(self, **kwargs):
            state["reads"].append(kwargs)
            return state["scores"].pop(0) if state["scores"] else state["result"]

    class FakeLangfuse:
        def __init__(self, **kwargs):
            state["client_kwargs"].append(kwargs)
            self.api = types.SimpleNamespace(evaluators=FakeEvaluators(), scores=FakeScores())

    return {"langfuse": fake_module("langfuse", Langfuse=FakeLangfuse)}, state


def openevals_fake() -> tuple[dict[str, types.ModuleType], dict]:
    state: dict[str, Any] = {"calls": [], "result": None}

    def create_llm_as_judge(**kwargs):
        state["calls"].append(kwargs)

        def evaluator(*, inputs, outputs, reference_outputs):
            return state["result"]

        return evaluator

    llm = fake_module("openevals.llm", create_llm_as_judge=create_llm_as_judge)
    return {"openevals": fake_module("openevals", llm=llm), "openevals.llm": llm}, state


def pydantic_evals_fake() -> tuple[dict[str, types.ModuleType], dict]:
    state: dict[str, Any] = {"calls": [], "contexts": [], "result": None}

    class FakeLLMJudge:
        def __init__(self, **kwargs):
            state["calls"].append(kwargs)

        def evaluate(self, context):
            state["contexts"].append(context)
            return state["result"]

    class FakeEvaluatorContext:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    evaluators = fake_module("pydantic_evals.evaluators", LLMJudge=FakeLLMJudge, EvaluatorContext=FakeEvaluatorContext)
    pkg = fake_module("pydantic_evals", evaluators=evaluators)
    return {"pydantic_evals": pkg, "pydantic_evals.evaluators": evaluators}, state


# ---------------------------------------------------------------------------
# (a) the registry
# ---------------------------------------------------------------------------
def test_the_adapter_registry_is_the_four_documented_rows():
    expected = {
        "mlflow": ("mlflow-make-judge", "mlflow", "mlflow"),
        "langfuse": ("langfuse-managed", "langfuse", "langfuse"),
        "langsmith": ("openevals", "openevals", "openevals"),
        "core": ("pydantic-evals", "pydantic_evals", "pydantic-evals"),
    }
    assert len(native.ADAPTERS) == 4
    rows = {evaluator: (row.key, row.import_name, row.package) for evaluator, row in native.ADAPTERS.items()}
    assert rows == expected
    # databricks is served through the mlflow evaluator and local/logfire/mixed through core, so
    # neither may appear as a registry key of its own
    assert "databricks" not in native.ADAPTERS
    for absent in ("local", "logfire", "mixed"):
        assert not native.has_native_judge(absent)
    assert all(native.has_native_judge(evaluator) for evaluator in expected)
    for row in native.ADAPTERS.values():
        assert row.key and row.description and "\n" not in row.description and "|" not in row.description


def test_server_side_is_set_only_for_the_langfuse_managed_adapter():
    """WP3b.3 D2: the estimate's honesty bit, and it renders nowhere in the capability matrix."""
    flags = {evaluator: row.server_side for evaluator, row in native.ADAPTERS.items()}
    assert flags == {"core": False, "langfuse": True, "langsmith": False, "mlflow": False}, flags
    # a new adapter defaults to client-side (the safe direction: it is counted, not silently free)
    default = native.NativeJudge(key="k", evaluator="e", import_name="i", package="p", description="d")
    assert default.server_side is False
    # the flag is an estimate decision, not a capability claim: it is not part of any payload
    assert "server_side" not in json.dumps(presets.capabilities_payload(), default=str)
    assert "server_side" not in presets.capabilities_text()


def test_adapters_hands_out_a_sorted_defensive_copy():
    copy = native.adapters()
    assert list(copy) == ["core", "langfuse", "langsmith", "mlflow"], "the matrix needs a sorted view"
    assert copy is not native.ADAPTERS
    assert copy == native.adapters(), "two calls must agree (deterministic payload)"
    copy["mlflow"] = None  # type: ignore[assignment]
    assert native.ADAPTERS["mlflow"].key == "mlflow-make-judge", "the registry was mutated through the copy"
    assert native.has_native_judge("mlflow") and not native.has_native_judge("sql")


# ---------------------------------------------------------------------------
# (b) each adapter sends the whole spec and normalizes the vendor result
# ---------------------------------------------------------------------------
def test_mlflow_adapter_sends_the_whole_spec_and_normalizes_feedback():
    modules, state = mlflow_fake()
    spec = a_spec(sampling={"temperature": 0.7, "max_tokens": 256, "seed": 7})
    with fake_modules(modules):
        judge = native.native_judge(spec, evaluator="mlflow")
        check_neutral_scorer(judge, spec)
        assert len(state["calls"]) == 1
        check_vendor_call(
            state["calls"][0],
            {
                "name": spec.name,
                "instructions": spec.rubric,
                "model": spec.model,
                "prompt_version": spec.prompt_version,
                "model_parameters": {"temperature": 0.7, "max_tokens": 256, "seed": 7},
            },
            "mlflow",
        )
        # the pin is a value, not a constant: another spec sends another model
        other = a_spec(model="anthropic:/claude-sonnet-4-5")
        native.native_judge(other, evaluator="mlflow")
        assert state["calls"][1]["model"] == other.model != spec.model
        check_normalization(judge, state)


def test_langfuse_adapter_configures_a_pinned_evaluator_and_reads_the_score_back():
    modules, state = langfuse_fake()
    spec = a_spec(sampling={"temperature": 0.0, "max_tokens": 128})
    with fake_modules(modules):
        judge = native.native_judge(spec, evaluator="langfuse")
        check_neutral_scorer(judge, spec)
        config = state["configs"][0]
        key = config["name"]
        # the config key is the identity: judge name + rubric_hash + prompt_version (never the rubric)
        assert key == f"{spec.name}:{judges.rubric_hash(spec.rubric)}:{spec.prompt_version}"
        assert spec.rubric not in key and "\n" not in key
        check_vendor_call(
            config,
            {
                "prompt": spec.rubric,
                "model": spec.model,
                # a spec with no seed must not send seed=None
                "modelParameters": {"temperature": 0.0, "max_tokens": 128},
            },
            "langfuse",
        )
        # a different rubric, or a bumped prompt version, names a different server-side evaluator
        native.native_judge(a_spec(rubric=RUBRIC + " Prefer the cheaper refund."), evaluator="langfuse")
        native.native_judge(a_spec(prompt_version="judge-v4"), evaluator="langfuse")
        assert state["configs"][1]["name"] != key and state["configs"][2]["name"] != key
        assert state["configs"][1]["name"] != state["configs"][2]["name"]
        # a row nobody has scored yet reads back as None, and the row really was submitted
        assert call_scorer(judge, inputs={}, outputs="refunded", expectations={}) == {}
        assert state["triggers"][-1]["evaluator"] == key
        assert state["reads"][-1]["submission_id"] == f"sub-{len(state['triggers'])}"
        # an empty read-back list is unscorable too, and a one-element list is that element
        state["result"] = []
        assert call_scorer(judge, inputs={}, outputs="x", expectations={}) == {}
        state["result"] = [{"value": 0.5}]
        assert call_scorer(judge, inputs={}, outputs="x", expectations={}) == {spec.name: 0.5}
        check_normalization(judge, state)


def test_openevals_adapter_sends_the_whole_spec_and_normalizes_the_score():
    modules, state = openevals_fake()
    spec = a_spec(sampling={"temperature": 0.2, "max_tokens": 64, "seed": 3})
    with fake_modules(modules):
        judge = native.native_judge(spec, evaluator="langsmith")
        check_neutral_scorer(judge, spec)
        check_vendor_call(
            state["calls"][0],
            {
                "model": spec.model,
                "prompt": spec.rubric,
                "feedback_key": spec.name,
                "prompt_version": spec.prompt_version,
                "model_parameters": {"temperature": 0.2, "max_tokens": 64, "seed": 3},
            },
            "openevals",
        )
        other = a_spec(model="anthropic:/claude-sonnet-4-5")
        native.native_judge(other, evaluator="langsmith")
        assert state["calls"][1]["model"] == other.model != spec.model
        check_normalization(judge, state)


def test_pydantic_evals_adapter_sends_the_whole_spec_and_normalizes_the_metric():
    modules, state = pydantic_evals_fake()
    spec = a_spec(sampling={"temperature": 0.1, "max_tokens": 32})
    with fake_modules(modules):
        judge = native.native_judge(spec, evaluator="core")
        check_neutral_scorer(judge, spec)
        check_vendor_call(
            state["calls"][0],
            {
                "rubric": spec.rubric,
                "model": spec.model,
                "prompt_version": spec.prompt_version,
                "model_settings": {"temperature": 0.1, "max_tokens": 32},
            },
            "pydantic-evals",
        )
        # the row reaches the evaluator context the judge scores
        call_scorer(judge, inputs={"q": "refund?"}, outputs="refunded", expectations={"ok": True})
        context = state["contexts"][-1]
        assert (context.name, context.inputs, context.output) == (spec.name, {"q": "refund?"}, "refunded")
        assert context.expected_output == {"ok": True}
        # pydantic-evals answers with {score_name: EvaluationResult}; one entry is unwrapped
        check_normalization(judge, state, wrap=lambda raw: {spec.name: types.SimpleNamespace(value=raw)})


# ---------------------------------------------------------------------------
# (c) the real ImportError path: the venv has none of the four SDKs
# ---------------------------------------------------------------------------
def test_a_missing_sdk_is_exit_6_naming_the_pip_package_and_never_the_rubric():
    spec = a_spec()
    for evaluator, adapter in native.adapters().items():
        assert adapter.import_name not in sys.modules, f"{adapter.import_name} leaked into sys.modules"
        try:
            native.native_judge(spec, evaluator=evaluator)
        except CapabilityError as exc:
            assert exc.code == EXIT_CAPABILITY, evaluator
            # chained from the ImportError (not a bare traceback, not exit 5)
            assert isinstance(exc.__cause__, ImportError), evaluator
            assert exc.hint == f"pip install {adapter.package}", (evaluator, exc.hint)
            assert f"pip install {adapter.package}" in str(exc), evaluator
            assert spec.name in str(exc), "the operation must name the spec it could not build"
            # the rubric is never emitted: not in the message, the hint, the details or a repr
            for surface in (str(exc), repr(exc), exc.hint or "", json.dumps(exc.details, default=str)):
                assert RUBRIC not in surface, (evaluator, surface)
        else:
            raise AssertionError(f"{evaluator}: a missing SDK built a scorer instead of raising CapabilityError")


def test_an_evaluator_without_an_adapter_is_exit_6_naming_the_adapters():
    spec = a_spec()
    assert not native.has_native_judge("sql")
    try:
        native.native_judge(spec, evaluator="sql")
    except CapabilityError as exc:
        assert exc.code == EXIT_CAPABILITY
        message = str(exc)
        assert "sql" in message
        for evaluator in native.ADAPTERS:
            assert evaluator in (exc.hint or ""), f"the hint does not name the {evaluator} adapter"
        assert "WP3b.3" in (exc.hint or ""), "the hint must point at the rubric fallback"
        assert RUBRIC not in message and RUBRIC not in repr(exc)
        assert exc.__cause__ is None, "a registry miss is not an ImportError"
    else:
        raise AssertionError("an evaluator without an adapter did not raise CapabilityError")
    # databricks is not an adapter key: it is served by the mlflow evaluator, never by its own row
    assert native.ADAPTERS["mlflow"].evaluator == "mlflow"


# ---------------------------------------------------------------------------
# (e) the wrapper layer
# ---------------------------------------------------------------------------
def test_spec_judge_is_the_wrapper_over_the_adapter_registry():
    modules, state = mlflow_fake()
    spec = a_spec()
    with fake_modules(modules):
        judge = judge_wrappers.spec_judge(spec, evaluator="mlflow")
        check_neutral_scorer(judge, spec)
        assert state["calls"][0]["model"] == spec.model
        assert call_scorer(judge, inputs={}, outputs="x", expectations={}) == {}
    # it does not ignore the evaluator kwarg: langfuse's SDK is absent, so it must refuse
    try:
        judge_wrappers.spec_judge(spec, evaluator="langfuse")
    except CapabilityError as exc:
        assert exc.code == EXIT_CAPABILITY and "langfuse" in str(exc)
    else:
        raise AssertionError("spec_judge built a scorer without consulting the evaluator kwarg")
    assert scorers.spec_judge is judge_wrappers.spec_judge, "spec_judge is not re-exported from evalops.scorers"


def test_the_existing_judge_wrappers_keep_their_signature_and_behaviour():
    assert list(inspect.signature(judge_wrappers.span_judge).parameters) == [
        "judge",
        "name",
        "kind",
        "select",
        "max_spans",
    ]
    assert list(inspect.signature(judge_wrappers.trajectory_judge).parameters) == ["judge", "name", "max_chars"]
    assert scorers.span_judge is judge_wrappers.span_judge
    assert scorers.trajectory_judge is judge_wrappers.trajectory_judge

    class StubTrace:
        """The two trace methods ``docs/AGENT_TRACES.md`` says these wrappers use."""

        def of_kind(self, kind):
            return [types.SimpleNamespace(kind=kind), types.SimpleNamespace(kind=kind)]

        def render(self, max_chars=300):
            return f"rendered<{max_chars}>"

    calls = []
    span = scorers.span_judge(lambda **kw: calls.append(("span", kw)) or "yes", name="step_ok")
    assert call_scorer(span, inputs={"q": "x"}, outputs="y", expectations={}, trace=StubTrace()) == {"step_ok": 1.0}
    assert len(calls) == 2, "span_judge must judge each selected span once"

    calls.clear()
    whole = scorers.trajectory_judge(lambda **kw: calls.append(("row", kw)) or 0.5, name="traj")
    assert call_scorer(whole, inputs={}, outputs="y", expectations={}, trace=StubTrace()) == {"traj": 0.5}
    assert len(calls) == 1, "trajectory_judge must make one call per row"
    assert calls[0][1]["trajectory"] == "rendered<300>"


# ---------------------------------------------------------------------------
# (g) no adapter surface emits the rubric
# ---------------------------------------------------------------------------
def test_no_adapter_surface_emits_the_rubric():
    spec = a_spec()
    for adapter in native.adapters().values():
        for surface in (adapter.key, adapter.package, adapter.description, repr(adapter)):
            assert RUBRIC not in surface
    payload = json.dumps(presets.capabilities_payload(), default=str)
    assert RUBRIC not in payload and spec.rubric not in payload
    assert RUBRIC not in presets.capabilities_text()
