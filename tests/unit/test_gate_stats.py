"""WP1.3: the statistics core of the gate -- stdev convention, CIs, exact pass^k, row identity,
baseline pinning, drift and attribution.

Every test here is callable with no arguments, so `python tests/run_without_pytest.py` runs the same
suite as pytest. The property tests use hypothesis (a dev extra, like `test_redact.py`).
"""

import json
import math
import pathlib
import shutil
import statistics
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from evalops import config as config_mod  # noqa: E402
from evalops import datasets, gate, locks, prompts, run  # noqa: E402
from evalops.ports import Ledger, LoadedPrompt, PromptStore, RowResult, RunRecord  # noqa: E402

SUCCESS = [{"metric": "traj/mean", "min": 1.0}]
PROPERTY = settings(max_examples=40, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _row(row_id, score, key=None, error=None):
    """One stored row: `score` is the `traj` scorer value (1.0 pass, 0.0 fail)."""
    return RowResult(
        row_id=row_id,
        inputs={},
        scores={"traj": score} if score is not None else {},
        error=error,
        row_key=key,
    )


def _present(value):
    """Assert an interval/block is present, then hand it back narrowed (never subscript an Optional)."""
    assert isinstance(value, dict), value
    return value


def _keys_for(records):
    """content -> row_key, so two orderings of the same rows are comparable by content."""
    keys = datasets.row_keys(records)
    assert len(keys) == len(records)
    return {json.dumps(r, sort_keys=True): k for r, k in zip(records, keys, strict=True)}


def _runs(order, matrix, with_key=True):
    """Per-run rows for dataset `order`; `matrix[run][content]` is that row's score."""
    runs = []
    for scores in matrix:
        rows = []
        for i, content in enumerate(order):
            key = datasets.row_key({"inputs": {"q": content}}) if with_key else None
            rows.append(_row(f"{i:05d}", scores[content], key))
        runs.append(rows)
    return runs


# ---------------------------------------------------------------------------
# (1) D1: one dispersion convention -- the sample standard deviation
# ---------------------------------------------------------------------------
def test_aggregate_reports_sample_stdev():
    agg = gate.aggregate([{"x": 1.0}, {"x": 3.0}])
    assert agg["x"]["std"] == statistics.stdev([1.0, 3.0])
    assert abs(agg["x"]["std"] - math.sqrt(2)) < 1e-12  # sample, not the population sd (1.0)
    assert agg["x"]["std"] != statistics.pstdev([1.0, 3.0])
    assert agg["x"]["n"] == 2
    # n == 1 has no dispersion and must not raise: it reports 0.0 (the key is unchanged)
    assert gate.aggregate([{"x": 5.0}])["x"]["std"] == 0.0


# ---------------------------------------------------------------------------
# (2) D2: Wilson interval (proportions)
# ---------------------------------------------------------------------------
def test_wilson_interval_is_symmetric_around_the_proportion():
    ci = _present(gate.wilson_interval(50, 100))
    assert ci["lower"] < 0.5 < ci["upper"]
    assert abs(ci["lower"] + ci["upper"] - 1.0) < 1e-9  # symmetric about p = 0.5
    assert (ci["method"], ci["n"], ci["k"]) == ("wilson", 100, 50)


def test_wilson_interval_edges_and_known_value():
    assert _present(gate.wilson_interval(0, 10))["lower"] == 0.0
    assert _present(gate.wilson_interval(10, 10))["upper"] == 1.0
    assert gate.wilson_interval(0, 0) is None and gate.wilson_interval(3, 0) is None
    ci = _present(gate.wilson_interval(8, 10))
    assert abs(ci["lower"] - 0.4901) < 1e-3
    assert abs(ci["upper"] - 0.9433) < 1e-3


# ---------------------------------------------------------------------------
# (3) D2: deterministic percentile bootstrap of the mean
# ---------------------------------------------------------------------------
def test_bootstrap_ci_is_deterministic_and_brackets_the_mean():
    vals = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0]
    first, second = _present(gate.bootstrap_ci(vals)), _present(gate.bootstrap_ci(vals))
    assert first == second  # fixed seed, local RNG: the same input twice is the same interval
    assert first["lower"] <= statistics.fmean(vals) <= first["upper"]
    assert (first["method"], first["n"], first["resamples"]) == ("bootstrap-percentile", 6, 2000)
    assert gate.bootstrap_ci([1.0]) is None and gate.bootstrap_ci([]) is None


