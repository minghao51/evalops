"""WP2.5: a below-threshold judge calibration fails `pr`/`main` end-to-end, and says what to fix.

Falsifies, in order: (B1) a real (not ``--dry-run``) ``pipeline pr`` with a calibration that cannot
meet its configured threshold stops at ``calibrate`` with exit 1 -- no plan/eval/gate, no eval run
row, no promoted alias; (B2) ``main`` stops the same way; (B3) a passing calibration lets the stage
proceed past the step; (B4) a missing calibration dataset is still a *skip*, never a failure; (H6)
``errors[0].message`` names the judge, metric, required limit and measured value, derived from
``calibrate``'s ``data.checks`` payload, while gate failure messages keep their pre-WP2.5 text.

Every test is a zero-argument function (the standalone runner calls them too) and offline: a temp git
project with a local bare ``origin``, the SQLite ledger, and the fixture app's ``helpful`` scorer.
"""

import json
import pathlib
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

SMOKE = (proj.REPO / "datasets" / "smoke.jsonl").read_text()
GOLDEN = SMOKE

#: `fake_app.helpful` says "no" iff the output contains "don't know" -- so these labels DISAGREE
#: with the scorer (agreement 0.0, kappa -1.0) and no positive threshold can pass.
DISAGREEING = "".join(
    json.dumps(record) + "\n"
    for record in (
        {"inputs": {"question": "q1"}, "outputs": "I don't know.", "expectations": {"human_labels": {"helpful": 1}}},
        {
            "inputs": {"question": "q2"},
            "outputs": "Refunds are only available within 30 days.",
            "expectations": {"human_labels": {"helpful": 0}},
        },
    )
)
#: The same two rows with labels that agree (agreement 1.0, kappa 1.0).
AGREEING = "".join(
    json.dumps(record) + "\n"
    for record in (
        {"inputs": {"question": "q1"}, "outputs": "I don't know.", "expectations": {"human_labels": {"helpful": 0}}},
        {
            "inputs": {"question": "q2"},
            "outputs": "Refunds are only available within 30 days.",
            "expectations": {"human_labels": {"helpful": 1}},
        },
    )
)

FAILING_AGREEMENT = "\njudges:\n  helpful: {min_agreement: 0.99}\n"
FAILING_KAPPA = "\njudges:\n  helpful: {min_kappa: 0.9}\n"
PASSING = "\njudges:\n  helpful: {min_agreement: 0.8}\n"
#: A gate that cannot pass: the smoke run's `facts/mean` is below 1.5.
TIGHT_GATE = '\ngates:\n  metrics:\n    "facts/mean": {min: 1.5}\n'
MESSAGE_PREFIX = "pipeline {stage} step '{step}' failed with exit code {code}"


def _config(extra: str = "", datasets=("smoke", "golden_regression")) -> str:
    block = "{" + ", ".join(f"{name}: {{}}" for name in datasets) + "}"
    base = proj.BASE_CONFIG.replace("datasets: {smoke: {}}", f"datasets: {block}")
    return base + extra


def _project(*, judges: str = "", calibration: str | None = DISAGREEING, gates: str = "") -> pathlib.Path:
    """A pipeline project: threshold config, an optional calibration dataset, optional gates."""
    datasets = ["smoke", "golden_regression"]
    files = {"golden_regression": GOLDEN}
    if calibration is not None:
        datasets.append("judge_calibration")
        files["judge_calibration"] = calibration
    extra = judges + gates
    return proj.pipeline_project(_config(extra, datasets=tuple(datasets)), datasets=files)


def _synced(root: pathlib.Path) -> None:
    code, out = proj.run_cli(root, "sync", "all")
    assert code == 0, out
    proj.commit(root, "sync")


def _steps(envelope: dict) -> list[dict]:
    return envelope["data"]["steps"]


def _names(envelope: dict) -> list[str]:
    return [step["name"] for step in _steps(envelope)]


def _step(envelope: dict, name: str) -> dict:
    return next(step for step in _steps(envelope) if step["name"] == name)


