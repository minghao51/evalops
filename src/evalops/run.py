"""The only way evaluations run: pinned versions, budget-checked, cost-measured, traces captured."""
import dataclasses
import functools
import inspect
import json
import os
import uuid

from . import cost, locks
from . import datasets as ds_mod
from . import prompts as pr_mod
from . import tools as tools_mod
from .ports import RowResult
from .registry import Registry
from .scoring import aggregate_rows, call_scorer, scorer_name, wants_trace
from .traces import capture as trace_capture
from .traces.model import AgentTrace
from .util import EXIT_GUARD, EXIT_USAGE, EvalOpsError, canonical, git_dirty, git_sha, sha256

STAGES = ("dev", "pr", "main", "nightly", "control")
TOOL_MODES = ("live", "record", "replay")


def _repeats(cfg, stage, repeats):
    return repeats or int(cfg.section("eval").get("repeats", {}).get(stage, 1))


def _build_scorers(cfg):
    fn = cfg.hook("scorers")
    return fn(evaluator=cfg.plugins["evaluator"]) if "evaluator" in inspect.signature(fn).parameters else fn()


def _tool_mode(cfg, stage, override):
    mode = override or (cfg.section("tools").get("mode_by_stage") or {}).get(stage) or "live"
    if mode not in TOOL_MODES:
        raise EvalOpsError(f"tool mode must be one of {TOOL_MODES}", EXIT_USAGE)
    return mode


def plan(cfg, dataset, stage, repeats=None, judge_only=False) -> dict:
    reg = Registry(cfg)
    entry = locks.read(cfg, ds_mod.KIND).get(dataset)
    if not entry:
        raise EvalOpsError(f"dataset '{dataset}' not synced; run `evalops sync datasets`", EXIT_USAGE)
    ledger = reg.get("ledger")
    meta = cfg.hook("judge_meta")()
    est = cost.estimate(cfg, entry["rows"], _repeats(cfg, stage, repeats), int(meta.get("llm_judge_count", 0)),
                        judge_only, cost.history(ledger))
    return {"summary": f"estimated ${est['total_usd']} for {est['llm_calls']} LLM calls",
            "estimate": est, "budget": cost.check_budget(cfg, ledger, est, override=True), "plugins": reg.describe()}


def row_to_json(r: RowResult, max_payload_chars) -> dict:
    d = {k: v for k, v in dataclasses.asdict(r).items() if k not in ("trace", "usage")}
    d["usage"] = dataclasses.asdict(r.usage) if r.usage else None
    d["trace"] = r.trace.to_dict(max_payload_chars) if r.trace is not None else None
    return d


def rows_from_artifact(text) -> list[RowResult]:
    rows = []
    for line in text.splitlines():
        d = json.loads(line)
        rows.append(RowResult(row_id=d["row_id"], inputs=d["inputs"], outputs=d.get("outputs"),
                              expectations=d.get("expectations"), trace_id=d.get("trace_id"),
                              scores=d.get("scores") or {}, error=d.get("error"),
                              trace=AgentTrace.from_dict(d.get("trace")), trace_metrics=d.get("trace_metrics") or {}))
    return rows


def _capturing(predict_fn, limits, stash):
    """For native evaluators (which run predict themselves): capture each call's trace by input."""
    if inspect.iscoroutinefunction(predict_fn):
        @functools.wraps(predict_fn)
        async def wrapped(**inputs):
            with trace_capture.row_capture(sha256(canonical(inputs))[:12], limits) as cap:
                try:
                    return await predict_fn(**inputs)
                finally:
                    stash[canonical(inputs)] = cap.trace()
        return wrapped

    @functools.wraps(predict_fn)
    def wrapped(**inputs):
        with trace_capture.row_capture(sha256(canonical(inputs))[:12], limits) as cap:
            try:
                return predict_fn(**inputs)
            finally:
                stash[canonical(inputs)] = cap.trace()
    return wrapped


def _post_score(rows, scorers):
    for r in rows:
        for s in scorers:
            try:
                r.scores.update(call_scorer(s, inputs=r.inputs, outputs=r.outputs, expectations=r.expectations,
                                            trace=r.trace))
            except Exception as e:  # noqa: BLE001
                r.error = (r.error + "; " if r.error else "") + f"{scorer_name(s)}: {type(e).__name__}: {e}"


