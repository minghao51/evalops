"""WP3b.3: the evalops rubric fallback over LiteLLM, strict parsing, per-call cost, spec wiring.

What this module falsifies, group by group:

(a) the fallback is a *neutral* scorer -- ``scorer(spec.name, llm=True)`` over
    ``(*, inputs, outputs, expectations, trace=None)`` -> ``float``, consumed by
    ``scoring.call_scorer`` unchanged -- and the **whole spec** reaches the LiteLLM call: the pinned
    ``model`` exactly (a second spec sends its own pin, so no literal is hiding there), the sampling
    (temperature/max_tokens, ``seed`` omitted when the spec has none), JSON response mode, and the
    rubric in the system message together with the declared scale. The rubric never leaves the
    request: it is absent from every error message, ``repr`` and envelope;
(b) strict parsing: valid JSON passes through, every other shape -- invalid JSON, no ``"score"``
    key, a bool, a numeric *string*, ``null``, a non-object, empty/missing content, the non-standard
    ``NaN``/``Infinity``/``-Infinity`` constants, a number that cannot become a finite float (a
    5000-digit int, ``1e400``) -- raises ``EvalOpsError`` (exit 5) naming the judge with at most
    ``MAX_EVIDENCE_CHARS`` of raw output, and **never** returns ``0.0`` or ``None``; an out-of-range
    score clamps to the declared scale's own bounds (1.7 -> 1.0, -2 -> 0.0 on binary; 9 -> 5.0,
    -3 -> 1.0 on ordinal 1..5) while an in-range score is returned unchanged; evidence that
    reproduces the rubric -- the rubric itself as content, or a vendor exception embedding the
    request body -- is redacted, so neither the raised message nor a persisted row error carries it;
(c) a missing ``litellm`` (the real venv has none) is ``CapabilityError`` -> exit 6 with
    ``pip install litellm``, chained from the ``ImportError``;
(d) token usage is reported through ``evalops.usage.report_usage`` -- the channel the row loop reads
    -- and a response with no usage reports nothing (evalops never invents tokens). The end-to-end
    half of H5 is pinned too: through a real ``evaluate`` (core evaluator, temp project, fake
    litellm) the judge's tokens land in ``row.usage`` *in addition to* the app's reported usage
    (210/50 + 1200/34 -> 1410/84, cost summed), ``cost.measure`` counts the combined tokens, and a
    scorer that raises still contributes the tokens it spent before the error;
(e) ``spec_judge`` dispatches on the executor: ``native`` -> the WP3b.2 registry (unchanged),
    ``litellm`` -> this fallback (evaluator-independently, even for an evaluator with no adapter),
    and a declared spec becomes a real metric in ``evaluate`` with either executor;
(f) the D1/D2 wiring in ``run.py``: a declared spec adds a scorer (and a *client-side* call to the
    estimate) unless its native adapter is server-side, a threshold-only rule changes nothing, and an
    app scorer colliding with a spec name is exit 2 before any ledger row;
(g) the D3 pin: ``calibrate`` reports real agreement/kappa/MAE pairs for a declared spec.

Fakes are installed with :func:`fake_modules` **inside each test** and removed in its ``finally``
(the same pattern as ``test_native_judges.py``): ``tests/unit/test_presets.py`` pins the real
offline vendor-import outcome in the same process, so a leaked ``sys.modules["litellm"]`` fake would
corrupt it. Every test is a zero-argument function, so ``python tests/run_without_pytest.py`` runs
this module too.
"""

import contextlib
import json
import pathlib
import shutil
import sqlite3
import sys
import types
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import cost as cost_mod  # noqa: E402
from evalops import judges as judges_mod  # noqa: E402
from evalops import run as run_mod  # noqa: E402
from evalops import usage as usage_mod  # noqa: E402
from evalops.ports import RowResult, Usage  # noqa: E402
from evalops.scorers import judges as judge_wrappers  # noqa: E402
from evalops.scorers import litellm_judge as fallback  # noqa: E402
from evalops.scoring import call_scorer, scorer_name  # noqa: E402
from evalops.util import EXIT_CAPABILITY, EXIT_RUNTIME, EXIT_USAGE, CapabilityError, EvalOpsError  # noqa: E402

#: A rubric distinctive enough that "did it leak?" is a real question (never asserted as a substring
#: of a *message*; only used to build the request and to prove its absence).
RUBRIC = "Give 1 when the reply refunds the order and 0 otherwise; judge only the reply, not the ask."

