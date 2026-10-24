"""Declarative CI stages: ``evalops pipeline <pr|main|release|nightly>``.

``ci/pipeline.sh`` used to own this logic; it is now a shim that ``exec``s this command, so the
stage table is data a human can read and a test can assert instead of bash. Nested evalops commands
run **in-process** through the CLI dispatch/capture helpers (no console script on PATH, no child
``evalops``), and only the git actions are subprocesses.

Stages (normative, same order and flags as the bash script):

  pr      guard -> sync all -> commit_state -> [calibrate] -> plan(smoke, pr) -> eval(smoke, pr) -> gate
  main    guard -> sync all --check -> [calibrate] -> plan(golden, main) -> eval(golden, main) -> gate
          -> read group from last_eval.json -> promote --alias candidate -> commit_state
  release require EVIDENCE_GROUP -> guard -> gate --group $EVIDENCE_GROUP -> baseline
          -> promote --alias production --evidence-group $EVIDENCE_GROUP -> commit_state
  nightly baseline -> [no baseline git_sha -> record the control step failed (exit 1)]
          -> [checkout baseline git_sha -> eval(golden, control) -> gate --label drift]
          -> restore the original ref in a ``finally``

``calibrate`` runs only when ``datasets/<CALIBRATION_DATASET>.jsonl`` exists. ``EVALOPS_BRANCH``
defaults to ``main`` for the ``main``/``release`` state commits (``pr`` has no default, exactly like
the bash ``:=`` behaviour), and ``EVALOPS_ACTOR`` defaults to ``ci`` before the first step.

``EVALOPS_PR`` routes the state commit to the bot branch ``evalops/state/pr-<N>`` (force-pushed:
derived state where the latest run of a PR wins) instead of a protected source branch, so a PR from a
fork can still publish its locks; neither ``EVALOPS_BRANCH`` nor its default is needed then. The
``release`` stage re-verifies ``guard`` + ``gate --group`` + ``baseline`` **now** before ``promote``
touches an alias: a stale ``EVIDENCE_GROUP`` is caught by the gate step (exit 1), and ``promote``
itself refuses a group it already released (exit 4). ``nightly`` fails loudly (exit 1, actionable
message) when the ``baseline`` step reports no production baseline.

Exit codes are never collapsed: a failed step propagates its own code (1 gate, 2 usage, 3 budget,
4 guard, 5 runtime, 6 capability, 7 contract) through an ``EvalOpsError`` that carries ``data``, so
the failure envelope keeps ``data.steps`` and names the stage and step in ``errors[0].message``. A
failed ``calibrate`` also names every failing check (judge, metric, required limit, measured value)
there, built from its ``data.checks`` payload; every other step's message is unchanged.

``--dry-run`` exits 0 and writes nothing (no file, no log, no git state): it lists exactly the
steps the real run would execute, with placeholders for values only a real run knows (``<group from
last_eval.json>``, ``<EVIDENCE_GROUP>``, ``<baseline git_sha>``, ``<original ref>``). Conditional
steps are listed with ``run: false`` and a ``reason``. The nightly ``restore`` step carries an extra
``details: {ref, head}`` read back from git *after* it ran, so a test can prove the restore executed
instead of trusting the step's own ``ok``.

Every nested evalops step's envelope is appended as one JSON line to ``.evalops/out/ci_log.jsonl``
(the bash ``ev ... | tee``); git actions are not envelopes and appear only in ``data.steps``. The
command's own ``--json`` stdout is exactly one envelope: nested output and hooks go to stderr.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Generator
from dataclasses import dataclass, field
from typing import Any

from .contract import formats
from .util import EXIT_GATE_FAILED, EXIT_GUARD, EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EvalOpsError

#: The stages this command accepts, in the order the CLI help lists them.
STAGES = ("pr", "main", "release", "nightly")

COMMIT_MESSAGE = "chore(evalops): update versioned state [skip ci]"
NO_BRANCH_MESSAGE = "evalops state changed but EVALOPS_BRANCH is unset; commit .evalops/locks and .evalops/store"
DEFAULT_BRANCH = "main"
DEFAULT_GIT_NAME = "evalops-bot"
DEFAULT_GIT_EMAIL = "evalops-bot@users.noreply.local"

GROUP_PLACEHOLDER = "<group from last_eval.json>"
EVIDENCE_PLACEHOLDER = "<EVIDENCE_GROUP>"
BASELINE_PLACEHOLDER = "<baseline git_sha>"
REF_PLACEHOLDER = "<original ref>"
BRANCH_PLACEHOLDER = "<EVALOPS_BRANCH>"
PR_PLACEHOLDER = "<EVALOPS_PR>"
BOT_BRANCH_PREFIX = "evalops/state/pr-"
NO_BASELINE_MESSAGE = (
    "no production baseline: establish one with `evalops pipeline release` (or "
    "`evalops promote --alias production`) and re-run `evalops pipeline nightly`"
)
NO_BASELINE_NOTE = (
    "without a production baseline the nightly stage fails loudly (exit 1) instead of running "
    "the control eval: establish one with `evalops pipeline release`"
)

EVALOPS = "evalops"
VCS = "vcs"
SKIP = "skip"


class _StageError(EvalOpsError):
    """A stage failure that carries ``data.steps`` into the failure envelope.

    Same pattern as ``cli._ReportError``: ``cli._error_envelope`` copies ``.data`` into the envelope,
    so a reviewer still sees every step that ran before the one that failed.
    """

    def __init__(self, message: str, code: int, data: dict[str, Any]):
        super().__init__(message, code)
        self.data = data


@dataclass
class _Step:
    """One stage action: a nested evalops command, a git action, or a skipped conditional."""

    name: str
    kind: str
    argv: list[str] | None = None
    run: bool = True
    reason: str | None = None
    command: str | None = None
    undo: _Step | None = None
    #: the nightly-only ``.evalops/out/baseline.json`` write (the release stage records only)
    writes_baseline: bool = False
    #: a step the stage records as failed without executing it (nightly: no baseline to compare)
    fail_code: int | None = None

    def text(self) -> str:
        if self.command:
            return self.command
        return f"{EVALOPS} {' '.join(self.argv or [])}".strip()


@dataclass
class _Context:
    """Per-run state: env-derived defaults plus values only a real run can resolve."""

    cfg: Any
    stage: str
    dry_run: bool
    smoke: str
    golden: str
    calibration: str
    evidence_group: str | None
    branch: str | None
    pr: str | None = None
    baseline_sha: str = ""
    pending_undo: list[_Step] = field(default_factory=list)
    error_detail: str = ""
    #: the failed nested step's ``data`` (``calibrate``'s failing checks are mined from it)
    error_data: dict[str, Any] = field(default_factory=dict)

    def calibration_present(self) -> bool:
        return (self.cfg.path("datasets") / f"{self.calibration}.jsonl").exists()


# ---------------------------------------------------------------------------
# stage table
# ---------------------------------------------------------------------------
def _ev(
    name: str, argv: list[str], *, run: bool = True, reason: str | None = None, writes_baseline: bool = False
) -> _Step:
    kind = EVALOPS if run else SKIP
    return _Step(name=name, kind=kind, argv=argv, run=run, reason=reason, writes_baseline=writes_baseline)


def _refused(name: str, argv: list[str], code: int, reason: str) -> _Step:
    """A step the stage records as failed without executing it (nightly without a baseline)."""
    return _Step(name=name, kind=EVALOPS, argv=argv, run=False, reason=reason, fail_code=code)


def _bot_branch(pr: str) -> str:
    """The fork-PR state branch: derived state, so the latest run of that PR wins."""
    return f"{BOT_BRANCH_PREFIX}{pr}"


def _vcs(name: str, argv: list[str] | None, *, command: str, undo: _Step | None = None) -> _Step:
    return _Step(name=name, kind=VCS, argv=argv, command=command, undo=undo)


def _calibrate_step(ctx: _Context) -> _Step:
    argv = ["calibrate", "--dataset", ctx.calibration]
    if ctx.calibration_present():
        return _ev("calibrate", argv)
    return _ev(
        "calibrate",
        argv,
        run=False,
        reason=f"datasets/{ctx.calibration}.jsonl not found",
    )


def _commit_state_step(ctx: _Context) -> _Step:
    """The bash ``commit_state``: stage state, no-op when clean, commit+push when a branch is known."""
    if ctx.pr:
        # A fork PR cannot push `.evalops/locks` to the protected source branch: publish the derived
        # state on a bot branch instead (the latest run of the PR wins, hence --force).
        pr = PR_PLACEHOLDER if ctx.dry_run else ctx.pr
        command = (
            f"git add -A .evalops/locks .evalops/store && git commit -m {COMMIT_MESSAGE!r} && "
            f"git push --force origin HEAD:refs/heads/{_bot_branch(pr)}"
        )
        return _vcs("commit_state", None, command=command)
    branch = ctx.branch or BRANCH_PLACEHOLDER
    command = (
        f"git add -A .evalops/locks .evalops/store && git commit -m {COMMIT_MESSAGE!r} && git push origin HEAD:{branch}"
    )
    return _vcs("commit_state", None, command=command)


def _plan_pr(ctx: _Context) -> Generator[_Step, None, None]:
    yield _ev("guard", ["guard"])
    yield _ev("sync", ["sync", "all"])
    yield _commit_state_step(ctx)
    yield _calibrate_step(ctx)
    yield _ev("plan", ["plan", "--dataset", ctx.smoke, "--stage", "pr"])
    yield _ev("eval", ["eval", "--dataset", ctx.smoke, "--stage", "pr"])
    yield _ev("gate", ["gate"])


def _plan_main(ctx: _Context) -> Generator[_Step, None, None]:
    yield _ev("guard", ["guard"])
    yield _ev("sync", ["sync", "all", "--check"])
    yield _calibrate_step(ctx)
    yield _ev("plan", ["plan", "--dataset", ctx.golden, "--stage", "main"])
    yield _ev("eval", ["eval", "--dataset", ctx.golden, "--stage", "main"])
    yield _ev("gate", ["gate"])
    group = GROUP_PLACEHOLDER if ctx.dry_run else _last_eval_group(ctx)
    if not ctx.dry_run:
        ctx.evidence_group = group
    yield _ev("promote", ["promote", "--alias", "candidate", "--evidence-group", group])
    yield _commit_state_step(ctx)


def _plan_release(ctx: _Context) -> Generator[_Step, None, None]:
    """Re-verify now (guard + a fresh gate + the current baseline) before moving ``production``.

    The gate step re-aggregates the group from the ledger and applies today's rules, so a stale
    ``EVIDENCE_GROUP`` (a tag that says "passed" but does not pass anymore) stops the stage here.
    """
    group = ctx.evidence_group or EVIDENCE_PLACEHOLDER
    yield _ev("guard", ["guard"])
    yield _ev("gate", ["gate", "--group", group])
    yield _ev("baseline", ["baseline"])
    yield _ev("promote", ["promote", "--alias", "production", "--evidence-group", group])
    yield _commit_state_step(ctx)


def _plan_nightly(ctx: _Context) -> Generator[_Step, None, None]:
    yield _ev("baseline", ["baseline"], writes_baseline=True)
    sha = BASELINE_PLACEHOLDER if ctx.dry_run else ctx.baseline_sha
    control = ["eval", "--dataset", ctx.golden, "--stage", "control"]
    if not ctx.dry_run and not sha:
        # WP2.2 (D1): a nightly run with no production baseline is a failure, not a skip.
        yield _refused("control", control, EXIT_GATE_FAILED, NO_BASELINE_MESSAGE)
        return
    original = REF_PLACEHOLDER if ctx.dry_run else _current_ref(ctx.cfg.root)
    checkout = ["git", "-c", "advice.detachedHead=false", "checkout", "-q", sha]
    restore = _vcs(
        "restore",
        ["git", "checkout", "-q", original],
        command=f"git checkout -q {original}",
    )
    yield _vcs("checkout", checkout, command=f"git checkout -q {sha}", undo=restore)
    yield _ev("eval", control, reason=NO_BASELINE_NOTE if ctx.dry_run else None)
    yield _ev("gate", ["gate", "--label", "drift"])
    yield restore


_PLANS = {
    "pr": _plan_pr,
    "main": _plan_main,
    "release": _plan_release,
    "nightly": _plan_nightly,
}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def run_pipeline(cfg: Any, stage: str, *, dry_run: bool = False, as_json: bool = True) -> dict[str, Any]:
    """Run one CI stage; returns the command's ``data`` or raises ``_StageError`` with ``data.steps``."""
    if stage not in STAGES:
        raise EvalOpsError(f"unknown stage {stage!r}; choose one of {STAGES}", EXIT_USAGE)
    branch_env = os.getenv("EVALOPS_BRANCH") or None
    ctx = _Context(
        cfg=cfg,
        stage=stage,
        dry_run=dry_run,
        smoke=os.getenv("SMOKE_DATASET") or "smoke",
        golden=os.getenv("GOLDEN_DATASET") or "golden_regression",
        calibration=os.getenv("CALIBRATION_DATASET") or "judge_calibration",
        evidence_group=os.getenv("EVIDENCE_GROUP") or None,
        branch=(branch_env or DEFAULT_BRANCH) if stage in ("main", "release") else branch_env,
        pr=(os.getenv("EVALOPS_PR") or "").strip() or None,
    )
    if stage == "release" and not dry_run and not ctx.evidence_group:
        # The bash `: "${EVIDENCE_GROUP:?...}"` check runs before any step: nothing executed.
        raise _StageError(
            "pipeline release requires EVIDENCE_GROUP (the main-stage evidence group id)",
            EXIT_USAGE,
            {"stage": stage, "dry_run": False, "steps": [], "summary": "release stage failed: EVIDENCE_GROUP is unset"},
        )
    if not dry_run:
        # The bash `export EVALOPS_ACTOR="${EVALOPS_ACTOR:-ci}"`: a real run tags its runs `ci`.
        os.environ.setdefault("EVALOPS_ACTOR", "ci")

    steps: list[dict[str, Any]] = []
    failure: EvalOpsError | None = None
    plan = _PLANS[stage](ctx)
    try:
        try:
            for step in plan:
                if dry_run:
                    steps.append(_planned_entry(step))
                    if not as_json:
                        print(f"[{stage}] {step.name}: {step.text()}")
                    continue
                if step.fail_code is not None:
                    entry = _refused_entry(step)
                    steps.append(entry)
                    if not as_json:
                        print(f"[{stage}] {step.name} ... exit {step.fail_code}")
                    # Same envelope shape as a failed nested step: stage + step + the nested reason.
                    ctx.error_detail = str(step.reason or "")
                    failure = _stage_error(ctx, steps, EvalOpsError(_step_detail(ctx, entry), step.fail_code))
                    break
                if not step.run:
                    steps.append(_skipped_entry(step))
                    if not as_json:
                        print(f"[{stage}] {step.name} ... skipped ({step.reason})")
                    continue
                try:
                    entry = _execute(ctx, step)
                except EvalOpsError as exc:  # commit_state without a branch, a git failure, ...
                    steps.append(_failed_entry(ctx, step, exc))
                    if not as_json:
                        print(f"[{stage}] {step.name} ... exit {exc.code}")
                    message = f"pipeline {stage} step '{step.name}' failed with exit code {exc.code}: {exc}"
                    failure = _stage_error(ctx, steps, EvalOpsError(message, exc.code))
                    break
                steps.append(entry)
                if not as_json:
                    print(f"[{stage}] {step.name} ... {'ok' if entry['ok'] else 'exit ' + str(entry['code'])}")
                if not entry["ok"]:
                    failure = _stage_error(ctx, steps, EvalOpsError(_step_detail(ctx, entry), int(entry["code"])))
                    break
        except EvalOpsError as exc:  # raised between steps (e.g. a v2 last_eval.json: exit 7)
            failure = _stage_error(ctx, steps, exc)
    finally:
        plan.close()
        if not dry_run and ctx.pending_undo:
            undo_failure = _drain_undo(ctx, steps)
            if failure is None:
                failure = undo_failure

    if dry_run:
        return {
            "stage": stage,
            "dry_run": True,
            "steps": steps,
            "summary": f"dry run: {len(steps)} step(s) for {stage}",
        }
    if failure is not None:
        raise failure
    data: dict[str, Any] = {
        "stage": stage,
        "dry_run": False,
        "steps": steps,
        "summary": f"{stage} stage passed: {len(steps)} step(s)",
    }
    if stage in ("main", "release") and ctx.evidence_group:
        data["evidence_group"] = ctx.evidence_group
        if not as_json:
            print(f"[{stage}] evidence group: {ctx.evidence_group}")
    if stage == "nightly" and ctx.baseline_sha:
        data["baseline_git_sha"] = ctx.baseline_sha
    return data


