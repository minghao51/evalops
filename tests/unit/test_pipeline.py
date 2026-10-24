"""WP2.1: `evalops pipeline <stage>` — the CI stage table in Python, bash shim, dry-run.

Falsifies, in order: (A) one envelope on stdout and the process code equals its code; (B) the
normative stage table in order with the exact flags, conditional calibrate, the main evidence-group
hand-off, release's required ``EVIDENCE_GROUP``, and nightly's ``data.baseline.git_sha`` (never the
bash legacy top-level lookup); (C) ``--dry-run`` lists every real step (placeholders, conditional
``run: false``) and writes nothing; (D) ``commit_state`` parity (no-op, exit 4 without a branch,
commit+push to a local bare origin, exit 5 on a git failure); (E) exit codes 1/3/4 propagate from a
failed step, stop the stage and keep ``data.steps``; (F) idempotence (a second ``pr``/``main`` adds
no VCS commit), main -> release promotion, nightly checkout/restore, the ``ci_log.jsonl`` log and the
bash shim's parity.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
All tests are offline: a temp git project, the SQLite ledger, and a **local bare** repo as `origin`.
"""

import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

SMOKE = (proj.REPO / "datasets" / "smoke.jsonl").read_text()
GOLDEN = SMOKE
COMMIT_STATE_MESSAGE = "chore(evalops): update versioned state [skip ci]"

#: Pre-recorded outputs + human labels: `fake_app.helpful` says no to "I don't know" and yes
#: otherwise, so the labels below agree perfectly (agreement 1.0, kappa 1.0).
CALIBRATION = "".join(
    json.dumps(record) + "\n"
    for record in (
        {"inputs": {"question": "q1"}, "outputs": "I don't know.", "expectations": {"human_labels": {"helpful": 0}}},
        {
            "inputs": {"question": "q2"},
            "outputs": "Refunds are only available within 30 days.",
            "expectations": {"human_labels": {"helpful": 1}},
        },
        {
            "inputs": {"question": "q3"},
            "outputs": "Use the Forgot password link, then check your email.",
            "expectations": {"human_labels": {"helpful": 1}},
        },
        {"inputs": {"question": "q4"}, "outputs": "I don't know.", "expectations": {"human_labels": {"helpful": 0}}},
    )
)
JUDGES = "\njudges:\n  helpful: {min_agreement: 0.8, min_kappa: 0.5}\n"
#: A drift gate that can only fail: the control run must *improve* `facts/mean` by 1.0.
TIGHT_DRIFT = '\ngates:\n  metrics:\n    "facts/mean": {max_drop: -1}\n'
TIGHT_GATE = '\ngates:\n  metrics:\n    "facts/mean": {min: 1.5}\n'


def _config(datasets=("smoke", "golden_regression"), extra: str = "") -> str:
    block = "{" + ", ".join(f"{name}: {{}}" for name in datasets) + "}"
    return proj.BASE_CONFIG.replace("datasets: {smoke: {}}", f"datasets: {block}") + extra


def _golden_project(config: str | None = None, files: dict | None = None) -> pathlib.Path:
    return proj.pipeline_project(config or _config(), datasets={"golden_regression": GOLDEN}, files=files)


def _synced(root: pathlib.Path) -> pathlib.Path:
    code, out = proj.run_cli(root, "sync", "all")
    assert code == 0, out
    proj.commit(root, "sync")
    return root


def _clean(*paths: pathlib.Path) -> None:
    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def _steps(envelope: dict) -> list[dict]:
    return envelope["data"]["steps"]


def _names(envelope: dict) -> list[str]:
    return [step["name"] for step in _steps(envelope)]


def _table(envelope: dict) -> list[tuple]:
    return [(step["name"], step["argv"], step["run"]) for step in _steps(envelope)]


def _executed(envelope: dict) -> list[dict]:
    return [step for step in _steps(envelope) if not step.get("skipped")]


def _rev_count(root: pathlib.Path) -> str:
    return proj.git_out(root, "rev-list", "--count", "HEAD")


def _released_baseline(root: pathlib.Path) -> str:
    """sync -> main -> release, then return the pinned baseline ``git_sha`` (needs a bare origin)."""
    _synced(root)
    code, main = proj.run_cli(root, "pipeline", "main")
    assert code == 0, main
    code, release = proj.run_cli(
        root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": main["data"]["evidence_group"]}
    )
    assert code == 0, release
    sha = proj.run_cli(root, "baseline")[1]["data"]["baseline"]["git_sha"]
    assert sha, "the release pinned a baseline with a git sha"
    return str(sha)


