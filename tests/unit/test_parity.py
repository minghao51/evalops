"""WP3.0: evaluator parity -- metrics, usage, and the tracer role across evaluators (ADR 0008).

What this module falsifies, one group per packet test:

(a) **C1 metrics**: ``error_rate``/``budget_exceeded_rate`` are emitted by ``run.py`` for *every*
    evaluator with the exact formula, after row errors are final (a native trace scorer's failure
    counts), a clean vendor run still *has* them (0.0, never absent), and ``plugins/core.py`` keeps no
    private copy;
(b) **C2 mlflow row errors**: a fake-SDK ``result_df`` failure/status column becomes
    ``row.error`` (``predict: ...``) -- one invented column name is never assumed, the mapping is
    ``VERIFY``-marked, and the guard reads both ways (column present *and* absent);
(c) **C3 usage stash**: ``evalops.report_usage(...)`` lands in ``row.usage`` under a vendor
    evaluator (``rows_missing_usage == 0``, ``gen_input_tokens > 0``), a retry's last attempt wins,
    identical-input rows share the last write (documented + pinned), and the core evaluator's own
    per-row collector is unchanged by the wrapper;
(d) **C4 backfill**: the configured tracer's ``usage()`` runs from ``run.py`` for every evaluator,
    exactly once per run, fills what it finds, and a miss stays an honest ``rows_missing_usage``;
(e) **C5 doctor**: the self-tracing-evaluator warning fires only on that wiring (naming both
    plugins, byte-exact text), it is driven by the explicit ``Plugin.self_tracing`` classification
    and never by ``provides`` (so ``evaluator: core`` + a separate tracer stays silent), the key is
    absent -- not ``[]`` -- otherwise, and the core wiring's ``doctor`` payload equals
    ``tests/fixtures/envelope/doctor.json``'s.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
No vendor SDK is imported: the mlflow side runs against a fake ``mlflow`` package installed for one
test only (the ``test_native_judges.py`` posture), so ``test_no_vendor_imports.py`` stays true.
"""

import contextlib
import importlib.util
import inspect
import itertools
import json
import os
import pathlib
import shutil
import sys
import types
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import run as run_mod  # noqa: E402
from evalops.plugins import core as core_plugin  # noqa: E402
from evalops.plugins.mlflow import MlflowPlugin  # noqa: E402
from evalops.ports import DatasetStore, Usage  # noqa: E402

FIXTURE = "parity_vendor_app.py"
_COUNTER = itertools.count()

#: Three rows, so a partial ``error_rate`` (2/3) can never be confused with 0.0 or 1.0.
THREE_ROWS = "".join(json.dumps({"inputs": {"question": q}}) + "\n" for q in ("one", "two", "three"))
TWO_IDENTICAL_ROWS = "".join(json.dumps({"inputs": {"question": "same"}}) + "\n" for _ in range(2))
ONE_ROW = json.dumps({"inputs": {"question": "hello"}}) + "\n"

#: The app used by the vendor-wiring tests. Its usage is trivial arithmetic so the harvested numbers
#: are exactly checkable: call N reports ``100*N`` input tokens and 7 output tokens. A ``retry...``
#: question fails its first attempt *after* reporting 999/999 (the numbers a retry must discard), and
#: a ``quiet...`` question reports nothing at all (the backfill tests need a row with no app usage).
APP = """
import evalops

_SEEN = set()
_CALLS = {"n": 0}


def behavior_config():
    return {"llm_endpoint": "wp30-fake@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        if question.startswith("retry") and question not in _SEEN:
            _SEEN.add(question)
            evalops.report_usage(input_tokens=999, output_tokens=999)  # the discarded attempt
            raise RuntimeError("429 Too Many Requests (simulated)")
        _CALLS["n"] += 1
        if not question.startswith("quiet"):
            evalops.report_usage(input_tokens=_CALLS["n"] * 100, output_tokens=7)
        return f"answer: {question}"

    return predict


def build_scorers(evaluator="core"):
    return []


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 0}
"""

#: The same app plus a trace-aware scorer that always raises: run.py's `_post_score` runs it *after*
#: `evaluator.evaluate` returns, so it is the pin that the two row metrics are computed afterwards.
APP_WITH_FAILING_TRACE_SCORER = (
    APP
    + """

@evalops.scorer("traced")
def traced(*, inputs, outputs, expectations, trace):
    raise RuntimeError("trace scorer exploded")


build_scorers = lambda evaluator="core": [traced]  # noqa: E731
"""
)