#: YAML for one fully specified judge on the fake app's project: a *litellm* executor.
LITELLM_JUDGE = """
judges:
  correctness:
    model: fake-judge-2024-11-20
    rubric: |
      Give 1 when the reply answers the question in the knowledge base and 0 otherwise.
    prompt_version: "1"
    judge: litellm
    scale: {kind: binary, min: 0.0, max: 1.0}
    sampling: {temperature: 0.0, max_tokens: 256, seed: 7}
"""
#: The same judge with no ``judge`` key at all: the evaluator's native adapter (the default).
NATIVE_JUDGE = LITELLM_JUDGE.replace("    judge: litellm\n", "")
#: Thresholds only -- the shipped ``evalops.yaml`` shape. Declares no spec (WP3b.1's discriminator).
THRESHOLD_JUDGE = "\njudges:\n  correctness: {min_agreement: 0.85, min_kappa: 0.6}\n"
#: An app scorer named ``helpful`` (``fake_app:build_scorers``) colliding with a declared spec.
COLLIDING_JUDGE = LITELLM_JUDGE.replace("  correctness:", "  helpful:")


# ---------------------------------------------------------------------------
# helpers: a validated spec, a fake litellm module, a strict-parse harness
# ---------------------------------------------------------------------------
def a_spec(**overrides: Any) -> judges_mod.JudgeSpec:
    """A real (validated) ``JudgeSpec``, with ``overrides`` applied to the raw rule."""
    rule: dict[str, Any] = {
        "rubric": RUBRIC,
        "model": "openai:/gpt-4o-2024-08-06",
        "prompt_version": "judge-v3",
    }
    rule.update(overrides)
    return judges_mod.JudgeSpec.from_rule(str(rule.pop("name", "refund_correctness")), rule)


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@contextlib.contextmanager
def fake_modules(modules: dict[str, types.ModuleType]):
    """Install fake SDK modules for exactly one test, restoring ``sys.modules`` afterwards."""
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


class LitellmState:
    """The fake ``litellm`` module's state: recorded calls, the content, and the usage block."""

    def __init__(self, content: Any = '{"score": 0.83}', prompt_tokens: Any = 120, completion_tokens: Any = 8):
        self.calls: list[dict] = []
        self.content = content
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def response(self) -> Any:
        """An OpenAI-shaped ``ModelResponse``: ``choices[0].message.content`` + ``usage``."""
        usage = None
        if self.prompt_tokens is not None or self.completion_tokens is not None:
            usage = types.SimpleNamespace(prompt_tokens=self.prompt_tokens, completion_tokens=self.completion_tokens)
        message = types.SimpleNamespace(content=self.content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=usage)


def litellm_fake(content: Any = '{"score": 0.83}', **usage: Any) -> tuple[dict[str, types.ModuleType], LitellmState]:
    """A fake ``litellm`` module: ``completion(**kwargs)`` records its kwargs and answers ``content``."""
    state = LitellmState(content=content, **usage)

    def completion(**kwargs):
        state.calls.append(kwargs)
        return state.response()

    return {"litellm": fake_module("litellm", completion=completion)}, state


def _error(fn) -> EvalOpsError:
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError, the call returned normally")


def _call(judge) -> float:
    """Call a scorer through ``call_scorer`` and return its one score (asserting it produced one)."""
    scores = call_scorer(judge, inputs={"question": "Can I get a refund?"}, outputs="Within 30 days.", expectations={})
    assert set(scores) == {scorer_name(judge)}, scores
    return scores[scorer_name(judge)]


def _judge_with(content: Any, *, spec: judges_mod.JudgeSpec | None = None, **usage: Any):
    """Build the fallback over a fake litellm whose completion answers ``content``."""
    modules, state = litellm_fake(content, **usage)
    spec = spec or a_spec()
    with fake_modules(modules):
        return fallback.litellm_judge(spec), state


# ---------------------------------------------------------------------------
# (a) the neutral scorer + the vendor call shape
# ---------------------------------------------------------------------------
def test_the_fallback_is_a_neutral_llm_scorer_named_after_the_spec():
    spec = a_spec()
    modules, state = litellm_fake('{"score": 0.83}')
    with fake_modules(modules):
        judge = fallback.litellm_judge(spec)
        assert scorer_name(judge) == spec.name
        # the decorator attaches these dynamically (the same contract WP3b.2's adapters honour)
        assert getattr(judge, "evalops_name", None) == spec.name, judge
        assert getattr(judge, "evalops_llm", None) is True, "a judge scorer must be marked llm"
        assert callable(judge)
        assert _call(judge) == 0.83
        assert len(state.calls) == 1


def test_the_full_spec_reaches_the_litellm_call():
    modules, state = litellm_fake('{"score": 1}')
    spec = a_spec(sampling={"temperature": 0.4, "max_tokens": 64, "seed": 11})
    with fake_modules(modules):
        judge = fallback.litellm_judge(spec)
        assert _call(judge) == 1.0
    call = state.calls[0]
    # the pin is a value, not a literal: another spec sends another model
    other = a_spec(model="anthropic:/claude-sonnet-4-5", name="other_judge")
    modules2, state2 = litellm_fake('{"score": 0}')
    with fake_modules(modules2):
        assert _call(fallback.litellm_judge(other)) == 0.0
    assert state2.calls[0]["model"] == other.model != call["model"]
    assert call["model"] == spec.model
    # the sampling rides along
    assert (call["temperature"], call["max_tokens"], call["seed"]) == (0.4, 64, 11)
    # JSON mode, so the output contract is requested, not merely described
    assert call["response_format"] == {"type": "json_object"}
    # the rubric + the declared scale are the system message; the row is the user message
    system, user = call["messages"][0], call["messages"][1]
    assert (system["role"], user["role"]) == ("system", "user")
    assert spec.rubric in system["content"]
    assert "0.0" in system["content"] and "1.0" in system["content"], "the scale declaration is missing"
    assert '"score"' in system["content"], "the JSON output contract is missing"
    assert "Can I get a refund?" in user["content"] and "Within 30 days." in user["content"]


