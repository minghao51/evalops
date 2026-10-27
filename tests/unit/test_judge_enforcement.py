"""WP3b.5: judge accountability -- gate pins, `judge_meta()` conflicts, per-verdict identity, calibration.

What this module falsifies, one group per packet section:

(C1/A) the gate refuses **every** rule without a concrete pin -- threshold-only, model-only with an
    alias, an empty model, an empty rule -- with exit 1 and a `judge pin: <name>` check naming
    `judges.<name>.model`; a pinned rule and a project without a `judges` section add no check (the
    gate goldens contain none, which is the scoped-refusal tripwire); `--label drift` is covered by
    the same `run_gate` enforcement point.
(C2/B) `conflicts_with_meta` compares only the two identity keys, requires exact equality with every
    declared spec (sorted-first failure), and is exit 2 at the load points (`plan`, `eval`,
    `calibrate`) while `evalops judges` stays a pure listing (exit 0).
(C3/C) a real `evaluate` stamps `judge_models[<name>]` on every verdict carrying the spec's score,
    records `judge.<name>.model`/`.prompt_version` on the run, round-trips through `rows.jsonl`, reads
    a v0 line without the field, and a declared-spec score without identity is the judge-named
    exit-5 contract violation.
(C4/D) the gate consumes the judge's latest recorded calibration run (failing tag, a violated or
    missing metric -> exit 1 with candidate/required/calibration_run), latest by `(start_ms, run_id)`,
    and emits **no** check when no calibration evidence exists (STATE compromise 14's posture).
(C5/E) `payload()["unpinned"]` is exhaustive and sorted, `write_human` names the pin fix, and the
    regenerated `judges.json` golden pins a model-only unpinned rule.

Every test is a zero-argument function (the standalone runner calls them too) and offline.
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

from evalops import config as config_mod  # noqa: E402
from evalops import judges as judges_mod  # noqa: E402
from evalops import run as run_mod  # noqa: E402
from evalops.ports import RowResult  # noqa: E402
from evalops.util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
GOLDENS = REPO / "tests" / "fixtures" / "envelope"

#: The smallest config the unit tests need (a `judge_meta` hook is optional for a direct call).
BASE = {"project": "wp35", "app": {"judge_meta": "os:getcwd"}}

#: A fully specified judge whose identity the fake app claims for `model` only (`prompt_version: 1`).
SPEC = {"model": "gpt-4o-2024-11-20", "rubric": "Score it 0 or 1.", "prompt_version": "3"}

#: A declared spec on the shared temp project: the fake app's `judge_meta` claims `fake-judge@1`, so
#: this block disagrees on purpose (C2's end-to-end conflict).
DISAGREEING_SPEC = """
judges:
  correctness:
    model: fake-judge-2024-11-20
    rubric: |
      Score it 0 or 1.
    prompt_version: "1"
    judge: litellm
"""

#: The same judge with the app's own pin: `judge_meta()` may add, so this one is accepted.
AGREEING_SPEC = """
judges:
  correctness:
    model: fake-judge@1
    rubric: |
      Score it 0 or 1.
    prompt_version: "1"
    judge: litellm
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _cfg(rules: dict[str, Any]):
    """A `Config` over a throwaway root with `judges` set to `rules` (no backend)."""
    return config_mod.Config({**BASE, "judges": rules}, proj.tmpdir("evalops-wp35-"))


def _error(fn) -> EvalOpsError:
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError, the call returned normally")


def _dotted(exc: EvalOpsError) -> list[str]:
    return [issue["path"] for issue in exc.details.get("errors", [])]


def _ledger_project(extra: str = ""):
    """A temp project whose ledger is a fresh in-memory fixture module -> (root, module)."""
    name = proj.ledger_name()
    root = proj.project(proj.ledger_config(name) + extra)
    module = proj.install_ledger(root, name)
    proj.ledger_clear(module)
    proj.add_run(module, "g1-run", tags={"evalops.group": "g1"}, metrics={"facts/mean": 1.0}, start_ms=5)
    return root, module


def _gate(root: pathlib.Path, *args: str) -> tuple[int, dict]:
    return proj.run_cli_in_process(root, "gate", "--group", "g1", *args)


def _judge_checks(envelope: dict, prefix: str) -> list[dict]:
    return [c for c in envelope["data"].get("checks") or [] if str(c.get("check", "")).startswith(prefix)]