def test_bootstrap_ci_widens_with_spread():
    wide, narrow = _present(gate.bootstrap_ci([0.0, 10.0])), _present(gate.bootstrap_ci([4.0, 6.0]))
    assert (wide["upper"] - wide["lower"]) > (narrow["upper"] - narrow["lower"])
    assert narrow["lower"] <= 5.0 <= narrow["upper"] and wide["lower"] <= 5.0 <= wide["upper"]


@PROPERTY
@given(st.lists(st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False), min_size=2, max_size=30))
def test_bootstrap_ci_always_brackets_the_sample_mean(vals):
    ci = _present(gate.bootstrap_ci(vals))
    assert ci["lower"] <= statistics.fmean(vals) <= ci["upper"]
    assert 0.0 <= ci["lower"] <= ci["upper"] <= 1.0


# ---------------------------------------------------------------------------
# (4) D3: exact pass^k semantics are monotone in k
# ---------------------------------------------------------------------------
@PROPERTY
@given(st.lists(st.lists(st.booleans(), min_size=4, max_size=4), min_size=3, max_size=6))
def test_pass_hat_k_and_pass_at_k_are_monotone_in_k(matrix):
    # rectangular on purpose: a ragged matrix would change the case set with k, not just the window
    runs = [[_row(f"{i:05d}", 1.0 if ok else 0.0, f"case-{i}") for i, ok in enumerate(row)] for row in matrix]
    hat = [gate.reliability(runs[:k], SUCCESS)["pass_hat_k"] for k in (1, 2, 3)]
    at = [gate.reliability(runs[:k], SUCCESS)["pass_at_k"] for k in (1, 2, 3)]
    assert hat[0] >= hat[1] >= hat[2]  # all of the first k repeats gets harder as k grows
    assert at[0] <= at[1] <= at[2]  # any of the first k repeats gets easier


def test_pass_hat_k_is_the_plugin_estimator_over_a_fixed_k():
    # case 0 passes in all three repeats, case 1 fails repeat 2, case 2 never passes
    runs = [
        [_row("0", 1.0, "c0"), _row("1", 1.0, "c1"), _row("2", 0.0, "c2")],
        [_row("0", 1.0, "c0"), _row("1", 0.0, "c1"), _row("2", 0.0, "c2")],
        [_row("0", 1.0, "c0"), _row("1", 1.0, "c1"), _row("2", 0.0, "c2")],
    ]
    rel = gate.reliability(runs, SUCCESS, ["1", "2", "3"])
    assert (rel["k"], rel["cases"]) == (3, 3)
    assert abs(rel["pass_hat_k"] - 1 / 3) < 1e-12  # only case 0 survives all three repeats
    assert abs(rel["pass_at_k"] - 2 / 3) < 1e-12  # cases 0 and 1 pass at least once
    assert abs(rel["success_rate"] - 5 / 9) < 1e-12
    assert rel["comparable"] is True and rel["cases_with_fewer_repeats"] == 0
    ci = _present(rel["pass_hat_k_ci"])
    assert ci["lower"] < rel["pass_hat_k"] < ci["upper"]


def test_reliability_reports_a_ragged_matrix_as_not_comparable():
    runs = [
        [_row("0", 1.0, "c0"), _row("1", 1.0, "c1")],
        [_row("0", 1.0, "c0")],  # c1 is missing from this repeat
    ]
    rel = gate.reliability(runs, SUCCESS, ["1", "2"])
    assert rel["k"] == 1 and rel["cases_with_fewer_repeats"] == 1 and rel["comparable"] is False


# ---------------------------------------------------------------------------
# (5) D3: repeats are ordered by `evalops.repeat` as an integer, never as a string
# ---------------------------------------------------------------------------
def _repeat_group():
    """10 runs: case-a in every run (succeeds only at repeat 2), case-b only in repeats 1 and 2.

    The ragged case makes `k = 2`, which is exactly when the *order* of the repeats decides what
    `v[:k]` contains -- a full-length vector is order-insensitive and would hide the bug.
    """
    runs = []
    for rep in range(1, 11):
        rows = [_row("00000", 1.0 if rep == 2 else 0.0, "case-a")]
        if rep <= 2:
            rows.append(_row("00001", 1.0, "case-b"))
        runs.append(rows)
    return runs