def _stage_error(ctx: _Context, steps: list[dict[str, Any]], exc: EvalOpsError) -> _StageError:
    data: dict[str, Any] = {
        "stage": ctx.stage,
        "dry_run": False,
        "steps": steps,
        "summary": f"{ctx.stage} stage failed: {exc}",
    }
    if ctx.evidence_group and ctx.stage in ("main", "release"):
        data["evidence_group"] = ctx.evidence_group
    if ctx.stage == "nightly" and ctx.baseline_sha:
        data["baseline_git_sha"] = ctx.baseline_sha
    return _StageError(str(exc), exc.code, data)


# ---------------------------------------------------------------------------
# step payloads
# ---------------------------------------------------------------------------
def _planned_entry(step: _Step) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": step.name,
        "kind": step.kind,
        "command": step.text(),
        "argv": step.argv,
        "run": step.run,
    }
    if not step.run:
        entry["reason"] = step.reason
    if step.reason and step.run:  # a note on the dry-run listing (the nightly no-baseline path)
        entry["reason"] = step.reason
    return entry


def _refused_entry(step: _Step) -> dict[str, Any]:
    """A step the stage refused to run: recorded as failed with its code and an actionable reason."""
    return {**_base_entry(step), "code": step.fail_code, "ok": False, "reason": step.reason}


