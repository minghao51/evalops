"""mlflow plugin (MLflow >= 3.2): all five roles. Works with local servers and Databricks.

options: tracking_uri, registry_uri (e.g. databricks-uc), experiment, namespace (e.g. catalog.schema)
API details verified against MLflow 3.x docs; re-verify on upgrades (unverified details are
beta-marked: see ``evalops.beta``).
"""

import os
import time
from typing import Any

from ..ports import (
    DatasetStore,
    EvalResult,
    Evaluator,
    Ledger,
    LoadedPrompt,
    Plugin,
    PromptStore,
    RowResult,
    RunRecord,
    Tracer,
    Usage,
)
from ..scoring import call_scorer, scorer_name, to_float
from .core import CorePlugin


def _trace_cost(info):
    c = getattr(info, "cost", None)  # MLflow >= 3.10 with a [genai]-enabled server
    if isinstance(c, (int, float)):
        return float(c)
    if isinstance(c, dict):
        for k in ("total_cost", "total", "total_cost_usd"):
            if isinstance(c.get(k), (int, float)):
                return float(c[k])
    return None


# VERIFY-BETA(mlflow-result-df): which `result_df` column carries a per-row predict failure is
# version-dependent,
# so every candidate below is probed only when that column is actually present in the frame -- no
# column name is invented beyond this list. A message column's non-empty value is the failure text;
# a status column counts as failed only when its value is a string outside the success set (or the
# boolean `False`). Anything else (a number, a null, a missing column) is not read as a failure.
_FAILURE_MESSAGE_COLUMNS = ("error", "error_message", "failure", "failure_message")
_FAILURE_STATUS_COLUMNS = ("status",)
_SUCCESS_STATUSES = frozenset({"ok", "success", "succeeded", "passed", "finished", "completed", "done"})


def _row_failure(columns, get) -> str | None:
    """The per-row predict failure this `result_df` frame reports, or ``None`` (WP3.0 A2).

    ``get`` is the frame row's lookup (``row.get``). Without a failure/status column -- the common
    case in the versions verified so far -- nothing is invented: the row is clean.
    """
    for column in columns:  # a message column wins over a generic status
        if str(column) in _FAILURE_MESSAGE_COLUMNS:
            value = get(column)
            if value is not None and str(value).strip():
                return str(value)
    for column in columns:
        if str(column) in _FAILURE_STATUS_COLUMNS:
            value = get(column)
            if isinstance(value, bool) and not value:
                return "failed"
            if isinstance(value, str) and value.strip().lower() not in _SUCCESS_STATUSES:
                return value
    return None