def _tamper_baseline_git_sha(root: pathlib.Path, sha: str) -> None:
    """Point the pinned baseline run's ``git_sha`` param at a commit that does not exist."""
    con = sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))
    try:
        cursor = con.execute(
            "UPDATE evalops_kv SET v=:sha WHERE kind='param' AND k='git_sha' AND run_id IN "
            "(SELECT run_id FROM evalops_kv WHERE kind='tag' AND k='evalops.baseline' AND v='true')",
            {"sha": sha},
        )
        assert cursor.rowcount == 1, "exactly one baseline run carries the pinned git_sha"
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------------------
# (A/C) dry-run: the whole stage table, placeholders, no side effects
# ---------------------------------------------------------------------------
def test_dry_run_lists_every_stage_and_writes_nothing():
    root = proj.pipeline_project(_config(), datasets={"golden_regression": GOLDEN})
    try:
        head = proj.git_out(root, "rev-parse", "HEAD")
        assert not (root / ".evalops").exists(), "the fixture starts with no runs"

        code, pr = proj.run_cli(root, "pipeline", "pr", "--dry-run")
        assert code == 0 and pr["code"] == 0, pr
        assert pr["data"]["dry_run"] is True and pr["data"]["stage"] == "pr"
        assert _table(pr) == [
            ("guard", ["guard"], True),
            ("sync", ["sync", "all"], True),
            ("commit_state", None, True),
            ("calibrate", ["calibrate", "--dataset", "judge_calibration"], False),
            ("plan", ["plan", "--dataset", "smoke", "--stage", "pr"], True),
            ("eval", ["eval", "--dataset", "smoke", "--stage", "pr"], True),
            ("gate", ["gate"], True),
        ]
        assert [step["kind"] for step in _steps(pr)] == [
            "evalops",
            "evalops",
            "vcs",
            "skip",
            "evalops",
            "evalops",
            "evalops",
        ]
        skipped = _steps(pr)[3]
        assert skipped["reason"] and "judge_calibration" in skipped["reason"]
        assert _steps(pr)[2]["command"].startswith("git add -A .evalops/locks .evalops/store")

        code, main = proj.run_cli(root, "pipeline", "--dry-run", "main")
        assert code == 0 and main["code"] == 0, main
        assert _table(main) == [
            ("guard", ["guard"], True),
            ("sync", ["sync", "all", "--check"], True),
            ("calibrate", ["calibrate", "--dataset", "judge_calibration"], False),
            ("plan", ["plan", "--dataset", "golden_regression", "--stage", "main"], True),
            ("eval", ["eval", "--dataset", "golden_regression", "--stage", "main"], True),
            ("gate", ["gate"], True),
            ("promote", ["promote", "--alias", "candidate", "--evidence-group", "<group from last_eval.json>"], True),
            ("commit_state", None, True),
        ]
        assert "HEAD:main" in _steps(main)[-1]["command"]

        code, release = proj.run_cli(root, "pipeline", "release", "--dry-run")
        assert code == 0 and release["code"] == 0, release
        assert _table(release) == [
            ("promote", ["promote", "--alias", "production", "--evidence-group", "<EVIDENCE_GROUP>"], True),
            ("commit_state", None, True),
        ]

        code, nightly = proj.run_cli(root, "pipeline", "nightly", "--dry-run")
        assert code == 0 and nightly["code"] == 0, nightly
        assert _table(nightly) == [
            ("baseline", ["baseline"], True),
            ("checkout", ["git", "-c", "advice.detachedHead=false", "checkout", "-q", "<baseline git_sha>"], True),
            ("eval", ["eval", "--dataset", "golden_regression", "--stage", "control"], True),
            ("gate", ["gate", "--label", "drift"], True),
            ("restore", ["git", "checkout", "-q", "<original ref>"], True),
        ]
        for envelope in (pr, main, release, nightly):
            for step in _steps(envelope):
                assert {"name", "kind", "command", "argv", "run"} <= set(step), step
                assert step["kind"] in {"evalops", "vcs", "skip"}, step
                assert step["command"], step
                if not step["run"]:
                    assert step["reason"] and step["kind"] == "skip", step

        # no side effect: no run directory, no log, no commit, no lock/store change
        assert not (root / ".evalops").exists()
        assert proj.git_out(root, "status", "--porcelain") == ""
        assert proj.git_out(root, "rev-parse", "HEAD") == head

        # an unknown or missing stage is a usage error (exit 2) that names the stage
        bad_code, bad = proj.run_cli(root, "pipeline", "bogus")
        assert bad_code == 2 == bad["code"] and "bogus" in bad["errors"][0]["message"], bad
        assert proj.run_cli(root, "pipeline")[0] == 2
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (B) conditional calibrate
# ---------------------------------------------------------------------------
def test_dry_run_calibrate_follows_the_dataset_file():
    root = proj.pipeline_project(_config(), datasets={"golden_regression": GOLDEN})
    try:
        code, absent = proj.run_cli(root, "pipeline", "pr", "--dry-run")
        assert code == 0, absent
        step = next(s for s in _steps(absent) if s["name"] == "calibrate")
        assert step["run"] is False and step["kind"] == "skip"
        assert "datasets/judge_calibration.jsonl not found" in step["reason"]

        (root / "datasets" / "judge_calibration.jsonl").write_text(CALIBRATION)
        code, present = proj.run_cli(root, "pipeline", "pr", "--dry-run")
        assert code == 0, present
        step = next(s for s in _steps(present) if s["name"] == "calibrate")
        assert step["run"] is True and step["kind"] == "evalops" and "reason" not in step
        assert step["argv"] == ["calibrate", "--dataset", "judge_calibration"]

        code, main = proj.run_cli(root, "pipeline", "main", "--dry-run")
        assert code == 0, main
        assert next(s for s in _steps(main) if s["name"] == "calibrate")["run"] is True
    finally:
        _clean(root)


