"""WP1.4 (D): ``calibrate._stats``, the binarization threshold and the calibrate checks.

Falsifies, in order: (D1) perfect agreement gives agreement/kappa 1.0 and mae 0.0, (D2) chance-level
pairs give kappa ~ 0 and below-chance pairs a negative kappa, (D3) the ``pe >= 1`` degenerate branch
returns kappa 1.0 instead of dividing by zero, (D4) the threshold binarizes both sides at ``>=``
(a custom threshold, values just below/above 0.5), ``_threshold(None)`` is 0.5 and a non-numeric
threshold is exit 2, (D5) ``calibrate`` without judges is exit 2, rows with no labeled pairs are a
failing check (never a crash), the agreement/kappa/mae rules pass and fail, and ``max_mae: null``
emits no check.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import pathlib
import shutil
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import calibrate as calibrate_mod  # noqa: E402
from evalops import config as config_mod  # noqa: E402
from evalops.util import EXIT_USAGE, EvalOpsError  # noqa: E402

#: `app.judge_meta` must resolve to a callable; `os:getcwd` is the smallest such hook.
BASE = {"project": "calibrate", "app": {"judge_meta": "os:getcwd"}}


# ---------------------------------------------------------------------------
# calibrate() harness: a stubbed evaluate()/Registry, so no dataset or backend is needed
# ---------------------------------------------------------------------------
def _calibrate(rows, judges: dict) -> dict:
    """Run ``calibrate`` over `rows` with `judges` configured; no ledger write, no generation."""
    cfg = config_mod.Config({**BASE, "judges": judges}, proj.tmpdir("evalops-calibrate-"))
    ledger = SimpleNamespace(log=lambda *args, **kwargs: None)
    real_evaluate, real_registry = calibrate_mod.evaluate, calibrate_mod.Registry
    calibrate_mod.evaluate = lambda *args, **kwargs: {"run_ids": [], "group": "unit", "_rows": [rows]}
    calibrate_mod.Registry = lambda cfg_: SimpleNamespace(get=lambda role: ledger)
    try:
        return calibrate_mod.calibrate(cfg, "judge_calibration")
    finally:
        calibrate_mod.evaluate, calibrate_mod.Registry = real_evaluate, real_registry
        shutil.rmtree(cfg.root, ignore_errors=True)


def _row(pred, human=None):
    expectations = {"human_labels": {"helpful": human}} if human is not None else None
    return SimpleNamespace(expectations=expectations, scores={"helpful": pred})


def _labels(*pairs):
    """Pairs of (prediction, human label), both on the 0..1 scale."""
    return [_row(pred, human) for pred, human in pairs]


# ---------------------------------------------------------------------------
# (D1) perfect agreement
# ---------------------------------------------------------------------------
def test_stats_perfect_agreement_has_full_kappa_and_zero_mae():
    assert calibrate_mod._stats([(1.0, 1.0), (0.0, 0.0)], 0.5) == {
        "n": 2,
        "agreement": 1.0,
        "kappa": 1.0,
        "mae": 0.0,
    }


# ---------------------------------------------------------------------------
# (D2) chance-level and below-chance agreement
# ---------------------------------------------------------------------------
def test_stats_chance_level_kappa_is_zero_and_below_chance_is_negative():
    # prediction and label are independent: raw agreement equals chance agreement (0.5)
    chance = calibrate_mod._stats([(1.0, 1.0), (0.0, 1.0), (1.0, 0.0), (0.0, 0.0)] * 2, 0.5)
    assert chance["agreement"] == 0.5
    assert abs(chance["kappa"]) < 1e-9

    # the judge disagrees with the label on every pair: chance agreement 0.5, observed 0 -> kappa -1
    below = calibrate_mod._stats([(1.0, 0.0), (0.0, 1.0), (1.0, 0.0), (0.0, 1.0)], 0.5)
    assert below["agreement"] == 0.0 and below["kappa"] < 0
    assert below["kappa"] == -1.0
    assert below["mae"] == 1.0


# ---------------------------------------------------------------------------
# (D3) the pe >= 1 degenerate branch
# ---------------------------------------------------------------------------
def test_stats_degenerate_pe_one_returns_kappa_one_instead_of_dividing_by_zero():
    # every prediction and every label binarizes to True -> pe == 1 -> 1 - pe would be 0
    assert calibrate_mod._stats([(1.0, 1.0), (1.0, 1.0)], 0.5)["kappa"] == 1.0
    # every value binarizes to False -> pe == 1 as well
    all_false = calibrate_mod._stats([(0.0, 0.0)], 0.5)
    assert all_false["kappa"] == 1.0 and all_false["agreement"] == 1.0


# ---------------------------------------------------------------------------
# (D4) binarization and the threshold
# ---------------------------------------------------------------------------
def test_threshold_binarizes_at_the_cut_off_and_a_custom_threshold_moves_it():
    # a custom threshold: both sides are below 0.7, so a 0.6/0.6 pair agrees
    assert calibrate_mod._stats([(0.6, 0.6)], 0.7) == {"n": 1, "agreement": 1.0, "kappa": 1.0, "mae": 0.0}
    # just below vs just above 0.5: the same pair flips from disagree to agree
    below = calibrate_mod._stats([(0.49, 0.5)], 0.5)
    assert below["agreement"] == 0.0  # 0.49 binarizes to False, 0.5 to True
    assert below["mae"] == 0.01  # the scalar error is untouched by the threshold
    above = calibrate_mod._stats([(0.5, 0.51)], 0.5)
    assert above["agreement"] == 1.0 and above["kappa"] == 1.0
    # the boundary itself is a pass (`>= threshold`, not `>`)
    assert calibrate_mod._stats([(0.5, 0.5)], 0.5)["agreement"] == 1.0

    assert calibrate_mod._threshold(None) == calibrate_mod.DEFAULT_THRESHOLD == 0.5
    assert calibrate_mod._threshold(0.7) == 0.7
    assert calibrate_mod._threshold("0.25") == 0.25


def test_a_non_numeric_threshold_is_exit_two():
    try:
        calibrate_mod._threshold("nope")
    except EvalOpsError as exc:
        assert exc.code == EXIT_USAGE == 2
        assert "threshold must be a number" in str(exc) and "nope" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("a non-numeric threshold must be a usage error")


# ---------------------------------------------------------------------------
# (D5) calibrate: no judges, no labeled pairs, and the threshold checks
# ---------------------------------------------------------------------------
def test_calibrate_without_judges_or_a_scorer_is_exit_two():
    root = proj.project()
    try:
        code, envelope = proj.run_cli(root, "calibrate", "--dataset", "judge_calibration")
        assert code == 2 and envelope["code"] == 2, envelope
        assert envelope["command"] == "calibrate" and envelope["ok"] is False
        assert "no judges to calibrate" in envelope["errors"][0]["message"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_calibrate_rows_without_labeled_pairs_fail_instead_of_crashing():
    out = _calibrate([_row(1.0), _row(0.0)], {"helpful": {"min_agreement": 0.9}})
    assert out["ok"] is False
    assert out["judges"] == {}  # no stats for a judge with nothing to compare
    assert out["checks"] == [{"check": "helpful: labeled+scored pairs", "passed": False, "candidate": 0}]


def test_calibrate_agreement_kappa_and_mae_checks_pass_and_fail():
    # rows: predictions 1.0/1.0, human labels 0.8/0.8 -> agreement 1.0, kappa 1.0, mae 0.2
    passed = _calibrate(_labels((1.0, 0.8), (1.0, 0.8)), {"helpful": {"min_agreement": 0.9, "min_kappa": 0.5}})
    assert passed["ok"] is True
    assert passed["checks"] == [
        {"check": "helpful: agreement >= 0.9", "passed": True, "candidate": 1.0},
        {"check": "helpful: kappa >= 0.5", "passed": True, "candidate": 1.0},
    ]
    assert passed["judges"]["helpful"] == {"n": 2, "agreement": 1.0, "kappa": 1.0, "mae": 0.2}

    failed = _calibrate(
        _labels((1.0, 0.8), (1.0, 0.8)),
        {"helpful": {"min_agreement": 1.1, "min_kappa": 1.1, "max_mae": 0.15}},
    )
    assert failed["ok"] is False
    assert failed["checks"] == [
        {"check": "helpful: agreement >= 1.1", "passed": False, "candidate": 1.0},
        {"check": "helpful: kappa >= 1.1", "passed": False, "candidate": 1.0},
        {"check": "helpful: mae <= 0.15", "passed": False, "candidate": 0.2},
    ]

    mae_ok = _calibrate(_labels((1.0, 0.8), (1.0, 0.8)), {"helpful": {"max_mae": 0.25}})
    assert mae_ok["ok"] is True
    assert mae_ok["checks"] == [{"check": "helpful: mae <= 0.25", "passed": True, "candidate": 0.2}]


def test_calibrate_max_mae_null_means_no_mae_check():
    assert _calibrate(_labels((1.0, 0.8)), {"helpful": {"label": "helpful", "max_mae": None}})["checks"] == []
    assert _calibrate(_labels((1.0, 0.8)), {"helpful": {"label": "helpful"}})["checks"] == []
    # a null max_mae does not suppress the checks that *are* configured
    mixed = _calibrate(_labels((1.0, 0.8), (1.0, 0.8)), {"helpful": {"max_mae": None, "min_agreement": 0.9}})
    assert mixed["checks"] == [{"check": "helpful: agreement >= 0.9", "passed": True, "candidate": 1.0}]
