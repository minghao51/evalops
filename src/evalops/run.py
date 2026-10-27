"""The only way evaluations run: pinned versions, budget-checked, cost-measured, traces captured."""

import contextlib
import dataclasses
import functools
import inspect
import json
import os
import uuid
from typing import Any, cast

from . import cost, judges, locks
from . import datasets as ds_mod
from . import prompts as pr_mod
from . import tools as tools_mod
from .contract import formats, redact
from .ports import DatasetStore, Evaluator, Ledger, RowResult, Tracer
from .registry import Registry
from .scorers.judges import spec_judge
from .scorers.native import adapters
from .scoring import aggregate_rows, call_scorer, scorer_name, wants_trace
from .traces import capture as trace_capture
from .traces.model import AgentTrace
from .util import (
    EXIT_BUDGET,
    EXIT_GUARD,
    EXIT_RUNTIME,
    EXIT_USAGE,
    EvalOpsError,
    canonical,
    git_dirty,
    git_sha,
    sha256,
)

STAGES = ("dev", "pr", "main", "nightly", "control")
TOOL_MODES = ("live", "record", "replay")


def _repeats(cfg, stage, repeats):
    return repeats or int(cfg.section("eval").get("repeats", {}).get(stage, 1))


def _build_scorers(cfg):
    fn = cfg.hook("scorers")
    return fn(evaluator=cfg.plugins["evaluator"]) if "evaluator" in inspect.signature(fn).parameters else fn()


def _client_side_judge_calls(cfg, evaluator: str) -> int:
    """How many declared judge specs make one **client-side** model call per row (WP3b.3 D2).

    This is the estimate's judge bump. A declared spec costs a local model call unless its executor
    runs it elsewhere: the ``litellm`` fallback always calls out from this process, and a native
    adapter does too unless the vendor runs the judge itself (``NativeJudge.server_side``, i.e. the
    Langfuse managed evaluator). An evaluator with no adapter is counted: it will be exit 6 at
    scorer build, and silently underestimating it first would be worse than an honest count.

    ``evaluator`` is a plugin **name** (``cfg.plugins["evaluator"]`` in ``plan``, the instantiated
    plugin's ``name`` in ``_evaluate``): resolving it needs no plugin instance. Projects with no
    declared spec get ``0`` and therefore a byte-identical estimate.
    """
    declared = judges.load(cfg)
    if not declared:
        return 0
    adapter = adapters().get(evaluator)
    # No adapter: the spec cannot build, but counting it keeps the estimate honest meanwhile.
    server_side_native = adapter is not None and adapter.server_side
    return sum(1 for spec in declared.values() if spec.judge == "litellm" or not server_side_native)


def _judge_identities(declared: dict[str, judges.JudgeSpec]) -> dict[str, dict[str, str]]:
    """The per-judge identity recorded on the run and on every verdict (WP3b.5 C3/C5)."""
    return {name: {"model": spec.model, "prompt_version": spec.prompt_version} for name, spec in declared.items()}


def _stamp_judge_identities(rows, declared: dict[str, judges.JudgeSpec]) -> None:
    """Stamp `row.judge_models` from the declared specs (WP3b.5 C3).

    Every declared judge whose score is on the row gets its pinned `{model, prompt_version}` entry
    (additive: a row with no declared-spec score gets `{}`). `--judge-only` re-scores from stored
    rows, so re-stamping here means a stale value can never survive. The contract is then enforced
    by :func:`_require_judge_identities`.
    """
    identities = _judge_identities(declared)
    for row in rows:
        scores = row.scores or {}
        row.judge_models = {name: identities[name] for name in identities if name in scores}
    _require_judge_identities(rows, declared)


