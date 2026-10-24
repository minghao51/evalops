"""WP2.2: release re-verification, evidence-group->version binding, fork-PR bot branch.

Falsifies, in order: (H1/A1/A3) a green release runs ``guard`` -> ``gate --group`` -> ``baseline`` ->
``promote`` -> ``commit_state`` in that order, logs one envelope per evalops step and writes no
``baseline.json``; (H2/A2) a group whose stored evidence says ``gate: passed`` but whose metrics fail
today's rules stops the stage at the ``gate`` step with exit 1, before ``promote`` touches an alias;
(H3/A1) per-step exit propagation -- a guard violation is exit 4 before the gate, an unknown group is
exit 2 at the gate step, and neither reaches ``promote``; (H4/B1/B2) a green release tags every run
of the group ``evalops.released.production``/``.app_version`` (params unchanged, tags additive) and a
second release of the same group is exit 4 naming the group and the already-released version, while
``candidate`` promotes stay re-runnable and write no released tags; (H5/C1-C3) ``EVALOPS_PR`` routes
``commit_state`` to the force-pushed bot branch ``evalops/state/pr-<N>`` without ``EVALOPS_BRANCH``,
never to the protected source branch, and ``--dry-run`` renders it with a ``<EVALOPS_PR>``
placeholder.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
All tests are offline: a temp git project, the SQLite ledger, and a **local bare** repo as `origin`.
"""

import json
import pathlib
import shutil
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

GOLDEN = (proj.REPO / "datasets" / "smoke.jsonl").read_text()
#: A gate that can only fail: the fake app's `facts/mean` never reaches 1.5.
TIGHT_GATE = '\ngates:\n  metrics:\n    "facts/mean": {min: 1.5}\n'
BOT_BRANCH = "evalops/state/pr-42"


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


def _aliases_path(root: pathlib.Path) -> pathlib.Path:
    return root / ".evalops" / "store" / "prompts" / "support_system" / "aliases.json"


def _ledger(root: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))


def _run_ids(root: pathlib.Path, group: str) -> list[str]:
    con = _ledger(root)
    try:
        rows = con.execute(
            "SELECT DISTINCT run_id FROM evalops_kv WHERE kind='tag' AND k='evalops.group' AND v=?", (group,)
        ).fetchall()
    finally:
        con.close()
    return [str(row[0]) for row in rows]


def _kv_map(root: pathlib.Path, run_id: str, kind: str) -> dict[str, str]:
    con = _ledger(root)
    try:
        rows = con.execute("SELECT k, v FROM evalops_kv WHERE run_id=? AND kind=?", (run_id, kind)).fetchall()
    finally:
        con.close()
    return {str(k): str(v) for k, v in rows}


def _log_lines(root: pathlib.Path) -> list[str]:
    return (root / ".evalops" / "out" / "ci_log.jsonl").read_text().splitlines()


def _main_group(root: pathlib.Path) -> str:
    """sync -> main (green), returning the main-stage evidence group; nothing released yet."""
    _synced(root)
    code, main = proj.run_cli(root, "pipeline", "main")
    assert code == 0 and main["code"] == 0, main
    return str(main["data"]["evidence_group"])