def test_the_seed_is_omitted_when_the_spec_has_none():
    modules, state = litellm_fake('{"score": 0.5}')
    with fake_modules(modules):
        assert _call(fallback.litellm_judge(a_spec(sampling={"temperature": 0.0, "max_tokens": 32}))) == 0.5
    assert "seed" not in state.calls[0], "a spec with no seed must not send seed=None"


def test_the_prompt_template_is_versioned_and_the_row_payload_is_bounded():
    assert fallback.PROMPT_TEMPLATE_VERSION.startswith("litellm-rubric/")
    assert fallback.MAX_ROW_CHARS > 0 and fallback.MAX_EVIDENCE_CHARS > 0
    # a row larger than the bound is truncated and says so, instead of blowing the context window
    modules, state = litellm_fake('{"score": 0.5}')
    with fake_modules(modules):
        judge = fallback.litellm_judge(a_spec())
        call_scorer(judge, inputs={"question": "q" * (fallback.MAX_ROW_CHARS * 2)}, outputs="x", expectations={})
    user = state.calls[0]["messages"][1]["content"]
    assert len(user) <= fallback.MAX_ROW_CHARS + 100, len(user)
    assert "truncated" in user


# ---------------------------------------------------------------------------
# (b) the strict-parse matrix
# ---------------------------------------------------------------------------
def test_a_valid_score_passes_through_unchanged():
    for content, expected in (
        ('{"score": 0.83}', 0.83),
        ('{"score": 0}', 0.0),
        ('{"score": 1}', 1.0),
        ('{"score": 0.5, "reason": "it refunds"}', 0.5),
        ('  {"score": 0.25}  ', 0.25),
    ):
        judge, _ = _judge_with(content)
        assert _call(judge) == expected, content


def test_every_broken_output_raises_naming_the_judge_and_never_returns_zero_or_none():
    """The headline strictness claim: a parse failure is an error, never a silent ``0.0``/``None``."""
    cases = {
        "invalid_json": "not json at all",
        "truncated_json": '{"score": 0.',
        "missing_score": '{"reason": "looks fine"}',
        "bool_score": '{"score": true}',
        "numeric_string_score": '{"score": "0.9"}',
        "null_score": '{"score": null}',
        "list_output": "[0.5]",
        "string_output": '"0.5"',
        "empty_content": "   ",
        "none_content": None,
    }
    spec = a_spec()
    for label, content in cases.items():
        modules, state = litellm_fake(content)
        with fake_modules(modules):
            judge = fallback.litellm_judge(spec)
            exc = _error(lambda bound=judge: _call(bound))
        assert exc.code == EXIT_RUNTIME, (label, exc.code)
        assert spec.name in str(exc), (label, str(exc))
        assert str(exc).startswith(f"judge '{spec.name}':"), (label, str(exc))
        # the rubric is never emitted, not even as context for a parse failure
        for surface in (str(exc), repr(exc), json.dumps(exc.details, default=str)):
            assert spec.rubric not in surface, (label, surface)
            assert "Give 1 when the reply refunds" not in surface, (label, surface)


def test_the_evidence_snippet_is_bounded_and_never_the_rubric():
    long_output = "z" * 5000
    judge, _ = _judge_with(long_output)
    exc = _error(lambda: _call(judge))
    assert exc.code == EXIT_RUNTIME
    assert long_output not in str(exc), "the whole completion was quoted"
    assert str(exc).count("z") <= fallback.MAX_EVIDENCE_CHARS + 8, str(exc).count("z")


def test_a_response_without_choices_is_a_rejection_not_a_crash():
    spec = a_spec()

    def completion(**kwargs):
        return types.SimpleNamespace(choices=[])

    with fake_modules({"litellm": fake_module("litellm", completion=completion)}):
        judge = fallback.litellm_judge(spec)
        exc = _error(lambda: _call(judge))
    assert exc.code == EXIT_RUNTIME and spec.name in str(exc), exc


def test_out_of_range_scores_clamp_to_the_declared_scale_and_in_range_passes_through():
    # binary 0..1: both sides clamp to the scale's own bounds
    assert _call(_judge_with('{"score": 1.7}')[0]) == 1.0
    assert _call(_judge_with('{"score": -2}')[0]) == 0.0
    for content, expected in (('{"score": 0.0}', 0.0), ('{"score": 0.42}', 0.42), ('{"score": 1.0}', 1.0)):
        assert _call(_judge_with(content)[0]) == expected, content

    # an ordinal 1..5 scale clamps to 1..5, not to 0..1
    ordinal = a_spec(scale={"kind": "ordinal", "min": 1.0, "max": 5.0})
    assert _call(_judge_with('{"score": 9}', spec=ordinal)[0]) == 5.0
    assert _call(_judge_with('{"score": -3}', spec=ordinal)[0]) == 1.0
    assert _call(_judge_with('{"score": 3}', spec=ordinal)[0]) == 3.0