# ---------------------------------------------------------------------------
# temp projects: a fixture vendor plugin module + the in-memory ledger + the app
# ---------------------------------------------------------------------------
VENDOR_CONFIG = """
project: wp30
default_env: local
plugins:
  prompts: git
  datasets: git
  ledger: "{ledger}:MemoryLedger"
  tracer: "{plugins}:{tracer}"
  evaluator: "{plugins}:{evaluator}"
app:
  behavior_config: {app}:behavior_config
  predict_fn: {app}:build_predict_fn
  scorers: {app}:build_scorers
  judge_meta: {app}:judge_meta
datasets: {{smoke: {{}}}}
eval: {{max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}}
cost:
  prices_per_1m_tokens: {{generation: {{input: 1.0, output: 4.0}}, judge: {{input: 1.0, output: 1.0}}}}
  budgets: {{per_run_usd: 5, per_month_usd: 50}}
guard: {{exclude: [".git", "src"]}}
"""


def _unique(prefix: str) -> str:
    """A process-unique module name, so two temp projects never share ``sys.modules`` state."""
    return f"wp30_{prefix}_{os.getpid()}_{next(_COUNTER)}"


def _import(root: pathlib.Path, name: str) -> Any:
    """Import ``root/<name>.py`` under ``name`` -- the same object the CLI's ``module:Class`` gets."""
    spec = importlib.util.spec_from_file_location(name, root / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _project(config_for, *, app_source: str = APP, dataset_text: str | None = None) -> tuple:
    """A committed temp project with the fixture plugins, the memory ledger and the app module."""
    plugins, ledger, app = _unique("plugins"), _unique("ledger"), _unique("app")
    config = config_for(plugins=plugins, ledger=ledger, app=app)
    root = proj.project(
        config,
        files={
            f"{plugins}.py": proj.fixture_text(FIXTURE),
            f"{ledger}.py": proj.MEMORY_LEDGER_SOURCE.read_text(),
            f"{app}.py": app_source,
        },
        dataset_text=dataset_text,
    )
    ledger_mod = _import(root, ledger)
    proj.ledger_clear(ledger_mod)
    plugins_mod = _import(root, plugins)
    plugins_mod.reset()  # every knob present, even for a test that sets none of them
    return root, plugins_mod, ledger_mod


def _vendor_project(*, evaluator="VendorEvaluator", tracer="RecordingTracer", **kwargs) -> tuple:
    """The vendor wiring: a non-core evaluator in front of the recording fixture tracer."""
    return _project(
        lambda **names: VENDOR_CONFIG.format(**names, evaluator=evaluator, tracer=tracer),
        **kwargs,
    )


def _core_project(*, app_source: str = APP, dataset_text: str | None = None) -> tuple:
    """The core evaluator in front of the *recording* tracer (so its ``usage()`` calls are visible)."""
    plugins, ledger, app = _unique("plugins"), _unique("ledger"), _unique("app")
    config = (
        proj.BASE_CONFIG.replace("ledger: sql", f'ledger: "{ledger}:MemoryLedger"')
        .replace("tracer: none, evaluator: core", f'tracer: "{plugins}:RecordingTracer", evaluator: core')
        .replace("fake_app:", f"{app}:")
    )
    root = proj.project(
        config,
        files={
            f"{plugins}.py": proj.fixture_text(FIXTURE),
            f"{ledger}.py": proj.MEMORY_LEDGER_SOURCE.read_text(),
            f"{app}.py": app_source,
        },
        dataset_text=dataset_text,
    )
    ledger_mod = _import(root, ledger)
    proj.ledger_clear(ledger_mod)
    plugins_mod = _import(root, plugins)
    plugins_mod.reset()  # every knob present, even for a test that sets none of them
    return root, plugins_mod, ledger_mod


def _sync(root: pathlib.Path) -> None:
    code, envelope = proj.run_cli_in_process(root, "sync", "all")
    assert code == 0, envelope
    proj.commit(root, "sync")


def _eval(root: pathlib.Path) -> dict:
    code, envelope = proj.run_cli_in_process(root, "eval", "--dataset", "smoke", "--stage", "dev")
    assert code == 0, envelope
    return envelope


def _metrics(envelope: dict) -> dict:
    return envelope["data"]["runs"][0]["metrics"]


def _rows(ledger_mod: Any, envelope: dict) -> list[dict]:
    """The persisted rows of the run -- what a downstream consumer (gate, traces) reads."""
    run_id = envelope["data"]["run_ids"][0]
    text = ledger_mod.MemoryLedger.artifacts[run_id]["evalops/rows.jsonl"]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# (C1) the run layer emits the row metrics for every evaluator
# ---------------------------------------------------------------------------
def test_c1_vendor_row_metrics_are_emitted_by_the_run_layer():
    root, plugins, _ledger = _vendor_project(dataset_text=THREE_ROWS)
    try:
        plugins.reset(errors={"00001": "predict: RuntimeError: boom", "00002": "budget_exceeded: max_steps (2 > 1)"})
        _sync(root)
        metrics = _metrics(_eval(root))
        assert metrics["error_rate"] == 2 / 3, metrics
        assert metrics["budget_exceeded_rate"] == 1 / 3, metrics
        # ownership (ADR 0008): core keeps no private copy of either metric -- the emission it lost
        # was `metrics["<name>"] = ...`, and a mutation that moves it back is caught right here.
        source = inspect.getsource(core_plugin)
        for assignment in ('"error_rate"]', '"budget_exceeded_rate"]'):
            assert assignment not in source, assignment
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c1_a_clean_vendor_run_still_reports_both_rates():
    root, plugins, _ledger = _vendor_project(dataset_text=THREE_ROWS)
    try:
        plugins.reset()
        _sync(root)
        metrics = _metrics(_eval(root))
        assert metrics["error_rate"] == 0.0, metrics  # present and 0.0, never absent
        assert metrics["budget_exceeded_rate"] == 0.0, metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c1_native_trace_scorer_errors_are_counted_too():
    """The emission sits *after* the native `_post_score` block, so a trace scorer's failure counts."""
    root, plugins, _ledger = _vendor_project(dataset_text=ONE_ROW, app_source=APP_WITH_FAILING_TRACE_SCORER)
    try:
        plugins.reset()
        _sync(root)
        envelope = _eval(root)
        rows = _rows(_ledger, envelope)
        assert "traced" in (rows[0]["error"] or ""), rows
        assert _metrics(envelope)["error_rate"] == 1.0, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c1_the_core_path_keeps_its_values():
    root, _plugins, _ledger = _core_project()
    try:
        _sync(root)
        metrics = _metrics(_eval(root))
        assert metrics["error_rate"] == 0.0, metrics
        assert metrics["budget_exceeded_rate"] == 0.0, metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C2) mlflow: a result_df failure column becomes a real `row.error`