def test_pr_runs_calibrate_when_the_dataset_is_present():
    root = proj.pipeline_project(
        _config(datasets=("smoke", "golden_regression", "judge_calibration"), extra=JUDGES),
        datasets={"golden_regression": GOLDEN, "judge_calibration": CALIBRATION},
    )
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 0 and envelope["code"] == 0, envelope
        step = next(s for s in _steps(envelope) if s["name"] == "calibrate")
        assert step["code"] == 0 and step["ok"] is True, step
        assert (root / ".evalops" / "out" / "calibration_report.md").exists()
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (B/F) pr happy path, log and idempotence
# ---------------------------------------------------------------------------
def test_pr_happy_path_logs_one_envelope_per_step_and_needs_no_commit():
    root = _golden_project()
    try:
        _synced(root)
        before = _rev_count(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 0 and envelope["code"] == 0, envelope
        assert envelope["data"]["dry_run"] is False and envelope["data"]["stage"] == "pr"
        for step in _executed(envelope):
            assert step["code"] == 0 and step["ok"] is True, step
        assert [step["name"] for step in _steps(envelope) if step.get("skipped")] == ["calibrate"]
        assert (root / ".evalops" / "out" / "gate_report.md").exists()
        assert _rev_count(root) == before, "an already-synced tree is a commit_state no-op"

        log = (root / ".evalops" / "out" / "ci_log.jsonl").read_text().splitlines()
        logged_evalops = [step["name"] for step in _steps(envelope) if step["kind"] == "evalops"]
        assert len(log) == len(logged_evalops) == 5, (len(log), logged_evalops)
        logged = [json.loads(line) for line in log]
        assert [entry["command"] for entry in logged] == ["guard", "sync", "plan", "eval", "gate"]
        assert all(entry["code"] == 0 for entry in logged)
    finally:
        _clean(root)


def test_pr_twice_adds_no_git_commit():
    root = _golden_project()
    try:
        _synced(root)
        before = _rev_count(root)
        first, first_out = proj.run_cli(root, "pipeline", "pr")
        second, second_out = proj.run_cli(root, "pipeline", "pr")
        assert first == 0 and second == 0, (first_out, second_out)
        assert _rev_count(root) == before
        assert next(s for s in _steps(second_out) if s["name"] == "commit_state")["committed"] is False
        assert proj.run_cli(root, "sync", "all", "--check")[0] == 0
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (E) exit codes propagate; the stage stops; data.steps survives
# ---------------------------------------------------------------------------
def test_gate_failure_propagates_exit_1_and_stops_before_promote():
    root = _golden_project(_config(extra=TIGHT_GATE))
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "main")
        assert code == 1 == envelope["code"], envelope
        assert envelope["ok"] is False and envelope["command"] == "pipeline"
        assert _names(envelope)[-1] == "gate"
        assert "promote" not in _names(envelope) and "commit_state" not in _names(envelope)
        assert _executed(envelope)[-1]["code"] == 1
        assert "gate" in envelope["errors"][0]["message"]
    finally:
        _clean(root)


