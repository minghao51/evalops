"""WP3.1a contract: the MLflow five-role plugin against a live MLflow tracking server.

Excluded from the offline suite by the ``contract`` marker. Follows ``test_ledger_postgres.py``'s
ladder: missing drivers -> ``importorskip``; no docker (or docker unusable) -> skip;
``docker compose up -d`` starts the services in ``docker-compose.yml``; the server has 60s to answer
``GET /health`` and Postgres 60s to answer ``SELECT 1``, then the test skips.

Set ``EVALOPS_TEST_MLFLOW_TRACKING_URI`` (default ``http://127.0.0.1:55000``) and/or
``EVALOPS_TEST_POSTGRES_URL`` (a SQLAlchemy URL) to run against already-running services instead.

What is asserted (WP3.1a C1-C5), each against the live server, never against a fake SDK:

* prompts: register -> load -> alias round-trip, and a missing alias is ``None``;
* datasets: write -> exists -> read round-trip of inputs + expectations;
* ledger: start_run -> log -> find_runs by tag -> put/get artifact byte-identical -> end_run;
* evaluator: a real ``run.py::_evaluate`` on a temp project (lookup-table predict, plain scorer)
  whose run is FINISHED server-side, with scores + ``trace_id`` per row, the scorer aggregate and
  ``error_rate`` in the server-side metrics, and ``evalops/rows.jsonl`` + the manifest retrievable
  through the ledger -- on the all-mlflow wiring, and through the mixed wiring (evaluator mlflow,
  ledger on the compose Postgres);
* the ``stored_rows`` judge-only delegation path (mlflow evaluator -> core re-score) live.

Deliberately not asserted (3.1b/3.2): ``budget_exceeded_rate`` > 0, langfuse/langsmith surfaces,
recorded HTTP fixtures, and anything on the 2026-10-27 e2e findings list.

Names are unique per test session (``TOKEN``), so a second run against the same compose volume can
never satisfy an assertion from a previous one; the module finalizer best-effort deletes the
experiment and prompt it created.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
from string import Template
from typing import Any, cast

import pytest

pytestmark = pytest.mark.contract

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "src"
DEFAULT_URI = "http://127.0.0.1:55000"
DEFAULT_POSTGRES_URL = "postgresql+psycopg://evalops:evalops@127.0.0.1:55432/evalops_test"
#: Per-session names, so two runs against one long-lived compose volume never share state.
TOKEN = uuid.uuid4().hex[:10]
EXPERIMENT = f"evalops-wp31a-{TOKEN}"

sys.path.insert(0, str(SRC))

_COMPOSE_STARTED = False


# ---------------------------------------------------------------------------
# skip ladder (the test_ledger_postgres.py pattern)
# ---------------------------------------------------------------------------
def _drivers() -> None:
    pytest.importorskip("mlflow", reason="the mlflow extra is not installed (pip install 'evalops[mlflow]')")
    pytest.importorskip("sqlalchemy", reason="the sql extra is not installed (pip install 'evalops[sql]')")
    pytest.importorskip("psycopg", reason="the Postgres driver is not installed (pip install 'evalops[sql]')")


def _start_compose() -> None:
    """`docker compose up -d` once per session: it brings up BOTH services (postgres + mlflow)."""
    global _COMPOSE_STARTED
    if _COMPOSE_STARTED:
        return
    if not shutil.which("docker"):
        pytest.skip("docker is not available and EVALOPS_TEST_MLFLOW_TRACKING_URI is unset")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - any docker problem means skip this optional contract test
        pytest.skip(f"docker is not usable ({type(exc).__name__}); set EVALOPS_TEST_MLFLOW_TRACKING_URI to run it")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), "up", "-d"],
            check=True,
            capture_output=True,
            text=True,
            # A first-time `up` also pulls the ~400MB mlflow image; a pull is not readiness.
            timeout=600,
        )
    except Exception as exc:  # noqa: BLE001 - image pull/start failures are an environment skip
        pytest.skip(f"could not start the MLflow/Postgres compose services: {type(exc).__name__}: {exc}")
    _COMPOSE_STARTED = True


def _wait_until_ready(uri: str) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{uri}/health", timeout=5) as response:  # noqa: S310 - test-owned http URL
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001 - the container is still booting
            time.sleep(1)
    pytest.skip(f"MLflow did not answer GET {uri}/health within 60s")


def _wait_until_postgres_ready(url: str) -> None:
    sqlalchemy = pytest.importorskip("sqlalchemy")

    engine = sqlalchemy.create_engine(url)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except Exception:  # noqa: BLE001 - the container is still booting
            time.sleep(1)
    pytest.skip("Postgres did not become ready within 60s")


# ---------------------------------------------------------------------------
# fixtures: one live server and one Postgres, shared by the module
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live_uri():
    _drivers()
    uri = (os.getenv("EVALOPS_TEST_MLFLOW_TRACKING_URI") or "").rstrip("/")
    if not uri:
        _start_compose()
        uri = DEFAULT_URI
    _wait_until_ready(uri)
    yield uri
    # Best-effort cleanup: the experiment (with its runs, datasets and artifacts) and the prompt
    # versions this session registered. Teardown must never turn into a test failure.
    mlflow, client = sys.modules.get("mlflow"), None
    if mlflow is not None:
        try:
            mlflow.set_tracking_uri(uri)
            client = mlflow.MlflowClient()
            experiment = client.get_experiment_by_name(EXPERIMENT)
            if experiment is not None:
                client.delete_experiment(experiment.experiment_id)
            # Prompts are registry-global, not experiment-scoped: delete the ones we named.
            for name in _prompt_names():
                with contextlib.suppress(Exception):  # already gone is fine
                    client.delete_prompt(name)
        except Exception:  # noqa: BLE001 - cleanup is not part of the contract under test
            pass


@pytest.fixture(scope="module")
def postgres_url():
    _drivers()
    url = os.getenv("EVALOPS_TEST_POSTGRES_URL")
    if not url:
        _start_compose()
        url = DEFAULT_POSTGRES_URL
    _wait_until_postgres_ready(url)
    return url


@pytest.fixture(autouse=True)
def _local_env(monkeypatch):
    """The temp projects are `local`-env projects; the subprocess and this process must agree."""
    monkeypatch.setenv("EVALOPS_ENV", "local")
    monkeypatch.setenv("EVALOPS_ACTOR", "test")
    monkeypatch.delenv("EVALOPS_CONFIG", raising=False)
    monkeypatch.delenv("EVALOPS_ALLOW_FROZEN_CHANGE", raising=False)


# ---------------------------------------------------------------------------
# config / project helpers
# ---------------------------------------------------------------------------
_PLUGIN_CONFIG = Template(
    """