def test_non_finite_and_unrepresentable_scores_are_rejected_naming_the_judge():
    """Reviewer regression: ``NaN``/``Infinity``/a 5000-digit int must never reach the metrics.

    ``json.loads`` accepts the non-standard constants by default, and ``NaN`` escapes the clamp
    because every comparison against it is ``False``; a huge integer literal either makes the
    decoder or the ``float()`` raise a raw ``ValueError``/``OverflowError``. Both are now the
    judge-named exit-5 rejection, and nothing non-finite can leave the clamp.
    """
    spec = a_spec()
    huge = "9" * 5000  # past CPython's 4300-digit int limit: the decoder itself refuses it
    overflow = "9" * 400  # a valid int that does not fit a float: `float()` overflows
    # label -> (content, the fragment only the *right* rejection produces). The fragments matter:
    # they separate the three distinct guards (parse_constant, decoder ValueError, float/finite),
    # so dropping one guard cannot hide behind another one catching the same input.
    cases = {
        "nan": ('{"score": NaN}', "non-standard JSON constant NaN"),
        "infinity": ('{"score": Infinity}', "non-standard JSON constant Infinity"),
        "negative_infinity": ('{"score": -Infinity}', "non-standard JSON constant -Infinity"),
        "nan_lowercase": ('{"score": nan}', "output is not valid JSON"),  # not a constant, not a number
        "overflowing_literal": ('{"score": 1e400}', "not a finite number"),  # finite syntax, infinite value
        "huge_int": ('{"score": ' + huge + "}", "number literal out of range"),
        "unrepresentable_int": ('{"score": ' + overflow + "}", "cannot represent as a float"),
    }
    for label, (content, expected) in cases.items():
        judge, _ = _judge_with(content)
        exc = _error(lambda bound=judge: _call(bound))
        assert exc.code == EXIT_RUNTIME, (label, exc.code)
        assert str(exc).startswith(f"judge '{spec.name}':"), (label, str(exc))
        assert expected in str(exc), (label, expected, str(exc))
        for surface in (str(exc), repr(exc), json.dumps(exc.details, default=str)):
            assert spec.rubric not in surface, (label, surface)

    # the clamp itself is the last line of defence: a non-finite value is rejected, never clamped
    for value in (float("nan"), float("inf"), float("-inf")):
        try:
            fallback._clamp(value, spec)
        except EvalOpsError as exc:
            assert exc.code == EXIT_RUNTIME and "finite" in str(exc), exc
        else:
            raise AssertionError(f"_clamp returned {value!r} instead of rejecting it")


# ---------------------------------------------------------------------------
# (b) the rubric is request-only: a rubric echo never reaches an error message
# ---------------------------------------------------------------------------
def test_a_rubric_echo_never_reaches_an_error_message_or_the_row_error():
    """Reviewer regression: content that *is* the rubric must not be quoted back as evidence.

    Both surfaces: the raised error's message/``details`` and, through a real ``evaluate``, the
    persisted row's ``error`` (the ``r.error`` the CLI reports).
    """
    spec = a_spec()
    judge, _ = _judge_with(spec.rubric)
    exc = _error(lambda: _call(judge))
    assert exc.code == EXIT_RUNTIME and spec.name in str(exc), exc
    assert fallback.RUBRIC_ECHO_MARKER in str(exc), "the rubric echo was not marked as redacted"
    for surface in (str(exc), repr(exc), json.dumps(exc.details, default=str)):
        assert spec.rubric not in surface, surface
        assert "refunds the order" not in surface, surface

    # a *vendor* exception that embeds the request body is sanitized the same way
    def completion(**kwargs):
        raise RuntimeError(f"400 Bad Request: request body was {kwargs['messages']}")

    with fake_modules({"litellm": fake_module("litellm", completion=completion)}):
        vendor_exc = _error(lambda: _call(fallback.litellm_judge(spec)))
    assert vendor_exc.code == EXIT_RUNTIME and vendor_exc.__cause__ is not None, vendor_exc
    assert "litellm call failed with RuntimeError" in str(vendor_exc), str(vendor_exc)
    for surface in (str(vendor_exc), repr(vendor_exc), json.dumps(vendor_exc.details, default=str)):
        assert spec.rubric not in surface, surface
        assert "refunds the order" not in surface, surface

    # ... and through a real evaluate: the persisted row error carries no rubric text either
    root = _project(LITELLM_JUDGE)
    try:
        declared = judges_mod.load(proj_config(root))["correctness"]
        modules, state = litellm_fake(declared.rubric)
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope  # a judge that could not answer is a row error, not a run failure
        assert state.calls, "the judge was never called"
        row = _persisted_rows(root)[0]
        assert "correctness" not in row["scores"], row  # no score: the rejection is an error, not a 0.0
        assert "correctness" in (row["error"] or ""), row  # the rejection is still recorded, named
        for surface in (row["error"] or "", json.dumps(row, default=str)):
            assert declared.rubric.strip() not in surface, surface
            assert "Give 1 when the reply answers the question" not in surface, surface
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c) the real ImportError path: litellm is genuinely absent from this venv
# ---------------------------------------------------------------------------
def test_a_missing_litellm_is_exit_6_with_the_install_hint():
    spec = a_spec()
    assert "litellm" not in sys.modules, "the fake leaked into sys.modules"
    try:
        fallback.litellm_judge(spec)
    except CapabilityError as exc:
        assert exc.code == EXIT_CAPABILITY == 6, exc.code
        assert isinstance(exc.__cause__, ImportError), exc.__cause__  # chained, not a bare traceback
        assert exc.hint == "pip install litellm", exc.hint
        assert "pip install litellm" in str(exc), str(exc)
        assert spec.name in str(exc), "the operation must name the judge it could not build"
        assert "litellm" in str(exc)
        for surface in (str(exc), repr(exc), exc.hint or "", json.dumps(exc.details, default=str)):
            assert spec.rubric not in surface, surface
    else:
        raise AssertionError("a missing litellm built a scorer instead of raising CapabilityError")