@contextlib.contextmanager
def fake_modules(modules: dict[str, Any]):
    """Install fake SDK modules for exactly one test, restoring `sys.modules` afterwards."""
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


def fake_litellm(score: Any = 1) -> Any:
    """A fake `litellm` whose completion answers `{"score": <score>}` (no usage block)."""

    def completion(**kwargs):
        message = types.SimpleNamespace(content=json.dumps({"score": score}))
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

    return types.SimpleNamespace(completion=completion)


def _clean(*paths: pathlib.Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C1/A) gate-time pin enforcement: the matrix
# ---------------------------------------------------------------------------
#: (label, judges block, the gate must fail?)
PIN_MATRIX: tuple[tuple[str, str, bool], ...] = (
    ("threshold-only without model", "  helpful: {min_agreement: 0.9}\n", True),
    ("model-only alias latest", "  traced: {model: latest}\n", True),
    ("model-only *-latest", "  traced: {model: fake-judge-latest}\n", True),
    ("a project default", "  traced: {model: default/openai/gpt-4}\n", True),
    ("a server-side default", "  traced: {model: openai/team/default}\n", True),
    ("an empty model", '  traced: {model: ""}\n', True),
    ("an empty rule", "  bare: {}\n", True),
    ("a pinned threshold-only rule", "  helpful: {min_agreement: 0.9, model: fake-judge@1}\n", False),
    ("a pinned model-only rule", "  traced: {model: fake-judge-2024-11-20}\n", False),
)


def test_every_unpinned_rule_shape_fails_the_gate_and_pinned_ones_do_not():
    for label, block, must_fail in PIN_MATRIX:
        root, _module = _ledger_project("\njudges:\n" + block)
        try:
            code, envelope = _gate(root)
            pin_checks = _judge_checks(envelope, "judge pin:")
            if must_fail:
                assert code == 1 == envelope["code"], (label, code, envelope)
                assert len(pin_checks) == 1, (label, pin_checks)
                check = pin_checks[0]
                name = next(iter(block.strip().split(":")[0].split()))
                assert check["check"] == f"judge pin: {name}", (label, check)
                assert check["passed"] is False, (label, check)
                reason = str(check["reason"])
                assert f"judges.{name}.model" in reason, (label, reason)
                assert "an unpinned judge fails the gate (exit 1)" in reason, (label, reason)
                assert "set judges." in reason, (label, reason)
            else:
                assert code == 0 == envelope["code"], (label, code, envelope)
                assert pin_checks == [], (label, pin_checks)
        finally:
            _clean(root)


def test_the_pin_refusal_applies_to_the_drift_label_too():
    root, _module = _ledger_project("\njudges:\n  helpful: {min_agreement: 0.9}\n")
    try:
        code, envelope = _gate(root, "--label", "drift")
        assert code == 1 == envelope["code"], envelope
        assert [c["check"] for c in _judge_checks(envelope, "judge pin:")] == ["judge pin: helpful"]
        assert envelope["data"]["checks"][-1]["check"] == "judge pin: helpful", "no new `data` key"
    finally:
        _clean(root)


def test_a_project_without_a_judges_section_gets_no_judge_check_and_the_goldens_have_none():
    """The scoped-refusal tripwire: no `judges:` -> no new check, and the gate goldens prove it."""
    for name in ("gate_pass.json", "gate_failure.json"):
        golden = proj.read_json(GOLDENS / name)
        assert golden["data"]["checks"], f"{name} must keep its real checks"
        assert not _judge_checks(golden, "judge pin:"), f"{name} gained a pin check"
        assert not _judge_checks(golden, "judge calibration:"), f"{name} gained a calibration check"
    root, _module = _ledger_project()
    try:
        code, envelope = _gate(root)
        assert code == 0 == envelope["code"], envelope
        assert not _judge_checks(envelope, "judge"), envelope["data"]["checks"]
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (C2/B) `judge_meta()` may add, never override
# ---------------------------------------------------------------------------
def test_conflicts_with_meta_accepts_equal_and_absent_identity_values():
    cfg = _cfg({"helpful": dict(SPEC)})
    assert judges_mod.conflicts_with_meta(cfg, {}) is None
    assert judges_mod.conflicts_with_meta(cfg, {"judge_model": SPEC["model"]}) is None
    assert judges_mod.conflicts_with_meta(cfg, {"judge_prompt_version": SPEC["prompt_version"]}) is None
    equal = {"judge_model": SPEC["model"], "judge_prompt_version": SPEC["prompt_version"]}
    assert judges_mod.conflicts_with_meta(cfg, equal) is None
    # non-identity metadata is never compared, whatever it holds
    assert judges_mod.conflicts_with_meta(cfg, {"llm_judge_count": 99, "rubric": "app prose"}) is None
    # zero declared specs: nothing to contradict
    assert judges_mod.conflicts_with_meta(_cfg({"helpful": {"min_agreement": 0.8}}), {"judge_model": "x"}) is None


