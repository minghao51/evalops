"""In-memory ledger plugin for the persisted-format tests: `module:Class` via evalops.yaml.

Nothing is persisted, so the test can inspect exactly which artifacts a run wrote and compare their
bytes against `evalops/manifest.json` (done-when (f)). State is class-level so the test can read the
same object the plugin instances wrote to.
"""

import time

from evalops.ports import Ledger, Plugin, RunRecord


class MemoryLedger(Plugin, Ledger):
    name = "memory"
    provides = ("ledger",)

    runs: dict[str, dict] = {}
    artifacts: dict[str, dict[str, str]] = {}

    def start_run(self, name: str, tags: dict) -> str:
        run_id = f"mem{len(self.runs) + 1:04d}"
        self.runs[run_id] = {
            "name": name,
            "tags": dict(tags),
            "params": {},
            "metrics": {},
            "start_ms": int(time.time() * 1000),
        }
        self.artifacts[run_id] = {}
        return run_id

    def end_run(self, run_id: str, status: str = "FINISHED") -> None:
        self.runs[run_id]["tags"]["evalops.status"] = status

    def log(
        self, run_id: str, params: dict | None = None, metrics: dict | None = None, tags: dict | None = None
    ) -> None:
        run = self.runs[run_id]
        run["params"].update(params or {})
        run["metrics"].update(metrics or {})
        run["tags"].update(tags or {})

    def put_artifact(self, run_id: str, name: str, text: str) -> None:
        self.artifacts[run_id][name] = text

    def get_artifact(self, run_id: str, name: str) -> str:
        return self.artifacts[run_id][name]

    def find_runs(
        self, tags: dict | None = None, since_ms: int | None = None, limit: int | None = None
    ) -> list[RunRecord]:
        found = [
            RunRecord(
                run_id=run_id,
                tags=dict(run["tags"]),
                params=dict(run["params"]),
                metrics=dict(run["metrics"]),
                start_ms=run["start_ms"],
            )
            for run_id, run in self.runs.items()
            if all(run["tags"].get(k) == v for k, v in (tags or {}).items())
            and (since_ms is None or run["start_ms"] >= since_ms)
        ]
        found.sort(key=lambda r: r.start_ms, reverse=True)
        return found[:limit] if limit else found
