"""langsmith plugin: prompts (read/write versions), datasets, tracer, evaluator.

Credentials via LANGSMITH_API_KEY (+ LANGSMITH_ENDPOINT for self-hosted). Prompts require
langchain-core (prompt objects are LangChain templates; evalops uses mustache {{var}} format).
No ledger: pair with plugins.ledger = sql (or mlflow).
Alias moves (commit tags) are NOT automated here: set_alias raises CapabilityError. Use the
LangSmith UI, or keep prompts in git/mlflow/langfuse if you want automated promotion.
Methods whose vendor details are unverified are beta-marked (see ``evalops.beta``).
"""

from ..ports import DatasetStore, EvalResult, Evaluator, LoadedPrompt, Plugin, PromptStore, RowResult, Tracer, Usage
from ..scoring import aggregate_rows, call_scorer, to_float
from .core import CorePlugin

_ROLE_BY_CLASS = {"System": "system", "Human": "user", "AI": "assistant"}


class LangsmithPlugin(Plugin, PromptStore, DatasetStore, Tracer, Evaluator):
    name = "langsmith"
    provides = ("prompts", "datasets", "tracer", "evaluator")

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        from langsmith import Client  # pyright: ignore[reportMissingImports]

        self.client = Client()

    def doctor(self):
        info = self.client.info if hasattr(self.client, "info") else None
        return {"plugin": self.name, "ok": True, "info": str(info)[:200] if info else None}

    # ---------------- prompts ----------------
    def register(self, name, template, tags):
        from langchain_core.prompts import ChatPromptTemplate, PromptTemplate  # pyright: ignore[reportMissingImports]

        if isinstance(template, list):
            obj = ChatPromptTemplate.from_messages(
                [(m["role"], m["content"]) for m in template], template_format="mustache"
            )
        else:
            obj = PromptTemplate.from_template(template, template_format="mustache")
        try:
            self.client.push_prompt(name, object=obj, description=f"evalops {tags.get('evalops.git_sha', '')}")
        except Exception as e:  # noqa: BLE001  identical content -> LangSmith refuses an empty commit
            if "nothing to commit" not in str(e).lower():
                raise
        return self.client.pull_prompt_commit(
            name
        ).commit_hash  # VERIFY-BETA(langsmith-pull-prompt-commit): attribute name

    def load(self, name, version):
        obj = self.client.pull_prompt(f"{name}:{version}")
        if hasattr(obj, "template"):
            template = obj.template
        else:
            template = []
            for m in getattr(obj, "messages", []):
                cls = type(m).__name__
                role = next((r for k, r in _ROLE_BY_CLASS.items() if cls.startswith(k)), "user")
                template.append({"role": role, "content": getattr(getattr(m, "prompt", m), "template", str(m))})
        return LoadedPrompt(name=name, version=str(version), template=template, native=obj)

    def get_alias(self, name, alias):
        try:
            return self.client.pull_prompt_commit(f"{name}:{alias}").commit_hash
        except Exception:  # noqa: BLE001
            return None

    def set_alias(self, name, alias, version):
        self._unsupported(
            "moving prompt commit tags",
            "Move the tag in the LangSmith UI, or use plugins.prompts = git|mlflow|langfuse.",
        )

    # ---------------- datasets ----------------
    def exists(self, physical):
        return bool(self.client.has_dataset(dataset_name=physical))

    def write(self, physical, records):
        ds = self.client.create_dataset(dataset_name=physical, description="created by evalops")
        self.client.create_examples(
            inputs=[r["inputs"] for r in records],
            outputs=[r.get("expectations") or {} for r in records],
            dataset_id=ds.id,
        )

    def read(self, physical):
        return [
            {"inputs": e.inputs, **({"expectations": e.outputs} if e.outputs else {})}
            for e in self.client.list_examples(dataset_name=physical)
        ]

    # ---------------- tracer ----------------
    def wrap(self, fn, *, run_id, row_id):
        import langsmith as ls  # pyright: ignore[reportMissingImports]

        def traced(**inputs):
            with ls.tracing_context(enabled=True):
                with ls.trace(
                    name="evalops.row", inputs=inputs, metadata={"evalops_run_id": run_id, "evalops_row_id": row_id}
                ) as rt:
                    out = fn(**inputs)
                    rt.end(outputs={"output": out})
                    return out, str(rt.id)

        return traced

    def usage(self, trace_ids):
        found = {}
        for tid in trace_ids:
            try:
                r = self.client.read_run(tid)
            except Exception:  # noqa: BLE001
                continue
            found[tid] = Usage(
                int(getattr(r, "prompt_tokens", 0) or 0),
                int(getattr(r, "completion_tokens", 0) or 0),
                getattr(r, "total_cost", None),
            )
        return found

    def flush(self):
        getattr(self.client, "flush", lambda: None)()

    # ---------------- evaluator (native client.evaluate) ----------------
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
        if stored_rows is not None or dataset_store is not self:
            if stored_rows is None:
                self._unsupported(
                    "native evaluation over non-LangSmith datasets",
                    "Use plugins.datasets = langsmith, or plugins.evaluator = core with tracer langsmith.",
                )
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

        def target(inputs: dict) -> dict:
            # judge-only is handled above, so `predict_fn` is present on this path (run.py contract).
            return {"output": predict_fn(**inputs)}  # pyright: ignore[reportOptionalCall]

        def make(s):
            def ev(inputs, outputs, reference_outputs):
                res = call_scorer(
                    s, inputs=inputs, outputs=(outputs or {}).get("output"), expectations=reference_outputs
                )
                return {"results": [{"key": k, "score": v} for k, v in res.items()]}

            return ev

        results = self.client.evaluate(
            target,
            data=dataset_physical,
            evaluators=[make(s) for s in scorers],
            experiment_prefix=run_name,
            metadata={"evalops_run_id": run_id},
            max_concurrency=int(concurrency.get("max_workers", 4)),
        )
        rows = []
        for i, r in enumerate(
            results
        ):  # VERIFY-BETA(langsmith-run-results): row dict keys run/example/evaluation_results
            run, ex = r["run"], r["example"]
            scores = {e.key: to_float(e.score) for e in (r.get("evaluation_results") or {}).get("results", [])}
            rows.append(
                RowResult(
                    row_id=f"{i:05d}",
                    inputs=ex.inputs,
                    outputs=(run.outputs or {}).get("output"),
                    expectations=ex.outputs,
                    scores={k: v for k, v in scores.items() if v is not None},
                    trace_id=str(run.id),
                    usage=Usage(
                        int(getattr(run, "prompt_tokens", 0) or 0),
                        int(getattr(run, "completion_tokens", 0) or 0),
                        getattr(run, "total_cost", None),
                    ),
                )
            )
        return EvalResult(
            rows=rows, metrics=aggregate_rows(rows), native_ref=f"langsmith:{getattr(results, 'experiment_name', '')}"
        )
