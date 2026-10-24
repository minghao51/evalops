"""logfire plugin: tracer only (Logfire is OpenTelemetry-based observability; no prompt registry or
datasets). Pair with prompts/datasets = git|mlflow|langfuse and ledger = sql|mlflow, evaluator = core.

options:
  configure: true            # call logfire.configure() (set false if your app already does)
  service_name: evalops
  read_token: ${LOGFIRE_READ_TOKEN}   # enables token usage lookup via the Query API
  input_tokens_attr / output_tokens_attr: OTel GenAI attribute names (defaults below)
Usage lookup relies on your LLM instrumentation (e.g. logfire.instrument_openai()) emitting
gen_ai.usage.* attributes; otherwise use evalops.report_usage() in predict_fn. VERIFY the SQL
against your Logfire version.
"""

import time

from ..ports import Plugin, Tracer, Usage


class LogfirePlugin(Plugin, Tracer):
    name = "logfire"
    provides = ("tracer",)

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        import logfire

        self.logfire = logfire
        if options.get("configure", True):
            logfire.configure(
                service_name=options.get("service_name", "evalops"),
                send_to_logfire=options.get("send_to_logfire", "if-token-present"),
            )
        self._started = time.time()

    def doctor(self):
        return {"plugin": self.name, "ok": True, "usage_lookup": bool(self.options.get("read_token"))}

    def wrap(self, fn, *, run_id, row_id):
        from opentelemetry import trace as otel

        logfire = self.logfire

        def traced(**inputs):
            with logfire.span("evalops.row {row_id}", row_id=row_id, evalops_run_id=run_id, inputs=inputs) as span:
                out = fn(**inputs)
                span.set_attribute("outputs", str(out)[:10000])
                tid = otel.get_current_span().get_span_context().trace_id
                return out, format(tid, "032x")

        return traced

    def flush(self):
        self.logfire.force_flush()

    def usage(self, trace_ids):
        token = self.options.get("read_token")
        if not token or not trace_ids:
            return {}
        from logfire.query_client import LogfireQueryClient

        ia = self.options.get("input_tokens_attr", "gen_ai.usage.input_tokens")
        oa = self.options.get("output_tokens_attr", "gen_ai.usage.output_tokens")
        ids = ", ".join(f"'{t}'" for t in trace_ids if all(c in "0123456789abcdef" for c in t))
        sql = (
            f"SELECT trace_id, SUM(CAST(attributes->>'{ia}' AS BIGINT)) AS i, "
            f"SUM(CAST(attributes->>'{oa}' AS BIGINT)) AS o FROM records "
            f"WHERE trace_id IN ({ids}) GROUP BY trace_id"
        )
        found = {}
        with LogfireQueryClient(read_token=token) as client:
            for _ in range(int(self.options.get("usage_retries", 5))):  # ingestion is asynchronous
                res = client.query_json_rows(sql=sql)
                rows = res.get("rows", []) if isinstance(res, dict) else res
                found = {r["trace_id"]: Usage(int(r.get("i") or 0), int(r.get("o") or 0)) for r in rows}
                if len(found) >= len(trace_ids):
                    break
                time.sleep(float(self.options.get("usage_retry_seconds", 3)))
        return found