def _run_kinds(root: pathlib.Path) -> list[str]:
    """Every ledger run's `evalops.kind` tag, so a test can see whether an eval row was written."""
    db = root / ".evalops" / "ledger.sqlite"
    if not db.exists():
        return []
    con = sqlite3.connect(str(db))
    try:
        rows = con.execute("SELECT v FROM evalops_kv WHERE kind='tag' AND k='evalops.kind' ORDER BY v").fetchall()
    except sqlite3.OperationalError:  # a ledger file with no table yet: no runs
        return []
    finally:
        con.close()
    return [str(value) for (value,) in rows]


def _alias_state(root: pathlib.Path) -> str | None:
    path = root / ".evalops" / "store" / "prompts" / "support_system" / "aliases.json"
    return path.read_text() if path.exists() else None


def _nested(root: pathlib.Path, command: str) -> dict:
    """The logged nested envelope of one step (`.evalops/out/ci_log.jsonl`, one line per step)."""
    log = (root / ".evalops" / "out" / "ci_log.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in log]
    return next(entry for entry in entries if entry["command"] == command)


def _clean(*paths: pathlib.Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


# ---------------------------------------------------------------------------
# (B1/H2/H6) pr: a below-threshold calibration fails the stage and stops it
# ---------------------------------------------------------------------------
def test_pr_below_threshold_calibration_exits_1_and_stops_before_plan():
    root = _project(judges=FAILING_AGREEMENT)
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        head = proj.git_out(root, "rev-parse", "HEAD")
        assert _run_kinds(root) == [], "the fixture starts with no eval run"

        code, envelope = proj.run_cli(root, "pipeline", "pr")

        # exit discipline: the process code is the envelope's code is 1
        assert code == 1 == envelope["code"], envelope
        assert envelope["ok"] is False and envelope["command"] == "pipeline"
        assert envelope["data"]["stage"] == "pr" and envelope["data"]["dry_run"] is False

        # the failing step is `calibrate`, and it is last: plan/eval/gate never ran
        names = _names(envelope)
        assert names == ["guard", "sync", "commit_state", "calibrate"], names
        calibrate = _steps(envelope)[-1]
        assert calibrate["name"] == "calibrate" and calibrate["code"] == 1 and calibrate["ok"] is False, calibrate
        assert "plan" not in names and "eval" not in names and "gate" not in names
        assert not (root / ".evalops" / "out" / "gate_report.md").exists()
        assert not (root / ".evalops" / "out" / "last_eval.json").exists()

        # no eval run row: the only run the stage wrote is the calibration run
        assert _run_kinds(root) == ["calibration"], _run_kinds(root)

        # nothing promoted and nothing committed: HEAD and the alias file are untouched
        assert proj.git_out(root, "rev-parse", "HEAD") == head
        assert proj.git_out(root, "status", "--porcelain") == ""
        assert _alias_state(root) is None
        assert "evidence_group" not in envelope["data"], "no group was reached"

        # H6: the message names judge, metric, required limit and measured value -- from the payload
        message = envelope["errors"][0]["message"]
        prefix = MESSAGE_PREFIX.format(stage="pr", step="calibrate", code=1)
        assert message.startswith(prefix + ": "), message
        assert message.endswith("; failing checks: helpful: agreement >= 0.99 (measured 0.0)"), message
        logged = _nested(root, "calibrate")["data"]["checks"]
        assert [c for c in logged if c["passed"] is False] == [
            {"check": "helpful: agreement >= 0.99", "passed": False, "candidate": 0.0}
        ], logged
    finally:
        _clean(root, origin)


def test_calibration_failure_message_is_built_from_the_payload_not_from_names():
    """A different configured threshold yields a different clause: nothing is hard-coded."""
    root = _project(judges=FAILING_KAPPA)
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 1 == envelope["code"], envelope
        message = envelope["errors"][0]["message"]
        assert message.endswith("; failing checks: helpful: kappa >= 0.9 (measured -1.0)"), message
        check = _nested(root, "calibrate")["data"]["checks"][0]
        assert check == {"check": "helpful: kappa >= 0.9", "passed": False, "candidate": -1.0}, check
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (B2/H3) main is pinned too, so the two tables cannot drift apart silently
# ---------------------------------------------------------------------------
def test_main_below_threshold_calibration_exits_1_at_calibrate():
    root = _project(judges=FAILING_AGREEMENT)
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        head = proj.git_out(root, "rev-parse", "HEAD")

        code, envelope = proj.run_cli(root, "pipeline", "main")

        assert code == 1 == envelope["code"], envelope
        assert envelope["data"]["stage"] == "main"
        assert _names(envelope) == ["guard", "sync", "calibrate"], _names(envelope)
        calibrate = _steps(envelope)[-1]
        assert calibrate["name"] == "calibrate" and calibrate["code"] == 1 and calibrate["ok"] is False, calibrate
        assert not (root / ".evalops" / "out" / "last_eval.json").exists(), "no eval -> no hand-off"
        assert _run_kinds(root) == ["calibration"], _run_kinds(root)
        assert proj.git_out(root, "rev-parse", "HEAD") == head
        assert _alias_state(root) is None
        assert "evidence_group" not in envelope["data"]
        prefix = MESSAGE_PREFIX.format(stage="main", step="calibrate", code=1)
        assert envelope["errors"][0]["message"].startswith(prefix + ": "), envelope["errors"][0]["message"]
        assert "failing checks: helpful: agreement >= 0.99" in envelope["errors"][0]["message"]
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (B3/H4) a passing calibration lets the stage proceed
# ---------------------------------------------------------------------------
def test_pr_passing_calibration_proceeds_past_the_step():
    root = _project(judges=PASSING, calibration=AGREEING)
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 0 == envelope["code"], envelope
        calibrate = _step(envelope, "calibrate")
        assert calibrate["code"] == 0 and calibrate["ok"] is True, calibrate
        # the stage ran to the end, so calibrate cannot have stopped it
        assert _names(envelope) == ["guard", "sync", "commit_state", "calibrate", "plan", "eval", "gate"]
        assert _step(envelope, "gate")["ok"] is True, envelope
        assert (root / ".evalops" / "out" / "calibration_report.md").exists()
        assert "failing checks:" not in json.dumps(envelope["data"])
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (B4/H5) absence is still a skip -- the decision, pinned
# ---------------------------------------------------------------------------
def test_missing_calibration_dataset_is_a_skip_not_a_failure():
    """Decision (WP2.5/B4): enforcement triggers on a calibration run that *fails*, never on a
    missing dataset. Making absence fatal would break every existing `pr` pipeline (most projects
    ship no `judges:`/`judge_calibration` dataset) and is not in the map row for phase 2 WP 2.5.
    """
    root = _project(judges=FAILING_AGREEMENT, calibration=None)
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        assert not (root / "datasets" / "judge_calibration.jsonl").exists()

        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 0 == envelope["code"], envelope
        calibrate = _step(envelope, "calibrate")
        assert calibrate.get("skipped") is True, calibrate
        assert calibrate["kind"] == "skip" and "reason" in calibrate, calibrate
        assert "datasets/judge_calibration.jsonl not found" in str(calibrate["reason"]), calibrate
        assert "code" not in calibrate and "ok" not in calibrate, "a skip is not a failure"
        assert not (root / ".evalops" / "out" / "calibration_report.md").exists(), "nothing calibrated"
        # no calibration run row either: the step never executed
        assert _run_kinds(root) == ["eval"], _run_kinds(root)
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (A3/H6) gate failure messages are byte-identical to the pre-WP2.5 text
# ---------------------------------------------------------------------------
def test_gate_failure_message_is_unchanged_and_gains_no_check_clause():
    """`gate` returns `checks` with the *same* entry shape as `calibrate`; its message must not
    gain the clause (A3). The nested gate envelope really does carry failing checks, so this is not
    vacuous: the discriminator has to work on a payload that looks like a calibration payload.
    """
    root = _project(judges=PASSING, calibration=AGREEING, gates=TIGHT_GATE)
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 1 == envelope["code"], envelope
        assert _names(envelope)[-1] == "gate" and _step(envelope, "gate")["code"] == 1
        message = envelope["errors"][0]["message"]
        # exactly: prefix, the nested reason, nothing appended
        assert re.fullmatch(
            r"pipeline pr step 'gate' failed with exit code 1: gate FAILED for group [0-9a-f]+", message
        ), message
        assert "failing checks:" not in message
        assert "measured" not in message
        failing = [c for c in _nested(root, "gate")["data"]["checks"] if c["passed"] is False]
        assert failing and all(isinstance(c["check"], str) for c in failing), failing
        assert failing[0]["candidate"] is not None, "a gate check carries a candidate too"
        assert _step(envelope, "calibrate")["ok"] is True, "calibration itself passed here"
    finally:
        _clean(root, origin)