# ---------------------------------------------------------------------------
# (H1/A1/A3) a green release re-verifies, then promotes
# ---------------------------------------------------------------------------
def test_green_release_reverifies_then_promotes_and_logs_one_envelope_per_step():
    root, origin = _golden_project(), None
    origin = proj.bare_origin(root)
    try:
        group = _main_group(root)
        before = len(_log_lines(root))
        code, release = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": group})
        assert code == 0 and release["code"] == 0, release
        assert _names(release) == ["guard", "gate", "baseline", "promote", "commit_state"], release
        assert _steps(release)[1]["argv"] == ["gate", "--group", group]
        assert _steps(release)[2]["name"] == "baseline" and _steps(release)[2]["kind"] == "evalops"
        assert _steps(release)[3]["argv"] == ["promote", "--alias", "production", "--evidence-group", group]
        assert [step["code"] for step in _steps(release)] == [0, 0, 0, 0, 0], release
        assert release["data"]["evidence_group"] == group
        # every nested evalops step lands in ci_log.jsonl, in stage order; git actions do not
        logged = [json.loads(line)["command"] for line in _log_lines(root)[before:]]
        assert logged == ["guard", "gate", "baseline", "promote"], logged
        assert not (root / ".evalops" / "out" / "baseline.json").exists(), "A3: baseline.json is nightly-only"
        assert json.loads(_aliases_path(root).read_text()) == {"candidate": "1", "production": "1"}
        assert proj.run_cli(root, "baseline")[1]["data"]["baseline"]["group"] == group
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (H2/A2) the gate is a fresh re-check: a stale `gate: passed` tag cannot release
# ---------------------------------------------------------------------------
def test_release_stops_at_a_fresh_gate_failure_before_promote():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        group = _main_group(root)
        run_id = _run_ids(root, group)[0]
        assert _kv_map(root, run_id, "tag")["evalops.gate"] == "passed", "the stored tag says passed"
        aliases = _aliases_path(root).read_bytes()
        before = len(_log_lines(root))

        # Tighten the rules *after* the evidence passed: only a fresh re-check can catch this.
        (root / "evalops.yaml").write_text(_config(extra=TIGHT_GATE))
        proj.commit(root, "tighten the gate")

        code, envelope = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": group})
        assert code == 1 == envelope["code"], envelope
        assert envelope["ok"] is False and envelope["command"] == "pipeline"
        assert _names(envelope) == ["guard", "gate"], "the stage stops at the gate, before baseline/promote"
        failed = _steps(envelope)[-1]
        assert failed["name"] == "gate" and failed["code"] == 1 and failed["ok"] is False, failed
        message = envelope["errors"][0]["message"]
        assert "pipeline release step 'gate' failed with exit code 1" in message, message
        assert _aliases_path(root).read_bytes() == aliases, "promote never ran: the production alias is untouched"
        assert proj.run_cli(root, "baseline")[1]["data"]["baseline"] is None, "the baseline never moved"
        logged = [json.loads(line)["command"] for line in _log_lines(root)[before:]]
        assert logged == ["guard", "gate"], logged
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (H3/A1) per-step exit propagation: guard 4, unknown group 2, both before promote
# ---------------------------------------------------------------------------
def test_release_guard_violation_is_exit_4_before_the_gate_step():
    sneaky = "from langfuse import get_client\nget_client().create_prompt(name='x', prompt='y')\n"
    root = _golden_project(files={"sneaky.py": sneaky})
    try:
        code, envelope = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": "g-any"})
        assert code == 4 == envelope["code"], envelope
        assert _names(envelope) == ["guard"], envelope
        assert _steps(envelope)[0]["code"] == 4
        assert "promote" not in _names(envelope) and "gate" not in _names(envelope)
    finally:
        _clean(root)


def test_release_unknown_group_is_exit_2_at_the_gate_step():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        _synced(root)
        code, envelope = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": "g-does-not-exist"})
        assert code == 2 == envelope["code"], envelope
        assert _names(envelope) == ["guard", "gate"], envelope
        assert _steps(envelope)[-1]["argv"] == ["gate", "--group", "g-does-not-exist"]
        assert "no runs for group g-does-not-exist" in envelope["errors"][0]["message"]
        assert "promote" not in _names(envelope)
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (H4/B1/B2) binding + stale refusal
# ---------------------------------------------------------------------------
def test_release_binds_the_group_to_the_version_and_refuses_a_released_group():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        group = _main_group(root)
        params_before = {run_id: _kv_map(root, run_id, "param") for run_id in _run_ids(root, group)}
        version = params_before[_run_ids(root, group)[0]]["app_version"]
        assert version, "the main stage recorded an app_version"

        code, release = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": group})
        assert code == 0 and release["code"] == 0, release

        for run_id in _run_ids(root, group):
            tags = _kv_map(root, run_id, "tag")
            assert tags["evalops.released.production"] == "true", tags
            assert tags["evalops.released.app_version"] == version, tags
            # additive: promote wrote tags only, never a param (no fingerprint/app_version change)
            assert _kv_map(root, run_id, "param") == params_before[run_id]

        aliases = _aliases_path(root).read_bytes()
        baseline_before = proj.run_cli(root, "baseline")[1]["data"]["baseline"]

        # B2: the same group can never be released twice -- exit 4 through the front door
        code, again = proj.run_cli(root, "pipeline", "release", env_extra={"EVIDENCE_GROUP": group})
        assert code == 4 == again["code"], again
        assert _names(again) == ["guard", "gate", "baseline", "promote"], again
        message = again["errors"][0]["message"]
        assert "pipeline release step 'promote' failed with exit code 4" in message, message
        assert group in message and version in message, message
        assert "fresh main-stage evidence group is required" in message, message
        assert _aliases_path(root).read_bytes() == aliases, "a refused re-release moves no alias"
        assert proj.run_cli(root, "baseline")[1]["data"]["baseline"] == baseline_before, "and no baseline"
    finally:
        _clean(root, origin)