def test_repeat_ordering_is_numeric_not_lexicographic():
    runs, tags = _repeat_group(), [str(i) for i in range(1, 11)]
    numeric = gate.reliability(runs, SUCCESS, tags)
    assert numeric["unordered_repeats"] is False
    assert numeric["k"] == 2
    assert numeric["pass_at_k"] == 1.0  # repeat 2 is inside the first two repeats
    assert numeric["pass_hat_k"] == 0.5  # repeat 1 failed for case-a

    lexicographic = sorted(tags)  # "1", "10", "2", "3", ...: exactly the string-sort bug
    shuffled = [runs[tags.index(tag)] for tag in lexicographic]
    # without a numeric order the same rows are read positionally: repeat 2 lands third, so pass@k
    # over the first two repeats misses it
    positional = gate.reliability(shuffled, SUCCESS)
    assert positional["unordered_repeats"] is True
    assert positional["pass_at_k"] == 0.5
    # given the tags, the order is restored and both agree with the canonical order
    restored = gate.reliability(shuffled, SUCCESS, lexicographic)
    assert restored["unordered_repeats"] is False
    assert (restored["pass_hat_k"], restored["pass_at_k"]) == (numeric["pass_hat_k"], numeric["pass_at_k"])
    # duplicate or missing tags cannot establish an order: say so instead of guessing
    assert gate.reliability(runs, SUCCESS, ["1"] * 10)["unordered_repeats"] is True
    assert gate.reliability(runs, SUCCESS, [""] * 10)["unordered_repeats"] is True


# ---------------------------------------------------------------------------
# (6) D4: stable row identity -- content, never the list index
# ---------------------------------------------------------------------------
ROWS = [
    {"inputs": {"q": "alpha"}, "expectations": {"e": 1}},
    {"inputs": {"q": "beta"}},
    {"inputs": {"q": "gamma"}, "expectations": {"e": 3}},
]


def test_row_key_is_content_addressed_and_order_independent():
    assert _keys_for(ROWS) == _keys_for(list(reversed(ROWS)))
    assert len(set(datasets.row_keys(ROWS))) == len(ROWS)
    # metadata, backend ids and recorded outputs never reach the row key
    decorated = {**ROWS[0], "row_id": "99999", "outputs": {"answer": "x"}, "schema_version": 1}
    assert datasets.row_key(decorated) == datasets.row_key(ROWS[0])
    assert datasets.row_key(ROWS[0]) != datasets.row_key(ROWS[1])


def test_duplicate_rows_get_distinct_deterministic_keys():
    dup = [ROWS[0], ROWS[0], ROWS[1], ROWS[0]]
    keys = datasets.row_keys(dup)
    assert len(set(keys)) == len(keys)  # identical rows are interchangeable but never the same row
    base = datasets.row_key(ROWS[0])
    assert keys[0] == f"{base}#1" and keys[1] == f"{base}#2" and keys[3] == f"{base}#3"
    assert keys[2] == datasets.row_key(ROWS[1])  # a unique row carries no suffix
    # identical rows are interchangeable, so reordering *distinct* rows must not change any key
    assert sorted(datasets.row_keys(list(reversed(ROWS)))) == sorted(datasets.row_keys(ROWS))


CAND = [{"alpha": 1.0, "beta": 1.0, "gamma": 0.0}]
BASE = [{"alpha": 0.0, "beta": 1.0, "gamma": 0.0}]


