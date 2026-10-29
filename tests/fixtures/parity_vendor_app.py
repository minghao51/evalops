"""Fixture vendor plugins for ``tests/unit/test_parity.py`` (WP3.0): an evaluator that is NOT core.

A vendor evaluator runs ``predict_fn`` itself and traces rows natively. It has no evalops per-row
usage collector and no evalops trace capture, and it reports a per-row failure in its own words --
exactly the shape WP3.0 makes parity-safe: ``error_rate``/``budget_exceeded_rate`` emission,
``evalops.report_usage`` harvesting and the configured tracer's usage backfill all have to happen in
``run.py`` for it.

Installed the way every ``module:Class`` plugin is (``_projects.install_ledger``'s pattern): copied
into a temp project by the test and imported under a process-unique module name, then named in that
project's ``evalops.yaml``::

    plugins: {..., tracer: "<name>:RecordingTracer", evaluator: "<name>:VendorEvaluator"}

Per-test knobs live in :data:`STATE` (:func:`reset` before every test), so nothing is shared between
tests and no vendor SDK is involved. The row loop is deliberately sequential: the last-write-wins
usage semantics this fixture pins are only checkable with a deterministic order.
"""

from evalops.ports import EvalResult, Evaluator, Plugin, RowResult, Tracer, Usage

#: The knobs of one test run. Keys:
#:   ``errors``    {row_id: message}   the failure the vendor reports for that row
#:   ``trace_ids`` {row_id: trace id}  what the tracer reports for that row
#:   ``retries``   int                 vendor-side extra predict attempts after an exception
#:   ``usage``     {trace id: Usage}   what the tracer can backfill
#:   ``usage_calls`` list of the id lists ``RecordingTracer.usage`` was asked for
#:   ``wrapped``   the row ids the tracer's ``wrap`` saw
#:   ``run_names`` the ``run_name`` the run layer handed the vendor evaluator (WP3.2a H1)
STATE: dict = {}


def reset(**knobs) -> dict:
    """Replace every knob (never merge): a test can never inherit another test's leftovers."""
    STATE.clear()
    STATE.update(
        {
            "errors": {},
            "trace_ids": {},
            "retries": 0,
            "usage": {},
            "usage_calls": [],
            "wrapped": [],
            "run_names": [],
        }
    )
    STATE.update(knobs)
    return STATE


def _predict(predict_fn, inputs):
    """One vendor-side retry loop: ``STATE["retries"]`` extra attempts after an exception."""
    attempts = max(STATE["retries"] + 1, 1)
    error = None
    for attempt in range(attempts):
        try:
            return predict_fn(**inputs), None
        except Exception as e:  # noqa: BLE001
            error = f"predict: {type(e).__name__}: {e}"
            if attempt == attempts - 1:
                break
    return None, error


def _run_rows(records, predict_fn):
    """The vendor row loop: build a ``RowResult`` per record, let the vendor report the failures."""
    rows = []
    for i, record in enumerate(records):
        row = RowResult(
            row_id=f"{i:05d}",
            inputs=record["inputs"],
            expectations=record.get("expectations"),
            trace_id=STATE["trace_ids"].get(f"{i:05d}"),
        )
        error = STATE["errors"].get(row.row_id)
        if error:  # a vendor backend reporting a failed prediction is not a successful row
            row.error = error
        elif predict_fn is not None:
            row.outputs, row.error = _predict(predict_fn, row.inputs)
        rows.append(row)
    return rows


def _record_run_name(run_name):
    """Record the run name the run layer passed -- it must be the ledger name, never an artifact."""
    STATE["run_names"].append(run_name)


class VendorEvaluator(Plugin, Evaluator):
    """A vendor evaluator: it runs predict itself, row by row, and reports failures its own way."""

    name = "parityvendor"
    provides = ("evaluator",)

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
        _record_run_name(run_name)
        # `stored_rows` is the judge-only path; this fixture only needs the record loop.
        return EvalResult(rows=_run_rows(records, predict_fn), metrics={})


class SelfTracingEvaluator(Plugin, Evaluator, Tracer):
    """A vendor evaluator that also serves the tracer role (the mlflow plugin's shape, ADR 0008).

    It owns row tracing, so a separately configured tracer is never asked to do anything. ``doctor``
    reads ``self_tracing``; this class is what makes the warning reachable in a test.
    """

    name = "parityself"
    provides = ("evaluator", "tracer")

    def wrap(self, fn, *, run_id, row_id):
        def traced(**inputs):
            return fn(**inputs), STATE["trace_ids"].get(row_id)

        return traced

    def usage(self, trace_ids):
        STATE["usage_calls"].append(list(trace_ids))
        return {tid: STATE["usage"][tid] for tid in trace_ids if tid in STATE["usage"]}

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
        _record_run_name(run_name)
        return EvalResult(rows=_run_rows(records, predict_fn), metrics={})


class RecordingTracer(Plugin, Tracer):
    """A configured tracer that records every ``usage()`` lookup and answers from ``STATE``."""

    name = "paritytracer"
    provides = ("tracer",)

    def wrap(self, fn, *, run_id, row_id):
        STATE["wrapped"].append(row_id)

        def traced(**inputs):
            return fn(**inputs), STATE["trace_ids"].get(row_id)

        return traced

    def usage(self, trace_ids):
        STATE["usage_calls"].append(list(trace_ids))
        return {tid: STATE["usage"][tid] for tid in trace_ids if tid in STATE["usage"]}


__all__ = ["STATE", "RecordedUsage", "RecordingTracer", "SelfTracingEvaluator", "VendorEvaluator", "reset"]

#: Re-exported so a test can build tracer answers without importing `evalops.ports` itself.
RecordedUsage = Usage