def _require_judge_identities(rows, declared: dict[str, judges.JudgeSpec]) -> None:
    """The contract: a declared judge's score must carry its resolved identity (CONTRACT §6).

    A verdict without a resolved model is `EvalOpsError(EXIT_RUNTIME)` naming the judge -- never a
    silent null. This is deliberately a separate check from the stamping, so dropping (or breaking)
    the stamping is caught here instead of disappearing into a `null` in the rows artifact.
    """
    for row in rows:
        present = row.judge_models or {}
        for name in sorted(set(row.scores or {}) & set(declared)):
            if name not in present:
                raise EvalOpsError(
                    f"judge '{name}': verdict has no resolved model; a declared judge's score must "
                    "record its pinned model and prompt version (CONTRACT §6)",
                    EXIT_RUNTIME,
                )


def _with_spec_judges(cfg, scorers, *, evaluator: str):
    """Append one scorer per declared judge spec, in sorted name order (WP3b.3 D1).

    The app's ``scorers`` hook keeps ownership of its own scorers: a declared spec that reuses one
    of its names is **exit 2**, naming both sources -- ambiguous ownership is refused rather than
    silently replacing or duplicating the app's scorer. Building the spec scorers here also makes a
    missing executor SDK exit 6 *before* the budget reservation and the ledger row, the same
    failure class as an app hook raising today. No declared spec -> the list is returned as-is.
    """
    declared = judges.load(cfg)
    if not declared:
        return list(scorers)
    out = list(scorers)
    taken = {scorer_name(s) for s in out}
    for name in sorted(declared):
        if name in taken:
            raise EvalOpsError(
                f"judge '{name}': ambiguous ownership: the app's `scorers` hook already provides a "
                f"scorer named '{name}' and `judges.{name}` declares a spec; rename one of them",
                EXIT_USAGE,
            )
        out.append(spec_judge(declared[name], evaluator=evaluator))
    return out


def _tool_mode(cfg, stage, override):
    mode = override or (cfg.section("tools").get("mode_by_stage") or {}).get(stage) or "live"
    if mode not in TOOL_MODES:
        raise EvalOpsError(f"tool mode must be one of {TOOL_MODES}", EXIT_USAGE)
    return mode


def plan(cfg, dataset, stage, repeats=None, judge_only=False) -> dict:
    # Judge specs are load-rejected first (CONTRACT §6, WP3b.1): an invalid declared spec is exit 2
    # with no cost estimate and no ledger write. No declared spec -> `{}` and nothing changes.
    judges.load(cfg)
    reg = Registry(cfg)
    entry = locks.entries(locks.read(cfg, ds_mod.KIND)).get(dataset)
    if not entry:
        raise EvalOpsError(f"dataset '{dataset}' not synced; run `evalops sync datasets`", EXIT_USAGE)
    ledger = cast(Ledger, reg.get("ledger"))
    meta = cfg.hook("judge_meta")()
    # WP3b.5 B3: a run-level identity that disagrees with a declared spec is exit 2 here, before
    # the cost estimate and before any ledger row (the same load point as `judges.load`).
    judges.conflicts_with_meta(cfg, meta)
    # D2/WP3b.3: a declared judge spec adds one client-side model call per row. The evaluator is
    # resolved by *name* from the config (a plain read) -- plan never instantiated that plugin and
    # still does not. No declared spec -> 0 extra calls, so existing estimates are unchanged.
    n_llm = int(meta.get("llm_judge_count", 0)) + _client_side_judge_calls(cfg, cfg.plugins["evaluator"])
    est = cost.estimate(
        cfg,
        entry["rows"],
        _repeats(cfg, stage, repeats),
        n_llm,
        judge_only,
        cost.history(ledger),
    )
    return {
        "summary": f"estimated ${est['total_usd']} for {est['llm_calls']} LLM calls",
        "estimate": est,
        "budget": cost.check_budget(cfg, ledger, est, override=True),
        "plugins": reg.describe(),
    }


