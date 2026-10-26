"""WP3b.4: the pairwise (position-swap) and panel (mean/majority/agreement) judge wrappers.

What this module falsifies, group by group:

(a) :func:`evalops.scorers.panel.normalize_pairwise` implements the closed set exactly -- verdict
    strings case-insensitively (stripped), ``1``/``-1``/``0`` (ints and floats), a mapping's
    ``'winner'`` value recursively -- flips ``'a'``/``'b'`` under ``swapped=True`` (``'tie'`` stays)
    and rejects everything else (bools, unknown strings, other numbers, ``None``, lists, a mapping
    without ``'winner'``) with an ``EvalOpsError`` naming the wrapper and the value. It never turns
    an unknown value into a silent ``'tie'``;
(b) :func:`evalops.scorers.panel.pairwise_judge` calls the judge **twice per row** with genuinely
    swapped candidates, normalizes the second verdict back into the first frame, and returns the
    two-metric multi-metric shape ``call_scorer`` consumes (``name`` + ``name/swap_agreement``): a
    consistent judge agrees (``1.0``), a position-biased judge is decision ``1.0`` with
    ``swap_agreement`` ``0.0`` (visible, never smoothed), a missing baseline names the key, and a
    judge exception propagates;
(c) :func:`evalops.scorers.panel.panel_judge` aggregates votes by ``mean`` / strict ``majority``
    (no majority -> the visible tie ``0.5``) / exact ``agreement`` (no consensus -> ``None``),
    always reports ``panel_disagreement`` when votes exist (even when the score is ``None``),
    excludes ``None`` votes, returns ``None`` with no companion when there are no votes at all,
    rejects multi-metric members by index, and validates members/method at construction (exit 2) --
    including a non-sequence ``judges`` (generator expression, string) and an unhashable ``method``,
    which must be a named usage error rather than a leaked ``TypeError``;
    members receive only the row kwargs they declare (``trace`` only when requested);
(d) both wrappers are neutral scorers (``scorer(name, llm=True)``) exported additively from
    ``evalops.scorers`` beside ``spec_judge``/``span_judge``/``trajectory_judge``, and both run
    end-to-end through a real ``evaluate`` on a temp project via the app hook: their metrics land in
    the persisted ``rows.jsonl`` scores and the run's aggregate metrics.

No SDK, no fake modules, no network: the wrappers only compose callables passed by the caller.
Every test is a zero-argument function, so ``python tests/run_without_pytest.py`` runs this module.
"""

import json
import pathlib
import shutil
import sqlite3
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

import evalops.scorers as public_scorers  # noqa: E402
from evalops.scorers import normalize_pairwise, pairwise_judge, panel, panel_judge  # noqa: E402
from evalops.scoring import call_scorer, scorer_name  # noqa: E402
from evalops.util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError  # noqa: E402

TINY = 1e-12


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _raises(fn) -> EvalOpsError:
    """The ``EvalOpsError`` ``fn`` raises (asserting it raises one)."""
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError")


def _vote(value: Any):
    """A minimal panel member: declares the row kwargs it needs and returns one fixed vote."""

    def member(*, inputs, outputs):
        return value

    return member


def _recording_judge(verdicts: list[Any], calls: list[dict] | None = None):
    """A deterministic pairwise judge that logs each call and answers from ``verdicts`` in order."""
    log = calls if calls is not None else []

    def judge(*, a, b, inputs):
        log.append({"a": a, "b": b, "inputs": inputs})
        return verdicts[min(len(log) - 1, len(verdicts) - 1)]

    return judge


def _persisted_rows(root: pathlib.Path) -> list[dict]:
    """The per-row documents of the latest run (``evalops/rows.jsonl``, read back from the ledger)."""
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


# ---------------------------------------------------------------------------
# (C1) the strict normalizer
# ---------------------------------------------------------------------------
def test_c1_normalizer_accepts_the_closed_set():
    assert normalize_pairwise("a") == "a"
    assert normalize_pairwise("A") == "a"
    assert normalize_pairwise(" a ") == "a"  # stripped
    assert normalize_pairwise("B") == "b"
    assert normalize_pairwise("TIE") == "tie"
    assert normalize_pairwise(1) == "a"
    assert normalize_pairwise(-1) == "b"
    assert normalize_pairwise(0) == "tie"
    assert normalize_pairwise(1.0) == "a"
    assert normalize_pairwise(-1.0) == "b"
    assert normalize_pairwise(0.0) == "tie"
    assert normalize_pairwise({"winner": "B"}) == "b"
    assert normalize_pairwise({"winner": {"winner": 1}}) == "a"  # the value normalizes recursively