def test_budget_failure_propagates_exit_3():
    root = _golden_project(_config().replace("per_run_usd: 5", "per_run_usd: 0.000001"))
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 3 == envelope["code"], envelope
        assert _names(envelope)[-1] == "eval"
        assert _executed(envelope)[-1]["code"] == 3
        assert "eval" in envelope["errors"][0]["message"]
    finally:
        _clean(root)


def test_guard_failure_propagates_exit_4():
    sneaky = "from langfuse import get_client\nget_client().create_prompt(name='x', prompt='y')\n"
    root = _golden_project(files={"sneaky.py": sneaky})
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 4 == envelope["code"], envelope
        assert _names(envelope) == ["guard"]
        assert _steps(envelope)[0]["code"] == 4
        assert "guard" in envelope["errors"][0]["message"]
    finally:
        _clean(root)


def test_capability_error_propagates_exit_6():
    config = _config(extra="\nenvironments:\n  broken: {plugins: {ledger: git}}\n")
    root = _golden_project(config)
    try:
        code, envelope = proj.run_cli(root, "pipeline", "nightly", env_extra={"EVALOPS_ENV": "broken"})
        assert code == 6 == envelope["code"], envelope
        assert _names(envelope) == ["baseline"]
        assert _steps(envelope)[0]["code"] == 6
        assert "does not support" in envelope["errors"][0]["message"]
    finally:
        _clean(root)


def test_contract_mismatch_propagates_exit_7():
    root = _golden_project()
    try:
        locks = root / ".evalops" / "locks" / "local"
        locks.mkdir(parents=True)
        (locks / "datasets.lock.json").write_text(json.dumps({"schema_version": 2, "smoke": {}}))
        code, envelope = proj.run_cli(root, "pipeline", "pr")
        assert code == 7 == envelope["code"], envelope
        assert _names(envelope) == ["guard"]
        assert _steps(envelope)[0]["code"] == 7
        assert "schema_version" in envelope["errors"][0]["message"]
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (D) commit_state: exit 4 / commit+push / no-op
# ---------------------------------------------------------------------------
def test_commit_state_no_branch_is_exit_4_then_commits_and_pushes():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        before = _rev_count(root)
        code, no_branch = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_BRANCH": None})
        assert code == 4 == no_branch["code"], no_branch
        assert _names(no_branch)[-1] == "commit_state"
        assert _steps(no_branch)[-1]["code"] == 4 and _steps(no_branch)[-1]["ok"] is False
        assert (
            "evalops state changed but EVALOPS_BRANCH is unset; commit .evalops/locks and .evalops/store"
            in no_branch["errors"][0]["message"]
        )
        assert _rev_count(root) == before, "the guard runs before any commit"

        code, pushed = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_BRANCH": "main"})
        assert code == 0 and pushed["code"] == 0, pushed
        step = next(s for s in _steps(pushed) if s["name"] == "commit_state")
        assert step["committed"] is True and step["code"] == 0
        head = proj.git_out(root, "rev-parse", "HEAD")
        assert proj.git_out(root, "log", "-1", "--pretty=%s") == COMMIT_STATE_MESSAGE
        assert proj.git_out(origin, "rev-parse", "main") == head, "the state commit reached the bare origin"
        assert proj.git_out(root, "status", "--porcelain") == ""

        after = _rev_count(root)
        code, again = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_BRANCH": "main"})
        assert code == 0, again
        assert next(s for s in _steps(again) if s["name"] == "commit_state")["committed"] is False
        assert _rev_count(root) == after, "a clean state is a no-op"
    finally:
        _clean(root, origin)