def evaluate(cfg, dataset, stage, repeats=None, judge_only=False, source_group=None, allow_dirty=False,
             override_budget=False, dry_run=False, tool_mode=None, kind="eval", scorer_filter=None) -> dict:
    if stage not in STAGES:
        raise EvalOpsError(f"stage must be one of {STAGES}", EXIT_USAGE)
    if judge_only and not source_group and kind != "calibration":
        raise EvalOpsError("--judge-only requires --source-group", EXIT_USAGE)
    dirty = git_dirty(cwd=cfg.root)
    if dirty and not allow_dirty:
        raise EvalOpsError("uncommitted changes; commit first (or --allow-dirty for dev)", EXIT_GUARD)
    pr_mod.sync(cfg, None, check=True)
    ds_mod.sync(cfg, None, check=True)

    reg = Registry(cfg)
    ledger, tracer, evaluator, dstore = reg.get("ledger"), reg.get("tracer"), reg.get("evaluator"), reg.get("datasets")
    reps = _repeats(cfg, stage, repeats)
    meta = cfg.hook("judge_meta")()
    n_llm = int(meta.get("llm_judge_count", 0))
    frozen = bool((cfg.section("datasets").get(dataset) or {}).get("frozen"))
    records, dlock = ds_mod.load(cfg, reg, dataset, verify=frozen)
    est = cost.estimate(cfg, dlock["rows"], reps, n_llm, judge_only, cost.history(ledger))
    budget = cost.check_budget(cfg, ledger, est, override_budget)
    if dry_run:
        return {"summary": f"dry run: would spend ~${est['total_usd']}", "estimate": est, "budget": budget,
                "plugins": reg.describe()}

    mode = _tool_mode(cfg, stage, tool_mode)
    cassette = tools_mod.Cassette(cfg.path("cassettes") / f"{dataset}.jsonl", mode,
                                  cfg.section("tools").get("on_miss", "error"))

    prompts, plock = pr_mod.load_locked(cfg, reg)
    full_cfg = {**cfg.hook("behavior_config")(), "prompts": {n: pr_mod.ref(e) for n, e in plock.items()},
                "judge": meta, "plugins": reg.describe(), "tool_cassette": cassette.fingerprint() if mode == "replay"
                else f"mode:{mode}"}
    cfg_hash = sha256(canonical(full_cfg))[:8]
    version = f"{cfg.data['project']}-{git_sha(cwd=cfg.root)}-{cfg_hash}"
    scorers = _build_scorers(cfg)
    if scorer_filter:
        scorers = [s for s in scorers if scorer_name(s) in scorer_filter]
    predict_fn = None if judge_only else cfg.hook("predict_fn")(prompts)
    native = evaluator.name != "core"
    trace_scorers = [s for s in scorers if wants_trace(s)] if native else []
    native_scorers = [s for s in scorers if s not in trace_scorers]
    limits = dict(cfg.section("eval").get("row_limits") or {})
    max_payload = cfg.section("traces").get("max_payload_chars", 4000)

    stored = None
    if kind == "calibration":  # pre-recorded outputs/traces in the dataset itself
        stored = [RowResult(f"{i:05d}", r["inputs"], r.get("outputs"), r.get("expectations"),
                            trace=AgentTrace.from_dict(r.get("trace"))) for i, r in enumerate(records)]
    elif judge_only:
        src_runs = ledger.find_runs(tags={"evalops.group": source_group}, limit=1)
        if not src_runs:
            raise EvalOpsError(f"no runs for source group {source_group}", EXIT_USAGE)
        stored = rows_from_artifact(ledger.get_artifact(src_runs[0].run_id, "evalops/rows.jsonl"))

    group, runs = uuid.uuid4().hex[:12], []
    promotable = not dirty and stage in ("main", "nightly") and kind == "eval"
    concurrency = cfg.section("eval")
    for i in range(reps):
        name = f"{stage}-{version}-{dataset}-r{i + 1}"
        run_id = ledger.start_run(name, {
            "evalops.group": group, "evalops.stage": stage, "evalops.repeat": str(i + 1), "evalops.kind": kind,
            "evalops.app_version": version, "evalops.promotable": str(promotable).lower(), "evalops.env": cfg.env,
            "evalops.budget_override": str(bool(budget["problems"]) and override_budget).lower(),
            "evalops.actor": os.getenv("EVALOPS_ACTOR", "unknown"),  # ci | human:<id> | agent:<name>
        })
        status = "FAILED"
        try:
            ledger.log(run_id, params={
                "app_version": version, "config_hash": cfg_hash, "git_sha": git_sha(short=False, cwd=cfg.root),
                "git_dirty": dirty, "dataset": dataset, "dataset_physical": dlock["physical"],
                "dataset_fingerprint": dlock["fingerprint"], "dataset_rows": dlock["rows"],
                "source_group": source_group or "", "tool_mode": mode, "tool_cassette": cassette.fingerprint(),
                **{f"plugin.{k}": v for k, v in reg.describe().items()},
                **{f"prompt.{n}": pr_mod.ref(e) for n, e in plock.items()},
                **{f"judge.{k}": v for k, v in meta.items()},
                "behavior_config": canonical(full_cfg)[:4000],
            })
            ledger.put_artifact(run_id, "evalops/cost_estimate.json", json.dumps(est))
            ledger.put_artifact(run_id, "evalops/prompts.lock.json", json.dumps(plock))
            ledger.put_artifact(run_id, "evalops/datasets.lock.json", json.dumps(locks.read(cfg, ds_mod.KIND)))

            stash: dict = {}
            fn = predict_fn
            if native and predict_fn is not None:
                fn = _capturing(predict_fn, limits, stash)
            with tools_mod.use(cassette):
                result = evaluator.evaluate(records=records, dataset_physical=dlock["physical"],
                                            dataset_store=dstore, predict_fn=fn,
                                            scorers=native_scorers if native else scorers, tracer=tracer,
                                            run_id=run_id, run_name=name, concurrency=concurrency,
                                            stored_rows=stored)
            if native:  # attach captured traces, then run trace-aware scorers ourselves
                for r in result.rows:
                    if r.trace is None:
                        r.trace = stash.get(canonical(r.inputs))
                    if r.trace is not None:
                        r.trace_metrics = r.trace.metrics()
                _post_score(result.rows, trace_scorers)
                result.metrics = {**result.metrics, **aggregate_rows(result.rows)}
            if mode == "record":
                cassette.save()

            ledger.put_artifact(run_id, "evalops/rows.jsonl", "".join(
                json.dumps(row_to_json(r, max_payload), default=str) + "\n" for r in result.rows))
            rows_n = max(dlock["rows"], 1)
            measured = cost.measure(cfg, result.rows) if stored is None else cost.measure(cfg, [])
            judge_usd = round(rows_n * n_llm * cost.judge_cost_per_call(cfg), 6)
            total = round(measured["gen_cost_usd"] + judge_usd, 6)
            cost_metrics = {
                "gen_input_tokens": measured["gen_input_tokens"], "gen_output_tokens": measured["gen_output_tokens"],
                "gen_input_tokens_per_row": measured["gen_input_tokens"] / rows_n,
                "gen_output_tokens_per_row": measured["gen_output_tokens"] / rows_n,
                "gen_cost_usd": measured["gen_cost_usd"], "judge_cost_usd_est": judge_usd,
                "total_cost_usd": total, "cost_per_row_usd": total / rows_n,
                "rows_missing_usage": measured["rows_missing_usage"], "tool_replay_misses": len(cassette.misses),
            }
            ledger.log(run_id, metrics={**result.metrics, **cost_metrics},
                       tags={"evalops.gen_cost_source": measured["gen_cost_source"],
                             **({"evalops.native_ref": result.native_ref} if result.native_ref else {})})
            runs.append({"run_id": run_id, "metrics": {**result.metrics, **cost_metrics},
                         "errors": sum(1 for r in result.rows if r.error), "native_ref": result.native_ref,
                         "rows": result.rows})
            status = "FINISHED"
        finally:
            ledger.end_run(run_id, status)

    actual = round(sum(r["metrics"]["total_cost_usd"] for r in runs), 4)
    out = {"summary": f"{stage} {kind} of {version} on {dataset}: {reps} run(s), "
                      f"${actual} actual vs ${est['total_usd']} est",
           "group": group, "app_version": version, "stage": stage, "dataset": dataset, "env": cfg.env, "kind": kind,
           "tool_mode": mode, "plugins": reg.describe(), "run_ids": [r["run_id"] for r in runs],
           "runs": [{k: v for k, v in r.items() if k != "rows"} for r in runs],
           "cost": {"estimate_usd": est["total_usd"], "actual_usd": actual, "budget": budget}}
    if cassette.misses:
        out["tool_replay_misses"] = cassette.misses[:20]
    if kind == "eval":
        locks.scratch(cfg, "last_eval.json").write_text(json.dumps(out, indent=2, default=str))
    out["_rows"] = [r["rows"] for r in runs]
    return out


