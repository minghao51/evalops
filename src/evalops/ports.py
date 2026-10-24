"""Role interfaces ("ports"). Plugins implement any subset; core code only talks to these."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

from .util import CapabilityError, render

ROLES = ("prompts", "datasets", "ledger", "tracer", "evaluator")


# ---------- data types ----------
@dataclass
class LoadedPrompt:
    name: str
    version: str
    template: Any  # str or list of chat messages, using {{var}} placeholders
    native: Any = None  # backend object, for apps that want it

    def format(self, **variables):
        return render(self.template, variables)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None  # None -> evalops computes from the price table


@dataclass
class RowResult:
    row_id: str
    inputs: dict
    outputs: Any = None
    expectations: dict | None = None
    scores: dict[str, float] = field(default_factory=dict)
    trace_id: str | None = None
    usage: Usage | None = None
    error: str | None = None
    trace: Any = None  # evalops.traces.AgentTrace (normalized, framework-neutral)
    trace_metrics: dict[str, float] = field(default_factory=dict)
    # Stable per-row identity (CONTRACT §4, additive): `datasets.row_key` of the record this row
    # was evaluated from. Appended so positional constructors stay valid; None means a v0 writer
    # and readers fall back to `row_id`.
    row_key: str | None = None


@dataclass
class EvalResult:
    rows: list[RowResult]
    metrics: dict[str, float]
    native_ref: str | None = None  # e.g. backend experiment/run URL or id


@dataclass
class RunRecord:
    run_id: str
    tags: dict
    params: dict
    metrics: dict
    start_ms: int


# ---------- base plugin ----------
class Plugin:
    """Base class. `provides` lists the roles a plugin implements."""

    name = "base"
    provides: tuple[str, ...] = ()

    def __init__(self, cfg, options: dict):
        self.cfg = cfg
        self.options = options or {}

    def _unsupported(self, op: str, hint: str = ""):
        raise CapabilityError(self.name, op, hint)

    def physical_name(self, name: str) -> str:
        """Backend-specific name for a logical prompt/dataset (e.g. UC catalog.schema prefix)."""
        ns = self.options.get("namespace") or ""
        return f"{ns}.{name}" if ns else name

    def doctor(self) -> dict:
        return {"plugin": self.name, "ok": True}


class PromptStore(ABC):
    @abstractmethod
    def register(self, name: str, template, tags: dict) -> str:
        """Create a NEW immutable version; return its version id (string)."""

    @abstractmethod
    def load(self, name: str, version: str) -> LoadedPrompt: ...

    @abstractmethod
    def get_alias(self, name: str, alias: str) -> str | None: ...

    @abstractmethod
    def set_alias(self, name: str, alias: str, version: str) -> None: ...


class DatasetStore(ABC):
    @abstractmethod
    def exists(self, physical: str) -> bool: ...

    @abstractmethod
    def write(self, physical: str, records: list[dict]) -> None:
        """Create the dataset and insert records. Called once per content hash."""

    @abstractmethod
    def read(self, physical: str) -> list[dict]:
        """Return records as [{"inputs": {...}, "expectations": {...}}]."""

    def native(self, physical: str):
        """Backend dataset object for native evaluators (optional)."""
        return physical


class Ledger(ABC):
    """Durable record of eval runs: params, metrics, tags, artifacts. Powers baselines, gates, budgets."""

    @abstractmethod
    def start_run(self, name: str, tags: dict) -> str: ...

    @abstractmethod
    def end_run(self, run_id: str, status: str = "FINISHED") -> None: ...

    @abstractmethod
    def log(
        self, run_id: str, params: dict | None = None, metrics: dict | None = None, tags: dict | None = None
    ) -> None: ...

    @abstractmethod
    def put_artifact(self, run_id: str, name: str, text: str) -> None: ...

    @abstractmethod
    def get_artifact(self, run_id: str, name: str) -> str: ...

    @abstractmethod
    def find_runs(
        self, tags: dict | None = None, since_ms: int | None = None, limit: int | None = None
    ) -> list[RunRecord]:
        """Runs whose tags match all given key/values, newest first."""


class Tracer(ABC):
    @abstractmethod
    def wrap(self, fn: Callable, *, run_id: str, row_id: str) -> Callable:
        """Return fn' (**inputs) -> (output, trace_id) that traces one row."""

    def usage(self, trace_ids: list[str]) -> dict[str, Usage]:
        return {}

    def flush(self) -> None:
        pass


class Evaluator(ABC):
    @abstractmethod
    def evaluate(
        self,
        *,
        records: list[dict],
        dataset_physical: str,
        dataset_store: DatasetStore,
        predict_fn: Callable | None,
        scorers: list,
        tracer: Tracer,
        run_id: str,
        run_name: str,
        concurrency: dict,
        stored_rows: list[RowResult] | None = None,
    ) -> EvalResult:
        """Run predict_fn over records (or re-score stored_rows) and score every row."""