def test_c1_swapped_flips_a_and_b_but_keeps_tie():
    assert normalize_pairwise("a", swapped=True) == "b"
    assert normalize_pairwise("b", swapped=True) == "a"
    assert normalize_pairwise("tie", swapped=True) == "tie"
    assert normalize_pairwise(1, swapped=True) == "b"
    assert normalize_pairwise(-1, swapped=True) == "a"
    assert normalize_pairwise({"winner": "b"}, swapped=True) == "a"


def test_c1_normalizer_rejects_unknown_values_with_the_wrapper_named():
    rejected = (True, False, "x", "tie-ish", 2, -2, 0.5, None, [], ["a"], {}, {"verdict": "a"})
    for value in rejected:
        err = _raises(lambda value=value: normalize_pairwise(value))
        assert err.code == EXIT_RUNTIME, (value, err.code)
        assert "pairwise" in str(err), (value, str(err))
        assert repr(value) in str(err), (value, str(err))

    # a mapping's value normalizes recursively, so the *leaf* is the offending value that is named
    err = _raises(lambda: normalize_pairwise({"winner": "perhaps"}))
    assert err.code == EXIT_RUNTIME and "pairwise" in str(err), (err.code, str(err))
    assert repr("perhaps") in str(err), str(err)


# ---------------------------------------------------------------------------
# (C2) pairwise: two calls, swapped, disagreement visible
# ---------------------------------------------------------------------------
def test_c2_pairwise_calls_both_orders_and_a_consistent_judge_agrees():
    calls: list[dict] = []
    scorer = pairwise_judge(_recording_judge(["a", "b"], calls), name="compare")
    out = call_scorer(
        scorer, inputs={"question": "how do I reset?"}, outputs="candidate", expectations={"baseline": "reference"}
    )
    assert out == {"compare": 1.0, "compare/swap_agreement": 1.0}, out
    assert calls == [
        {"a": "candidate", "b": "reference", "inputs": {"question": "how do I reset?"}},
        {"a": "reference", "b": "candidate", "inputs": {"question": "how do I reset?"}},
    ], calls


def test_c2_a_position_biased_judge_is_visible_as_zero_swap_agreement():
    calls: list[dict] = []
    scorer = pairwise_judge(_recording_judge(["a", "a"], calls), name="compare")  # always prefers `a`
    out = call_scorer(scorer, inputs={}, outputs="candidate", expectations={"baseline": "reference"})
    # the first candidate wins the decision, but the two calls disagree: reported, never hidden
    assert out == {"compare": 1.0, "compare/swap_agreement": 0.0}, out
    assert len(calls) == 2 and calls[0]["a"] == calls[1]["b"], calls


def test_c2_a_consistent_tie_stays_a_tie():
    scorer = pairwise_judge(_recording_judge(["tie", "tie"]), name="compare")
    out = call_scorer(scorer, inputs={}, outputs="one", expectations={"baseline": "other"})
    assert out == {"compare": 0.5, "compare/swap_agreement": 1.0}, out