# ---------------------------------------------------------------------------
# (d) per-call cost: the report_usage channel
# ---------------------------------------------------------------------------
def test_token_usage_is_reported_to_the_active_row_collector():
    judge, state = _judge_with('{"score": 0.9}', prompt_tokens=1200, completion_tokens=34)
    with usage_mod.collect() as collector:
        _call(judge)
    assert collector.reported is True
    assert (collector.input_tokens, collector.output_tokens) == (1200, 34)
    # a second call adds to the same row rather than replacing the first (per call, not per judge)
    with usage_mod.collect() as collector2:
        _call(judge)
        _call(judge)
    assert (collector2.input_tokens, collector2.output_tokens) == (2400, 68)


def test_a_response_without_usage_reports_nothing():
    for kwargs in ({"prompt_tokens": None, "completion_tokens": None},):
        judge, _ = _judge_with('{"score": 0.9}', **kwargs)
        with usage_mod.collect() as collector:
            assert _call(judge) == 0.9
        assert collector.reported is False, "evalops invented tokens for a response that had none"
        assert (collector.input_tokens, collector.output_tokens) == (0, 0)


def test_usage_outside_a_row_is_a_no_op():
    judge, _ = _judge_with('{"score": 0.9}', prompt_tokens=10, completion_tokens=2)
    assert _call(judge) == 0.9  # no collector active: must not raise, must not record anywhere


# ---------------------------------------------------------------------------
# (d) end-to-end H5: the judge's tokens reach `row.usage` and `cost.measure`
# ---------------------------------------------------------------------------
#: A temp app whose usage is trivial arithmetic: predict reports 210/50 tokens and $0.30 of cost.
#: Fixed numbers (unlike the shared fake app, whose tokens depend on the rendered prompt) so the
#: merged total is exactly checkable: 210/50 + 1200/34 -> 1410/84. It is its own module (not a
#: ``fake_app.py`` override) because hooks are imported with ``importlib.import_module``: one
#: ``fake_app`` in ``sys.modules`` is shared by every in-process test in this file.
USAGE_APP_MODULE = "wp33_usage_app"
USAGE_APP = """
import evalops


def behavior_config():
    return {"llm_endpoint": "fixed-usage@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        evalops.report_usage(input_tokens=210, output_tokens=50, cost_usd=0.3)
        return "Refunds are only available within 30 days."

    return predict


def build_scorers(evaluator="core"):
    return []


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 0}
"""

#: The same app that reports *no* usage at all (is the judge's usage still attributed? is cost None?).
QUIET_APP_MODULE = "wp33_quiet_app"
QUIET_APP = USAGE_APP.replace("        evalops.report_usage(input_tokens=210, output_tokens=50, cost_usd=0.3)\n", "")

#: `BASE_CONFIG` with scorer retries off, so a raising scorer is called exactly once (deterministic
#: token accounting for the "tokens spent before an error" pin).
NO_RETRY_CONFIG = proj.BASE_CONFIG.replace("retries: 1,", "retries: 0,")


def _usage_app_config(module: str, base: str = proj.BASE_CONFIG) -> str:
    """`base` with every `app:` hook pointed at ``module`` instead of the shared fake app."""
    return base.replace("fake_app:", f"{module}:")