def _skipped_entry(step: _Step) -> dict[str, Any]:
    return {
        "name": step.name,
        "kind": SKIP,
        "command": step.text(),
        "argv": step.argv,
        "skipped": True,
        "reason": step.reason,
    }


def _base_entry(step: _Step) -> dict[str, Any]:
    return {"name": step.name, "kind": step.kind, "command": step.text(), "argv": step.argv}


def _failed_entry(ctx: _Context, step: _Step, exc: EvalOpsError) -> dict[str, Any]:
    return {**_base_entry(step), "code": exc.code, "ok": False}


def _step_detail(ctx: _Context, entry: dict[str, Any]) -> str:
    """`errors[0].message` names the stage, the step and (when it has one) the nested reason.

    A failed calibration also lists the checks that failed, built from its payload: a CI log that
    only said "judge calibration FAILED" could not tell an operator which judge, metric, required
    limit or measured value to fix.
    """
    message = f"pipeline {ctx.stage} step '{entry['name']}' failed with exit code {entry['code']}"
    if ctx.error_detail:
        message = f"{message}: {ctx.error_detail}"
    failing = _failing_calibration_checks(ctx.error_data)
    return f"{message}; failing checks: {', '.join(failing)}" if failing else message


def _failing_calibration_checks(data: dict[str, Any]) -> list[str]:
    """`calibrate`'s failing `data.checks`, as ``<check> (measured <candidate>)``.

    Keyed on the payload shape, never on the step name: ``checks`` is a list of objects carrying a
    string ``check`` and ``passed is False``. ``gate``'s checks have the *same* entry shape, so the
    discriminator is ``data.judges`` -- ``calibrate`` reports per-judge stats next to its checks and
    no other command does. Without that discriminator every gate failure message would gain the
    clause too, and those messages stay byte-identical to the pre-WP2.5 pipeline.
    """
    if not isinstance(data.get("judges"), dict):
        return []
    checks = data.get("checks")
    if not isinstance(checks, list):
        return []
    clauses: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        text = check.get("check")
        if not isinstance(text, str) or check.get("passed") is not False:
            continue
        measured = check.get("candidate")
        clauses.append(text if measured is None else f"{text} (measured {measured})")
    return clauses


