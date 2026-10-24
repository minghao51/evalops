"""langfuse plugin (Python SDK v3+; docs current as of SDK v4/v5): prompts, datasets, tracer, evaluator.

Credentials via LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_BASE_URL (or LANGFUSE_HOST).
No ledger: pair with plugins.ledger = sql (or mlflow).
Notes: prompt versions are immutable; labels are pointers (one version per label). update_prompt's
new_labels REPLACES the labels on a version, so set_alias keeps the version's existing labels.
Methods marked VERIFY are best-effort against SDK changes.
"""

import hashlib
import time

from ..ports import DatasetStore, EvalResult, Evaluator, LoadedPrompt, Plugin, PromptStore, RowResult, Tracer, Usage
from ..scoring import call_scorer, to_float
from ..util import canonical
from .core import CorePlugin


class LangfusePlugin(Plugin, PromptStore, DatasetStore, Tracer, Evaluator):
    name = "langfuse"
    provides = ("prompts", "datasets", "tracer", "evaluator")

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        from langfuse import get_client

        self.lf = get_client()

    def doctor(self):
        ok = bool(getattr(self.lf, "auth_check", lambda: True)())
        return {"plugin": self.name, "ok": ok}

    # ---------------- prompts ----------------
    def register(self, name, template, tags):
        p = self.lf.create_prompt(
            name=name,
            prompt=template,
            type="chat" if isinstance(template, list) else "text",
            labels=[],
            config={"evalops": tags},
        )
        return str(p.version)

    def _get(self, name, **kw):
        try:
            return self.lf.get_prompt(name, type="text", cache_ttl_seconds=0, **kw)
        except Exception:  # noqa: BLE001  chat prompts must be fetched with type="chat"
            return self.lf.get_prompt(name, type="chat", cache_ttl_seconds=0, **kw)

    def load(self, name, version):
        p = self._get(name, version=int(version))
        return LoadedPrompt(name=name, version=str(version), template=p.prompt, native=p)

    def get_alias(self, name, alias):
        try:
            return str(self._get(name, label=alias).version)
        except Exception:  # noqa: BLE001  404 when no version carries the label
            return None

    def set_alias(self, name, alias, version):
        current = set(getattr(self._get(name, version=int(version)), "labels", None) or [])
        labels = sorted((current | {alias}) - {"latest"})
        self.lf.update_prompt(name=name, version=int(version), new_labels=labels)

    # ---------------- datasets ----------------
    def exists(self, physical):
        try:
            self.lf.get_dataset(physical)
            return True
        except Exception:  # noqa: BLE001
            return False

    def write(self, physical, records):
        self.lf.create_dataset(name=physical, metadata={"created_by": "evalops"})
        for r in records:  # stable ids make retries idempotent
            item_id = hashlib.sha256(f"{physical}:{canonical(r)}".encode()).hexdigest()[:32]
            self.lf.create_dataset_item(
                dataset_name=physical, id=item_id, input=r["inputs"], expected_output=r.get("expectations")
            )
        self.lf.flush()

    def read(self, physical):
        ds = self.lf.get_dataset(physical)
        out = []
        for it in ds.items:
            if str(getattr(it, "status", "ACTIVE")).upper().endswith("ARCHIVED"):
                continue
            out.append({"inputs": it.input, **({"expectations": it.expected_output} if it.expected_output else {})})
        return out

    def native(self, physical):
        return self.lf.get_dataset(physical)

    # ---------------- tracer ----------------
    def wrap(self, fn, *, run_id, row_id):
        lf = self.lf

        def traced(**inputs):
            with lf.start_as_current_observation(
                as_type="span",
                name="evalops.row",
                input=inputs,
                metadata={"evalops_run_id": run_id, "evalops_row_id": row_id},
            ) as obs:
                out = fn(**inputs)
                obs.update(output=out)
                return out, getattr(obs, "trace_id", None)

        return traced

    def usage(self, trace_ids):
        """VERIFY: reads trace cost/usage via the public API; ingestion is async, so retry briefly."""
        found, attempts = {}, int(self.options.get("usage_retries", 3))
        for _ in range(attempts):
            for tid in [t for t in trace_ids if t not in found]:
                try:
                    t = self.lf.api.trace.get(tid)
                except Exception:  # noqa: BLE001
                    continue
                tin = tout = 0
                for o in getattr(t, "observations", None) or []:
                    ud = getattr(o, "usage_details", None) or {}
                    tin += int(ud.get("input", 0) or 0)
                    tout += int(ud.get("output", 0) or 0)
                found[tid] = Usage(tin, tout, getattr(t, "total_cost", None))
            if len(found) == len(trace_ids):
                break
            time.sleep(float(self.options.get("usage_retry_seconds", 3)))
        return found

    def flush(self):
        self.lf.flush()

    # ---------------- evaluator (native experiment runner) ----------------
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
        if stored_rows is not None:
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
        from langfuse import Evaluation

        def task(*, item, **kwargs):
            inp = item.input if hasattr(item, "input") else item["input"]
            return predict_fn(**inp)

        def make(s):
            def ev(*, input, output, expected_output=None, **kwargs):  # noqa: A002 (Langfuse signature)
                res = call_scorer(s, inputs=input, outputs=output, expectations=expected_output)
                return [Evaluation(name=k, value=v) for k, v in res.items()]

            return ev

        kwargs = dict(
            name=run_name,
            run_name=run_name,
            task=task,
            evaluators=[make(s) for s in scorers],
            max_concurrency=int(concurrency.get("max_workers", 4)),
            metadata={"evalops_run_id": run_id},
        )
        if dataset_store is self:
            result = self.native(dataset_physical).run_experiment(**kwargs)
        else:  # VERIFY: top-level runner over local data
            data = [{"input": r["inputs"], "expected_output": r.get("expectations")} for r in records]
            result = self.lf.run_experiment(data=data, **kwargs)
        self.lf.flush()

        rows = []
        for i, ir in enumerate(getattr(result, "item_results", []) or []):
            item = getattr(ir, "item", None)
            inp = (
                getattr(item, "input", None)
                if item is not None and not isinstance(item, dict)
                else (item or {}).get("input")
            )
            exp = (
                getattr(item, "expected_output", None)
                if item is not None and not isinstance(item, dict)
                else (item or {}).get("expected_output")
            )
            scores = {
                getattr(e, "name", f"s{j}"): to_float(getattr(e, "value", None))
                for j, e in enumerate(getattr(ir, "evaluations", []) or [])
            }
            rows.append(
                RowResult(
                    row_id=f"{i:05d}",
                    inputs=inp or {},
                    outputs=getattr(ir, "output", None),
                    expectations=exp,
                    scores={k: v for k, v in scores.items() if v is not None},
                    trace_id=getattr(ir, "trace_id", None),
                )
            )
        usage = self.usage([r.trace_id for r in rows if r.trace_id])
        for r in rows:
            r.usage = usage.get(r.trace_id)
        from ..scoring import aggregate_rows

        ref = getattr(result, "dataset_run_url", None) or getattr(result, "dataset_run_id", None)
        return EvalResult(rows=rows, metrics=aggregate_rows(rows), native_ref=f"langfuse:{ref}" if ref else None)