def test_commit_state_git_push_failure_is_exit_5_with_git_stderr():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        proj.git(root, "remote", "set-url", "origin", str(origin / "missing.git"))
        code, envelope = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_BRANCH": "main"})
        assert code == 5 == envelope["code"], envelope
        assert _names(envelope)[-1] == "commit_state"
        message = envelope["errors"][0]["message"]
        assert "git push failed" in message or "git commit failed" in message
        assert "Traceback" not in message
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (B/H5) main -> release
# ---------------------------------------------------------------------------
def test_main_promotes_candidate_and_release_moves_production():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        code, main = proj.run_cli(root, "pipeline", "main")
        assert code == 0 and main["code"] == 0, main
        group = main["data"]["evidence_group"]
        assert group
        aliases = root / ".evalops" / "store" / "prompts" / "support_system" / "aliases.json"
        assert json.loads(aliases.read_text()) == {"candidate": "1"}
        assert next(s for s in _steps(main) if s["name"] == "commit_state")["committed"] is True
        after_first = _rev_count(root)

        code, again = proj.run_cli(root, "pipeline", "main")
        assert code == 0, again
        assert again["data"]["evidence_group"] != group, "a new run group each time"
        assert _rev_count(root) == after_first, "second main: same alias version, no VCS commit"
        assert next(s for s in _steps(again) if s["name"] == "commit_state")["committed"] is False

        code, release = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": group})
        assert code == 0 and release["code"] == 0, release
        assert release["data"]["evidence_group"] == group
        assert json.loads(aliases.read_text()) == {"candidate": "1", "production": "1"}
        assert next(s for s in _steps(release) if s["name"] == "commit_state")["committed"] is True
        baseline = proj.run_cli(root, "baseline")[1]
        assert baseline["data"]["baseline"]["group"] == group
        assert baseline["data"]["baseline"]["app_version"]

        code, missing = proj.run_cli(root, "pipeline", "release")
        assert code == 2 == missing["code"], missing
        assert missing["data"]["steps"] == []
        assert "EVIDENCE_GROUP" in missing["errors"][0]["message"]
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (B/H5) nightly: no baseline skips; with baseline it checks out, controls, restores
# ---------------------------------------------------------------------------
def test_nightly_without_a_baseline_skips_the_control_run():
    root = _golden_project()
    try:
        code, envelope = proj.run_cli(root, "pipeline", "nightly")
        assert code == 0 == envelope["code"], envelope
        assert _names(envelope) == ["baseline", "control"]
        assert _steps(envelope)[0]["code"] == 0
        control = _steps(envelope)[1]
        assert control["skipped"] is True and control["kind"] == "skip"
        assert "baseline git_sha" in control["reason"]
        assert not (root / ".evalops" / "out" / "last_eval.json").exists(), "no control eval ran"
        written = json.loads((root / ".evalops" / "out" / "baseline.json").read_text())
        assert written["command"] == "baseline" and written["data"]["baseline"] is None
    finally:
        _clean(root)


def test_nightly_uses_data_baseline_git_sha_and_restores_the_ref_on_failure():
    root = _golden_project(_config(extra=TIGHT_DRIFT))
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        branch = proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD")
        code, main = proj.run_cli(root, "pipeline", "main")
        assert code == 0, main
        assert proj.run_cli(root, "baseline")[1]["data"]["baseline"] is None, "main promotes candidate only"
        code, release = proj.run_cli(
            root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": main["data"]["evidence_group"]}
        )
        assert code == 0, release
        baseline = proj.run_cli(root, "baseline")[1]["data"]["baseline"]
        sha = baseline["git_sha"]
        assert sha, "the release pinned a baseline with a git sha"
        before = proj.git_out(root, "rev-parse", "HEAD")

        code, nightly = proj.run_cli(root, "pipeline", "nightly")
        assert code == 1 == nightly["code"], nightly
        assert _names(nightly) == ["baseline", "checkout", "eval", "gate", "restore"]
        assert nightly["data"]["baseline_git_sha"] == sha, "read from data.baseline, not the legacy top level"
        checkout = _steps(nightly)[1]
        assert checkout["code"] == 0 and checkout["argv"][-1] == sha
        assert _steps(nightly)[-2]["name"] == "gate" and _steps(nightly)[-2]["code"] == 1
        assert _steps(nightly)[-1]["name"] == "restore" and _steps(nightly)[-1]["code"] == 0
        assert "gate" in nightly["errors"][0]["message"]

        # restored in a `finally` even though the gate failed: same branch, same commit as before
        assert proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD") == branch
        assert proj.git_out(root, "rev-parse", "HEAD") == before
    finally:
        _clean(root, origin)