def _persisted_rows(root: pathlib.Path) -> list[dict]:
    """The per-row documents of the latest run (`evalops/rows.jsonl`, read back from the ledger)."""
    run_id = proj.read_json(proj.last_eval_path(root))["run_ids"][0]
    conn = sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))
    try:
        found = conn.execute(
            "SELECT body FROM evalops_artifacts WHERE run_id=? AND name=?", (run_id, "evalops/rows.jsonl")
        ).fetchone()
    finally:
        conn.close()
    assert found is not None, "the run has no rows artifact"
    return [json.loads(line) for line in str(found[0]).splitlines() if line.strip()]


def _rows_with_usage(root: pathlib.Path) -> list[RowResult]:
    """The persisted rows as `RowResult`s, usage included (what `cost.measure` consumes)."""
    return [
        RowResult(
            row_id=doc["row_id"],
            inputs=doc["inputs"],
            outputs=doc.get("outputs"),
            usage=Usage(**doc["usage"]) if doc.get("usage") else None,
        )
        for doc in _persisted_rows(root)
    ]


def test_h5_the_judge_tokens_are_added_to_the_app_reported_usage_end_to_end():
    """The packet's H5, both halves: `row.usage` *and* `cost.measure` see the judge's tokens."""
    modules, state = litellm_fake('{"score": 1}', prompt_tokens=1200, completion_tokens=34)
    root = _project(
        LITELLM_JUDGE,
        config=_usage_app_config(USAGE_APP_MODULE),
        files={f"{USAGE_APP_MODULE}.py": USAGE_APP},
    )
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        assert len(state.calls) == 1, state.calls
        row = _persisted_rows(root)[0]
        assert row["scores"] == {"correctness": 1.0}, row  # the judge still scored normally
        # app 210/50 + judge 1200/34 -> 1410/84, summed, never the judge's numbers replacing the app's.
        # The app reported $0.30 and the judge reported no cost, so the merged cost is still $0.30.
        assert row["usage"] == {"input_tokens": 1410, "output_tokens": 84, "cost_usd": 0.3}, row
        # the run envelope's cost metrics come from `cost.measure` over these rows
        metrics = _metrics(root)
        assert (metrics["gen_input_tokens"], metrics["gen_output_tokens"]) == (1410, 84), metrics
        assert metrics["gen_cost_usd"] == 0.3, metrics
        assert metrics["rows_missing_usage"] == 0, metrics
        # ... and `cost.measure` called directly on the persisted rows agrees (not an envelope artifact)
        measured = cost_mod.measure(proj_config(root), _rows_with_usage(root))
        assert (measured["gen_input_tokens"], measured["gen_output_tokens"]) == (1410, 84), measured
        assert measured["gen_cost_usd"] == 0.3 and measured["gen_cost_source"] == "reported", measured
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_h5_a_scorer_error_keeps_the_tokens_spent_before_it():
    """The `__error__` sentinel path still reads the collector: tokens spent are tokens spent."""
    modules, state = litellm_fake("not json at all", prompt_tokens=1200, completion_tokens=34)
    root = _project(
        LITELLM_JUDGE,
        config=_usage_app_config(USAGE_APP_MODULE, NO_RETRY_CONFIG),
        files={f"{USAGE_APP_MODULE}.py": USAGE_APP},
    )
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope  # a judge that could not answer is a row error, not a run failure
        assert len(state.calls) == 1, state.calls
        row = _persisted_rows(root)[0]
        # the strict-parse rejection is recorded exactly as before: no score, the judge named
        assert row["scores"] == {}, row
        assert "correctness" in (row["error"] or ""), row
        # ... and the tokens the failed judge already paid for are still attributed
        assert row["usage"] == {"input_tokens": 1410, "output_tokens": 84, "cost_usd": 0.3}, row
        measured = cost_mod.measure(proj_config(root), _rows_with_usage(root))
        assert (measured["gen_input_tokens"], measured["gen_output_tokens"]) == (1410, 84), measured
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_h5_judge_usage_without_app_usage_leaves_the_cost_unset():
    """Neither side invented: an app that reports nothing still gets the judge's tokens, cost None."""
    modules, state = litellm_fake('{"score": 1}', prompt_tokens=1200, completion_tokens=34)
    root = _project(
        LITELLM_JUDGE,
        config=_usage_app_config(QUIET_APP_MODULE),
        files={f"{QUIET_APP_MODULE}.py": QUIET_APP},
    )
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        row = _persisted_rows(root)[0]
        assert row["usage"] == {"input_tokens": 1200, "output_tokens": 34, "cost_usd": None}, row
        metrics = _metrics(root)
        assert (metrics["gen_input_tokens"], metrics["gen_output_tokens"]) == (1200, 34), metrics
        # no reported cost anywhere -> the price table prices the row (and says so)
        measured = cost_mod.measure(proj_config(root), _rows_with_usage(root))
        assert measured["gen_cost_source"] == "price_table", measured
    finally:
        shutil.rmtree(root, ignore_errors=True)


#: Two declared litellm judges, so the core evaluator's scorer pool runs them on two worker threads.
TWO_LITELLM_JUDGES = (
    LITELLM_JUDGE
    + """  helpfulness:
    model: fake-judge-2024-11-20
    rubric: |
      Give 1 when the reply is polite and 0 otherwise.
    prompt_version: "1"
    judge: litellm
    scale: {kind: binary, min: 0.0, max: 1.0}
    sampling: {temperature: 0.0, max_tokens: 256}
"""
)