def test_a_disagreeing_judge_model_is_exit_2_naming_the_dotted_key_and_both_values():
    exc = _error(lambda: judges_mod.conflicts_with_meta(_cfg({"helpful": dict(SPEC)}), {"judge_model": "other@1"}))
    assert exc.code == EXIT_USAGE == 2, exc
    assert _dotted(exc) == ["judges.helpful.model"], _dotted(exc)
    message = str(exc)
    assert "judges.helpful.model" in message and SPEC["model"] in message and "other@1" in message, message
    assert "must never override the config pin" in message, message


def test_a_disagreeing_prompt_version_is_exit_2_naming_prompt_version():
    exc = _error(lambda: judges_mod.conflicts_with_meta(_cfg({"helpful": dict(SPEC)}), {"judge_prompt_version": "9"}))
    assert exc.code == 2 and _dotted(exc) == ["judges.helpful.prompt_version"], _dotted(exc)
    assert SPEC["prompt_version"] in str(exc) and "9" in str(exc), str(exc)


def test_a_non_string_identity_value_equals_nothing_and_is_a_conflict():
    for value in ("", 1, None, ["gpt-4o"]):
        exc = _error(
            lambda value=value: judges_mod.conflicts_with_meta(_cfg({"helpful": dict(SPEC)}), {"judge_model": value})
        )
        assert exc.code == 2 and _dotted(exc) == ["judges.helpful.model"], (value, str(exc))


def test_specs_are_checked_in_sorted_name_order_so_the_first_one_fires():
    rules = {
        "zeta": {**SPEC, "model": "zeta-model"},
        "alpha": {**SPEC, "model": "alpha-model"},
    }
    exc = _error(lambda: judges_mod.conflicts_with_meta(_cfg(rules), {"judge_model": "nope"}))
    assert _dotted(exc) == ["judges.alpha.model"], _dotted(exc)


def test_plan_eval_and_calibrate_reject_the_conflict_while_the_listing_stays_green():
    root = proj.project(proj.BASE_CONFIG + DISAGREEING_SPEC)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        for args in (
            ("plan", "--dataset", "smoke", "--stage", "dev"),
            ("eval", "--dataset", "smoke", "--stage", "dev"),
            ("calibrate", "--dataset", "judge_calibration"),
        ):
            code, envelope = proj.run_cli(root, *args)
            assert code == 2 == envelope["code"], (args, code, envelope)
            message = envelope["errors"][0]["message"]
            assert "judges.correctness.model" in message, (args, message)
            assert "fake-judge@1" in message and "fake-judge-2024-11-20" in message, (args, message)
            assert envelope["data"] == {}, (args, envelope["data"])

        # B3's listing exemption: `evalops judges` never consults `judge_meta()`
        code, envelope = proj.run_cli(root, "judges")
        assert code == 0 == envelope["code"], envelope
        assert [entry["name"] for entry in envelope["data"]["judges"]] == ["correctness"], envelope
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (C3/C) resolved model + prompt version per verdict
# ---------------------------------------------------------------------------
def _eval_rows(root: pathlib.Path, run_id: str) -> tuple[list[RowResult], dict[str, str]]:
    """The rows artifact and the run params of one run, read back from the SQLITE ledger."""
    con = sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))
    try:
        body = con.execute(
            "SELECT body FROM evalops_artifacts WHERE run_id=? AND name=?", (run_id, "evalops/rows.jsonl")
        ).fetchone()[0]
        params = dict(con.execute("SELECT k, v FROM evalops_kv WHERE kind='param' AND run_id=?", (run_id,)))
    finally:
        con.close()
    return run_mod.rows_from_artifact(body), params