def test_candidate_promotes_stay_re_runnable_and_write_no_released_tags():
    root = _golden_project()
    origin = proj.bare_origin(root)
    try:
        group = _main_group(root)
        for _attempt in range(2):
            code, out = proj.run_cli(root, "promote", "--alias", "candidate", "--evidence-group", group)
            assert code == 0 and out["code"] == 0, out
        for run_id in _run_ids(root, group):
            tags = _kv_map(root, run_id, "tag")
            assert "evalops.released.production" not in tags, tags
            assert "evalops.released.app_version" not in tags, tags
        assert json.loads(_aliases_path(root).read_text()) == {"candidate": "1"}
    finally:
        _clean(root, origin)


# ---------------------------------------------------------------------------
# (H5/C1-C3) fork-PR state goes to the bot branch, never the source branch
# ---------------------------------------------------------------------------
def test_fork_pr_pushes_state_to_a_bot_branch_without_EVALOPS_BRANCH():
    root = _golden_project()
    origin = proj.bare_origin(root)
    env_extra = {"EVALOPS_PR": "42", "EVALOPS_BRANCH": None}
    try:
        code, first = proj.run_cli(root, "pipeline", "pr", env_extra=env_extra)
        assert code == 0 and first["code"] == 0, first
        step = next(step for step in _steps(first) if step["name"] == "commit_state")
        assert step["committed"] is True and step["code"] == 0, step
        assert step["bot_branch"] == BOT_BRANCH, step
        first_sha = proj.git_out(origin, "rev-parse", f"refs/heads/{BOT_BRANCH}")
        assert first_sha == proj.git_out(root, "rev-parse", "HEAD")
        assert proj.git_out(origin, "for-each-ref", "--format=%(refname)") == f"refs/heads/{BOT_BRANCH}", (
            "a fork PR never pushes the protected source branch"
        )

        # A diverging second run still lands: the bot branch is derived state, force-pushed.
        proj.git(root, "reset", "--hard", "HEAD~1")
        prompt = root / "prompts" / "support_system.txt"
        prompt.write_text(prompt.read_text() + "\n# fork-PR tweak\n")
        proj.commit(root, "prompt tweak")
        code, second = proj.run_cli(root, "pipeline", "pr", env_extra=env_extra)
        assert code == 0 and second["code"] == 0, second
        again = next(step for step in _steps(second) if step["name"] == "commit_state")
        assert again["committed"] is True and again["bot_branch"] == BOT_BRANCH, again
        second_sha = proj.git_out(origin, "rev-parse", f"refs/heads/{BOT_BRANCH}")
        assert second_sha != first_sha, "the branch moved"
        assert second_sha == proj.git_out(root, "rev-parse", "HEAD")
        ancestor = subprocess.run(
            ["git", "merge-base", "--is-ancestor", first_sha, second_sha], cwd=str(root), capture_output=True
        )
        assert ancestor.returncode == 1, "the second state commit diverged: only --force can push it"
    finally:
        _clean(root, origin)


def test_pr_dry_run_renders_the_bot_branch_push_when_EVALOPS_PR_is_set():
    root = _golden_project()
    try:
        head = proj.git_out(root, "rev-parse", "HEAD")
        code, dry = proj.run_cli(
            root, "pipeline", "pr", "--dry-run", env_extra={"EVALOPS_PR": "7", "EVALOPS_BRANCH": None}
        )
        assert code == 0 and dry["code"] == 0, dry
        step = next(step for step in _steps(dry) if step["name"] == "commit_state")
        assert step["kind"] == "vcs" and step["run"] is True
        assert "git push --force origin HEAD:refs/heads/evalops/state/pr-<EVALOPS_PR>" in step["command"], step

        # without EVALOPS_PR the dry-run push is byte-identical to WP2.1
        code, plain = proj.run_cli(root, "pipeline", "pr", "--dry-run", env_extra={"EVALOPS_BRANCH": None})
        assert code == 0, plain
        plain_step = next(step for step in _steps(plain) if step["name"] == "commit_state")
        assert "git push origin HEAD:<EVALOPS_BRANCH>" in plain_step["command"], plain_step

        assert not (root / ".evalops").exists(), "the dry run writes nothing"
        assert proj.git_out(root, "rev-parse", "HEAD") == head
    finally:
        _clean(root)