def test_h5_every_pool_worker_thread_reports_its_own_judge_usage():
    """Contextvars do not cross threads: each scorer's collector is opened *inside* its worker."""
    modules, state = litellm_fake('{"score": 1}', prompt_tokens=1200, completion_tokens=34)
    config = _usage_app_config(USAGE_APP_MODULE).replace("max_scorer_workers: 1", "max_scorer_workers: 2")
    root = _project(TWO_LITELLM_JUDGES, config=config, files={f"{USAGE_APP_MODULE}.py": USAGE_APP})
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        assert len(state.calls) == 2, state.calls
        row = _persisted_rows(root)[0]
        assert row["scores"] == {"correctness": 1.0, "helpfulness": 1.0}, row
        # app 210/50 + two judge calls of 1200/34 each: both threads' collectors were read
        assert row["usage"] == {"input_tokens": 2610, "output_tokens": 118, "cost_usd": 0.3}, row
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (e) the spec_judge dispatch
# ---------------------------------------------------------------------------
def test_spec_judge_dispatches_on_the_executor():
    litellm = a_spec(judge="litellm")
    native = a_spec()
    modules, state = litellm_fake('{"score": 0.75}')
    with fake_modules(modules):
        # the fallback is evaluator-independent: an evaluator with NO native adapter still works
        judge = judge_wrappers.spec_judge(litellm, evaluator="sql")
        assert _call(judge) == 0.75
        assert state.calls[0]["model"] == litellm.model
    # ... while a native spec keeps the WP3b.2 behaviour: no adapter is exit 6
    try:
        judge_wrappers.spec_judge(native, evaluator="sql")
    except CapabilityError as exc:
        assert exc.code == EXIT_CAPABILITY and "sql" in str(exc), exc
        assert "WP3b.3" in (exc.hint or ""), exc.hint
    else:
        raise AssertionError("a native spec built a scorer for an evaluator without an adapter")


def test_a_native_spec_still_uses_the_evaluator_adapter():
    modules, state = pydantic_evals_fake(result=0.6)
    spec = a_spec()
    with fake_modules(modules):
        judge = judge_wrappers.spec_judge(spec, evaluator="core")
        assert _call(judge) == 0.6
    assert state["calls"][0]["rubric"] == spec.rubric


def pydantic_evals_fake(result: Any = None) -> tuple[dict[str, types.ModuleType], dict]:
    """A fake ``pydantic_evals``: the WP3b.2 ``core`` adapter's SDK, returning ``result``."""
    state: dict[str, Any] = {"calls": [], "contexts": [], "result": result}

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
# (f) the wiring: a declared spec is a real metric in `evaluate`
# ---------------------------------------------------------------------------
def _project(judges_block: str = "", *, config: str | None = None, files: dict | None = None) -> pathlib.Path:
    """A synced, committed temp project (the fake app, the SQLite ledger, the smoke dataset)."""
    root = proj.project((config or proj.BASE_CONFIG) + judges_block, files=files)
    assert proj.run_cli(root, "sync", "all")[0] == 0, "sync failed"
    proj.commit(root, "sync")
    return root


def _metrics(root: pathlib.Path) -> dict:
    """The metrics of the run recorded in ``.evalops/out/last_eval.json``."""
    return proj.read_json(proj.last_eval_path(root))["runs"][0]["metrics"]