project: wp31a-contract
default_env: local
plugins: {prompts: mlflow, datasets: mlflow, ledger: mlflow, tracer: mlflow, evaluator: mlflow}
plugin_config:
$plugin_config
app:
  behavior_config: $module:behavior_config
  predict_fn: $module:build_predict_fn
  scorers: $module:build_scorers
  judge_meta: $module:judge_meta
"""
)

_PROJECT_CONFIG = Template(
    """
project: wp31a-contract
default_env: local
plugins: $plugins
plugin_config:
$plugin_config
app:
  behavior_config: $module:behavior_config
  predict_fn: $module:build_predict_fn
  scorers: $module:build_scorers
  judge_meta: $module:judge_meta
datasets: {$dataset: {}}
eval: {max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
"""
)

#: A deterministic app: no network, no sleeps, no vendor calls. `match` compares the lookup answer,
#: `shout` (a second, plain scorer) exists so the judge-only pass can re-score with a scorer the
#: recorded run did not aggregate.
_APP_SOURCE = """
import evalops

ANSWERS = {"what is one plus one?": "two", "what is two plus two?": "four"}


def behavior_config():
    return {"llm_endpoint": "lookup-table@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        return ANSWERS.get(question, "unknown")

    return predict


@evalops.scorer("match")
def match(*, outputs, expectations):
    return 1.0 if str(outputs) == str((expectations or {}).get("expected")) else 0.0


@evalops.scorer("shout")
def shout(*, outputs):
    return 1.0 if str(outputs) == str(outputs).lower() else 0.0


def build_scorers(evaluator="core"):
    return [match, shout]


def judge_meta():
    return {"llm_judge_count": 0, "judge_prompt_version": "1"}
"""


def _records() -> list[dict]:
    """The two-row dataset, freshly built each call: MLflow annotates caller-owned records in place."""
    return [
        {"inputs": {"question": "what is one plus one?"}, "expectations": {"expected": "two"}},
        {"inputs": {"question": "what is two plus two?"}, "expectations": {"expected": "four"}},
    ]


def _project_root(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    root = tmp_path / name
    root.mkdir()
    return root


def _load(root: pathlib.Path):
    from evalops import config

    return config.load(str(root / "evalops.yaml"))


def _plugins(tmp_path: pathlib.Path, uri: str) -> dict[str, Any]:
    """The three role plugins on a plain mlflow config (no project files needed for C1-C3)."""
    from evalops.registry import Registry

    root = _project_root(tmp_path, "plugins")
    (root / "evalops.yaml").write_text(
        _PLUGIN_CONFIG.substitute(
            plugin_config=f'  mlflow: {{tracking_uri: "{uri}", experiment: "{EXPERIMENT}"}}',
            module="wp31a_noop",
        )
    )
    registry = Registry(_load(root))
    return {role: registry.get(role) for role in ("prompts", "datasets", "ledger")}


def _app_module() -> str:
    return f"wp31a_app_{TOKEN}_{uuid.uuid4().hex[:6]}"


def _project(
    tmp_path: pathlib.Path, uri: str, *, name: str, postgres_url: str | None = None
) -> tuple[pathlib.Path, str, str, str]:
    """A git-clean temp project (synced+committed), like tests/unit/_projects.py's helper.

    Returns `(root, prompt_name, dataset_name, app_module)`. `postgres_url` selects the mixed
    wiring (evaluator mlflow over the SQL ledger on the compose Postgres).
    """
    module, prompt, dataset = _app_module(), f"wp31a_prompt_{TOKEN}", f"wp31a_rows_{TOKEN}"
    if postgres_url:
        plugins = "{prompts: mlflow, datasets: mlflow, ledger: sql, tracer: mlflow, evaluator: mlflow}"
        plugin_config = (
            f'  mlflow: {{tracking_uri: "{uri}", experiment: "{EXPERIMENT}"}}\n  sql: {{url: "{postgres_url}"}}'
        )
    else:
        plugins = "{prompts: mlflow, datasets: mlflow, ledger: mlflow, tracer: mlflow, evaluator: mlflow}"
        plugin_config = f'  mlflow: {{tracking_uri: "{uri}", experiment: "{EXPERIMENT}"}}'
    root = _project_root(tmp_path, name)
    (root / "evalops.yaml").write_text(
        _PROJECT_CONFIG.substitute(plugins=plugins, plugin_config=plugin_config, module=module, dataset=dataset)
    )
    (root / ".gitignore").write_text((REPO / ".gitignore").read_text())
    (root / "prompts").mkdir()
    (root / "prompts" / f"{prompt}.txt").write_text("Answer briefly.\n\nQuestion: {{question}}\n")
    (root / "datasets").mkdir()
    (root / "datasets" / f"{dataset}.jsonl").write_text("".join(json.dumps(record) + "\n" for record in _records()))
    (root / f"{module}.py").write_text(_APP_SOURCE)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "wp31a@example.test")
    _git(root, "config", "user.name", "wp31a")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _sync(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "evalops: sync locks")
    return root, prompt, dataset, module


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _cli_env() -> dict[str, str]:
    env = {**os.environ, "PYTHONPATH": f"{SRC}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"}
    for name in ("EVALOPS_CONFIG", "EVALOPS_REDACT", "EVALOPS_ALLOW_FROZEN_CHANGE"):
        env.pop(name, None)
    return env


def _sync(root: pathlib.Path) -> dict:
    """`evalops sync all` (the real CLI) -> its `--json` document; the lock is what `_evaluate` checks."""
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", "sync", "all"],
        cwd=str(root),
        env=_cli_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def _prompt_names() -> list[str]:
    return [f"wp31a_prompt_{TOKEN}"]


def _as_set(records: list[dict]) -> list[str]:
    return sorted(json.dumps(record, sort_keys=True) for record in records)


def _all_rows(ledger, run_id: str) -> list[dict]:
    return [json.loads(line) for line in ledger.get_artifact(run_id, "evalops/rows.jsonl").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# C1: prompts
# ---------------------------------------------------------------------------
def test_prompts_register_load_and_alias_roundtrip(tmp_path, live_uri):
    store = _plugins(tmp_path, live_uri)["prompts"]
    name = _prompt_names()[0]
    template = "Answer briefly.\n\nQuestion: {{question}}\n"
    version = store.register(name, template, {"evalops.source": "tests/contract/test_mlflow_plugin.py"})

    assert store.load(name, version).template == template
    assert store.get_alias(name, "prod") is None  # nothing promoted yet
    store.set_alias(name, "prod", version)
    assert store.get_alias(name, "prod") == str(version)
    assert store.get_alias(name, "no-such-alias") is None


# ---------------------------------------------------------------------------
# C2: datasets
# ---------------------------------------------------------------------------
def test_datasets_write_exists_and_read_roundtrip(tmp_path, live_uri):
    store = _plugins(tmp_path, live_uri)["datasets"]
    name = f"wp31a_dataset_{TOKEN}"

    assert store.exists(name) is False
    store.write(name, _records())
    assert store.exists(name) is True
    # Record *order* is the backend's business (MLflow returns them newest-first); the content is not.
    # A fresh `_records()` is the expected value: MLflow annotates the caller's dicts in place
    # (`source`/`tags`), which this plugin's `read` does not return (reported as a live finding).
    assert _as_set(store.read(name)) == _as_set(_records())


# ---------------------------------------------------------------------------
# C3: ledger
# ---------------------------------------------------------------------------
def test_ledger_start_log_find_and_artifact_roundtrip(tmp_path, live_uri):
    ledger = _plugins(tmp_path, live_uri)["ledger"]
    group = f"wp31a-ledger-{TOKEN}"
    run_id = ledger.start_run(f"wp31a-ledger-{TOKEN}", {"evalops.group": group})
    ledger.log(
        run_id,
        params={"dataset": "wp31a_rows", "format_version": 1},
        metrics={"match/mean": 1.0, "error_rate": 0.0},
        tags={"evalops.stage": "dev"},
    )

    found = ledger.find_runs(tags={"evalops.group": group}, limit=10)
    assert [r.run_id for r in found] == [run_id]
    assert found[0].params["dataset"] == "wp31a_rows"  # params round-trip as strings
    assert found[0].metrics["match/mean"] == 1.0
    assert found[0].metrics["error_rate"] == 0.0
    assert found[0].tags["evalops.stage"] == "dev"  # plus mlflow's own runName tag

    text = "héllo\nworld\n"  # byte-identical, including a non-ASCII character
    ledger.put_artifact(run_id, "evalops/roundtrip.txt", text)
    assert ledger.get_artifact(run_id, "evalops/roundtrip.txt") == text
    ledger.end_run(run_id)

    mlflow = pytest.importorskip("mlflow")
    mlflow.set_tracking_uri(live_uri)
    assert mlflow.MlflowClient().get_run(run_id).info.status == "FINISHED"


# ---------------------------------------------------------------------------
# C4: the evaluator through the real run.py::_evaluate (all-mlflow wiring)
# ---------------------------------------------------------------------------
def test_evaluator_run_evaluate_is_finished_with_scores_metrics_and_artifacts(tmp_path, live_uri):
    from evalops import run
    from evalops.contract import formats
    from evalops.ports import Ledger
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, live_uri, name="mlflow")
    cfg = _load(root)
    out = run.evaluate(cfg, dataset, "dev")
    run_id = out["run_ids"][0]

    # The run the plugin started is FINISHED on the server, queried by the group tag the run layer set.
    mlflow = pytest.importorskip("mlflow")
    mlflow.set_tracking_uri(live_uri)
    client = mlflow.MlflowClient()
    experiment_id = client.get_run(run_id).info.experiment_id
    server_runs = client.search_runs(
        [experiment_id], filter_string=f"tags.`evalops.group` = '{out['group']}'", max_results=10
    )
    assert [r.info.run_id for r in server_runs] == [run_id]
    assert server_runs[0].info.status == "FINISHED"
    # WP3.0: the scorer aggregate AND `error_rate` are on the server-side run, for a vendor evaluator.
    assert server_runs[0].data.metrics["match/mean"] == 1.0
    assert server_runs[0].data.metrics["error_rate"] == 0.0

    assert out["runs"][0]["metrics"]["match/mean"] == 1.0
    assert out["runs"][0]["metrics"]["error_rate"] == 0.0
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    rows = _all_rows(ledger, run_id)
    assert len(rows) == len(_records())
    assert [r["scores"]["match"] for r in rows] == [1.0, 1.0]
    assert all(r["trace_id"] for r in rows), rows

    # The artifacts are retrievable *from the live ledger*, and the manifest hashes what was stored.
    rows_text = ledger.get_artifact(run_id, "evalops/rows.jsonl")
    manifest = json.loads(ledger.get_artifact(run_id, "evalops/manifest.json"))
    assert manifest["artifacts"]["evalops/rows.jsonl"] == formats.sha256(rows_text)
    assert "evalops/prompts.lock.json" in manifest["artifacts"]
    assert "evalops/datasets.lock.json" in manifest["artifacts"]


# ---------------------------------------------------------------------------
# C5: stored_rows judge-only re-score, on the mixed wiring (evaluator mlflow + ledger sql/postgres)
# ---------------------------------------------------------------------------
def test_stored_rows_judge_only_rescore_through_the_mlflow_evaluator(tmp_path, live_uri, postgres_url):
    from evalops import run
    from evalops.ports import Ledger
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, live_uri, name="mixed", postgres_url=postgres_url)
    cfg = _load(root)
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()

    # Source run: the real _evaluate, evaluator=mlflow, ledger=Postgres.
    out = run.evaluate(cfg, dataset, "dev")
    run_id = out["run_ids"][0]
    assert out["runs"][0]["metrics"]["match/mean"] == 1.0
    assert out["runs"][0]["metrics"]["error_rate"] == 0.0
    # The mlflow evaluator's own run (the native ref) is FINISHED on the server.
    native_ref = out["runs"][0]["native_ref"]
    assert native_ref.startswith("mlflow-run:")
    mlflow = pytest.importorskip("mlflow")
    mlflow.set_tracking_uri(live_uri)
    assert mlflow.MlflowClient().get_run(native_ref.split(":", 1)[1]).info.status == "FINISHED"
    # The stored rows live in the Postgres ledger and are read back through it.
    assert [r["scores"]["match"] for r in _all_rows(ledger, run_id)] == [1.0, 1.0]

    # Judge-only: no predict_fn; the mlflow evaluator delegates to the core re-score with `shout`.
    rerun = run.evaluate(cfg, dataset, "dev", judge_only=True, source_group=out["group"], scorer_filter={"shout"})
    rerun_id = rerun["run_ids"][0]
    assert rerun_id != run_id
    rows = _all_rows(ledger, rerun_id)
    assert len(rows) == len(_records())
    assert [r["scores"]["shout"] for r in rows] == [1.0, 1.0]
    assert "match" not in rows[0]["scores"]  # the filter selected only the re-score scorer
    assert rerun["runs"][0]["metrics"]["shout/mean"] == 1.0
    assert rerun["runs"][0]["metrics"]["error_rate"] == 0.0
