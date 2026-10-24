"""evalops CLI: one interface for humans, any CI system, and coding agents, over any backend.

Every command supports --json and uses stable exit codes (docs/CONTRACT.md §1):
  0 ok | 1 gate failed | 2 usage/config | 3 budget exceeded | 4 guard violation | 5 runtime
  | 6 unsupported by plugin | 7 contract/format mismatch

With --json, stdout is exactly one envelope document (docs/CONTRACT.md §2); the process exit code
always equals `envelope["code"]`. A parse error (unknown flag, bad choice) is a code-2 envelope
too, and anything a dispatch hook prints is forwarded to stderr so it cannot corrupt the document.
"""

import argparse
import contextlib
import os
import sys
import tempfile
from collections.abc import Callable
from typing import Any, NoReturn, cast

from . import __version__, config as config_mod
from . import envelope
from .util import EXIT_OK, EXIT_RUNTIME, EXIT_USAGE, EvalOpsError

JSON_FLAG = "--json"
VERSION_FLAG = "--version"


class _ReportError(EvalOpsError):
    """A failure that also carries a structured report into the envelope's ``data``."""

    def __init__(self, message: str, code: int, details: dict[str, Any], data: dict[str, Any]):
        super().__init__(message, code, details)
        self.data = data


class _ArgumentParser(argparse.ArgumentParser):
    """``argparse`` with one change: a parse error raises instead of printing and exiting.

    Only ``error()`` is overridden, so ``--help`` and ``--version`` keep argparse's own exit 0.
    The usage line still goes to stderr for humans; ``main`` renders the code-2 envelope (argparse's
    message text is used verbatim, so the contract does not fork argparse's phrasing). The message
    names the offending argument (``unrecognized arguments: --made-up-flag``).
    """

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        raise EvalOpsError(f"{self.prog}: error: {message}", EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    p = _ArgumentParser(prog="evalops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to evalops.yaml (default: search upward from cwd)")
    p.add_argument("--json", action="store_true", help="machine-readable output (one envelope document)")
    # `--version` is answered in main() before the parser runs, so it works without a config file
    # and can honour --json; the action here only documents the flag and covers `--help`.
    p.add_argument("--version", action="version", version=f"evalops {__version__}", help="print the version and exit")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plugins", help="show configured plugins per role and all available plugins")
    sub.add_parser("capabilities", help="the preset/plugin capability matrix, generated from code")
    ci = sub.add_parser("ci-adapters", help="the CI adapters (GitHub, GitLab, Azure, Jenkins) generated from code")
    ci.add_argument("--write", action="store_true", help="render and (over)write the four adapter files")
    ci.add_argument(
        "--dir", default=None, metavar="PATH", help="directory to write them under (default: the repo root)"
    )
    sub.add_parser("doctor", help="instantiate every configured plugin and check connectivity")
    sub.add_parser("guard", help="static checks: no bypass of versioning, locks present, frozen data intact")

    ini = sub.add_parser("init", help="write a starter evalops.yaml for one of the built-in presets")
    ini.add_argument("--preset", required=True, choices=list(_preset_names()), help="which preset to render")
    ini.add_argument("--force", action="store_true", help="overwrite an existing evalops.yaml")
    ini.add_argument("--dir", default=".", metavar="PATH", help="directory to write evalops.yaml into (default: cwd)")

    pl = sub.add_parser("pipeline", help="run one CI stage: guard, sync, eval, gate, promote, state commit")
    pl.add_argument("stage", choices=list(_pipeline_stages()))
    pl.add_argument("--dry-run", action="store_true", help="list the steps without running or writing anything")
    # The global --json comes *before* the sub-command, which `ci/pipeline.sh pr ...` cannot inject.
    # SUPPRESS keeps the top-level value when this flag is absent; passing it after the stage
    # (`ci/pipeline.sh pr --json`) still renders exactly one envelope on stdout.
    pl.add_argument(
        "--json",
        dest="json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output (also accepted after the stage, for the ci/pipeline.sh shim)",
    )

    s = sub.add_parser("sync", help="register changed prompts/datasets in the configured backends")
    s.add_argument("what", choices=["prompts", "datasets", "all"])
    s.add_argument("--check", action="store_true", help="verify only; fail if out of sync")

    for name, helptext in (("plan", "estimate cost without running"), ("eval", "run an evaluation")):
        e = sub.add_parser(name, help=helptext)
        e.add_argument("--dataset", required=True)
        e.add_argument("--stage", default="dev", choices=["dev", "pr", "main", "nightly", "control"])
        e.add_argument("--repeats", type=int)
        e.add_argument("--judge-only", action="store_true", help="re-score stored outputs of --source-group")
        if name == "eval":
            e.add_argument("--source-group")
            e.add_argument("--allow-dirty", action="store_true", help="dev only; run is unpromotable")
            e.add_argument("--override-budget", action="store_true", help="humans only; tagged on the run")
            e.add_argument("--dry-run", action="store_true")
            e.add_argument(
                "--tool-mode",
                choices=["live", "record", "replay"],
                help="override tools.mode_by_stage (record = refresh cassettes)",
            )

    c = sub.add_parser("calibrate", help="check LLM judges against human labels (no generation)")
    c.add_argument("--dataset", required=True)
    c.add_argument("--scorer", action="append", help="judge(s) to calibrate (default: all in `judges:`)")
    c.add_argument("--allow-dirty", action="store_true")

    tr = sub.add_parser("traces", help="show normalized agent traces of a run group")
    tr.add_argument("--group", help="default: group from .evalops/out/last_eval.json")
    tr.add_argument("--row", help="row id, e.g. 00003")
    tr.add_argument(
        "--unknown-attrs",
        action="store_true",
        help="report span attribute names normalize() did not model (names only, never values)",
    )

    g = sub.add_parser("gate", help="compare a run group to the production baseline")
    g.add_argument("--group", help="default: group from .evalops/out/last_eval.json")
    g.add_argument("--label", default="gate", choices=["gate", "drift"])

    pr = sub.add_parser("promote", help="move a prompt alias using a passing evidence group")
    pr.add_argument("--alias", required=True, choices=["candidate", "staging", "production"])
    pr.add_argument("--evidence-group", required=True)

    rb = sub.add_parser("rollback", help="incident path: point an alias at a known version")
    rb.add_argument("--alias", required=True)
    rb.add_argument("--prompt", required=True)
    rb.add_argument("--version", required=True)

    sub.add_parser("baseline", help="show the current production baseline (git sha, group)")
    sub.add_parser("cost", help="month-to-date eval spend vs budget")
    sub.add_parser("judges", help="declared judge specs (pinned model, rubric identity) and the unpinned worklist")

    cf = sub.add_parser("config", help="validate, print the JSON Schema of, or explain evalops.yaml")
    cfsub = cf.add_subparsers(dest="config_action", required=True)
    cfsub.add_parser("validate", help="validate evalops.yaml; exit 2 naming every bad key path")
    cfsub.add_parser("schema", help="print the generated JSON Schema of evalops.yaml")
    cfsub.add_parser("explain", help="resolved configuration with per-key provenance")

    lg = sub.add_parser("ledger", help="apply or verify the SQL ledger's table schema")
    lgsub = lg.add_subparsers(dest="ledger_action", required=True)
    lgsub.add_parser("migrate", help="apply pending ledger schema steps (idempotent, preserves rows)")
    lgsub.add_parser("verify", help="read-only integrity report: orphans, manifest hashes, budget totals")

    rd = sub.add_parser("redact", help="dry-run the redaction rules: reports only, never writes")
    rd.add_argument(
        "--check",
        action="store_true",
        help="optional no-op: the command always reports only and never writes (exits 0 with differences)",
    )
    rd.add_argument("--group", help="ledger artifacts of this run group (default: group from last_eval.json)")
    rd.add_argument("--file", action="append", default=[], metavar="PATH", help="repeatable: .jsonl or .json")
    return p


def dispatch(args, cfg) -> dict:
    from . import cost, gate, guard, run
    from . import datasets as ds_mod
    from . import prompts as pr_mod
    from .ports import ROLES, Ledger
    from .registry import Registry, available

    if args.cmd == "config":
        return config_mod.run_config_command(cfg, args.config_action)
    if args.cmd == "ledger":
        ledger = cast(Ledger, Registry(cfg).get("ledger"))
        if args.ledger_action == "migrate":
            result = ledger.migrate()
            applied = result.get("applied") or []
            return {
                "summary": f"ledger schema {result.get('from')} -> {result.get('to')} (applied: {applied or 'none'})",
                **result,
            }
        report = ledger.verify()
        problems = report.get("problems") or []
        if report.get("ok", True):
            return {"summary": f"ledger ok (schema {report.get('schema_version')})", **report}
        # A failed integrity report is a runtime failure (exit 5), but callers still need the report.
        raise _ReportError(
            f"ledger verify found {len(problems)} problem(s)",
            EXIT_RUNTIME,
            {"problems": problems},
            report,
        )
    if args.cmd == "redact":
        from .contract import redact as redact_mod

        # `--check` is optional and changes nothing: the command is always a read-only report.
        return redact_mod.run_check(cfg, group=args.group, files=args.file, as_json=args.json)
    if args.cmd == "plugins":
        reg = Registry(cfg)
        roles = {}
        for role in ROLES:
            try:
                reg.get(role)
                roles[role] = {"plugin": cfg.plugins[role], "ok": True}
            except EvalOpsError as e:
                roles[role] = {"plugin": cfg.plugins[role], "ok": False, "error": str(e)}
        return {"summary": f"env '{cfg.env}'", "roles": roles, "available": available()}
    if args.cmd == "doctor":
        reg, report, ok = Registry(cfg), {}, True
        for role in ROLES:
            try:
                report[role] = reg.get(role).doctor()
            except Exception as e:  # noqa: BLE001
                ok, report[role] = False, {"plugin": cfg.plugins[role], "ok": False, "error": str(e)}
        return {"ok": ok, "summary": f"doctor {'ok' if ok else 'found problems'} for env '{cfg.env}'", "roles": report}
    if args.cmd == "guard":
        return guard.run_guard(cfg)
    if args.cmd == "pipeline":
        from . import pipeline

        return pipeline.run_pipeline(cfg, args.stage, dry_run=args.dry_run, as_json=args.json)
    if args.cmd == "sync":
        reg, out = Registry(cfg), {}
        if args.what in ("prompts", "all"):
            out["prompts"] = pr_mod.sync(cfg, reg, check=args.check)
        if args.what in ("datasets", "all"):
            out["datasets"] = ds_mod.sync(cfg, reg, check=args.check)
        return {"summary": "; ".join(v["summary"] for v in out.values()), **out}
    if args.cmd == "plan":
        return run.plan(cfg, args.dataset, args.stage, args.repeats, args.judge_only)
    if args.cmd == "eval":
        out = run.evaluate(
            cfg,
            args.dataset,
            args.stage,
            args.repeats,
            args.judge_only,
            args.source_group,
            args.allow_dirty,
            args.override_budget,
            args.dry_run,
            args.tool_mode,
        )
        out.pop("_rows", None)
        return out
    if args.cmd == "calibrate":
        from . import calibrate

        return calibrate.calibrate(cfg, args.dataset, args.scorer, args.allow_dirty)
    if args.cmd == "traces":
        from . import locks
        from .contract import formats

        group = args.group
        if not group:
            last = locks.scratch(cfg, "last_eval.json")
            # Versioned read: a future schema_version is exit 7, not a raw json.loads crash or misread.
            group = formats.read_versioned(last)["group"] if last.exists() else None
        if not group:
            # No --group and no last_eval.json is a usage error (exit 2), the same verdict
            # `gate.run_gate` gives for the same situation: the caller named no group.
            raise EvalOpsError("no --group and no last_eval.json", EXIT_USAGE)
        return run.show_traces(cfg, group, args.row, unknown_attrs=args.unknown_attrs)
    if args.cmd == "gate":
        return gate.run_gate(cfg, args.group, args.label)
    if args.cmd == "promote":
        return pr_mod.promote(cfg, Registry(cfg), args.alias, args.evidence_group)
    if args.cmd == "rollback":
        return pr_mod.rollback(cfg, Registry(cfg), args.alias, args.prompt, args.version)
    if args.cmd == "baseline":
        return run.baseline(cfg)
    if args.cmd == "cost":
        spent = float(cost.month_spend(Registry(cfg).get("ledger")))
        cap = cfg.section("cost").get("budgets", {}).get("per_month_usd")
        return {"summary": f"month to date ${spent} of ${cap}", "month_spent_usd": spent, "per_month_usd": cap}
    if args.cmd == "judges":
        from . import judges as judges_mod

        # A declared but invalid spec is exit 2 here too (same load-time rejection as plan/eval).
        data = judges_mod.payload(cfg)
        if not args.json:  # human output is free-form (CONTRACT §2)
            judges_mod.write_human(data)
        return data
    raise EvalOpsError(f"unknown command {args.cmd}")


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    if _version_requested(parser, argv):  # before the parser: needs no config file and honours --json
        return _print_version(as_json=JSON_FLAG in argv, env=_early_env(argv))
    try:
        args = parser.parse_args(argv)
    except EvalOpsError as e:  # a parse error: exit 2 *with* an envelope, not a bare argparse exit
        env = _early_env(argv, e)
        return _finish(envelope.from_error(_command_from_argv(parser, argv), env, e), JSON_FLAG in argv)
    command = args.cmd
    if command in ("init", "capabilities", "ci-adapters"):
        # `init` writes evalops.yaml (there may be none yet) and `capabilities`/`ci-adapters` are
        # static data generated from code, so all three answer before the config loads -- like
        # `--version`, and with the same resolved `env`.
        return _finish(_static_envelope(args, argv), args.json)
    try:
        cfg = config_mod.load(args.config)
    except Exception as e:  # noqa: BLE001 - a broken config must still produce a valid envelope
        return _finish(envelope.from_error(command, _early_env(argv, e), e), args.json)
    # Every command is bound to the config it just loaded (CONTRACT §5.1): the sinks that take no
    # `Config` (`AgentTrace.to_dict`, `Cassette.save`) must never inherit another config's rules.
    from .contract import redact as redact_mod

    try:
        redact_mod.activate(cfg)
    except Exception as e:  # noqa: BLE001 - binding failures are envelopes too (exit 2 / exit 5)
        return _finish(_error_envelope(args, cfg, e), args.json)
    if args.json:
        # A hook that prints must not corrupt the document: file descriptor 1 is captured for the
        # whole dispatch (so `sys.__stdout__` and child processes are caught too) and the captured
        # text is forwarded to stderr, leaving the envelope as the only thing on stdout.
        captured, env_dict = _captured_stdout(lambda: _dispatch_envelope(args, cfg))
        _forward_to_stderr(captured)
    else:
        env_dict = _dispatch_envelope(args, cfg)
    return _finish(env_dict, args.json)


def _captured_stdout(run: Callable[[], dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """Run ``run`` with **both** stdout levels redirected -> ``(captured, result)``.

    Two bypasses have to be closed for the envelope to stay the only thing on stdout:

    * a hook writing to ``sys.__stdout__`` or spawning a child that inherits the descriptor never
      touches ``sys.stdout``, so file descriptor 1 is redirected to a temp file (``os.dup2``);
    * a hook writing to a ``sys.stdout`` that is a *separate stream* (pytest ``capsys``, an
      embedder's ``contextlib.redirect_stdout``) never touches the descriptor, so ``sys.stdout`` is
      rebound to ``sys.stderr`` for the whole dispatch.

    Both are restored before ``main`` renders the envelope to the real ``sys.stdout``; the captured
    descriptor bytes are appended to stderr afterwards, in one diagnostic stream.
    """
    sys.stdout.flush()
    outer_stdout = sys.stdout
    with tempfile.TemporaryFile(mode="w+b") as captured:
        saved = os.dup(1)
        try:
            os.dup2(captured.fileno(), 1)
            try:
                with contextlib.redirect_stdout(sys.stderr):
                    try:
                        result = run()
                    finally:
                        # Flush every Python-level stdout buffer while fd 1 still points at the temp
                        # file, so nothing a hook left buffered lands in the envelope afterwards.
                        for stream in (sys.stderr, outer_stdout, sys.__stdout__):
                            if stream is not None:  # sys.__stdout__ is None on a detached interpreter
                                with contextlib.suppress(Exception):
                                    stream.flush()
            finally:
                os.dup2(saved, 1)
        finally:
            os.close(saved)
        captured.seek(0)
        return captured.read().decode("utf-8", "replace"), result


def _error_envelope(args, cfg, exc: Exception) -> dict[str, Any]:
    """Map one dispatch/binding exception to an envelope (EvalOpsError keeps its code, else exit 5)."""
    if isinstance(exc, EvalOpsError):
        data = getattr(exc, "data", None)
        if isinstance(data, dict):
            # A command that returns a structured failure report (ledger verify) keeps it in `data`.
            # Normalize the code exactly like `envelope.from_error`: a code outside the matrix (git's
            # 128, 0, a non-int) would make `envelope.failure` raise ValueError, costing the whole
            # envelope (and `data.steps`) instead of reporting the failure.
            code = getattr(exc, "code", envelope.EXIT_RUNTIME)
            if not isinstance(code, int) or code == EXIT_OK or code not in envelope.EXIT_MATRIX:
                code = envelope.EXIT_RUNTIME
            return envelope.failure(
                args.cmd,
                cfg.env,
                code,
                str(exc),
                hint=getattr(exc, "hint", None),
                details=getattr(exc, "details", None),
                data=data,
            )
        return envelope.from_error(args.cmd, cfg.env, exc)
    return envelope.failure(args.cmd, cfg.env, envelope.EXIT_RUNTIME, f"{type(exc).__name__}: {exc}")


def _dispatch_envelope(args, cfg) -> dict[str, Any]:
    """Run ``dispatch`` and always return an envelope: usage errors, crashes and gate failures."""
    try:
        result = dispatch(args, cfg)
        if args.cmd == "ledger" and getattr(args, "ledger_action", None) == "verify":
            # The verify verdict is `data.ok`, not the envelope's success flag: keep it visible.
            return envelope.success(args.cmd, cfg.env, dict(result))
        return envelope.from_result(args.cmd, cfg.env, result)
    except Exception as e:  # noqa: BLE001 - unexpected failures are exit 5, never a traceback on stdout
        return _error_envelope(args, cfg, e)


def _forward_to_stderr(text: str) -> None:
    """JSON mode: anything dispatch printed is a diagnostic, so it belongs on stderr."""
    if not text:
        return
    sys.stderr.write(text)
    if not text.endswith("\n"):
        sys.stderr.write("\n")
    sys.stderr.flush()


def _static_envelope(args, argv: list[str]) -> dict[str, Any]:
    """Envelope for the commands that never read ``evalops.yaml`` (``init``, ``capabilities``, ``ci-adapters``)."""
    from . import ci_adapters, presets

    env = _early_env(argv)
    try:
        if args.cmd == "init":
            data = presets.init(args.preset, directory=args.dir, force=args.force)
        elif args.cmd == "ci-adapters":
            data = ci_adapters.command_payload(write=args.write, directory=args.dir)
        else:
            data = presets.capabilities_payload()
    except EvalOpsError as exc:
        return envelope.from_error(args.cmd, env, exc)
    except Exception as exc:  # noqa: BLE001 - an unexpected failure is still an envelope
        return envelope.failure(args.cmd, env, envelope.EXIT_RUNTIME, f"{type(exc).__name__}: {exc}")
    return envelope.success(args.cmd, env, data)


def _pipeline_stages() -> tuple[str, ...]:
    """The stage names `pipeline` accepts (one source of truth: the pipeline module's table)."""
    from .pipeline import STAGES

    return STAGES


def _preset_names() -> tuple[str, ...]:
    """The preset names ``init --preset`` accepts (one source of truth: ``presets.PRESETS``)."""
    from .presets import PRESET_NAMES

    return PRESET_NAMES


def _subcommand_names(parser: argparse.ArgumentParser) -> set[str]:
    """Every sub-command name the parser knows (the subparsers action is the only one with dict choices)."""
    names: set[str] = set()
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            names.update(choices)
    return names


def _top_level_tokens(argv: list[str]) -> list[str]:
    """The argv tokens the *top-level* parser reads, with every option value consumed.

    A ``--config`` value is a file name, not a sub-command: ``evalops --config rollback --version``
    with a config file called ``rollback`` must still answer the top-level ``--version``. The scan
    therefore skips ``--config X`` / ``--config=X`` entirely, treats every other ``-``-prefixed
    token as a flag (the parser reports an unknown one), and stops at the first non-option token:
    that token is the real sub-command, and nothing after it belongs to the top level.
    """
    tokens: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--config":
            index += 2  # the next token is the value, whatever it happens to look like
            continue
        if token.startswith("--config="):
            index += 1
            continue
        tokens.append(token)
        index += 1
        if not token.startswith("-"):
            break  # the first non-option token is the sub-command
    return tokens


def _version_requested(parser: argparse.ArgumentParser, argv: list[str]) -> bool:
    """True when ``--version`` is the top-level flag rather than a sub-command's own option.

    ``evalops rollback --alias production --prompt p --version 1`` passes the version to roll back
    to, so the flag only counts before the sub-command token; otherwise it would print the evalops
    version instead of running the command.
    """
    names = _subcommand_names(parser)
    for token in _top_level_tokens(argv):
        if token in names:
            return False
        if token == VERSION_FLAG:
            return True
    return False


def _command_from_argv(parser: argparse.ArgumentParser, argv: list[str]) -> str:
    """Best-effort ``command`` for the envelope when parsing itself failed.

    The first *top-level* argument that is a known subcommand wins (``evalops --json plugins
    --bad`` -> ``plugins``); an option value is never a subcommand (``--config rollback``), and a
    failure before any subcommand (``evalops --json``) reports ``cli``.
    """
    names = _subcommand_names(parser)
    return next((token for token in _top_level_tokens(argv) if token in names), "cli")


def _finish(env_dict: dict[str, Any], as_json: bool) -> int:
    """Render the envelope and return its code, so the process exit code is the envelope code."""
    envelope.render(env_dict, as_json, sys.stdout)
    return int(env_dict["code"])


def _print_version(as_json: bool, env: str) -> int:
    if as_json:
        envelope.render(envelope.success("version", env, {"version": __version__}), True, sys.stdout)
    else:
        print(f"evalops {__version__}")
    return EXIT_OK


def _config_from_argv(argv: list[str]) -> str | None:
    """``--config PATH`` / ``--config=PATH`` from raw argv (early paths run before argparse)."""
    for index, token in enumerate(argv):
        if token == "--config":
            return argv[index + 1] if index + 1 < len(argv) else None
        if token.startswith("--config="):
            return token.split("=", 1)[1]
    return None


def _early_env(argv: list[str], exc: BaseException | None = None) -> str:
    """The env for an envelope built before (or without) a loaded ``Config`` (CONTRACT.md §2).

    A failure that got far enough to validate the config carries the resolved env in its details;
    that wins. Otherwise resolve it best-effort the way ``load()`` finds the file, so `--version`
    and argparse errors report the project's `default_env` instead of a hard-coded `local`.
    """
    details = getattr(exc, "details", None) or {}
    if details.get("env"):
        return str(details["env"])
    return config_mod.resolve_env(_config_from_argv(argv))


if __name__ == "__main__":
    sys.exit(main())