def test_a_declared_spec_becomes_a_real_metric_in_evaluate_with_either_executor():
    modules, state = litellm_fake('{"score": 1}')
    root = _project(LITELLM_JUDGE)
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        metrics = _metrics(root)
        # the fallback ran once per row and its score reached the aggregate as `correctness/mean`
        assert metrics["correctness/mean"] == 1.0, metrics
        assert len(state.calls) == 1, state.calls
        assert state.calls[0]["model"] == "fake-judge-2024-11-20"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ... and the same declaration with no `judge` key runs the core evaluator's native adapter
    modules, state = pydantic_evals_fake(result=0.25)
    root = _project(NATIVE_JUDGE)
    try:
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        assert _metrics(root)["correctness/mean"] == 0.25, _metrics(root)
        assert state["calls"][0]["model"] == "fake-judge-2024-11-20"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_threshold_only_rules_add_no_scorer_and_change_nothing():
    root = _project()
    try:
        judges_mod.load(proj_config(root))
        assert run_mod._with_spec_judges(proj_config(root), [], evaluator="core") == [], "a spec appeared"
    finally:
        shutil.rmtree(root, ignore_errors=True)

    root = _project(THRESHOLD_JUDGE)
    try:
        cfg = proj_config(root)
        assert judges_mod.load(cfg) == {}, "thresholds declared a spec"
        assert run_mod._with_spec_judges(cfg, [], evaluator="core") == []
        assert run_mod._client_side_judge_calls(cfg, "core") == 0
        code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        # byte-identical behaviour: only the app's own scorers are in the aggregate
        assert set(_metrics(root)) >= {"facts/mean", "helpful/mean"}, _metrics(root)
        assert "correctness/mean" not in _metrics(root), _metrics(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def proj_config(root: pathlib.Path):
    from evalops import config as config_mod

    return config_mod.load(str(root / "evalops.yaml"))


def test_an_app_scorer_colliding_with_a_spec_name_is_exit_2_before_any_ledger_row():
    name = proj.ledger_name()
    root = proj.project(proj.ledger_config(name) + COLLIDING_JUDGE)
    module = proj.install_ledger(root, name)
    proj.ledger_clear(module)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == EXIT_USAGE == 2, (code, envelope)
        message = envelope["errors"][0]["message"]
        assert "helpful" in message, message
        assert "scorers" in message and "judges.helpful" in message, "both sources must be named: " + message
        # nothing was reserved and nothing was recorded: the refusal precedes the ledger row
        assert module.MemoryLedger.runs == {}, module.MemoryLedger.runs
        assert module.MemoryLedger.artifacts == {}, module.MemoryLedger.artifacts
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (f) the estimate bump (D2)
# ---------------------------------------------------------------------------
def _plan(root: pathlib.Path) -> dict:
    code, envelope = proj.run_cli_in_process(root, "plan", "--dataset", "smoke", "--stage", "dev")
    assert code == 0, envelope
    return envelope["data"]["estimate"]


def test_plan_counts_one_client_side_call_per_declared_spec():
    baseline = _project()
    litellm = _project(LITELLM_JUDGE)
    native = _project(NATIVE_JUDGE)
    try:
        before = _plan(baseline)
        after = _plan(litellm)
        # one client-side judge call per row (smoke has one row, one repeat) ...
        assert after["llm_calls"] == before["llm_calls"] + 1, (before, after)
        assert after["llm_judges"] == before["llm_judges"] + 1, (before, after)
        # ... and it is priced: the judge dollars move, the generation dollars do not
        assert after["judge_usd"] > before["judge_usd"], (before["judge_usd"], after["judge_usd"])
        assert after["gen_usd"] == before["gen_usd"], (before["gen_usd"], after["gen_usd"])
        # a native spec on the core evaluator is client-side too (pydantic-evals runs locally)
        assert _plan(native)["llm_calls"] == after["llm_calls"], _plan(native)
        # a threshold-only rule does not bump anything
        assert _plan(_project(THRESHOLD_JUDGE))["llm_calls"] == before["llm_calls"]
    finally:
        for root in (baseline, litellm, native):
            shutil.rmtree(root, ignore_errors=True)


def test_a_server_side_native_adapter_does_not_bump_the_estimate():
    """`langfuse-managed` runs the judge on Langfuse's side: no local model call, no extra estimate."""
    langfuse = proj.BASE_CONFIG.replace("evaluator: core", "evaluator: langfuse")
    server_side = _project(NATIVE_JUDGE, config=langfuse)
    client_side = _project(LITELLM_JUDGE, config=langfuse)
    try:
        assert run_mod._client_side_judge_calls(proj_config(server_side), "langfuse") == 0
        assert run_mod._client_side_judge_calls(proj_config(client_side), "langfuse") == 1
        # the litellm fallback is evaluator-independent: it is *always* a client-side call
        assert _plan(client_side)["llm_calls"] == _plan(server_side)["llm_calls"] + 1
    finally:
        for root in (server_side, client_side):
            shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (g) calibration (D3): a declared spec yields real pairs
# ---------------------------------------------------------------------------
CALIBRATION_ROWS = "".join(
    json.dumps(record) + "\n"
    for record in (
        {"inputs": {"q": "q1"}, "outputs": "refunded", "expectations": {"human_labels": {"correctness": 1}}},
        {"inputs": {"q": "q2"}, "outputs": "no refund", "expectations": {"human_labels": {"correctness": 1}}},
    )
)


def test_calibrate_reports_real_pairs_for_a_declared_spec():
    config = proj.BASE_CONFIG.replace("datasets: {smoke: {}}", "datasets: {smoke: {}, judge_calibration: {}}")
    root = _project(
        LITELLM_JUDGE,
        config=config,
        files={"datasets/judge_calibration.jsonl": CALIBRATION_ROWS},
    )
    try:
        modules, state = litellm_fake('{"score": 1}')
        with fake_modules(modules):
            code, envelope = proj.run_cli_in_process(root, "calibrate", "--dataset", "judge_calibration")
        assert code == 0, envelope
        data = envelope["data"]
        assert data["judges"]["correctness"]["n"] == 2, data
        assert data["judges"]["correctness"]["agreement"] == 1.0, data
        assert data["judges"]["correctness"]["mae"] == 0.0, data
        assert len(state.calls) == 2, state.calls
        assert pathlib.Path(data["report"]).exists(), "the calibration report was not written"
    finally:
        shutil.rmtree(root, ignore_errors=True)
