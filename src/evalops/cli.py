"""evalops CLI: one interface for humans, any CI system, and coding agents, over any backend.

Every command supports --json and uses stable exit codes:
  0 ok | 1 gate failed | 2 usage/config | 3 budget exceeded | 4 guard violation | 5 runtime | 6 unsupported by plugin
"""

import argparse
import sys

from . import config as config_mod
from .util import EXIT_GATE_FAILED, EXIT_OK, EXIT_RUNTIME, EvalOpsError, emit


def build_parser():
    p = argparse.ArgumentParser(
        prog="evalops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", help="path to evalops.yaml (default: search upward from cwd)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("plugins", help="show configured plugins per role and all available plugins")
    sub.add_parser("doctor", help="instantiate every configured plugin and check connectivity")
    sub.add_parser("guard", help="static checks: no bypass of versioning, locks present, frozen data intact")

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

    cf = sub.add_parser("config", help="validate, print the JSON Schema of, or explain evalops.yaml")
    cfsub = cf.add_subparsers(dest="config_action", required=True)
    cfsub.add_parser("validate", help="validate evalops.yaml; exit 2 naming every bad key path")
    cfsub.add_parser("schema", help="print the generated JSON Schema of evalops.yaml")
    cfsub.add_parser("explain", help="resolved configuration with per-key provenance")
    return p


def dispatch(args, cfg) -> dict:
    from . import cost, gate, guard, run
    from . import datasets as ds_mod
    from . import prompts as pr_mod
    from .ports import ROLES
    from .registry import Registry, available

    if args.cmd == "config":
        return config_mod.run_config_command(cfg, args.config_action)
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
            raise EvalOpsError("no --group and no last_eval.json")
        return run.show_traces(cfg, group, args.row)
    if args.cmd == "gate":
        return gate.run_gate(cfg, args.group, args.label)
    if args.cmd == "promote":
        return pr_mod.promote(cfg, Registry(cfg), args.alias, args.evidence_group)
    if args.cmd == "rollback":
        return pr_mod.rollback(cfg, Registry(cfg), args.alias, args.prompt, args.version)
    if args.cmd == "baseline":
        return run.baseline(cfg)
    if args.cmd == "cost":
        spent = cost.month_spend(Registry(cfg).get("ledger"))
        cap = cfg.section("cost").get("budgets", {}).get("per_month_usd")
        return {"summary": f"month to date ${spent} of ${cap}", "month_spent_usd": spent, "per_month_usd": cap}
    raise EvalOpsError(f"unknown command {args.cmd}")


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = config_mod.load(args.config)
        result = dispatch(args, cfg)
        ok = result.get("ok", True)
        emit({"ok": ok, "env": cfg.env, **result}, args.json)
        return EXIT_OK if ok else EXIT_GATE_FAILED
    except EvalOpsError as e:
        emit({"ok": False, "error": str(e), "code": e.code, **e.details}, args.json)
        return e.code
    except Exception as e:  # noqa: BLE001
        emit({"ok": False, "error": f"{type(e).__name__}: {e}", "code": EXIT_RUNTIME}, args.json)
        return EXIT_RUNTIME


if __name__ == "__main__":
    sys.exit(main())