class MlflowPlugin(Plugin, PromptStore, DatasetStore, Ledger, Tracer, Evaluator):
    name = "mlflow"
    provides = ("prompts", "datasets", "ledger", "tracer", "evaluator")
    # ADR 0008: mlflow.genai traces the rows itself, so a separately configured tracer is unused.
    self_tracing = True

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        import mlflow  # pyright: ignore[reportMissingImports]

        self.mlflow = mlflow
        if uri := options.get("tracking_uri"):
            mlflow.set_tracking_uri(uri)
        if reg := options.get("registry_uri"):
            mlflow.set_registry_uri(reg)
        self.exp_id = mlflow.set_experiment(options.get("experiment", cfg.data["project"])).experiment_id
        self.client = mlflow.MlflowClient()

    def doctor(self):
        return {
            "plugin": self.name,
            "ok": True,
            "tracking_uri": self.mlflow.get_tracking_uri(),
            "experiment_id": self.exp_id,
            "mlflow_version": self.mlflow.__version__,
        }

    # ---------------- prompts ----------------
    def register(self, name, template, tags):
        pv = self.mlflow.genai.register_prompt(
            name=name, template=template, commit_message=f"evalops {tags.get('evalops.git_sha', '')}", tags=tags
        )
        return str(pv.version)

    def load(self, name, version):
        pv = self.mlflow.genai.load_prompt(f"prompts:/{name}/{version}")
        return LoadedPrompt(name=name, version=str(version), template=pv.template, native=pv)

    def get_alias(self, name, alias):
        try:
            return str(self.mlflow.genai.load_prompt(f"prompts:/{name}@{alias}").version)
        except Exception:  # noqa: BLE001
            return None

    def set_alias(self, name, alias, version):
        self.mlflow.genai.set_prompt_alias(name=name, alias=alias, version=int(version))

    # ---------------- datasets ----------------
    def _ds(self):
        from mlflow.genai import datasets  # pyright: ignore[reportMissingImports]

        return datasets

    def exists(self, physical):
        try:
            self._ds().get_dataset(name=physical)
            return True
        except Exception:  # noqa: BLE001
            return False

    def write(self, physical, records):
        d = self._ds()
        # `Any`: the datasets module handle, so a failed create leaves no narrowed-away type behind.
        ds: Any = None
        for kwargs in (
            {"name": physical, "experiment_id": self.exp_id},
            {"name": physical},
            {"uc_table_name": physical},
        ):  # VERIFY-BETA(mlflow-dataset-api): signature varies by version/backend
            try:
                ds = d.create_dataset(**kwargs)
                break
            except TypeError:
                continue
        ds.merge_records(records)

    def read(self, physical):
        df = self._ds().get_dataset(name=physical).to_df()  # VERIFY-BETA(mlflow-dataset-api): to_df on upgrades
        out = []
        for _, row in df.iterrows():
            exp = row["expectations"] if "expectations" in df.columns else None
            out.append({"inputs": row["inputs"], **({"expectations": exp} if isinstance(exp, dict) and exp else {})})
        return out

    def native(self, physical):
        return self._ds().get_dataset(name=physical)

    # ---------------- ledger ----------------
    def start_run(self, name, tags):
        return self.client.create_run(self.exp_id, run_name=name, tags=tags).info.run_id

    def end_run(self, run_id, status="FINISHED"):
        self.client.set_terminated(run_id, status=status)

    def log(self, run_id, params=None, metrics=None, tags=None):
        from mlflow.entities import Metric, Param, RunTag  # pyright: ignore[reportMissingImports]

        ts = int(time.time() * 1000)
        self.client.log_batch(
            run_id,
            metrics=[Metric(k, float(v), ts, 0) for k, v in (metrics or {}).items() if isinstance(v, (int, float))],
            params=[Param(k, str(v)[:500]) for k, v in (params or {}).items()],
            tags=[RunTag(k, str(v)) for k, v in (tags or {}).items()],
        )

    def put_artifact(self, run_id, name, text):
        self.client.log_text(run_id, text, name)

    def get_artifact(self, run_id, name):
        return self.mlflow.artifacts.load_text(f"runs:/{run_id}/{name}")

    def find_runs(self, tags=None, since_ms=None, limit=None):
        clauses = [f"tags.`{k}` = '{v}'" for k, v in (tags or {}).items()]
        if since_ms is not None:
            clauses.append(f"attributes.start_time >= {int(since_ms)}")
        out, token = [], None
        while True:
            page = self.client.search_runs(
                [self.exp_id],
                " and ".join(clauses),
                order_by=["attributes.start_time DESC"],
                max_results=min(limit or 1000, 1000),
                page_token=token,
            )
            out += [
                RunRecord(
                    r.info.run_id, dict(r.data.tags), dict(r.data.params), dict(r.data.metrics), int(r.info.start_time)
                )
                for r in page
            ]
            token = getattr(page, "token", None)
            if not token or (limit and len(out) >= limit):
                return out[:limit] if limit else out

    # ---------------- tracer ----------------
    def wrap(self, fn, *, run_id, row_id):
        mlflow = self.mlflow

        def traced(**inputs):
            with mlflow.start_span(
                name="evalops.row", attributes={"evalops.run_id": run_id, "evalops.row_id": row_id}
            ) as span:
                span.set_inputs(inputs)
                out = fn(**inputs)
                span.set_outputs(out)
                return out, getattr(span, "trace_id", None) or getattr(span, "request_id", None)

        return traced

    def usage(self, trace_ids):
        found = {}
        for tid in trace_ids:
            try:
                info = self.mlflow.get_trace(tid).info
            except Exception:  # noqa: BLE001
                continue
            u = getattr(info, "token_usage", None) or {}
            if u or _trace_cost(info) is not None:
                found[tid] = Usage(int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0), _trace_cost(info))
        return found

    def flush(self):
        if hasattr(self.mlflow, "flush_trace_async_logging"):
            self.mlflow.flush_trace_async_logging()

    # ---------------- evaluator (native mlflow.genai.evaluate) ----------------
    def _to_mlflow_scorers(self, scorers):
        from mlflow.genai.scorers import Scorer, scorer as mlflow_scorer  # pyright: ignore[reportMissingImports]

        out = []
        for s in scorers:
            if isinstance(s, Scorer):
                out.append(s)
                continue
            name = scorer_name(s)

            def fn(inputs, outputs, expectations, _s=s):
                res = call_scorer(_s, inputs=inputs, outputs=outputs, expectations=expectations)
                return next(iter(res.values()), None)

            fn.__name__ = name
            try:
                out.append(mlflow_scorer(name=name)(fn))
            except TypeError:  # older decorator without name=
                out.append(mlflow_scorer(fn))
        return out

    def evaluate(
        self,
        *,
        records,
        dataset_physical,
        dataset_store,
        predict_fn,
        scorers,
        tracer,
        run_id,
        run_name,
        concurrency,
        stored_rows=None,
    ):
        if stored_rows is not None:  # judge-only: core re-scores stored outputs with any scorer type
            return CorePlugin(self.cfg, {}).evaluate(
                records=records,
                dataset_physical=dataset_physical,
                dataset_store=dataset_store,
                predict_fn=None,
                scorers=scorers,
                tracer=self,
                run_id=run_id,
                run_name=run_name,
                concurrency=concurrency,
                stored_rows=stored_rows,
            )
        os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_WORKERS", str(concurrency.get("max_workers", 4)))
        os.environ.setdefault("MLFLOW_GENAI_EVAL_MAX_SCORER_WORKERS", str(concurrency.get("max_scorer_workers", 2)))
        data = dataset_store.native(dataset_physical) if dataset_store is self else records
        same_ledger = self.cfg.plugins.get("ledger") == "mlflow"
        run_ctx = self.mlflow.start_run(run_id=run_id) if same_ledger else self.mlflow.start_run(run_name=run_name)
        with run_ctx as active:
            res = self.mlflow.genai.evaluate(data=data, predict_fn=predict_fn, scorers=self._to_mlflow_scorers(scorers))
        rows = []
        df = getattr(res, "result_df", None)  # VERIFY-BETA(mlflow-result-df): column names across versions
        if df is not None:
            for i, (_, r) in enumerate(df.iterrows()):
                get = r.get if hasattr(r, "get") else (lambda k, d=None: d)
                scores = {c[: -len("/value")]: to_float(r[c]) for c in df.columns if str(c).endswith("/value")}
                # WP3.0 A2: a per-row failure/status column, when present, becomes `row.error` (so
                # the run layer's `error_rate` is truthful). A row whose prediction failed has no
                # output to score, which is the core evaluator's score-skip rule.
                failure = _row_failure(df.columns, get)
                outputs = get("outputs", get("response"))
                rows.append(
                    RowResult(
                        row_id=f"{i:05d}",
                        inputs=get("inputs", get("request")) or {},
                        outputs=outputs,
                        expectations=get("expectations"),
                        scores={k: v for k, v in scores.items() if v is not None}
                        if not (failure and outputs is None)
                        else {},
                        trace_id=get("trace_id"),
                        error=f"predict: {failure}" if failure else None,
                    )
                )
            usage = self.usage([r.trace_id for r in rows if r.trace_id])
            for r in rows:
                r.usage = usage.get(r.trace_id)
        return EvalResult(rows=rows, metrics=dict(res.metrics), native_ref=f"mlflow-run:{active.info.run_id}")