def baseline(cfg) -> dict:
    runs = Registry(cfg).get("ledger").find_runs(tags={"evalops.baseline": "true"}, limit=1)
    if not runs:
        return {"summary": "no production baseline yet", "baseline": None}
    r = runs[0]
    return {"summary": f"baseline {r.tags.get('evalops.app_version')}",
            "baseline": {"group": r.tags.get("evalops.group"), "git_sha": r.params.get("git_sha"),
                         "app_version": r.tags.get("evalops.app_version"), "run_id": r.run_id}}


def show_traces(cfg, group, row=None, as_tree=True) -> dict:
    ledger = Registry(cfg).get("ledger")
    runs = ledger.find_runs(tags={"evalops.group": group}, limit=1)
    if not runs:
        raise EvalOpsError(f"no runs for group {group}", EXIT_USAGE)
    out = []
    for r in rows_from_artifact(ledger.get_artifact(runs[0].run_id, "evalops/rows.jsonl")):
        if row and r.row_id != row:
            continue
        out.append({"row_id": r.row_id, "inputs": r.inputs, "scores": r.scores, "error": r.error,
                    "trace_metrics": r.trace_metrics, "tree": r.trace.render() if r.trace else None})
    return {"summary": f"{len(out)} row(s) from run {runs[0].run_id}", "rows": out}