def _put_artifact(cfg, artifacts: dict[str, str], name: str, text: str) -> str:
    """Redact one artifact *payload* and remember its exact *stored* text; nothing is written here.

    This is the artifact redaction boundary (CONTRACT §5.1): the payload is redacted here, after
    `app_version`/`cfg_hash` were computed, so the manifest hashes the bytes that will be stored.
    Only run payloads pass through it (`evalops/cost_estimate.json`, both locks,
    `evalops/rows.jsonl`, the last one being line-delimited JSON and so taking the JSONL path);
    `evalops/manifest.json` is derived metadata and is written verbatim, never redacted.
    `evaluate` buffers every payload and writes them only once all are verified, so a `strict`
    refusal cannot leave a partial run behind. Returns the stored text.
    """
    stored = redact.enforce_artifact(cfg, name, text)
    artifacts[name] = stored
    return stored


def row_to_json(r: RowResult, max_payload_chars) -> dict:
    d = {k: v for k, v in dataclasses.asdict(r).items() if k not in ("trace", "usage")}
    d["usage"] = dataclasses.asdict(r.usage) if r.usage else None
    d["trace"] = r.trace.to_dict(max_payload_chars) if r.trace is not None else None
    return d


def rows_from_artifact(text, source: str = "evalops/rows.jsonl") -> list[RowResult]:
    """Parse a rows.jsonl artifact: v0 lines (no version fields) read fine, extra fields are ignored.

    `judge_models` is read additively (WP3b.5 C4): a v0 line has no judge identity and yields
    ``None``, while a current line round-trips the resolved identity of every declared judge.
    """
    rows = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError as exc:
            raise EvalOpsError(f"{source}: not valid JSON: {exc}", EXIT_RUNTIME) from exc
        formats.check_version(d, source)
        rows.append(
            RowResult(
                row_id=d["row_id"],
                inputs=d["inputs"],
                outputs=d.get("outputs"),
                expectations=d.get("expectations"),
                trace_id=d.get("trace_id"),
                scores=d.get("scores") or {},
                error=d.get("error"),
                trace=AgentTrace.from_dict(d.get("trace"), source=source),
                trace_metrics=d.get("trace_metrics") or {},
                row_key=d.get("row_key"),  # additive: v0 rows have no stable key
                judge_models=d.get("judge_models"),  # additive: v0 rows have no judge identity
            )
        )
    return rows


def _capturing(predict_fn, limits, stash):
    """For native evaluators (which run predict themselves): capture each call's trace by input."""
    if inspect.iscoroutinefunction(predict_fn):

        @functools.wraps(predict_fn)
        async def async_wrapped(**inputs):
            with trace_capture.row_capture(sha256(canonical(inputs))[:12], limits) as cap:
                try:
                    return await predict_fn(**inputs)
                finally:
                    stash[canonical(inputs)] = cap.trace()

        return async_wrapped

    @functools.wraps(predict_fn)
    def sync_wrapped(**inputs):
        with trace_capture.row_capture(sha256(canonical(inputs))[:12], limits) as cap:
            try:
                return predict_fn(**inputs)
            finally:
                stash[canonical(inputs)] = cap.trace()

    return sync_wrapped


def _post_score(rows, scorers):
    for r in rows:
        for s in scorers:
            try:
                r.scores.update(
                    call_scorer(s, inputs=r.inputs, outputs=r.outputs, expectations=r.expectations, trace=r.trace)
                )
            except Exception as e:  # noqa: BLE001
                r.error = (r.error + "; " if r.error else "") + f"{scorer_name(s)}: {type(e).__name__}: {e}"


def evaluate(
    cfg,
    dataset,
    stage,
    repeats=None,
    judge_only=False,
    source_group=None,
    allow_dirty=False,
    override_budget=False,
    dry_run=False,
    tool_mode=None,
    kind="eval",
    scorer_filter=None,
) -> dict:
    """Run one evaluation, bound to `cfg`'s redaction rules for the whole run.

    The binding is scoped (`redact.use`), so a failure -- including a `strict` refusal -- restores
    whatever redactor was active before instead of leaving this config's rules behind.
    """
    judges.load(cfg)  # exit 2 here, before any estimate/budget reservation/ledger row (WP3b.1)
    with redact.use(cfg):
        return _evaluate(
            cfg,
            dataset,
            stage,
            repeats=repeats,
            judge_only=judge_only,
            source_group=source_group,
            allow_dirty=allow_dirty,
            override_budget=override_budget,
            dry_run=dry_run,
            tool_mode=tool_mode,
            kind=kind,
            scorer_filter=scorer_filter,
        )