def test_alignment_by_row_key_survives_a_reordered_dataset():
    fwd = _runs(["alpha", "beta", "gamma"], CAND)
    rev = _runs(["gamma", "beta", "alpha"], CAND)
    assert gate.reliability(fwd, SUCCESS, ["1"])["aligned_by"] == "row_key"
    assert gate.reliability(fwd, SUCCESS, ["1"])["pass_hat_k"] == gate.reliability(rev, SUCCESS, ["1"])["pass_hat_k"]
    base = _runs(["alpha", "beta"], BASE)  # baseline only has two of the three rows
    paired = _present(gate.paired_drift(rev, base, "traj/mean"))
    assert paired["aligned_by"] == "row_key" and paired["aligned"] == 2
    assert paired["unaligned"] == {"candidate": 1, "baseline": 0}
    # alpha's delta is +1.0, beta's is 0.0 -> pairing by content never pairs gamma with alpha
    assert abs(paired["mean_delta"] - 0.5) < 1e-12


def test_v0_rows_without_a_row_key_fall_back_to_row_id():
    v0 = _runs(["alpha", "beta", "gamma"], CAND, with_key=False)
    rel = gate.reliability(v0, SUCCESS, ["1"])
    assert rel["aligned_by"] == "row_id" and rel["pass_hat_k"] == 2 / 3
    paired = _present(gate.paired_drift(v0, v0, "traj/mean"))
    assert paired["aligned_by"] == "row_id" and paired["mean_delta"] == 0.0


# ---------------------------------------------------------------------------
# (7) D6: paired drift aligns on shared keys only, and its CI shrinks with n
# ---------------------------------------------------------------------------
def test_paired_drift_ignores_keys_missing_from_one_side():
    cand = _runs(["alpha", "beta", "gamma"], CAND)
    base = _runs(["alpha", "beta"], BASE)
    paired = _present(gate.paired_drift(cand, base, "traj/mean"))
    assert paired["aligned"] == 2
    assert paired["unaligned"] == {"candidate": 1, "baseline": 0}
    ci = _present(paired["ci"])
    assert ci["lower"] <= paired["mean_delta"] <= ci["upper"]
    assert gate.paired_drift(cand, base, "not_a_metric") is None
    assert gate.paired_drift([], base, "traj/mean") is None


def test_paired_drift_ci_shrinks_as_aligned_n_grows():
    def width(n):
        base_rows, cand_rows = [], []
        for i in range(n):
            key, delta = f"k{i:04d}", 0.05 + 0.01 * ((i % 4) - 1.5)
            base_rows.append(_row(f"{i:05d}", 0.5, key))
            cand_rows.append(_row(f"{i:05d}", 0.5 + delta, key))
        paired = _present(gate.paired_drift([cand_rows], [base_rows], "traj/mean"))
        assert paired["aligned"] == n and paired["aligned_by"] == "row_key"
        ci = _present(paired["ci"])
        return ci["upper"] - ci["lower"]

    assert width(64) < width(4)


def test_drift_reports_effect_size_and_significance():
    def runs(prefix, correctness, cost):
        return [
            RunRecord(
                f"{prefix}{i}",
                {},
                {},
                {"correctness/mean": correctness + 0.001 * i, "cost_per_row_usd": cost},
                1,
            )
            for i in range(5)
        ]

    rules = {"metrics": {"correctness/mean": {"min": 0.8}}}
    cand, base = runs("c", 0.70, 0.02), runs("b", 0.90, 0.01)
    out = gate.drift(cand, base, rules)
    d = out["metrics"]["correctness/mean"]
    assert d["delta"] < 0 and d["pct_change"] < 0
    assert d["z"] is not None and d["p"] < 0.05 and d["significant"] is True
    assert d["cohen_d"] is not None and d["cohen_d"] < 0
    # a metric present in both aggregates is reported even without a rule
    assert set(out["metrics"]) == {"correctness/mean", "cost_per_row_usd"}
    assert out["metrics"]["cost_per_row_usd"]["pct_change"] > 0
    # no per-run dispersion to test against -> z/p stay None instead of dividing by zero
    one = gate.drift(cand[:1], base[:1], rules)["metrics"]["correctness/mean"]
    assert one["z"] is None and one["p"] is None and one["significant"] is False
    # drift is a pure read: it neither mutates its inputs nor produces checks
    assert rules == {"metrics": {"correctness/mean": {"min": 0.8}}}
    assert "checks" not in out