def test_every_verdict_carries_its_judges_resolved_model_and_prompt_version():
    root = proj.project(proj.BASE_CONFIG + AGREEING_SPEC)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        with fake_modules({"litellm": fake_litellm()}):
            code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0 == envelope["code"], envelope
        run_id = envelope["data"]["run_ids"][0]
        rows, params = _eval_rows(root, run_id)

        scored = [row for row in rows if "correctness" in (row.scores or {})]
        assert scored, "the declared spec produced no verdict to check"
        identity = {"model": "fake-judge@1", "prompt_version": "1"}
        assert all(row.judge_models and row.judge_models["correctness"] == identity for row in scored), [
            row.judge_models for row in rows
        ]

        # C5: the identity is recorded on the run too (one additive key pair per declared spec)
        assert params["judge.correctness.model"] == "fake-judge@1", params
        assert params["judge.correctness.prompt_version"] == "1", params
        # the app's existing `judge.*` metadata is untouched beside it
        assert params["judge.judge_model"] == "fake-judge@1", params

        # C4: rows.jsonl carries the field and reads back; a v0 line without it reads fine
        con = sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))
        try:
            body = con.execute(
                "SELECT body FROM evalops_artifacts WHERE run_id=? AND name=?", (run_id, "evalops/rows.jsonl")
            ).fetchone()[0]
        finally:
            con.close()
        assert "judge_models" in body, body[:400]
        v0 = {"row_id": "00000", "inputs": {"q": "x"}, "scores": {"correctness": 1.0}, "schema_version": 1}
        assert run_mod.rows_from_artifact(json.dumps(v0))[0].judge_models is None
    finally:
        _clean(root)


def test_a_declared_spec_score_without_identity_is_the_judge_named_exit_5_contract_violation():
    spec = judges_mod.JudgeSpec.from_rule("correctness", dict(SPEC))
    row = RowResult("00000", {"q": "x"}, scores={"correctness": 1.0})
    exc = _error(lambda: run_mod._require_judge_identities([row], {"correctness": spec}))
    assert exc.code == EXIT_RUNTIME == 5, exc
    assert "correctness" in str(exc) and "resolved model" in str(exc), str(exc)
    # a row with no declared-spec score is not a violation
    innocent = RowResult("00001", {}, scores={"facts": 1.0})
    assert run_mod._require_judge_identities([innocent], {"correctness": spec}) is None


# ---------------------------------------------------------------------------
# (C4/D) calibration as a gate input
# ---------------------------------------------------------------------------
CALIBRATED_JUDGE = "\njudges:\n  helpful: {min_agreement: 0.8, min_kappa: 0.5, max_mae: 0.2, model: fake-judge@1}\n"
CALIBRATION_TAGS = {"evalops.kind": "calibration"}


def _calibration_project() -> tuple[pathlib.Path, Any]:
    return _ledger_project(CALIBRATED_JUDGE)


def _add_calibration(module: Any, run_id: str, *, start_ms: int, tag: str = "passed", **metrics: float) -> None:
    proj.add_run(
        module,
        run_id,
        tags={**CALIBRATION_TAGS, "evalops.calibration": tag},
        metrics={f"judge.helpful.{key}": value for key, value in metrics.items()},
        start_ms=start_ms,
    )


def test_passing_calibration_evidence_passes_the_gate():
    root, module = _calibration_project()
    try:
        _add_calibration(module, "cal-1", start_ms=10, agreement=0.9, kappa=0.6, mae=0.1)
        code, envelope = _gate(root)
        assert code == 0 == envelope["code"], envelope
        checks = _judge_checks(envelope, "judge calibration:")
        assert len(checks) == 3, checks
        assert all(check["check"] == "judge calibration: helpful" for check in checks), checks
        assert all(check["passed"] is True for check in checks), checks
        assert {check["required"] for check in checks} == {0.8, 0.5, 0.2}, checks
        assert {check["calibration_run"] for check in checks} == {"cal-1"}, checks
    finally:
        _clean(root)