def _evaluate(
    cfg,
    dataset,
    stage,
    repeats=None,
    judge_only=False,
    source_group=None,
    allow_dirty=False,
    override_budget=False,
    dry_run=False,
    tool_mode=None,
    kind="eval",
    scorer_filter=None,
) -> dict:
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
    ledger = cast(Ledger, reg.get("ledger"))
    tracer = cast(Tracer, reg.get("tracer"))
    evaluator_plugin = reg.get("evaluator")
    evaluator = cast(Evaluator, evaluator_plugin)
    dstore = cast(DatasetStore, reg.get("datasets"))
    reps = _repeats(cfg, stage, repeats)
    meta = cfg.hook("judge_meta")()
    # WP3b.5 B3: `judge_meta()` may add metadata but never override a declared spec's pin. Exit 2
    # here, before any estimate, budget reservation or ledger row.
    judges.conflicts_with_meta(cfg, meta)
    # D2/WP3b.3: the estimate also pays for the declared judge specs that call out from this
    # process (the litellm fallback, or a native adapter that is not server-side).
    n_llm = int(meta.get("llm_judge_count", 0)) + _client_side_judge_calls(cfg, evaluator_plugin.name)
    frozen = bool((cfg.section("datasets").get(dataset) or {}).get("frozen"))
    records, dlock = ds_mod.load(cfg, reg, dataset, verify=frozen)
    est = cost.estimate(cfg, dlock["rows"], reps, n_llm, judge_only, cost.history(ledger))
    budget = cost.check_budget(cfg, ledger, est, override_budget)
    if dry_run:
        return {
            "summary": f"dry run: would spend ~${est['total_usd']}",
            "estimate": est,
            "budget": budget,
            "plugins": reg.describe(),
        }

    mode = _tool_mode(cfg, stage, tool_mode)
    cassette = tools_mod.Cassette(
        cfg.path("cassettes") / f"{dataset}.jsonl", mode, cfg.section("tools").get("on_miss", "error")
    )

    prompts, plock = pr_mod.load_locked(cfg, reg)
    full_cfg = {
        **cfg.hook("behavior_config")(),
        "prompts": {n: pr_mod.ref(e) for n, e in plock.items()},
        "judge": meta,
        "plugins": reg.describe(),
        "tool_cassette": cassette.fingerprint() if mode == "replay" else f"mode:{mode}",
    }
    cfg_hash = sha256(canonical(full_cfg))[:8]
    version = f"{cfg.data['project']}-{git_sha(cwd=cfg.root)}-{cfg_hash}"
    scorers = _build_scorers(cfg)
    # D1/WP3b.3: a declared judge spec is a real scorer. Appended after the app's own scorers (so
    # their order is untouched) and before the `--scorer` filter, which must be able to select one.
    # A name collision with an app scorer is exit 2 here -- before the budget reservation and the
    # ledger row below -- and a missing executor SDK is exit 6 at the same point.
    declared = judges.load(cfg)
    judge_identities = _judge_identities(declared)
    scorers = _with_spec_judges(cfg, scorers, evaluator=evaluator_plugin.name)
    if scorer_filter:
        scorers = [s for s in scorers if scorer_name(s) in scorer_filter]
    predict_fn = None if judge_only else cfg.hook("predict_fn")(prompts)
    native = getattr(evaluator_plugin, "name", "core") != "core"
    trace_scorers = [s for s in scorers if wants_trace(s)] if native else []
    native_scorers = [s for s in scorers if s not in trace_scorers]
    limits = dict(cfg.section("eval").get("row_limits") or {})
    max_payload = cfg.section("traces").get("max_payload_chars", 4000)

    stored = None
    if kind == "calibration":  # pre-recorded outputs/traces in the dataset itself
        keys = ds_mod.row_keys(records)
        stored = [
            RowResult(
                f"{i:05d}",
                r["inputs"],
                r.get("outputs"),
                r.get("expectations"),
                trace=AgentTrace.from_dict(r.get("trace"), source=f"dataset:{dataset}"),
                row_key=keys[i],
            )
            for i, r in enumerate(records)
        ]
    elif judge_only:
        src_runs = ledger.find_runs(tags={"evalops.group": source_group}, limit=1)
        if not src_runs:
            raise EvalOpsError(f"no runs for source group {source_group}", EXIT_USAGE)
        stored = rows_from_artifact(ledger.get_artifact(src_runs[0].run_id, "evalops/rows.jsonl"))

    group = uuid.uuid4().hex[:12]
    runs: list[dict] = []
    promotable = not dirty and stage in ("main", "nightly") and kind == "eval"
    concurrency = cfg.section("eval")
    for i in range(reps):
        name = f"{stage}-{version}-{dataset}-r{i + 1}"
        run_id = ledger.start_run(
            name,
            {
                "evalops.group": group,
                "evalops.stage": stage,
                "evalops.repeat": str(i + 1),
                "evalops.kind": kind,
                "evalops.app_version": version,
                "evalops.promotable": str(promotable).lower(),
                "evalops.env": cfg.env,
                "evalops.budget_override": str(bool(budget["problems"]) and override_budget).lower(),
                "evalops.actor": os.getenv("EVALOPS_ACTOR", "unknown"),  # ci | human:<id> | agent:<name>
            },
        )
        status = "FAILED"
        artifacts: dict[str, str] = {}
        pending: list[tuple[str, str]] = []
        month = cost.utc_month(cost._now_ms())
        month_cap = cfg.section("cost").get("budgets", {}).get("per_month_usd")
        month_limit = float(month_cap) if month_cap is not None else None
        run_estimate = est["total_usd"] / max(reps, 1)
        reserved = False
        try:
            if not ledger.budget_reserve(run_id, run_estimate, month, month_limit):
                info = {
                    "month": month,
                    "reserved_usd": ledger.budget_reserved(month),
                    "limit_usd": month_limit,
                    "estimate_usd": est["total_usd"],
                    "run_estimate_usd": run_estimate,
                }
                raise EvalOpsError(
                    f"budget exceeded: monthly reservation for {month} would exceed per_month_usd {month_limit}",
                    EXIT_BUDGET,
                    info,
                )
            reserved = True
            ledger.log(
                run_id,
                params={
                    "app_version": version,
                    "config_hash": cfg_hash,
                    "git_sha": git_sha(short=False, cwd=cfg.root),
                    "git_dirty": dirty,
                    "dataset": dataset,
                    "dataset_physical": dlock["physical"],
                    "dataset_fingerprint": dlock["fingerprint"],
                    "dataset_rows": dlock["rows"],
                    "source_group": source_group or "",
                    "tool_mode": mode,
                    "tool_cassette": cassette.fingerprint(),
                    # additive: which writer/flavor produced this run (readers default to v0 when absent)
                    "evalops_version": formats.evalops_version(),
                    "format_version": formats.FORMAT_VERSION,
                    **{f"plugin.{k}": v for k, v in reg.describe().items()},
                    **{f"prompt.{n}": pr_mod.ref(e) for n, e in plock.items()},
                    **{f"judge.{k}": v for k, v in meta.items()},
                    # WP3b.5 C5: the resolved identity of every declared spec is recorded *on the
                    # run*, beside the existing `judge.*` app metadata (additive keys).
                    **{f"judge.{name}.model": identity["model"] for name, identity in judge_identities.items()},
                    **{
                        f"judge.{name}.prompt_version": identity["prompt_version"]
                        for name, identity in judge_identities.items()
                    },
                    "behavior_config": canonical(full_cfg)[:4000],
                },
            )
            # Buffer the metadata artifacts: nothing is written until every payload has been
            # redacted and verified (CONTRACT §5.1, strict is atomic). The manifest (staged after the
            # rows) hashes exactly these *stored* texts.
            for name, body in (
                (cost.ESTIMATE_ARTIFACT, json.dumps(formats.stamp(est), sort_keys=True)),
                ("evalops/prompts.lock.json", json.dumps(locks.document(plock), sort_keys=True)),
                (
                    "evalops/datasets.lock.json",
                    json.dumps(locks.document(locks.read(cfg, ds_mod.KIND)), sort_keys=True),
                ),
            ):
                pending.append((name, _put_artifact(cfg, artifacts, name, body)))

            stash: dict = {}
            fn = predict_fn
            if native and predict_fn is not None:
                fn = _capturing(predict_fn, limits, stash)
            with tools_mod.use(cassette):
                result = evaluator.evaluate(
                    records=records,
                    dataset_physical=dlock["physical"],
                    dataset_store=dstore,
                    predict_fn=fn,
                    scorers=native_scorers if native else scorers,
                    tracer=tracer,
                    run_id=run_id,
                    run_name=name,
                    concurrency=concurrency,
                    stored_rows=stored,
                )
            if native:  # attach captured traces, then run trace-aware scorers ourselves
                for r in result.rows:
                    if r.trace is None:
                        r.trace = stash.get(canonical(r.inputs))
                    if r.trace is not None:
                        r.trace_metrics = r.trace.metrics()
                _post_score(result.rows, trace_scorers)
                result.metrics = {**result.metrics, **aggregate_rows(result.rows)}

            # WP3b.5 C3: stamp the resolved identity of each declared spec per verdict (both the
            # native-scorer path and the `_post_score` trace-scorer path converge here), then enforce
            # the contract: a verdict without a resolved model is exit 5, never a silent null.
            # `--judge-only` re-scores from stored rows and is re-stamped from the current specs, so
            # a stale value can never survive.
            _stamp_judge_identities(result.rows, declared)

            rows_text = "".join(
                json.dumps(formats.stamp(row_to_json(r, max_payload)), default=str) + "\n" for r in result.rows
            )
            pending.append(("evalops/rows.jsonl", _put_artifact(cfg, artifacts, "evalops/rows.jsonl", rows_text)))
            # Derived metadata, staged last: the manifest hashes the *stored* texts above and is
            # written verbatim. It is never redacted and never strict-rescanned -- a rule matching a
            # digest (e.g. `[0-9a-f]{64}`) or the `{"artifacts": {...}}` shape would otherwise
            # rewrite the hashes and silently break the integrity of what was stored.
            manifest_text = json.dumps(formats.manifest(artifacts))
            pending.append(("evalops/manifest.json", manifest_text))
            # `save()` is verified independently; running it before the artifact loop means its own
            # strict refusal still leaves no artifact, and a rows refusal never writes a cassette.
            if mode == "record":
                cassette.save()
            for name, stored_text in pending:
                ledger.put_artifact(run_id, name, stored_text)
            rows_n = max(dlock["rows"], 1)
            measured = cost.measure(cfg, result.rows) if stored is None else cost.measure(cfg, [])
            judge_usd = round(rows_n * n_llm * cost.judge_cost_per_call(cfg), 6)
            total = round(measured["gen_cost_usd"] + judge_usd, 6)
            cost_metrics = {
                "gen_input_tokens": measured["gen_input_tokens"],
                "gen_output_tokens": measured["gen_output_tokens"],
                "gen_input_tokens_per_row": measured["gen_input_tokens"] / rows_n,
                "gen_output_tokens_per_row": measured["gen_output_tokens"] / rows_n,
                "gen_cost_usd": measured["gen_cost_usd"],
                "judge_cost_usd_est": judge_usd,
                "total_cost_usd": total,
                "cost_per_row_usd": total / rows_n,
                "rows_missing_usage": measured["rows_missing_usage"],
                "tool_replay_misses": len(cassette.misses),
            }
            ledger.log(
                run_id,
                metrics={**result.metrics, **cost_metrics},
                tags={
                    "evalops.gen_cost_source": measured["gen_cost_source"],
                    **({"evalops.native_ref": result.native_ref} if result.native_ref else {}),
                },
            )
            ledger.budget_settle(run_id, total)
            reserved = False
            runs.append(
                {
                    "run_id": run_id,
                    "metrics": {**result.metrics, **cost_metrics},
                    "errors": sum(1 for r in result.rows if r.error),
                    "native_ref": result.native_ref,
                    "rows": result.rows,
                }
            )
            status = "FINISHED"
        finally:
            if reserved:
                with contextlib.suppress(Exception):  # release must never mask the run's original failure
                    ledger.budget_release(run_id)
            ledger.end_run(run_id, status)

    actual = round(sum(r["metrics"]["total_cost_usd"] for r in runs), 4)
    out = {
        "summary": f"{stage} {kind} of {version} on {dataset}: {reps} run(s), "
        f"${actual} actual vs ${est['total_usd']} est",
        "group": group,
        "app_version": version,
        "stage": stage,
        "dataset": dataset,
        "env": cfg.env,
        "kind": kind,
        "tool_mode": mode,
        "plugins": reg.describe(),
        "run_ids": [r["run_id"] for r in runs],
        "runs": [{k: v for k, v in r.items() if k != "rows"} for r in runs],
        "cost": {"estimate_usd": est["total_usd"], "actual_usd": actual, "budget": budget},
    }
    if cassette.misses:
        out["tool_replay_misses"] = cassette.misses[:20]
    if kind == "eval":
        formats.write_versioned(locks.scratch(cfg, "last_eval.json"), out)
    out["_rows"] = [r["rows"] for r in runs]
    return out