def test_c2_a_missing_baseline_is_an_error_naming_the_key():
    def judge(**_kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("the judge was called without a baseline")

    scorer = pairwise_judge(judge, name="compare")
    err = _raises(lambda: scorer(inputs={}, outputs="candidate", expectations={}))
    assert err.code == EXIT_RUNTIME and "baseline" in str(err), (err.code, str(err))
    err = _raises(lambda: scorer(inputs={}, outputs="candidate", expectations=None))
    assert err.code == EXIT_RUNTIME and "baseline" in str(err), (err.code, str(err))

    custom = pairwise_judge(judge, name="compare", baseline_key="reference")
    err = _raises(lambda: custom(inputs={}, outputs="candidate", expectations={"baseline": "x"}))
    assert err.code == EXIT_RUNTIME and "reference" in str(err), (err.code, str(err))


def test_c2_judge_exceptions_propagate():
    def judge(*, a, b, inputs):
        raise RuntimeError("judge exploded")

    scorer = pairwise_judge(judge, name="compare")
    try:
        scorer(inputs={}, outputs="candidate", expectations={"baseline": "reference"})
    except RuntimeError as exc:
        assert "judge exploded" in str(exc)
    else:
        raise AssertionError("a judge exception must reach the row's error channel")


# ---------------------------------------------------------------------------
# (C3) panel: methods, disagreement, member contract
# ---------------------------------------------------------------------------
def test_c3_mean_is_the_vote_mean():
    scorer = panel_judge([_vote(2.0), _vote(4.0)], name="quality")
    out = call_scorer(scorer, inputs={}, outputs="x", expectations={})
    assert out["quality"] == 3.0, out
    assert abs(out["quality/panel_disagreement"] - 0.5) < TINY, out  # 1 - 1/2


def test_c3_majority_needs_more_than_half_and_ties_visibly_at_half():
    three = panel_judge([_vote(1.0), _vote(1.0), _vote(0.0)], name="quality", method="majority")
    out = call_scorer(three, inputs={}, outputs="x", expectations={})
    assert out["quality"] == 1.0, out
    assert abs(out["quality/panel_disagreement"] - 1 / 3) < TINY, out

    split = panel_judge([_vote(1.0), _vote(0.0)], name="quality", method="majority")
    out = call_scorer(split, inputs={}, outputs="x", expectations={})
    assert out["quality"] == 0.5, out  # no strict majority: a visible tie, never a silent pick
    assert out["quality/panel_disagreement"] == 0.5, out

    zeros = panel_judge([_vote(0.0), _vote(0.0)], name="quality", method="majority")
    out = call_scorer(zeros, inputs={}, outputs="x", expectations={})
    assert out["quality"] == 0.0, out  # a real 0.0 majority is not a missing majority


def test_c3_agreement_needs_exact_consensus():
    unanimous = panel_judge([_vote(0.9), _vote(0.9), _vote(0.9)], name="quality", method="agreement")
    out = call_scorer(unanimous, inputs={}, outputs="x", expectations={})
    assert out == {"quality": 0.9, "quality/panel_disagreement": 0.0}, out

    split = panel_judge([_vote(0.9), _vote(0.8)], name="quality", method="agreement")
    out = call_scorer(split, inputs={}, outputs="x", expectations={})
    assert "quality" not in out, out  # no consensus: no score, never an invented one
    assert out == {"quality/panel_disagreement": 0.5}, out
    # the raw multi-metric shape still carries the explicit None and the disagreement companion
    raw = split(inputs={}, outputs="x", expectations={})
    assert raw[0] == {"key": "quality", "score": None} and raw[1]["score"] == 0.5, raw


def test_c3_panel_disagreement_is_reported_for_every_method():
    cases = (
        ("mean", [_vote(1.0), _vote(1.0), _vote(0.0)], 1 / 3),
        ("majority", [_vote(1.0), _vote(1.0), _vote(0.0)], 1 / 3),
        ("agreement", [_vote(1.0), _vote(1.0), _vote(0.0)], 1 / 3),
        ("mean", [_vote(1.0), _vote(1.0)], 0.0),
        ("majority", [_vote(1.0), _vote(1.0)], 0.0),
        ("agreement", [_vote(1.0), _vote(1.0)], 0.0),
    )
    for method, members, expected in cases:
        out = call_scorer(panel_judge(members, name="quality", method=method), inputs={}, outputs="x", expectations={})
        assert abs(out["quality/panel_disagreement"] - expected) < TINY, (method, out)


def test_c3_none_votes_are_excluded_and_an_empty_vote_set_is_none():
    scorer = panel_judge([_vote(None), _vote(1.0), _vote(None)], name="quality")
    out = call_scorer(scorer, inputs={}, outputs="x", expectations={})
    assert out == {"quality": 1.0, "quality/panel_disagreement": 0.0}, out

    empty = panel_judge([_vote(None), _vote(None)], name="quality", method="agreement")
    assert empty(inputs={}, outputs="x", expectations={}) is None
    assert call_scorer(empty, inputs={}, outputs="x", expectations={}) == {}  # no companion either


def test_c3_multi_metric_members_are_rejected_by_index():
    def listy(*, inputs, outputs):
        return [{"key": "x", "score": 1.0}]

    def mapping(*, inputs, outputs):
        return {"score": 1.0}

    def unparseable(*, inputs, outputs):
        return "maybe"

    for member in (listy, mapping, unparseable):
        scorer = panel_judge([_vote(1.0), member], name="quality")
        err = _raises(lambda scorer=scorer: call_scorer(scorer, inputs={}, outputs="x", expectations={}))
        assert err.code == EXIT_RUNTIME, (member.__name__, err.code)
        assert "member 1" in str(err), (member.__name__, str(err))


def test_c3_construction_errors_are_exit_2():
    for judges in ([], [_vote(1.0)]):
        err = _raises(lambda judges=judges: panel_judge(judges, name="quality"))
        assert err.code == EXIT_USAGE and "at least 2" in str(err), (judges, err.code, str(err))

    err = _raises(lambda: panel_judge([_vote(1.0), _vote(1.0)], name="quality", method="median"))
    assert err.code == EXIT_USAGE and "median" in str(err), (err.code, str(err))

    not_callable: Any = [_vote(1.0), "not-a-callable"]
    err = _raises(lambda: panel_judge(not_callable, name="quality"))
    assert err.code == EXIT_USAGE and "member 1" in str(err), (err.code, str(err))

    not_a_sequence: Any = None
    err = _raises(lambda: panel_judge(not_a_sequence, name="quality"))
    assert err.code == EXIT_USAGE, (err.code, str(err))


def test_a_generator_or_string_is_not_a_valid_panel():
    members = [_vote(1.0), _vote(1.0)]

    # a generator expression is not a sequence: accepting it would consume it once and leave a panel
    # whose votes depend on how often the scorer runs, so construction is a usage error that names it
    generator: Any = (member for member in members)
    err = _raises(lambda: panel_judge(generator, name="quality"))
    assert err.code == EXIT_USAGE, (err.code, str(err))
    assert "generator" in str(err) and "sequence" in str(err), str(err)
    assert list(generator) == members, "the rejected generator must not have been consumed"

    # a string is iterable, but its members would be characters -- also a usage error, said as such
    for text in ("ab", ""):
        bad: Any = text
        err = _raises(lambda bad=bad: panel_judge(bad, name="quality"))
        assert err.code == EXIT_USAGE, (text, err.code, str(err))
        assert "str" in str(err) and "characters" in str(err), (text, str(err))


def test_an_unhashable_method_is_a_usage_error_not_a_type_error():
    for method in ([], {"method": "mean"}, set()):
        bad: Any = method
        err = _raises(lambda bad=bad: panel_judge([_vote(1.0), _vote(1.0)], name="quality", method=bad))
        assert err.code == EXIT_USAGE, (method, err.code, str(err))
        assert repr(method) in str(err), (method, str(err))
        for accepted in sorted(("mean", "majority", "agreement")):
            assert accepted in str(err), (method, accepted, str(err))

    # a plain unknown-but-hashable method keeps the same exit-2 shape (no regression)
    err = _raises(lambda: panel_judge([_vote(1.0), _vote(1.0)], name="quality", method="median"))
    assert err.code == EXIT_USAGE and "median" in str(err), (err.code, str(err))


def test_c3_members_receive_only_the_kwargs_they_declare():
    seen: list[tuple] = []

    def row_only(*, inputs, outputs):
        seen.append(("row", inputs, outputs))
        return 1.0

    def full(*, inputs, outputs, expectations, trace):
        seen.append(("full", inputs, outputs, expectations, trace))
        return 1.0

    scorer = panel_judge([row_only, full], name="quality")
    out = call_scorer(scorer, inputs={"q": "reset"}, outputs="answer", expectations={"baseline": "ref"}, trace="TRACE")
    assert out["quality"] == 1.0, out
    assert seen == [
        ("row", {"q": "reset"}, "answer"),  # no expectations/trace: it did not declare them
        ("full", {"q": "reset"}, "answer", {"baseline": "ref"}, "TRACE"),
    ], seen


# ---------------------------------------------------------------------------
# (H5) additive exports + neutral-scorer identity
# ---------------------------------------------------------------------------
def test_h5_exports_are_additive_and_both_wrappers_are_neutral_scorers():
    assert public_scorers.normalize_pairwise is panel.normalize_pairwise
    assert public_scorers.pairwise_judge is panel.pairwise_judge
    assert public_scorers.panel_judge is panel.panel_judge
    for old in ("spec_judge", "span_judge", "trajectory_judge"):
        assert hasattr(public_scorers, old), old

    pair = pairwise_judge(_recording_judge(["a", "b"]), name="pair")
    jury = panel_judge([_vote(1.0), _vote(1.0)], name="panel")
    for scorer, expected in ((pair, "pair"), (jury, "panel")):
        assert scorer_name(scorer) == expected
        assert scorer.__dict__["evalops_llm"] is True


# ---------------------------------------------------------------------------
# (C4) end-to-end through a real `evaluate` (app hook, SQLite ledger)
# ---------------------------------------------------------------------------
#: A temp app whose only scorer is a pairwise judge: the deterministic judge prefers the longer
#: candidate, so a consistent decision must reproduce across the swap. It has its own module name
#: because hooks load with `importlib.import_module`, and one `sys.modules` entry is shared.
PAIRWISE_APP_MODULE = "wp34_pairwise_app"
PAIRWISE_APP = """
from evalops.scorers import pairwise_judge


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question):
        return "Refund within 30 days."

    return predict


def _judge(*, a, b, inputs):
    if len(str(a)) > len(str(b)):
        return "a"
    if len(str(b)) > len(str(a)):
        return "b"
    return "tie"


def build_scorers(evaluator="core"):
    return [pairwise_judge(_judge, name="pairwise_length")]


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 2}
"""

#: The baseline is deliberately longer than the prediction, so B wins in *both* orders.
PAIRWISE_DATASET = (
    '{"inputs": {"question": "What is the refund window?"}, '
    '"expectations": {"baseline": "Refunds are only available within 30 days, per policy."}}\n'
)

PANEL_APP_MODULE = "wp34_panel_app"
PANEL_APP = """
from evalops.scorers import panel_judge


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question):
        return "Refunds are only available within 30 days."

    return predict


def _optimist(*, inputs, outputs):
    return 1.0


def _pessimist(*, inputs, outputs):
    return 0.0


def build_scorers(evaluator="core"):
    return [panel_judge([_optimist, _pessimist], name="panel_verdict", method="mean")]


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 2}
"""


def _app_config(module: str) -> str:
    """``BASE_CONFIG`` with every ``app:`` hook pointed at ``module`` instead of the shared fake app."""
    return proj.BASE_CONFIG.replace("fake_app:", f"{module}:")


def test_c4_pairwise_metrics_persist_through_a_real_evaluate():
    root = proj.project(
        _app_config(PAIRWISE_APP_MODULE),
        files={f"{PAIRWISE_APP_MODULE}.py": PAIRWISE_APP},
        dataset_text=PAIRWISE_DATASET,
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope

        rows = _persisted_rows(root)
        assert len(rows) == 1, rows
        assert rows[0]["scores"] == {"pairwise_length": 0.0, "pairwise_length/swap_agreement": 1.0}, rows[0]
        metrics = proj.read_json(proj.last_eval_path(root))["runs"][0]["metrics"]
        assert metrics["pairwise_length/mean"] == 0.0, metrics
        assert metrics["pairwise_length/swap_agreement/mean"] == 1.0, metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c4_panel_metrics_persist_through_a_real_evaluate():
    root = proj.project(_app_config(PANEL_APP_MODULE), files={f"{PANEL_APP_MODULE}.py": PANEL_APP})
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope

        rows = _persisted_rows(root)
        assert len(rows) == 1, rows
        assert rows[0]["scores"] == {"panel_verdict": 0.5, "panel_verdict/panel_disagreement": 0.5}, rows[0]
        metrics = proj.read_json(proj.last_eval_path(root))["runs"][0]["metrics"]
        assert metrics["panel_verdict/mean"] == 0.5, metrics
        assert metrics["panel_verdict/panel_disagreement/mean"] == 0.5, metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)