# ---------------------------------------------------------------------------
# executing one step
# ---------------------------------------------------------------------------
def _execute(ctx: _Context, step: _Step) -> dict[str, Any]:
    if step.kind == EVALOPS:
        return _execute_evalops(ctx, step)
    if step.kind == VCS:
        return _execute_vcs(ctx, step)
    raise EvalOpsError(f"unknown step kind {step.kind!r}", EXIT_RUNTIME)


def _execute_evalops(ctx: _Context, step: _Step) -> dict[str, Any]:
    nested = _run_nested(str(ctx.cfg.source) if ctx.cfg.source else None, step.argv or [])
    _append_log(ctx.cfg, nested)
    if step.name == "baseline" and step.writes_baseline:  # release records only: no file write
        _write_baseline(ctx, nested)
    entry = {**_base_entry(step), "code": int(nested["code"]), "ok": bool(nested["ok"])}
    if not entry["ok"]:
        errors = nested.get("errors") or [{}]
        ctx.error_detail = str(errors[0].get("message") or "")
        nested_data = nested.get("data")
        ctx.error_data = nested_data if isinstance(nested_data, dict) else {}
    return entry


def _execute_vcs(ctx: _Context, step: _Step) -> dict[str, Any]:
    entry = _base_entry(step)
    if step.name == "commit_state":
        committed = _commit_state(ctx.cfg, ctx.branch, ctx.pr)
        entry.update({"code": EXIT_OK, "ok": True, "committed": committed})
        if ctx.pr:
            entry["bot_branch"] = _bot_branch(ctx.pr)
        return entry
    code, _, stderr = _git(ctx.cfg.root, step.argv or [])
    if code != EXIT_OK:
        # git's own exit code (128 for a bad ref) is not in the evalops matrix: a git failure is
        # EXIT_RUNTIME, exactly like `_git_checked`, so the failure envelope can carry `data.steps`.
        detail = stderr.strip() or f"exit {code}"
        raise EvalOpsError(f"git failed: {detail}", EXIT_RUNTIME)
    entry.update({"code": code, "ok": True})
    if step.name == "restore":
        # Proof the restore really ran: the ref/HEAD **after** the git call, not the plan's argv.
        entry["details"] = _ref_state(ctx.cfg.root)
    if step.undo is not None:
        ctx.pending_undo.append(step.undo)
    if step.name == "restore" and ctx.pending_undo:
        ctx.pending_undo.pop()
    return entry