def baseline(cfg) -> dict:
    runs = cast(Ledger, Registry(cfg).get("ledger")).find_runs(tags={"evalops.baseline": "true"}, limit=1)
    if not runs:
        return {"summary": "no production baseline yet", "baseline": None}
    r = runs[0]
    return {
        "summary": f"baseline {r.tags.get('evalops.app_version')}",
        "baseline": {
            "group": r.tags.get("evalops.group"),
            "git_sha": r.params.get("git_sha"),
            "app_version": r.tags.get("evalops.app_version"),
            "run_id": r.run_id,
        },
    }


def show_traces(cfg, group, row=None, as_tree=True, unknown_attrs=False) -> dict:
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    runs = ledger.find_runs(tags={"evalops.group": group}, limit=1)
    if not runs:
        raise EvalOpsError(f"no runs for group {group}", EXIT_USAGE)
    out = []
    aggregate: dict[str, int] = {}
    for r in rows_from_artifact(ledger.get_artifact(runs[0].run_id, "evalops/rows.jsonl")):
        if row and r.row_id != row:
            continue
        item = {
            "row_id": r.row_id,
            "inputs": r.inputs,
            "scores": r.scores,
            "error": r.error,
            "trace_metrics": r.trace_metrics,
            "tree": r.trace.render() if r.trace else None,
        }
        if unknown_attrs:
            # Additive, opt-in: the default payload stays byte-identical (the envelope golden).
            per_trace = r.trace.unknown_attributes() if r.trace else {}
            item["unknown_attributes"] = sorted(per_trace)
            for name, count in per_trace.items():
                aggregate[name] = aggregate.get(name, 0) + count
        out.append(item)
    data: dict[str, Any] = {"summary": f"{len(out)} row(s) from run {runs[0].run_id}", "rows": out}
    if unknown_attrs:
        data["unknown_attributes"] = dict(sorted(aggregate.items()))
    return data