def test_failing_calibration_evidence_fails_the_gate_with_measured_required_and_run():
    for label, supplied, tag in (
        ("a violated agreement limit", {"agreement": 0.1, "kappa": 0.6, "mae": 0.1}, "passed"),
        ("a failed calibration tag", {"agreement": 0.9, "kappa": 0.6, "mae": 0.1}, "failed"),
        ("a missing metric", {"kappa": 0.6, "mae": 0.1}, "passed"),
    ):
        root, module = _calibration_project()
        try:
            _add_calibration(module, "cal-1", start_ms=10, tag=tag, **supplied)
            code, envelope = _gate(root)
            assert code == 1 == envelope["code"], (label, code, envelope)
            failing = [c for c in _judge_checks(envelope, "judge calibration:") if c["passed"] is False]
            assert failing, (label, envelope["data"]["checks"])
            for check in failing:
                assert check["check"] == "judge calibration: helpful", (label, check)
                assert set(check) >= {"candidate", "required", "calibration_run"}, (label, check)
                assert check["calibration_run"] == "cal-1", (label, check)
            if label == "a violated agreement limit":
                expected = {
                    "check": "judge calibration: helpful",
                    "passed": False,
                    "candidate": 0.1,
                    "required": 0.8,
                    "calibration_run": "cal-1",
                }
                assert failing == [expected], failing
            if label == "a missing metric":
                assert any(check["candidate"] is None and check["required"] == 0.8 for check in failing), failing
        finally:
            _clean(root)


def test_no_calibration_evidence_emits_no_check_and_the_gate_is_unchanged():
    """STATE compromise 14: enforcement consumes failing evidence, never a missing dataset."""
    root, _module = _calibration_project()
    try:
        code, envelope = _gate(root)
        assert code == 0 == envelope["code"], envelope
        assert _judge_checks(envelope, "judge calibration:") == [], envelope["data"]["checks"]
        assert _judge_checks(envelope, "judge pin:") == [], envelope["data"]["checks"]
    finally:
        _clean(root)


def test_the_latest_calibration_run_wins_by_start_ms_then_run_id():
    root, module = _calibration_project()
    try:
        # two runs at the same start_ms: the run_id tiebreak must pick `cal-z` (failing), not `cal-a`
        _add_calibration(module, "cal-a", start_ms=10, agreement=0.9, kappa=0.6, mae=0.1)
        _add_calibration(module, "cal-z", start_ms=10, agreement=0.1, kappa=0.6, mae=0.1)
        code, envelope = _gate(root)
        assert code == 1 == envelope["code"], envelope
        failing = [c for c in _judge_checks(envelope, "judge calibration:") if c["passed"] is False]
        assert failing and all(check["calibration_run"] == "cal-z" for check in failing), failing

        # a newer run wins by start_ms regardless of its run_id
        _add_calibration(module, "cal-new", start_ms=20, agreement=0.9, kappa=0.6, mae=0.1)
        code, envelope = _gate(root)
        assert code == 0 == envelope["code"], envelope
        assert {check["calibration_run"] for check in _judge_checks(envelope, "judge calibration:")} == {"cal-new"}
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (C5/E) the exhaustive worklist and its warning line
# ---------------------------------------------------------------------------
def test_payload_unpinned_is_the_exhaustive_sorted_worklist():
    rules = {
        "threshold_only": {"min_agreement": 0.85},
        "model_only_unpinned": {"model": "gpt-latest"},
        "model_only_pinned": {"model": "gpt-4o-2024-11-20"},
        "declared": dict(SPEC),
        "empty": {},
    }
    payload = judges_mod.payload(_cfg(rules))
    assert payload["unpinned"] == ["empty", "model_only_unpinned", "threshold_only"], payload
    assert [entry["name"] for entry in payload["judges"]] == ["declared"], payload


def test_write_human_names_the_pin_fix_for_every_unpinned_rule():
    import io

    data = judges_mod.payload(_cfg({"helpful": {"min_agreement": 0.8}, "traced": {"model": "gpt-latest"}}))
    stream = io.StringIO()
    judges_mod.write_human(data, stream)
    lines = [line for line in stream.getvalue().splitlines() if line.startswith("warning:")]
    assert lines == [
        "warning: judges.helpful is unpinned; gate refuses it (exit 1) — set judges.helpful.model to a concrete model",
        "warning: judges.traced is unpinned; gate refuses it (exit 1) — set judges.traced.model to a concrete model",
    ], lines


def test_the_judges_golden_pins_a_model_only_unpinned_rule_and_never_the_pinned_one():
    golden = proj.read_json(GOLDENS / "judges.json")
    assert golden["data"]["unpinned"] == ["aliased", "helpful"], golden["data"]["unpinned"]
    assert [entry["name"] for entry in golden["data"]["judges"]] == ["correctness"], golden
    # a model-only rule with a concrete pin declares no spec and needs no refusal: it appears nowhere
    assert "traced" not in json.dumps(golden["data"]), "a pinned model-only rule must not join the worklist"
