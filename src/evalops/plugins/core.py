"""core plugin: backend-agnostic evaluator + 'none' tracer.

The core evaluator runs predict_fn and scorers itself with bounded concurrency, retries with
exponential backoff + jitter, an optional requests-per-minute limiter, and async predict support.
It works with ANY tracer plugin (MLflow, Langfuse, LangSmith, Logfire, none) and any scorer that
accepts (inputs, outputs, expectations) keyword arguments, including MLflow's built-in judges.
"""
import asyncio
import inspect
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from ..ports import EvalResult, Evaluator, Plugin, RowResult, Tracer, Usage
from ..scoring import aggregate_rows, call_scorer, scorer_name
from ..tools import ToolReplayMiss
from ..traces import capture as trace_capture
from ..traces.capture import BudgetExceeded
from ..traces.instrument import instrument
from ..usage import collect

NO_RETRY = (BudgetExceeded, ToolReplayMiss)


class _RateLimiter:
    def __init__(self, rpm: int | None):
        self.interval = 60.0 / rpm if rpm else 0.0
        self.lock, self.next_at = threading.Lock(), 0.0

    def wait(self):
        if not self.interval:
            return
        with self.lock:
            now = time.monotonic()
            delay = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if delay:
            time.sleep(delay)


def _syncify(fn, timeout):
    if not inspect.iscoroutinefunction(fn):
        return fn

    def run(**kwargs):
        return asyncio.run(asyncio.wait_for(fn(**kwargs), timeout))
    return run


def _retry(call, retries, backoff, limiter):
    for attempt in range(retries + 1):
        try:
            limiter.wait()
            return call()
        except NO_RETRY:
            raise
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(backoff * (2 ** attempt) + random.uniform(0, backoff))


class CorePlugin(Plugin, Tracer, Evaluator):
    name = "core"
    provides = ("tracer", "evaluator")

    # ---- 'none' tracer ----
    def wrap(self, fn, *, run_id, row_id):
        def traced(**inputs):
            return fn(**inputs), None
        return traced

    # ---- evaluator ----
    def evaluate(self, *, records, dataset_physical, dataset_store, predict_fn, scorers, tracer, run_id,
                 run_name, concurrency, stored_rows=None):
        workers = int(concurrency.get("max_workers", 4))
        scorer_workers = max(1, min(int(concurrency.get("max_scorer_workers", 2)), len(scorers) or 1))
        retries = int(concurrency.get("retries", 3))
        backoff = float(concurrency.get("backoff_seconds", 1.0))
        limiter = _RateLimiter(concurrency.get("requests_per_minute"))
        limits = dict(concurrency.get("row_limits") or {})
        timeout = float(limits.get("max_seconds") or concurrency.get("async_timeout", 300))
        predict = _syncify(predict_fn, timeout) if predict_fn else None
        tcfg = self.cfg.section("traces")
        if tcfg.get("capture", True):
            trace_capture.install_otel()       # no-op when OpenTelemetry isn't installed
            instrument(tcfg.get("instrument") or [])

        def score(row: RowResult):
            def one(s):
                try:
                    return _retry(lambda: call_scorer(s, inputs=row.inputs, outputs=row.outputs,
                                                      expectations=row.expectations, trace=row.trace),
                                  retries, backoff, limiter)
                except Exception as e:  # noqa: BLE001
                    return {"__error__": f"{scorer_name(s)}: {type(e).__name__}: {e}"}
            with ThreadPoolExecutor(scorer_workers) as pool:
                for res in pool.map(one, scorers):
                    if "__error__" in res:
                        row.error = (row.error + "; " if row.error else "") + res["__error__"]
                    else:
                        row.scores.update(res)
            return row

        def run_row(item):
            i, rec = item
            row = RowResult(row_id=f"{i:05d}", inputs=rec["inputs"], expectations=rec.get("expectations"))
            traced = tracer.wrap(predict, run_id=run_id, row_id=row.row_id)
            caps = []

            def attempt():  # fresh capture per attempt: a failed attempt's spans never pollute the trace
                with trace_capture.row_capture(row.row_id, limits, {"evalops.run_id": run_id}) as cap, \
                        collect() as usage:
                    caps.append((cap, usage))
                    return traced(**row.inputs)

            try:
                row.outputs, row.trace_id = _retry(attempt, retries, backoff, limiter)
            except Exception as e:  # noqa: BLE001
                kind = "budget_exceeded" if isinstance(e, BudgetExceeded) else "predict"
                row.error = f"{kind}: {type(e).__name__}: {e}"
            if caps:
                cap, usage = caps[-1]
                row.trace = cap.trace()
                if usage.reported:
                    row.usage = Usage(usage.input_tokens, usage.output_tokens, usage.cost_usd)
                if cap.exceeded and not row.error:  # frameworks that can't be aborted: fail post hoc
                    row.error = f"budget_exceeded: {cap.exceeded}"
            if row.trace is not None:
                row.trace_metrics = row.trace.metrics()
            if row.error and row.outputs is None:
                return row
            return score(row)

        with ThreadPoolExecutor(workers) as pool:
            if stored_rows is not None:  # judge-only: re-score stored outputs, no generation
                rows = list(pool.map(score, [RowResult(r.row_id, r.inputs, r.outputs, r.expectations,
                                                       trace_id=r.trace_id, trace=r.trace,
                                                       trace_metrics=dict(r.trace_metrics))
                                             for r in stored_rows]))
            else:
                rows = list(pool.map(run_row, enumerate(records)))
        tracer.flush()

        missing = [r.trace_id for r in rows if r.trace_id and r.usage is None
                   and not (r.trace is not None and any(r.trace.tokens()))]
        if missing:
            found = tracer.usage(missing)
            for r in rows:
                if r.usage is None and r.trace_id in found:
                    r.usage = found[r.trace_id]
        metrics = aggregate_rows(rows)
        metrics["error_rate"] = sum(1 for r in rows if r.error) / max(len(rows), 1)
        metrics["budget_exceeded_rate"] = sum(1 for r in rows if (r.error or "").startswith("budget_exceeded")) \
            / max(len(rows), 1)
        return EvalResult(rows=rows, metrics=metrics)