def _drain_undo(ctx: _Context, steps: list[dict[str, Any]]) -> _StageError | None:
    """Run every pending undo action (the nightly ref restore) even when the stage failed."""
    failure: _StageError | None = None
    while ctx.pending_undo:
        step = ctx.pending_undo.pop()
        code, _, stderr = _git(ctx.cfg.root, step.argv or [])
        entry = {**_base_entry(step), "code": code if code == EXIT_OK else EXIT_RUNTIME, "ok": code == EXIT_OK}
        if code == EXIT_OK:
            entry["details"] = _ref_state(ctx.cfg.root)
        steps.append(entry)
        if code != EXIT_OK and failure is None:
            failure = _stage_error(
                ctx,
                steps,
                EvalOpsError(f"git restore failed: {stderr.strip()}", EXIT_RUNTIME),
            )
    return failure


def _run_nested(source: str | None, argv: list[str]) -> dict[str, Any]:
    """Run one evalops command in-process, exactly as the CLI would run it.

    The config is reloaded per step from the same file the pipeline was given (``cli.main`` loads
    it per process), so the nightly control run sees the config of the checked-out baseline ref,
    not the ref the pipeline started on.
    """
    from . import cli
    from . import config as config_mod
    from .contract import redact

    nested_cfg = config_mod.load(source)
    redact.activate(nested_cfg)
    args = cli.build_parser().parse_args([cli.JSON_FLAG, *argv])
    captured, nested = cli._captured_stdout(lambda: cli._dispatch_envelope(args, nested_cfg))
    cli._forward_to_stderr(captured)
    return nested