def test_drift_pairs_rows_by_row_key_only():
    out = gate.drift(
        [RunRecord("c", {}, {}, {"traj/mean": 1.0}, 1)],
        [RunRecord("b", {}, {}, {"traj/mean": 0.0}, 1)],
        {},
        cand_rows=_runs(["alpha", "beta"], CAND),
        base_rows=_runs(["alpha", "beta"], BASE),
    )
    assert out["aligned_by"] == "row_key" and out["paired"]["traj/mean"]["aligned"] == 2
    v0 = gate.drift(
        [RunRecord("c", {}, {}, {"traj/mean": 1.0}, 1)],
        [RunRecord("b", {}, {}, {"traj/mean": 0.0}, 1)],
        {},
        cand_rows=_runs(["alpha", "beta"], CAND, with_key=False),
        base_rows=_runs(["alpha", "beta"], BASE, with_key=False),
    )
    assert v0["aligned_by"] == "row_id" and v0["paired"] == {}  # positional pairing is not drift


# ---------------------------------------------------------------------------
# (8) D5: the baseline is pinned by group + hash
# ---------------------------------------------------------------------------
class _MemoryLedger(Ledger):
    """Newest-first `find_runs`, tag/param mutation in `log`, and no rows artifacts."""

    name = "memory"
    provides = ("ledger",)

    def __init__(self, runs=None):
        self.runs = list(runs or [])

    def start_run(self, name, tags):
        run_id = f"run-{len(self.runs)}"
        self.runs.append(RunRecord(run_id, dict(tags), {}, {}, 0))
        return run_id

    def end_run(self, run_id, status="FINISHED"):
        pass

    def log(self, run_id, params=None, metrics=None, tags=None):
        for r in self.runs:
            if r.run_id == run_id:
                r.tags.update(tags or {})
                r.params.update(params or {})
                r.metrics.update(metrics or {})

    def put_artifact(self, run_id, name, text):
        pass

    def get_artifact(self, run_id, name):
        raise KeyError(name)  # no rows artifact: the gate skips it, exactly like a missing one

    def find_runs(self, tags=None, since_ms=None, limit=None):
        found = [r for r in self.runs if all(r.tags.get(k) == v for k, v in (tags or {}).items())]
        found.reverse()  # the ledger contract: newest first
        return found[:limit] if limit is not None else found


class _MemoryPrompts(PromptStore):
    name = "git"
    provides = ("prompts",)

    def __init__(self):
        self.versions, self.aliases = {}, {}

    def register(self, name, template, tags):
        version = str(1 + sum(1 for n, _ in self.versions if n == name))
        self.versions[(name, version)] = template
        return version

    def load(self, name, version):
        return LoadedPrompt(name, version, self.versions[(name, version)])

    def get_alias(self, name, alias):
        return self.aliases.get((name, alias))

    def set_alias(self, name, alias, version):
        self.aliases[(name, alias)] = version


class _FakeCfg:
    def __init__(self, root, gates=None):
        self.root = root
        self.env = "local"
        self.lock_dir = root / ".evalops"
        self.gates = gates or {}

    def section(self, key):
        return self.gates if key == "gates" else {}


class _Registry:
    """Stands in for `evalops.registry.Registry` so `run_gate` runs against in-memory plugins."""

    ledger = None
    prompts = None
    describe_map = {"prompts": "git", "ledger": "memory"}

    def __init__(self, cfg=None):
        pass

    def get(self, role):
        return self.ledger if role == "ledger" else self.prompts

    def describe(self):
        return dict(self.describe_map)


class _patched_registry:
    """Point `gate.run_gate`'s `Registry(cfg)` at the in-memory plugins (no fixtures in the runner)."""

    def __init__(self, ledger, store=None):
        self.ledger, self.store, self.old = ledger, store, None

    def __enter__(self):
        self.old = gate.Registry
        bound = type("_BoundRegistry", (_Registry,), {"ledger": self.ledger, "prompts": self.store or _MemoryPrompts()})
        gate.Registry = bound  # type: ignore[assignment]  # test double, same call shape as Registry(cfg)
        return bound

    def __exit__(self, *exc):
        gate.Registry = self.old
        return False