def test_nightly_success_restore_really_moves_head_back_and_records_it():
    """(B/H5) success path: the in-plan restore executes git and reads HEAD back from the repo.

    The failure-path test above is satisfied by the `finally` drain, so it cannot see a `restore`
    step that reports success without running git. This one can: the restore step's `details` are
    captured from `git symbolic-ref`/`git rev-parse` *after* the git call, so a skipped restore
    leaves HEAD detached on the baseline sha instead of on the original branch.
    """
    root = _golden_project()  # default gates: the drift diagnostics never fail the gate
    origin = proj.bare_origin(root)
    try:
        _released_baseline(root)
        branch = proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD")
        before = proj.git_out(root, "rev-parse", "HEAD")

        code, nightly = proj.run_cli(root, "pipeline", "nightly")
        assert code == 0 == nightly["code"], nightly
        assert _names(nightly) == ["baseline", "checkout", "eval", "gate", "restore"]
        assert [step["code"] for step in _steps(nightly)] == [0, 0, 0, 0, 0]

        restore = _steps(nightly)[-1]
        assert restore["argv"][-1] == branch, "the plan restores the original branch, not the sha"
        assert restore["details"] == {"ref": branch, "head": before}, "post-restore ref/HEAD read from git"

        # the repo itself is back where it started, not just the step's report
        assert proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD") == branch
        assert proj.git_out(root, "rev-parse", "HEAD") == before
    finally:
        _clean(root, origin)


def test_nightly_unresolvable_baseline_sha_is_exit_5_with_the_steps_in_the_envelope():
    """(A/E) a git failure (exit 128) inside a step is exit 5: one envelope, `data.steps`, no traceback."""
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        pinned = _released_baseline(root)
        branch = proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD")
        missing = "0123456789abcdef0123456789abcdef01234567"
        assert missing != pinned
        _tamper_baseline_git_sha(root, missing)

        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "--json", "pipeline", "nightly"],
            cwd=str(root),
            env=proj.env(),
            capture_output=True,
            text=True,
        )
        assert "Traceback" not in proc.stderr, proc.stderr
        envelope = proj.one_document(proc.stdout)  # exit 128 must not cost the envelope
        assert envelope["code"] == 5 and proc.returncode == envelope["code"], (proc.returncode, envelope)
        assert envelope["command"] == "pipeline" and envelope["ok"] is False
        assert envelope["data"]["baseline_git_sha"] == missing
        steps = _steps(envelope)
        assert [step["name"] for step in steps] == ["baseline", "checkout"]
        checkout = steps[-1]
        assert checkout["code"] == 5 and checkout["ok"] is False
        assert checkout["argv"][-1] == missing
        assert "nightly" in envelope["errors"][0]["message"] and "checkout" in envelope["errors"][0]["message"]
        # the failed checkout never moved HEAD: the branch is still checked out
        assert proj.git_out(root, "symbolic-ref", "-q", "--short", "HEAD") == branch
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (A/F) stdout contract, hook prints, bash shim
# ---------------------------------------------------------------------------
def test_json_stdout_is_one_envelope_and_hook_prints_go_to_stderr():
    noisy = proj.fixture_text("fake_app.py") + (
        "\n_orig_behavior_config = behavior_config\n\n\n"
        "def behavior_config():\n"
        "    print('HOOK-NOISE-MARKER')\n"
        "    return _orig_behavior_config()\n"
    )
    root = _golden_project(_config().replace("fake_app:", "noisy_app:"), files={"noisy_app.py": noisy})
    try:
        _synced(root)
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "--json", "pipeline", "pr"],
            cwd=str(root),
            env=proj.env(),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        envelope = proj.one_document(proc.stdout)  # exactly one JSON document, nothing else
        assert envelope["command"] == "pipeline" and envelope["code"] == 0
        assert "HOOK-NOISE-MARKER" in proc.stderr, "a hook print is a diagnostic on stderr"
        assert "HOOK-NOISE-MARKER" not in proc.stdout
        log = (root / ".evalops" / "out" / "ci_log.jsonl").read_text().splitlines()
        assert len(log) == 5 and all(json.loads(line)["command"] != "pipeline" for line in log)
        assert "HOOK-NOISE-MARKER" not in "".join(log)
    finally:
        _clean(root)


def test_nested_steps_load_the_same_config_file_the_pipeline_was_given():
    root = _golden_project()
    try:
        (root / "custom.yaml").write_text((root / "evalops.yaml").read_text())
        (root / "evalops.yaml").unlink()
        proj.commit(root, "custom config name")
        code, envelope = proj.run_cli(root, "--config", "custom.yaml", "pipeline", "nightly")
        assert code == 0 and envelope["code"] == 0, envelope
        assert _names(envelope) == ["baseline", "control"]
        assert _steps(envelope)[0]["code"] == 0, "the nested baseline step found custom.yaml"
    finally:
        _clean(root)