def _append_log(cfg: Any, nested: dict[str, Any]) -> None:
    path = _out_path(cfg, "ci_log.jsonl")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(nested, default=str) + "\n")


def _write_baseline(ctx: _Context, nested: dict[str, Any]) -> None:
    # Exactly what `evalops --json baseline > .evalops/out/baseline.json` writes: the envelope.
    path = _out_path(ctx.cfg, "baseline.json")
    path.write_text(json.dumps(nested, indent=2, default=str) + "\n", encoding="utf-8")
    baseline = nested.get("data", {}).get("baseline") or {}
    ctx.baseline_sha = str(baseline.get("git_sha") or "")


def _out_path(cfg: Any, name: str) -> Any:
    d = cfg.lock_dir / "out"  # lock_dir creates .evalops; only a real run ever gets here
    d.mkdir(parents=True, exist_ok=True)
    return d / name


# ---------------------------------------------------------------------------
# git actions (subprocesses only here)
# ---------------------------------------------------------------------------
def _git(root: Any, argv: list[str]) -> tuple[int, str, str]:
    proc = subprocess.run(argv, cwd=str(root), capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def _git_checked(root: Any, argv: list[str]) -> None:
    code, stdout, stderr = _git(root, argv)
    if code != EXIT_OK:
        detail = stderr.strip() or stdout.strip() or f"exit {code}"
        raise EvalOpsError(f"git {argv[1] if len(argv) > 1 else ''} failed: {detail}", EXIT_RUNTIME)


def _commit_state(cfg: Any, branch: str | None, pr: str | None = None) -> bool:
    """Commit + push versioned state; returns ``committed``; no-op when nothing is staged.

    Mirrors the bash helper: `git add ... || true` (missing/ignored paths are not an error), then
    `git diff --cached --quiet` for the no-op, then the ``EVALOPS_BRANCH`` guard, then bot identity,
    commit and ``git push origin HEAD:<branch>``. With ``EVALOPS_PR`` set the push goes to the bot
    branch ``evalops/state/pr-<N>`` with ``--force`` instead (derived state: the latest run wins, so
    a previous run's commit can never wedge a later run of the same PR); a source branch is then
    neither required nor touched.
    """
    _git(cfg.root, ["git", "add", "-A", ".evalops/locks", ".evalops/store"])
    code, _, stderr = _git(cfg.root, ["git", "diff", "--cached", "--quiet"])
    if code == 0:
        return False
    if code != 1:
        raise EvalOpsError(f"git diff --cached failed: {stderr.strip()}", EXIT_RUNTIME)
    if not branch and not pr:
        raise EvalOpsError(NO_BRANCH_MESSAGE, EXIT_GUARD)
    _git_checked(cfg.root, ["git", "config", "user.name", os.getenv("GIT_BOT_NAME") or DEFAULT_GIT_NAME])
    _git_checked(cfg.root, ["git", "config", "user.email", os.getenv("GIT_BOT_EMAIL") or DEFAULT_GIT_EMAIL])
    _git_checked(cfg.root, ["git", "commit", "-m", COMMIT_MESSAGE])
    if pr:
        _git_checked(cfg.root, ["git", "push", "--force", "origin", f"HEAD:refs/heads/{_bot_branch(pr)}"])
    else:
        _git_checked(cfg.root, ["git", "push", "origin", f"HEAD:{branch}"])
    return True


def _ref_state(root: Any) -> dict[str, str]:
    """The current branch/ref and HEAD commit, read back from git after a restore executed."""
    code, stdout, _ = _git(root, ["git", "symbolic-ref", "-q", "--short", "HEAD"])
    ref = stdout.strip() if code == EXIT_OK else ""
    code, stdout, stderr = _git(root, ["git", "rev-parse", "HEAD"])
    if code != EXIT_OK:
        raise EvalOpsError(f"git rev-parse HEAD failed: {stderr.strip()}", EXIT_RUNTIME)
    head = stdout.strip()
    return {"ref": ref or head, "head": head}


def _current_ref(root: Any) -> str:
    code, stdout, _ = _git(root, ["git", "symbolic-ref", "-q", "--short", "HEAD"])
    if code == 0 and stdout.strip():
        return stdout.strip()
    code, stdout, stderr = _git(root, ["git", "rev-parse", "HEAD"])
    if code != 0:
        raise EvalOpsError(f"git rev-parse HEAD failed: {stderr.strip()}", EXIT_RUNTIME)
    return stdout.strip()


# ---------------------------------------------------------------------------
# last_eval.json hand-off (v1 envelope: `data.baseline.git_sha`, never the legacy top level)
# ---------------------------------------------------------------------------
def _last_eval_group(ctx: _Context) -> str:
    path = ctx.cfg.lock_dir / "out" / "last_eval.json"
    if not path.exists():
        raise EvalOpsError("gate wrote no .evalops/out/last_eval.json; cannot read the evidence group", EXIT_RUNTIME)
    group = formats.read_versioned(path).get("group")
    if not group:
        raise EvalOpsError("last_eval.json has no group; cannot promote", EXIT_RUNTIME)
    return str(group)