def _evidence(run_id, group, fingerprint, app_version="proj-abcdef12-12345678"):
    return RunRecord(
        run_id,
        {"evalops.group": group, "evalops.promotable": "true", "evalops.gate": "passed"},
        {
            "app_version": app_version,
            "dataset": "smoke",
            "dataset_fingerprint": fingerprint,
            "prompt.support_system": "git:support_system@1",
            "git_sha": "0" * 40,
        },
        {"facts/mean": 0.9},
        1,
    )


def _promote(cfg, ledger, store, group="g1"):
    locks.write(cfg, "prompts", {"support_system": {"plugin": "git", "physical": "support_system", "version": "1"}})
    return prompts.promote(cfg, type("_Reg", (_Registry,), {"ledger": ledger, "prompts": store})(), "production", group)


def test_promote_pins_the_baseline_to_app_version_and_dataset_hash():
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-pin-"))
    try:
        cfg, ledger, store = _FakeCfg(root), _MemoryLedger([_evidence("r1", "g1", "fp-a")]), _MemoryPrompts()
        _promote(cfg, ledger, store)
        run = ledger.runs[0]
        assert run.tags["evalops.baseline"] == "true"
        assert run.tags["evalops.baseline.app_version"] == run.params["app_version"]
        assert run.tags["evalops.baseline.dataset_fingerprint"] == "fp-a"

        # a candidate on different data is not a drift measurement: the gate fails and says why
        ledger.runs.append(_evidence("c1", "g2", "fp-b"))
        with _patched_registry(ledger):
            out = gate.run_gate(cfg, group="g2")
        assert out["ok"] is False
        assert [c["check"] for c in out["checks"] if not c["passed"]] == ["baseline dataset fingerprint matches"]
        assert out["baseline"] == {
            "group": "g1",
            "app_version": run.params["app_version"],
            "dataset_fingerprint": "fp-a",
            "pinned": True,
        }
        assert _present(out["drift"])["metrics"]["facts/mean"]["delta"] == 0.0
        assert set(out["attribution"]["changed"]) == {"dataset_fingerprint"}  # the data differs

        # the same data on both sides is not a failure, and the pin is reported
        ledger.runs.append(_evidence("c2", "g3", "fp-a"))
        with _patched_registry(ledger):
            ok = gate.run_gate(cfg, group="g3")
        assert ok["ok"] is True and [c for c in ok["checks"] if not c["passed"]] == []
        assert _present(ok["baseline"])["pinned"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_baseline_pin_that_disagrees_with_its_run_fails():
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-pin-bad-"))
    try:
        base = _evidence("r1", "g1", "fp-a")
        base.tags.update(
            {
                "evalops.baseline": "true",
                "evalops.baseline.app_version": base.params["app_version"],
                "evalops.baseline.dataset_fingerprint": "moved-fp",
            }
        )
        ledger = _MemoryLedger([base, _evidence("c1", "g2", "fp-a")])
        with _patched_registry(ledger):
            out = gate.run_gate(_FakeCfg(root), group="g2")
        assert out["ok"] is False
        failed = [c["check"] for c in out["checks"] if not c["passed"]]
        assert "baseline pin matches run" in failed
        assert _present(out["baseline"])["pinned"] is True
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_no_baseline_means_no_baseline_checks():
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-nobase-"))
    try:
        ledger = _MemoryLedger([_evidence("c1", "g1", "fp-a")])
        with _patched_registry(ledger):
            out = gate.run_gate(_FakeCfg(root), group="g1")
        assert out["ok"] is True and out["baseline"] is None and out["drift"] is None
        assert out["attribution"] == {"changed": {}, "summary": "no baseline run to attribute against"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (9) D7: change attribution names exactly the changed identity/provenance params
# ---------------------------------------------------------------------------
def test_attribution_names_exactly_the_changed_params():
    def run(params, run_id="r"):
        return RunRecord(run_id, {"evalops.group": "g"}, params, {}, 1)

    base = run(
        {
            "git_sha": "a" * 40,
            "prompt.support_system": "git:support_system@1",
            "judge.helpful": "openai/gpt@1",
            "plugin.ledger": "sql",
            "dataset": "smoke",
            "behavior_config": '{"tools": []}',
        }
    )
    cand = run(
        {
            "git_sha": "b" * 40,
            "prompt.support_system": "git:support_system@2",
            "judge.helpful": "openai/gpt@1",
            "plugin.ledger": "sql",
            "dataset": "smoke",
            "behavior_config": '{"tools": [{"very": "different but truncated',
        }
    )
    out = gate.attribution([cand], [base])
    assert set(out["changed"]) == {"git_sha", "prompt.support_system"}
    assert out["changed"]["git_sha"] == {"candidate": "b" * 40, "baseline": "a" * 40}
    assert out["changed"]["prompt.support_system"] == {
        "candidate": "git:support_system@2",
        "baseline": "git:support_system@1",
    }
    assert "behavior_config" not in out["changed"]
    assert "behavior_config" not in json.dumps(out)
    assert "git_sha" in out["summary"]
    # unchanged, non-attributable and absent-side cases never invent a change
    assert gate.attribution([cand], [cand])["changed"] == {}
    assert gate.attribution([run({"foo": "1"})], [run({"foo": "2"})])["changed"] == {}
    assert gate.attribution([], [base])["changed"] == {}
    # only the newest run of each group is compared
    newer = run({**base.params, "git_sha": "c" * 40}, run_id="r2")
    newest = gate.attribution([newer, cand], [base])
    assert newest["changed"]["git_sha"]["candidate"] == "c" * 40
    assert "prompt.support_system" not in newest["changed"]


# ---------------------------------------------------------------------------
# (10) additive: drift/attribution never change a check outcome
# ---------------------------------------------------------------------------
def test_additive_diagnostics_do_not_change_check_outcomes():
    rules = {"metrics": {"correctness/mean": {"min": 0.8, "max_drop": 0.03}}, "cost": {"max_cost_per_row_usd": 0.05}}
    cand = gate.aggregate([{"correctness/mean": 0.88, "cost_per_row_usd": 0.01}] * 3)
    base = gate.aggregate([{"correctness/mean": 0.90, "cost_per_row_usd": 0.01}] * 3)
    passed, checks = gate.check(cand, base, rules)
    assert passed and [c["passed"] for c in checks] == [True, True, True]
    gate.drift(
        [RunRecord("c", {}, {}, {"correctness/mean": 0.88}, 1)],
        [RunRecord("b", {}, {}, {"correctness/mean": 0.9}, 1)],
        rules,
    )
    gate.attribution([RunRecord("c", {}, {"git_sha": "x"}, {}, 1)], [RunRecord("b", {}, {"git_sha": "y"}, {}, 1)])
    assert (passed, checks) == gate.check(cand, base, rules)


# ---------------------------------------------------------------------------
# (11) D2: the bootstrap lower-bound rule is opt-in per metric and additive
# ---------------------------------------------------------------------------
def test_min_ci_lower_bootstrap_rule_reads_the_bootstrap_lower_bound():
    rows = [[_row("0", 0.9, "a"), _row("1", 0.9, "b"), _row("2", 0.9, "c")]]
    passes = gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower_bootstrap": 0.9}}})
    assert [c["check"] for c in passes] == ["traj/mean bootstrap CI lower >= 0.9"]
    assert passes[0]["passed"] is True and passes[0]["candidate"] == 0.9
    fails = gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower_bootstrap": 0.95}}})
    assert fails[0]["passed"] is False
    # below two values there is no interval, so the rule fails instead of passing on a guess
    one = gate.check_rows([[_row("0", 0.9, "a")]], [], {"metrics": {"traj/mean": {"min_ci_lower_bootstrap": 0.0}}})
    assert one[0]["passed"] is False and one[0]["candidate"] is None
    # a rule that is not configured produces no check
    assert gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min": 0.5}}}) == []


# ---------------------------------------------------------------------------
# (12) D4 persistence: row_key is additive in rows.jsonl and v0 still reads
# ---------------------------------------------------------------------------
def test_rows_artifact_round_trips_the_row_key_and_reads_v0():
    row = RowResult("00000", {"q": "alpha"}, outputs="x", row_key="0123456789abcdef")
    stored = json.dumps(run.row_to_json(row, 100))
    back = run.rows_from_artifact(stored)
    assert back[0].row_key == "0123456789abcdef"
    # a v0 line (no row_key) reads fine and reports no key: the gate then falls back to row_id
    v0 = {"row_id": "00000", "inputs": {"q": "alpha"}, "outputs": "x", "schema_version": 1}
    assert run.rows_from_artifact(json.dumps(v0))[0].row_key is None


# ---------------------------------------------------------------------------
# (13) the new rule keys load from a temp evalops.yaml (exit 0) and reach the gate
# ---------------------------------------------------------------------------
def _gates_from_yaml(block: str) -> dict:
    """Load the ``gates`` section from a temp ``evalops.yaml``; a bad key raises (exit 2), so ok is 0."""
    directory = pathlib.Path(tempfile.mkdtemp(prefix="evalops-gate-stats-"))
    try:
        path = directory / "evalops.yaml"
        path.write_text(
            "project: gate-stats\n"
            "plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}\n"
            "app: {predict_fn: app.hooks:build_predict_fn}\n"
            f"gates:\n{block}",
            encoding="utf-8",
        )
        return config_mod.load(str(path)).section("gates")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def test_min_ci_lower_bootstrap_rule_loads_from_yaml_and_uses_the_bootstrap_bound():
    # skewed sample: the normal approximation reaches below 0 while the bootstrap lower bound is 0.0
    vals = [0.0] * 8 + [1.0, 1.0]
    rows = [[_row(str(i), v, f"k{i}") for i, v in enumerate(vals)]]
    rules = _gates_from_yaml("  metrics:\n    traj/mean: {min_ci_lower_bootstrap: -0.03}\n")
    assert rules == {"metrics": {"traj/mean": {"min_ci_lower_bootstrap": -0.03}}}
    assert gate._needs_rows(rules) is True
    boot = gate.check_rows(rows, [], rules)
    assert [c["check"] for c in boot] == ["traj/mean bootstrap CI lower >= -0.03"]
    assert boot[0]["passed"] is True and boot[0]["method"] == "bootstrap-percentile"
    assert boot[0]["candidate"] == _present(gate.bootstrap_ci(vals))["lower"] == 0.0
    # the same threshold on the normal approximation fails: the rule really reads a *different* bound
    normal = gate.check_rows(rows, [], {"metrics": {"traj/mean": {"min_ci_lower": -0.03}}})
    assert normal[0]["passed"] is False
    assert normal[0]["candidate"] < boot[0]["candidate"] == 0.0
    assert gate.ci_lower(rows, "traj/mean") != boot[0]["candidate"]


def test_min_pass_hat_k_wilson_rule_gates_the_wilson_lower_bound():
    # three cases, each succeeding on both repeats: pass_hat_k = 1.0, Wilson lower bound ~0.438
    runs = [[_row("0", 1.0, "a"), _row("1", 1.0, "b"), _row("2", 1.0, "c")] for _ in range(2)]
    rules = _gates_from_yaml(
        "  reliability:\n    success: [{metric: traj/mean, min: 1.0}]\n    min_pass_hat_k_wilson: 0.4\n"
    )
    rel = rules["reliability"]
    assert rel["min_pass_hat_k_wilson"] == 0.4 and gate._needs_rows(rules) is True
    ci = _present(gate.reliability(runs, rel["success"])["pass_hat_k_ci"])
    assert ci["lower"] > 0.4
    checks = [c for c in gate.check_rows(runs, [], rules) if c["check"].startswith("pass_hat_k_ci.lower")]
    assert checks == [
        {"check": "pass_hat_k_ci.lower >= 0.4", "passed": True, "candidate": ci["lower"], "method": "wilson"}
    ]
    # above the Wilson lower bound it fails, although the pass_hat_k point estimate is 1.0
    strict = {**rel, "min_pass_hat_k_wilson": 0.5}
    failed = [c for c in gate.check_rows(runs, [], {**rules, "reliability": strict}) if "wilson" in str(c)]
    assert failed[0]["check"] == "pass_hat_k_ci.lower >= 0.5" and failed[0]["passed"] is False
    # a missing interval fails instead of passing on a guess
    missing = gate.check_rows([], [], rules)
    assert missing[0]["check"] == "pass_hat_k_ci.lower >= 0.4"
    assert missing[0]["passed"] is False and missing[0]["candidate"] is None