def test_bash_shim_delegates_to_the_pipeline_command():
    shim = proj.REPO / "ci" / "pipeline.sh"
    root = proj.pipeline_project(_config(), datasets={"golden_regression": GOLDEN})
    try:
        venv_bin = str(proj.REPO / ".venv" / "bin")
        env = {**proj.env(), "PATH": f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"}
        proc = subprocess.run(
            ["bash", str(shim), "main", "--dry-run", "--json"], cwd=str(root), env=env, capture_output=True, text=True
        )
        assert proc.returncode == 0, proc.stderr
        shimmed = proj.one_document(proc.stdout)

        in_process_code, in_process = proj.run_cli_in_process(root, "pipeline", "main", "--dry-run")
        assert in_process_code == 0, in_process
        assert shimmed["data"]["steps"] == in_process["data"]["steps"]

        # the global flag before the sub-command works through the shim too
        leading = subprocess.run(
            ["bash", str(shim), "--json", "pr", "--dry-run"], cwd=str(root), env=env, capture_output=True, text=True
        )
        assert leading.returncode == 0, leading.stderr
        assert proj.one_document(leading.stdout)["data"]["stage"] == "pr"

        help_run = subprocess.run(["bash", str(shim), "--help"], cwd=str(root), env=env, capture_output=True)
        assert help_run.returncode == 0
        assert subprocess.run(["bash", str(shim), "bogus"], cwd=str(root), env=env, capture_output=True).returncode == 2
        assert subprocess.run(["bash", str(shim)], cwd=str(root), env=env, capture_output=True).returncode == 2
        assert not (root / ".evalops").exists(), "the shim inherits the CLI's no-write dry-run"

        # human mode prints one short line per step (and no JSON document)
        human = subprocess.run(
            ["bash", str(shim), "pr", "--dry-run"], cwd=str(root), env=env, capture_output=True, text=True
        )
        assert human.returncode == 0, human.stderr
        assert "[pr] guard: evalops guard" in human.stdout, human.stdout
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (A) EVALOPS_ACTOR default
# ---------------------------------------------------------------------------
def test_pipeline_defaults_the_actor_to_ci_and_keeps_an_explicit_one():
    root = _golden_project()
    try:
        _synced(root)
        code, out = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_ACTOR": None})
        assert code == 0, out
        db = root / ".evalops" / "ledger.sqlite"
        con = sqlite3.connect(str(db))
        try:
            actors = {row[0] for row in con.execute("SELECT v FROM evalops_kv WHERE kind='tag' AND k='evalops.actor'")}
        finally:
            con.close()
        assert actors == {"ci"}, actors

        code, out = proj.run_cli(root, "pipeline", "pr", env_extra={"EVALOPS_ACTOR": "agent:wptest"})
        assert code == 0, out
        con = sqlite3.connect(str(db))
        try:
            actors = {row[0] for row in con.execute("SELECT v FROM evalops_kv WHERE kind='tag' AND k='evalops.actor'")}
        finally:
            con.close()
        assert "agent:wptest" in actors and "test" not in actors, actors
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (A) a data-carrying EvalOpsError cannot escape the exit matrix (defence in depth)
# ---------------------------------------------------------------------------
def test_error_envelope_normalizes_a_data_carrying_error_outside_the_matrix():
    """`_StageError`/any data-carrying error must still produce one in-matrix envelope.

    Fix (a) maps git failures to exit 5 at the source; this pins fix (b), so a future
    data-carrying error with a raw code (git 128) cannot turn into a ValueError and empty stdout.
    """
    from evalops import cli
    from evalops.util import EvalOpsError

    class Args:
        cmd = "pipeline"

    class Cfg:
        env = "local"

    steps = [{"name": "checkout", "code": 5, "ok": False}]
    exc = EvalOpsError("git failed: bad ref", 128)
    exc.data = {"stage": "nightly", "steps": steps}
    envelope = cli._error_envelope(Args(), Cfg(), exc)
    assert envelope["ok"] is False and envelope["code"] == 5, envelope
    assert envelope["command"] == "pipeline" and envelope["env"] == "local", envelope
    assert envelope["data"]["steps"] == steps, envelope
    assert envelope["errors"][0]["code"] == 5, envelope
