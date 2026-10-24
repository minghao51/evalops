"""WP1.4 (A+B): every branch of ``gate.check`` and the row-level statistics helpers.

Falsifies, in order: (A1) a metric missing from the candidate is one failing check with its reason,
(A2) ``min``/``max`` at and around the bound, (A3) ``max_drop`` within/exceeded/without a baseline,
(A4) ``max_increase_pct`` including the mean-0 baseline guard (no ZeroDivisionError, no check),
(A5) ``min_ci_lower`` with rows and the ``n < 2`` case, (A6) the normal bound vs the bootstrap bound
on a skewed sample that straddles one threshold, (A7/A8) the two cost rules, (A9) the vacuous gate,
(B1) ``reliability([])``, (B2) ``_row_value`` sources and a missing value counting as success,
(B3) ``ci_lower`` below two values and at exactly two, (B4) the reliability rules and the absent
baseline, (B5) the ``success: []`` guard, (B6) ``_needs_rows`` per rule kind, (B7) the always-passing
``reliability (info)`` entry.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import math
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from evalops import gate  # noqa: E402
from evalops.ports import RowResult  # noqa: E402

SUCCESS = [{"metric": "traj/mean", "min": 1.0}]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _agg(values: dict) -> dict:
    """`aggregate()` output for a single run with these metrics (the shape `check` reads)."""
    return gate.aggregate([dict(values)])


def _rows(*score_lists) -> list[list]:
    """One list per run; `score_lists[run][case]` is that case's `traj` score.

    Row ids are the case index, so the same case aligns across runs (the `row_id` fallback).
    """
    return [[RowResult(str(i), {}, scores={"traj": v}) for i, v in enumerate(scores)] for scores in score_lists]


def _checks_by_name(checks: list[dict]) -> dict[str, dict]:
    return {c["check"]: c for c in checks}


# ---------------------------------------------------------------------------
# (A1) a metric missing from the candidate
# ---------------------------------------------------------------------------
def test_check_missing_metric_is_one_failing_check_with_its_reason():
    rules = {"metrics": {"facts/mean": {"min": 0.5, "max_drop": 0.1}}}
    candidate = _agg({"other/mean": 1.0})
    passed, checks = gate.check(candidate, _agg({"facts/mean": 0.9}), rules)
    assert passed is False
    # ONE check for the metric, and neither the min nor the max_drop rule is evaluated
    assert checks == [{"check": "facts/mean", "passed": False, "reason": "metric missing from candidate"}]

    # the same when there is no baseline at all and the candidate is empty
    assert gate.check({}, None, rules) == (
        False,
        [{"check": "facts/mean", "passed": False, "reason": "metric missing from candidate"}],
    )


# ---------------------------------------------------------------------------
# (A2) min and max
# ---------------------------------------------------------------------------
def test_check_min_and_max_pass_at_the_bound_and_fail_beyond_it():
    rules = {"metrics": {"facts/mean": {"min": 0.8, "max": 0.95}}}
    passed, checks = gate.check(_agg({"facts/mean": 0.8}), None, rules)
    assert passed is True
    assert [c["check"] for c in checks] == ["facts/mean >= 0.8", "facts/mean <= 0.95"]
    assert [c["candidate"] for c in checks] == [0.8, 0.8]

    below = gate.check(_agg({"facts/mean": 0.79}), None, rules)
    assert below[0] is False and below[1][0]["passed"] is False and below[1][1]["passed"] is True

    above = gate.check(_agg({"facts/mean": 0.96}), None, rules)
    assert above[0] is False and above[1][0]["passed"] is True and above[1][1]["passed"] is False

    # an exact `max` bound passes (the comparison is `<=`, not `<`)
    assert gate.check(_agg({"facts/mean": 0.95}), None, rules)[0] is True


# ---------------------------------------------------------------------------
# (A3) max_drop
# ---------------------------------------------------------------------------
def test_check_max_drop_within_exceeded_and_without_a_baseline():
    rules = {"metrics": {"facts/mean": {"max_drop": 0.05}}}
    within = gate.check(_agg({"facts/mean": 0.95}), _agg({"facts/mean": 1.0}), rules)
    assert within[0] is True
    check = within[1][0]
    assert check["check"] == "facts/mean drop <= 0.05" and check["passed"] is True
    assert check["candidate"] == 0.95 and check["baseline"] == 1.0
    assert abs(check["delta"] + 0.05) < 1e-12  # exactly at the bound passes (the epsilon)

    exceeded = gate.check(_agg({"facts/mean": 0.94}), _agg({"facts/mean": 1.0}), rules)
    assert exceeded[0] is False
    assert exceeded[1][0]["passed"] is False and abs(exceeded[1][0]["delta"] + 0.06) < 1e-12

    # no baseline -> no drop check at all (a drop needs two sides)
    assert gate.check(_agg({"facts/mean": 0.1}), None, rules) == (True, [])
    # a baseline that lacks the metric has no mean either -> still no check
    assert gate.check(_agg({"facts/mean": 0.1}), _agg({"other/mean": 1.0}), rules) == (True, [])


# ---------------------------------------------------------------------------
# (A4) max_increase_pct, including the mean-0 baseline guard
# ---------------------------------------------------------------------------
def test_check_max_increase_pct_and_the_zero_baseline_guard():
    rules = {"metrics": {"trace.tool_calls/mean": {"max_increase_pct": 10}}}
    within = gate.check(_agg({"trace.tool_calls/mean": 110.0}), _agg({"trace.tool_calls/mean": 100.0}), rules)
    assert within[0] is True
    assert within[1] == [
        {
            "check": "trace.tool_calls/mean increase <= 10%",
            "passed": True,
            "candidate": 110.0,
            "baseline": 100.0,
            "increase_pct": 10.0,
        }
    ]

    exceeded = gate.check(_agg({"trace.tool_calls/mean": 111.0}), _agg({"trace.tool_calls/mean": 100.0}), rules)
    assert exceeded[0] is False and exceeded[1][0]["passed"] is False and exceeded[1][0]["increase_pct"] == 11.0

    # a mean-0 baseline is not a percentage: the guard emits no check and cannot divide by zero
    zero = gate.check(_agg({"trace.tool_calls/mean": 4.0}), _agg({"trace.tool_calls/mean": 0.0}), rules)
    assert zero == (True, [])
    # ... and no baseline is the same: no check
    assert gate.check(_agg({"trace.tool_calls/mean": 4.0}), None, rules) == (True, [])


# ---------------------------------------------------------------------------
# (A5) min_ci_lower with rows, and n < 2
# ---------------------------------------------------------------------------
def test_check_rows_min_ci_lower_pass_fail_and_below_two_values():
    rows = _rows([0.8, 0.9, 0.9])
    lower = gate.ci_lower(rows, "traj/mean")
    assert lower is not None and lower < 0.9  # the bound is below the mean, as expected

    passed = gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower": lower - 0.01}}})
    assert passed == [
        {
            "check": f"traj/mean 95% CI lower >= {lower - 0.01}",
            "passed": True,
            "candidate": lower,
        }
    ]
    failed = gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower": lower + 0.01}}})
    assert failed[0]["passed"] is False and failed[0]["candidate"] == lower

    # n < 2 has no dispersion, so there is no bound: the rule fails instead of passing on a guess
    single = gate.check_rows(_rows([0.99]), [], {"metrics": {"traj/mean": {"min_ci_lower": 0.0}}})
    assert single[0]["passed"] is False and single[0]["candidate"] is None


# ---------------------------------------------------------------------------
# (A6) the normal bound vs the bootstrap bound on one skewed sample
# ---------------------------------------------------------------------------
def test_min_ci_lower_and_min_ci_lower_bootstrap_straddle_one_threshold():
    vals = [0.0] * 8 + [1.0, 1.0]  # mean 0.2, but a heavy floor at 0
    rows = [[RowResult(str(i), {}, scores={"traj": v}, row_key=f"k{i}") for i, v in enumerate(vals)]]
    normal, boot = gate.ci_lower(rows, "traj/mean"), gate.bootstrap_ci(vals)
    assert normal is not None and boot is not None
    threshold = -0.05
    assert normal < threshold <= boot["lower"], (normal, boot["lower"])

    rules = {"metrics": {"traj/mean": {"min_ci_lower": threshold, "min_ci_lower_bootstrap": threshold}}}
    checks = _checks_by_name(gate.check_rows(rows, [], rules))
    assert checks[f"traj/mean 95% CI lower >= {threshold}"]["passed"] is False
    assert checks[f"traj/mean 95% CI lower >= {threshold}"]["candidate"] == normal
    assert checks[f"traj/mean bootstrap CI lower >= {threshold}"]["passed"] is True
    assert checks[f"traj/mean bootstrap CI lower >= {threshold}"]["candidate"] == boot["lower"]
    assert checks[f"traj/mean bootstrap CI lower >= {threshold}"]["method"] == "bootstrap-percentile"


# ---------------------------------------------------------------------------
# (A7) cost per row
# ---------------------------------------------------------------------------
def test_check_cost_per_row_max_pass_fail_and_no_candidate_cost():
    rules = {"cost": {"max_cost_per_row_usd": 0.01}}
    passed = gate.check(_agg({"cost_per_row_usd": 0.01}), None, rules)
    assert passed == (True, [{"check": "cost_per_row <= $0.01", "passed": True, "candidate": 0.01}])

    failed = gate.check(_agg({"cost_per_row_usd": 0.02}), None, rules)
    assert failed[0] is False and failed[1][0]["passed"] is False

    # no `cost_per_row_usd` in the candidate -> the rule is not evaluated at all
    assert gate.check(_agg({"facts/mean": 1.0}), None, rules) == (True, [])


# ---------------------------------------------------------------------------
# (A8) cost per row increase
# ---------------------------------------------------------------------------
def test_check_cost_per_row_increase_pct_pass_fail_and_no_baseline_cost():
    rules = {"cost": {"max_cost_per_row_increase_pct": 100}}
    passed = gate.check(_agg({"cost_per_row_usd": 0.02}), _agg({"cost_per_row_usd": 0.01}), rules)
    assert passed[0] is True and passed[1] == [
        {
            "check": "cost_per_row increase <= 100%",
            "passed": True,
            "candidate": 0.02,
            "baseline": 0.01,
            "increase_pct": 100.0,
        }
    ]

    strict = {"cost": {"max_cost_per_row_increase_pct": 50}}
    failed = gate.check(_agg({"cost_per_row_usd": 0.02}), _agg({"cost_per_row_usd": 0.01}), strict)
    assert failed[0] is False and failed[1][0]["passed"] is False

    # a baseline without a cost metric, or with a cost of exactly 0, has no percentage: no check
    assert gate.check(_agg({"cost_per_row_usd": 0.02}), _agg({"facts/mean": 1.0}), rules) == (True, [])
    assert gate.check(_agg({"cost_per_row_usd": 0.02}), _agg({"cost_per_row_usd": 0.0}), rules) == (True, [])


# ---------------------------------------------------------------------------
# (A9) the vacuous gate
# ---------------------------------------------------------------------------
def test_check_with_no_rules_passes_vacuously():
    for rules in ({}, {"metrics": {}, "cost": {}}, {"reliability": {}}):
        passed, checks = gate.check(_agg({"facts/mean": 0.0}), None, rules)
        assert passed is True, rules
        assert checks == [], rules
    # an empty baseline is not a failure either
    assert gate.check(_agg({"facts/mean": 1.0}), {}, {}) == (True, [])


# ---------------------------------------------------------------------------
# (B1) reliability of no runs
# ---------------------------------------------------------------------------
def test_reliability_of_no_runs_is_an_empty_report():
    assert gate.reliability([], SUCCESS) == {}
    assert gate.reliability([], []) == {}
    assert gate.reliability([], SUCCESS, repeats=[]) == {}


# ---------------------------------------------------------------------------
# (B2) _row_value sources
# ---------------------------------------------------------------------------
def test_row_value_reads_scores_and_trace_metrics_and_a_missing_value_succeeds():
    row = RowResult("0", {}, scores={"facts": 0.7}, trace_metrics={"tool_calls": 2})
    assert gate._row_value(row, "facts/mean") == 0.7
    assert gate._row_value(row, "trace.tool_calls/mean") == 2
    assert gate._row_value(row, "facts/p95") == 0.7  # the suffix is stripped for every rule key
    assert gate._row_value(row, "missing/mean") is None
    assert gate._row_value(row, "trace.missing/mean") is None

    # a missing value counts as success: it is not evidence of a failed case
    missing = [[RowResult("0", {}, scores={}), RowResult("1", {}, scores={"other": 0.0})]]
    rel = gate.reliability(missing, SUCCESS)
    assert rel["cases"] == 2 and rel["pass_hat_k"] == 1.0 and rel["success_rate"] == 1.0


# ---------------------------------------------------------------------------
# (B3) ci_lower
# ---------------------------------------------------------------------------
def test_ci_lower_needs_two_values_and_is_exact_at_two():
    assert gate.ci_lower([], "traj/mean") is None
    assert gate.ci_lower(_rows([1.0]), "traj/mean") is None
    assert gate.ci_lower(_rows([1.0, None]), "traj/mean") is None  # one real value only

    values = [0.0, 1.0]
    lower = gate.ci_lower(_rows(values), "traj/mean")
    assert lower is not None
    expected = statistics.fmean(values) - 1.96 * statistics.stdev(values) / math.sqrt(len(values))
    assert lower == expected  # sample stdev (n-1) under the unchanged formula
    assert abs(lower - (-0.48)) < 1e-12  # 0.5 - 1.96 * 0.7071 / sqrt(2)

    # values across runs are pooled: the bound is the one three values give, not the two-value one
    pooled_values = [0.0, 0.0, 1.0]
    pooled = gate.ci_lower(_rows([0.0], [0.0, 1.0]), "traj/mean")
    assert pooled is not None and pooled != lower
    expected_pooled = statistics.fmean(pooled_values) - 1.96 * statistics.stdev(pooled_values) / math.sqrt(3)
    assert pooled == expected_pooled


# ---------------------------------------------------------------------------
# (B4) the reliability rules and the absent baseline
# ---------------------------------------------------------------------------
def test_check_rows_pass_hat_k_rules_and_max_drop_with_and_without_a_baseline():
    cand = _rows([1.0, 1.0], [1.0, 0.0])  # case 0 succeeds twice, case 1 once -> pass_hat_k 0.5
    rel = {"success": SUCCESS}

    passed = gate.check_rows(cand, [], {"reliability": {**rel, "min_pass_hat_k": 0.5}})
    assert passed[0] == {"check": "pass_hat_k (k=2) >= 0.5", "passed": True, "candidate": 0.5}
    failed = gate.check_rows(cand, [], {"reliability": {**rel, "min_pass_hat_k": 0.75}})
    assert failed[0]["passed"] is False and failed[0]["candidate"] == 0.5

    baseline = _rows([1.0, 1.0], [1.0, 1.0])  # baseline pass_hat_k 1.0
    within = gate.check_rows(cand, baseline, {"reliability": {**rel, "max_drop_pass_hat_k": 0.5}})
    assert within[0] == {
        "check": "pass_hat_k drop <= 0.5",
        "passed": True,
        "candidate": 0.5,
        "baseline": 1.0,
    }
    exceeded = gate.check_rows(cand, baseline, {"reliability": {**rel, "max_drop_pass_hat_k": 0.1}})
    assert exceeded[0]["passed"] is False and exceeded[0]["baseline"] == 1.0

    # no baseline rows -> no drop check (there is nothing to drop from)
    without = gate.check_rows(cand, [], {"reliability": {**rel, "max_drop_pass_hat_k": 0.1}})
    assert [c for c in without if "drop" in c["check"]] == []

    # the CI rule is in this same block (B4): the normal bound passes below it and fails above it
    rows = _rows([0.9, 0.9, 0.9])
    assert gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower": 0.9}}})[0]["passed"] is True
    assert gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower": 0.95}}})[0]["passed"] is False


# ---------------------------------------------------------------------------
# (B5) the success: [] guard
# ---------------------------------------------------------------------------
def test_a_reliability_rule_with_an_empty_success_vector_emits_nothing():
    # No reliability check is emitted while `success` is empty, even with the Wilson rule configured:
    # the whole block lives behind `if rel.get("success")`.
    rules = {"reliability": {"success": [], "min_pass_hat_k": 0.9, "min_pass_hat_k_wilson": 0.9, "min_pass_at_k": 0.9}}
    assert gate.check_rows(_rows([1.0]), [], rules) == []
    # a success-based rule with an empty vector is not a reason to load rows either; the Wilson
    # bound on its own is the one exception (B6 pins that separately).
    assert gate._needs_rows({"reliability": {"success": [], "min_pass_hat_k": 0.9}}) is False
    # a non-empty vector does emit the reliability checks
    assert gate.check_rows(_rows([1.0]), [], {"reliability": {**rules["reliability"], "success": SUCCESS}}) != []


# ---------------------------------------------------------------------------
# (B6) _needs_rows per rule kind
# ---------------------------------------------------------------------------
def test_needs_rows_is_true_exactly_for_the_row_reading_rules():
    costing = {"cost": {"max_cost_per_row_usd": 0.1}}
    assert gate._needs_rows({}) is False
    assert gate._needs_rows({"metrics": {}}) is False
    assert gate._needs_rows(costing) is False
    # a plain metrics-only rule reads metrics, never stored rows
    assert gate._needs_rows({"metrics": {"facts/mean": {"min": 0.5, "max": 1.0, "max_drop": 0.1}}}) is False
    assert gate._needs_rows({"metrics": {"facts/mean": {"max_increase_pct": 10}}}) is False

    for rules in (
        {"metrics": {"facts/mean": {"min_ci_lower": 0.5}}},
        {"metrics": {"facts/mean": {"min_ci_lower_bootstrap": 0.5}}},
        {"metrics": {"facts/mean": {"min": 0.5}}, "reliability": {"success": SUCCESS}},
        {"reliability": {"min_pass_hat_k_wilson": 0.4}},  # no `success`, still needs rows
    ):
        assert gate._needs_rows(rules) is True, rules


# ---------------------------------------------------------------------------
# (B7) the info entry
# ---------------------------------------------------------------------------
def test_reliability_info_entry_always_passes_and_carries_the_candidate_fields():
    rules = {"reliability": {"success": SUCCESS, "min_pass_hat_k": 1.1}}  # a threshold that must fail
    checks = gate.check_rows(_rows([1.0, 1.0], [1.0, 1.0]), [], rules)
    info = checks[-1]
    assert info["check"] == "reliability (info)"
    assert info["passed"] is True  # information, never a gate
    assert info["cand_k"] == 2 and info["cand_cases"] == 2 and info["cand_comparable"] is True
    assert info["cand_pass_hat_k"] == 1.0 and info["cand_pass_at_k"] == 1.0 and info["cand_success_rate"] == 1.0
    assert info["cand_pass_hat_k_ci"]["lower"] < 1.0
    # the failing threshold check is separate and does fail
    assert [c["passed"] for c in checks if c["check"] != "reliability (info)"] == [False]