# ---------------------------------------------------------------------------
class _Frame:
    """The minimal `result_df` surface `MlflowPlugin.evaluate` iterates (columns + dict-like rows)."""

    def __init__(self, rows: list[dict]):
        self._rows = list(rows)
        self.columns = list(self._rows[0]) if self._rows else []

    def iterrows(self):
        return iter(list(enumerate(self._rows)))


class _StubStore(DatasetStore):
    """A dataset store that is not the evaluator itself, so ``evaluate`` uses the record list."""

    def exists(self, physical):
        return True

    def write(self, physical, records):
        raise AssertionError("the fake-SDK evaluator test never writes a dataset")

    def read(self, physical):
        return []

    def native(self, physical):
        return physical


class _StubCfg:
    """What `MlflowPlugin` reads from a config: `data['project']` and the role wiring."""

    data = {"project": "wp30"}
    plugins = {"ledger": "sql", "tracer": "core", "evaluator": "mlflow"}


def _fake_module(name: str, **attributes: Any) -> types.ModuleType:
    """A ``types.ModuleType`` standing in for one vendor module (``setattr``, never attribute syntax)."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def _mlflow_fake(rows: list[dict], metrics: dict | None = None) -> tuple[dict, dict]:
    """A fake `mlflow` package exposing only the surface `MlflowPlugin.evaluate` touches."""
    state: dict[str, Any] = {"evaluate": [], "runs": []}

    class _Run:
        def __enter__(self):
            return types.SimpleNamespace(info=types.SimpleNamespace(run_id="mlf-run-1"))

        def __exit__(self, *exc):
            return False

    def start_run(**kwargs):
        state["runs"].append(kwargs)
        return _Run()

    def evaluate(**kwargs):
        state["evaluate"].append(kwargs)
        return types.SimpleNamespace(result_df=_Frame(rows), metrics=dict(metrics or {}))

    scorers = _fake_module("mlflow.genai.scorers", Scorer=type("Scorer", (), {}), scorer=lambda **kwargs: lambda fn: fn)
    genai = _fake_module("mlflow.genai", evaluate=evaluate, scorers=scorers)
    mlflow = _fake_module(
        "mlflow",
        genai=genai,
        set_experiment=lambda name: types.SimpleNamespace(experiment_id="exp-1"),
        MlflowClient=lambda: types.SimpleNamespace(),
        start_run=start_run,
        get_tracking_uri=lambda: "fake://",
        __version__="3.2.fake",
    )
    return {"mlflow": mlflow, "mlflow.genai": genai, "mlflow.genai.scorers": scorers}, state


@contextlib.contextmanager
def _fake_modules(modules: dict[str, types.ModuleType]):
    """Install fake SDK modules for exactly one test, restoring ``sys.modules`` afterwards."""
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def _mlflow_rows(rows: list[dict]):
    """Run the real `MlflowPlugin.evaluate` against a fake SDK and return its `RowResult`s."""
    modules, _state = _mlflow_fake(rows)
    with _fake_modules(modules):
        plugin = MlflowPlugin(_StubCfg(), {})
        result = plugin.evaluate(
            records=[{"inputs": {"question": f"q{i}"}} for i in range(len(rows))],
            dataset_physical="smoke",
            dataset_store=_StubStore(),
            predict_fn=lambda **inputs: "out",
            scorers=[],
            tracer=plugin,  # the mlflow plugin is its own (self-tracing) tracer
            run_id="run-1",
            run_name="wp30",
            concurrency={},
        )
    return result.rows


def test_c2_a_result_df_error_column_marks_the_row_failed():
    rows = _mlflow_rows(
        [
            {
                "inputs": {"question": "a"},
                "outputs": None,
                "error": "RuntimeError: boom",
                "facts/value": 0.0,
            },
            {
                "inputs": {"question": "b"},
                "outputs": None,
                "error": "ToolReplayMiss: no cassette",
                "facts/value": 0.0,
            },
        ]
    )
    assert [r.error for r in rows] == ["predict: RuntimeError: boom", "predict: ToolReplayMiss: no cassette"], rows
    # a failed prediction has no output to score: the core evaluator's score-skip rule, same here
    assert [r.scores for r in rows] == [{}, {}], rows
    assert run_mod._row_metrics(rows)["error_rate"] == 1.0
    # the other probed message column name behaves the same way (the mapping is VERIFY-marked)
    other = _mlflow_rows([{"inputs": {"question": "a"}, "outputs": None, "error_message": "KeyError: q"}])
    assert other[0].error == "predict: KeyError: q", other
    assert run_mod._row_metrics(other)["error_rate"] == 1.0


def test_c2_a_frame_without_a_failure_column_invents_no_error():
    rows = _mlflow_rows(
        [
            {"inputs": {"question": "a"}, "outputs": "fine", "facts/value": 1.0},
            {"inputs": {"question": "b"}, "outputs": "fine", "facts/value": 0.0},
        ]
    )
    assert [r.error for r in rows] == [None, None], rows
    assert rows[0].scores == {"facts": 1.0}, rows  # scores are untouched when nothing failed
    assert run_mod._row_metrics(rows)["error_rate"] == 0.0


def test_c2_a_status_column_is_read_only_when_the_column_exists():
    clean = _mlflow_rows([{"inputs": {"question": "a"}, "outputs": "x", "status": "ok"}])
    assert clean[0].error is None, clean
    failed = _mlflow_rows([{"inputs": {"question": "a"}, "outputs": "x", "status": "error"}])
    assert failed[0].error == "predict: error", failed
    # a status column that is present but successful never invents a failure
    assert [r.error for r in _mlflow_rows([{"status": "completed", "outputs": "x"}])] == [None]


# ---------------------------------------------------------------------------
# (C3) `evalops.report_usage` reaches `row.usage` under a vendor evaluator
# ---------------------------------------------------------------------------
def test_c3_app_reported_usage_lands_in_the_row_under_a_vendor_evaluator():
    root, plugins, ledger = _vendor_project(dataset_text=ONE_ROW)
    try:
        plugins.reset()
        _sync(root)
        envelope = _eval(root)
        row = _rows(ledger, envelope)[0]
        assert row["usage"] == {"input_tokens": 100, "output_tokens": 7, "cost_usd": None}, row
        metrics = _metrics(envelope)
        assert metrics["rows_missing_usage"] == 0, metrics
        assert metrics["gen_input_tokens"] == 100, metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c3_a_retry_overwrites_the_discarded_attempts_usage():
    root, plugins, ledger = _vendor_project(dataset_text=json.dumps({"inputs": {"question": "retry please"}}) + "\n")
    try:
        plugins.reset(retries=1)  # the vendor retries once; the app reports 999/999 then fails
        _sync(root)
        envelope = _eval(root)
        row = _rows(ledger, envelope)[0]
        # last attempt wins (core's `caps[-1]` semantics): 100/7, never the discarded 999/999
        assert row["usage"] == {"input_tokens": 100, "output_tokens": 7, "cost_usd": None}, row
        assert row["error"] is None, row
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c3_identical_input_rows_share_the_last_write():
    """Documented and pinned: the stash is keyed by inputs, so the last identical row wins."""
    root, plugins, ledger = _vendor_project(dataset_text=TWO_IDENTICAL_ROWS)
    try:
        plugins.reset()
        _sync(root)
        envelope = _eval(root)
        rows = _rows(ledger, envelope)
        assert [r["usage"]["input_tokens"] for r in rows] == [200, 200], rows  # 100 then 200: last write
        assert _metrics(envelope)["gen_input_tokens"] == 400, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c3_the_core_evaluators_own_collector_is_unchanged():
    """Core keeps its exact per-row collector: the wrapper never replaces what it recorded."""
    root, _plugins, ledger = _core_project()
    try:
        _sync(root)
        envelope = _eval(root)
        rows = _rows(ledger, envelope)
        assert all(r["usage"] is not None for r in rows), rows
        metrics = _metrics(envelope)
        assert metrics["rows_missing_usage"] == 0, metrics
        # the persisted per-row usage *is* the sum `cost.measure` reports (nothing dropped/duplicated)
        assert metrics["gen_input_tokens"] == sum(r["usage"]["input_tokens"] for r in rows), metrics
        assert metrics["gen_output_tokens"] == sum(r["usage"]["output_tokens"] for r in rows), metrics
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C4) one configured-tracer backfill per run, for every evaluator
# ---------------------------------------------------------------------------
def test_c4_the_configured_tracer_backfills_a_vendor_row():
    root, plugins, ledger = _vendor_project(dataset_text=json.dumps({"inputs": {"question": "quiet one"}}) + "\n")
    try:
        plugins.reset(trace_ids={"00000": "trace-a"}, usage={"trace-a": Usage(11, 2, 0.5)})
        _sync(root)
        envelope = _eval(root)
        row = _rows(ledger, envelope)[0]
        assert row["usage"] == {"input_tokens": 11, "output_tokens": 2, "cost_usd": 0.5}, row
        assert plugins.STATE["usage_calls"] == [["trace-a"]], plugins.STATE["usage_calls"]
        assert _metrics(envelope)["rows_missing_usage"] == 0, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c4_a_tracer_miss_stays_an_honest_missing_row():
    root, plugins, ledger = _vendor_project(dataset_text=json.dumps({"inputs": {"question": "quiet one"}}) + "\n")
    try:
        plugins.reset(trace_ids={"00000": "trace-a"})  # the tracer knows nothing about it
        _sync(root)
        envelope = _eval(root)
        assert _rows(ledger, envelope)[0]["usage"] is None
        assert plugins.STATE["usage_calls"] == [["trace-a"]], plugins.STATE["usage_calls"]  # asked, not skipped
        assert _metrics(envelope)["rows_missing_usage"] == 1, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c4_core_backfills_exactly_once_per_run_and_still_gets_filled():
    root, plugins, ledger = _core_project(dataset_text=json.dumps({"inputs": {"question": "quiet one"}}) + "\n")
    try:
        plugins.reset(trace_ids={"00000": "trace-a"}, usage={"trace-a": Usage(5, 3)})
        _sync(root)
        envelope = _eval(root)
        assert _rows(ledger, envelope)[0]["usage"] == {"input_tokens": 5, "output_tokens": 3, "cost_usd": None}
        assert plugins.STATE["usage_calls"] == [["trace-a"]], plugins.STATE["usage_calls"]
        assert _metrics(envelope)["rows_missing_usage"] == 0, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c4_core_calls_the_tracer_once_even_when_it_finds_nothing():
    """A leftover private block in core would ask a second time: the miss case counts the calls."""
    root, plugins, _ledger = _core_project(dataset_text=json.dumps({"inputs": {"question": "quiet one"}}) + "\n")
    try:
        plugins.reset(trace_ids={"00000": "trace-a"})  # a miss
        _sync(root)
        envelope = _eval(root)
        assert plugins.STATE["usage_calls"] == [["trace-a"]], plugins.STATE["usage_calls"]
        assert _metrics(envelope)["rows_missing_usage"] == 1, _metrics(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C5) doctor: honest about a self-tracing evaluator, silent otherwise
# ---------------------------------------------------------------------------
WARNING = (
    "evaluator '{evaluator}' traces rows natively (self-tracing); the configured tracer '{tracer}' "
    "is not used for row tracing — usage backfill may still query it for rows missing usage"
)


def _mark_self_tracing(plugins_mod: Any, name: str = "SelfTracingEvaluator") -> None:
    """Opt the fixture evaluator into the explicit classification, exactly as ``plugins/mlflow.py`` does.

    ``self_tracing`` is a declaration, not a detection: the fixture module is a fixed input of this
    WP, so the flag is set on the class here rather than inherited from ``provides`` (a self-tracing
    evaluator that serves the tracer role still has to say so).
    """
    getattr(plugins_mod, name).self_tracing = True


def test_c5_the_self_tracing_flag_is_declared_per_plugin_class():
    """The classification lives on the plugin classes: mlflow opt in, core (a delegator) does not."""
    assert MlflowPlugin.self_tracing is True  # declares `tracer` AND traces rows via mlflow.genai
    assert core_plugin.CorePlugin.self_tracing is False  # declares `tracer` but delegates to it


def test_c5_core_wiring_has_no_warnings_key_and_matches_the_doctor_golden():
    root = proj.project()  # the golden project's wiring: tracer none (the core plugin) + evaluator core
    try:
        code, envelope = proj.run_cli(root, "doctor")
        assert code == 0 and envelope["ok"] is True, envelope
        assert "warnings" not in envelope["data"], envelope["data"]  # absent, not []
        golden = json.loads((pathlib.Path(__file__).resolve().parents[1] / "fixtures/envelope/doctor.json").read_text())
        assert envelope["data"] == golden["data"], envelope["data"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c5_core_with_a_separate_tracer_never_warns():
    """The reviewer's false positive: declaring `tracer` in `provides` is not self-tracing.

    Core declares `tracer` yet delegates: it wraps every row with the configured tracer
    (``tracer.wrap``) and ``run.py``'s backfill queries it. So ``evaluator: core`` + another tracer
    must stay silent -- a rule keyed on ``provides`` warns here and turns this test red.
    """
    root, plugins, _ledger = _core_project()
    try:
        plugins.reset()
        code, envelope = proj.run_cli_in_process(root, "doctor")
        assert code == 0 and envelope["ok"] is True, envelope
        assert "warnings" not in envelope["data"], envelope["data"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c5_a_self_tracing_evaluator_with_a_separate_tracer_warns():
    root, plugins, _ledger = _vendor_project(evaluator="SelfTracingEvaluator")
    try:
        plugins.reset()
        _mark_self_tracing(plugins)
        code, envelope = proj.run_cli_in_process(root, "doctor")
        assert code == 0 and envelope["ok"] is True, envelope  # a warning, never a failure
        assert envelope["data"]["warnings"] == [WARNING.format(evaluator="parityself", tracer="paritytracer")]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c5_a_self_tracing_evaluator_that_serves_the_tracer_role_is_silent():
    root, plugins, _ledger = _vendor_project(evaluator="SelfTracingEvaluator", tracer="SelfTracingEvaluator")
    try:
        plugins.reset()
        _mark_self_tracing(plugins)  # self-tracing, but nothing else is configured to trace
        code, envelope = proj.run_cli_in_process(root, "doctor")
        assert code == 0 and envelope["ok"] is True, envelope
        assert "warnings" not in envelope["data"], envelope["data"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_c5_a_plain_vendor_evaluator_never_warns():
    """A vendor evaluator that does not declare `tracer` is not self-tracing: nothing to say."""
    root, plugins, _ledger = _vendor_project()
    try:
        plugins.reset()
        code, envelope = proj.run_cli_in_process(root, "doctor")
        assert code == 0 and envelope["ok"] is True, envelope
        assert "warnings" not in envelope["data"], envelope["data"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
