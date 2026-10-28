"""WP3.1b contract: the Langfuse four-role plugin against a live self-hosted Langfuse stack.

Excluded from the offline suite by the ``contract`` marker. Follows ``test_ledger_postgres.py`` and
``test_mlflow_plugin.py``: missing drivers -> ``importorskip``; no docker (or docker unusable) ->
skip; two ``up``s start the contract services -- ``docker compose -f docker-compose.yml up -d`` for
the ledger Postgres (B2's ``ledger: sql``, shared with the other contract modules) and ``docker
compose -f docker-compose.langfuse.yml up -d`` for the six-container Langfuse stack, which is a
compose project of its own (``name: evalops-contract-langfuse``); ``langfuse-web`` has 300s to answer
``GET /api/public/health`` on first boot (it runs the Postgres + ClickHouse migrations), then the
test skips.

The stack boots itself from ``LANGFUSE_INIT_*`` envs: org/project/user and the pinned API keys are
part of the compose fixture (``pk-lf-e2e-contract`` / ``sk-lf-e2e-contract``), so no manual account
exists anywhere. Overrides: ``EVALOPS_TEST_LANGFUSE_BASE_URL`` (default ``http://127.0.0.1:33000``),
``EVALOPS_TEST_LANGFUSE_PUBLIC_KEY`` / ``_SECRET_KEY`` (defaults = the pinned keys above) and
``EVALOPS_TEST_POSTGRES_URL`` for the mixed-wiring ledger.

What is asserted (WP3.1b C1-C6), each against the live server, never against a fake SDK:

* prompts: register -> load -> alias round-trip, label preservation without re-adding ``latest``, a
  missing alias is ``None``, a second register bumps the version;
* datasets: write -> exists -> read round-trip, write idempotence (the sha256 item ids upsert, so a
  retry cannot duplicate rows), order-insensitive compare (port order is undefined);
* tracer: a real ``run.py::_evaluate`` (core evaluator + langfuse tracer) whose rows carry
  ``trace_id``s that are readable server-side with the ``evalops.row`` span name, plus the plugin's
  ``usage()`` VERIFY line exercised with those real ids;
* evaluator: a real ``run.evaluate`` on a temp project (git-clean, real sync) taking the native
  ``dataset.run_experiment`` path, with scores, ``native_ref`` = ``langfuse:<url>``, the scorer
  aggregate AND ``error_rate`` (WP3.0), app-reported usage (WP3.0 B2), rows+manifest artifacts in
  the Postgres ledger, and the dataset run visible server-side. Then the local-data VERIFY branch
  (``datasets: git`` so ``dataset_store`` is not langfuse -> top-level ``run_experiment(data=...)``);
* the ``stored_rows`` judge-only delegation (langfuse evaluator -> core re-score) on the mixed
  wiring (evaluator langfuse, ledger sql/postgres);
* ``doctor`` ``auth_check`` true against the self-bootstrapped project.

Two live findings are visible in the assertions themselves and reported for WP3.2 (never patched
here): Langfuse SDK 5.0.4 -- the version the ``langfuse>=3`` extra resolves to -- removed
``client.api.trace.get`` (the plugin's ``usage()`` VERIFY line), and a v4 self-host no longer serves
the legacy ``GET /api/public/traces/{id}`` at all (404, "events_only mode"), so the plugin's
``usage()`` read cannot work on the current supported pairing; and the SDK drops an item whose
``task`` raised from ``ExperimentResult.item_results``, so the row never reaches ``RowResult.error``
and ``error_rate`` cannot see it. The read-back in C3/C4 uses the surface the installed SDK and
server actually expose (legacy ``api.trace.get`` when it answers, ``api.observations.get_many``
otherwise), so the harness stays green either way while the plugin gap is reported, not patched.

Deliberately not asserted (WP3.2): ``budget_exceeded_rate`` > 0, langsmith surfaces, cost accuracy
(usage is asserted present, not exact), archived-dataset filtering beyond the read filter.

Names are unique per session (``TOKEN``), so a second run against the same compose volumes can never
satisfy an assertion from a previous one. Cleanup is the module finalizer's job (WP3.1b E3), in this
order: the prompts this session registered are deleted through the API; the Langfuse compose project
is torn down with ``docker compose -f tests/contract/docker-compose.langfuse.yml down`` (best-effort:
a teardown failure is a recorded warning, never a test error) -- but **only** when this module
started it, i.e. when the boot helper itself ran that file's ``up -d`` and
``EVALOPS_TEST_LANGFUSE_BASE_URL`` supplied no externally managed stack; an external stack is never
torn down. That ``down`` is scoped by the file's ``name:``, so the shared ledger Postgres/MLflow
project (``docker-compose.yml``) can never be collateral -- the reason the stack is a separate
project instead of a ``--profile`` of the shared one. ``docker compose -f
tests/contract/docker-compose.langfuse.yml down -v`` stays the manual full reset. Langfuse
exposes no SDK delete for datasets or experiments (and deleting their children leaves a half-linked
experiment), so that residue is documented and named, not silently dropped.
"""

from __future__ import annotations

import contextlib
import datetime
import importlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import warnings
from string import Template
from typing import Any, cast

import pytest

pytestmark = pytest.mark.contract

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parents[1]
SRC = REPO / "src"
#: The shared contract project: the ledger Postgres B2 wires as ``ledger: sql`` (and the MLflow
#: slice the other contract modules use). Booted here, never torn down here.
LEDGER_COMPOSE = HERE / "docker-compose.yml"
#: The Langfuse stack as its own compose project (``name: evalops-contract-langfuse``): a ``down``
#: through this file is project-scoped, so it cannot reach the ledger project.
LANGFUSE_COMPOSE = HERE / "docker-compose.langfuse.yml"
DEFAULT_BASE_URL = "http://127.0.0.1:33000"
DEFAULT_PUBLIC_KEY = "pk-lf-e2e-contract"
DEFAULT_SECRET_KEY = "sk-lf-e2e-contract"
DEFAULT_POSTGRES_URL = "postgresql+psycopg://evalops:evalops@127.0.0.1:55432/evalops_test"
#: The org/project the compose fixture seeds through `LANGFUSE_INIT_*` (asserted when we own the URL).
SEEDED_PROJECT_ID = "wp31b-contract-project"
#: First boot of the Langfuse project runs the Postgres + ClickHouse migrations (A4).
COMPOSE_READY_SECONDS = 300
#: `down` stops the Langfuse project's six containers; a hung one must not hang the session (E3).
COMPOSE_DOWN_SECONDS = 180
#: An explicitly provided URL is not ours to boot: a dead one must skip quickly, not hang (H4).
EXTERNAL_READY_SECONDS = 60
#: Trace ingestion is asynchronous (worker queue -> ClickHouse): poll a documented condition.
INGESTION_SECONDS = 120
#: Per-session names, so two runs against one long-lived stack never share state.
TOKEN = uuid.uuid4().hex[:10]

sys.path.insert(0, str(SRC))

#: Set by `_start_compose` when *this module* ran the Langfuse project's `up -d` (never when
#: `EVALOPS_TEST_LANGFUSE_BASE_URL` supplied an externally managed stack): only such a stack is ours
#: to tear down again. It covers the Langfuse project only -- the shared ledger project stays up.
_COMPOSE_STARTED = False
#: Cleanup inventory: the prompts are removable through the SDK, the rest is not (see `_cleanup`).
_PROMPTS: list[str] = []


class _Live:
    """What the module needs from the live stack, resolved once per session."""

    def __init__(self, *, seeded: bool):
        #: True when the compose fixture (not an override) supplies the stack.
        self.seeded = seeded


# ---------------------------------------------------------------------------
# skip ladder (the test_ledger_postgres.py / test_mlflow_plugin.py pattern)
# ---------------------------------------------------------------------------
def _drivers() -> None:
    pytest.importorskip("langfuse", reason="the langfuse extra is not installed (pip install 'evalops[langfuse]')")
    pytest.importorskip("sqlalchemy", reason="the sql extra is not installed (pip install 'evalops[sql]')")
    pytest.importorskip("psycopg", reason="the Postgres driver is not installed (pip install 'evalops[sql]')")


def _up(path: pathlib.Path) -> None:
    """`docker compose -f <path> up -d`; a failure is the caller's to turn into an environment skip."""
    subprocess.run(
        ["docker", "compose", "-f", str(path), "up", "-d"],
        check=True,
        capture_output=True,
        text=True,
        # A first-time Langfuse `up` also pulls ~2GB (web+worker, clickhouse, chainguard minio):
        # a pull is not readiness, so it may not eat the readiness budget below.
        timeout=1200,
    )


def _start_compose() -> None:
    """Boot both contract compose projects once per session: the ledger stack, then Langfuse.

    Two files, two projects: ``docker-compose.yml`` carries the ledger Postgres (and the MLflow
    slice the other contract modules use), ``docker-compose.langfuse.yml`` the six Langfuse
    containers + two inits. They are separate compose projects on purpose -- a ``--profile``-filtered
    ``down`` on one shared file takes the *whole* project down (profiles filter ``up``, not
    ``down``), so the Langfuse teardown would have removed the ledger Postgres with it.
    """
    global _COMPOSE_STARTED
    if _COMPOSE_STARTED:
        return
    if not shutil.which("docker"):
        pytest.skip("docker is not available and EVALOPS_TEST_LANGFUSE_BASE_URL is unset")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - any docker problem means skip this optional contract test
        pytest.skip(
            f"docker is not usable ({type(exc).__name__}); set EVALOPS_TEST_LANGFUSE_BASE_URL to run this against one"
        )
    try:
        _up(LEDGER_COMPOSE)
    except Exception as exc:  # noqa: BLE001 - image pull/start failures are an environment skip
        pytest.skip(f"could not start the ledger compose services: {type(exc).__name__}: {exc}")
    try:
        _up(LANGFUSE_COMPOSE)
    except Exception as exc:  # noqa: BLE001 - image pull/start failures are an environment skip
        pytest.skip(f"could not start the Langfuse compose services: {type(exc).__name__}: {exc}")
    _COMPOSE_STARTED = True


def _wait_until_ready(base_url: str, seconds: int) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base_url}/api/public/health", timeout=5) as response:  # noqa: S310
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001 - the container is still booting / migrating
            time.sleep(1)
    pytest.skip(f"Langfuse did not answer GET {base_url}/api/public/health within {seconds}s")


# ---------------------------------------------------------------------------
# fixtures: one live stack and one Postgres, shared by the module
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def live():
    _drivers()
    base_url = (os.getenv("EVALOPS_TEST_LANGFUSE_BASE_URL") or "").rstrip("/")
    public_key = os.getenv("EVALOPS_TEST_LANGFUSE_PUBLIC_KEY") or DEFAULT_PUBLIC_KEY
    secret_key = os.getenv("EVALOPS_TEST_LANGFUSE_SECRET_KEY") or DEFAULT_SECRET_KEY
    seeded = not base_url
    # The plugin's credentials are read from the environment by `langfuse.get_client()`, which is a
    # process-wide singleton: set them before the first plugin is instantiated (module scope).
    env = pytest.MonkeyPatch()
    env.setenv("LANGFUSE_PUBLIC_KEY", public_key)
    env.setenv("LANGFUSE_SECRET_KEY", secret_key)
    env.setenv("LANGFUSE_BASE_URL", base_url or DEFAULT_BASE_URL)
    if not base_url:
        _start_compose()
        base_url = DEFAULT_BASE_URL
    _wait_until_ready(base_url, COMPOSE_READY_SECONDS if seeded else EXTERNAL_READY_SECONDS)
    try:
        yield _Live(seeded=seeded)
    finally:
        _cleanup()
        env.undo()


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
# live-server helpers (documented conditions, never blanket sleeps)
# ---------------------------------------------------------------------------
def _client():
    """The SDK client the plugin uses; credentials come from the env the `live` fixture set.

    Resolved through ``importlib`` so the offline suite (no ``langfuse`` extra) can collect this
    module without importing the vendor SDK at module import time.
    """
    return importlib.import_module("langfuse").get_client()


def _observation_names(trace_id: str) -> list[str]:
    """Span names for one trace, on whichever read surface the live stack exposes.

    The plugin's ``usage()`` VERIFY line reads ``api.trace.get``: SDK <= 4 exposes it, SDK 5 removed
    it in favour of ``api.observations.get_many`` (live finding, WP3.2 -- never patched here). A v4
    self-host also answers the legacy ``GET /public/traces/{id}`` with 404 ("v4 events_only mode"),
    so the read-back tries the legacy surface and falls through, instead of assuming one SDK shape.
    """
    client = _client()
    getter = getattr(client.api.trace, "get", None)
    if getter is not None:
        with contextlib.suppress(Exception):  # v4 events_only mode removed the legacy read
            trace = getter(trace_id)
            return [getattr(o, "name", "") for o in getattr(trace, "observations", None) or []]
    response = client.api.observations.get_many(trace_id=trace_id, limit=50)
    return [getattr(o, "name", "") for o in response.data]


def _trace_get_works(trace_id: str) -> bool:
    """True iff the plugin's own VERIFY read (``api.trace.get``) answers for a real trace id."""
    getter = getattr(_client().api.trace, "get", None)
    if getter is None:
        return False
    try:
        getter(trace_id)
        return True
    except Exception:  # noqa: BLE001 - absent SDK method (5.x) or 404 (v4 events_only mode)
        return False


def _poll_observation_names(trace_id: str) -> list[str]:
    """Ingestion is async: poll until the trace has observations, with a deadline."""
    deadline = time.time() + INGESTION_SECONDS
    while time.time() < deadline:
        names = _observation_names(trace_id)
        if names:
            return names
        time.sleep(1)
    return []


def _experiments(dataset_id: str | None = None, *, experiment_id: str | None = None) -> list[Any]:
    client = _client()
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    kwargs: dict[str, Any] = {"from_start_time": since}
    if dataset_id:
        kwargs["dataset_id"] = dataset_id
    if experiment_id:
        kwargs["id"] = experiment_id
    return list(client.api.experiments.list(**kwargs).data)


def _poll_experiments(
    *, dataset_id: str | None = None, experiment_id: str | None = None, items: int | None = None
) -> list[Any]:
    """Poll until the experiment (and, when given, its item count) is visible: ingestion is async."""
    deadline = time.time() + INGESTION_SECONDS
    found: list[Any] = []
    while time.time() < deadline:
        found = _experiments(dataset_id, experiment_id=experiment_id)
        if found and (items is None or sum(e.item_count for e in found) == items):
            return found
        time.sleep(1)
    return found


def _experiment_id_from_ref(ref: str) -> str:
    """`langfuse:<experiment url with ?baseline=<id>>` (or a bare id when no URL resolved)."""
    tail = ref.split("langfuse:", 1)[1]
    return tail.rsplit("baseline=", 1)[-1] if "baseline=" in tail else tail


def _cleanup() -> None:
    """Best-effort entity cleanup, then stack teardown; never part of the contract under test (E3).

    Only the prompt objects this session registered can be removed cleanly. The SDK exposes no
    delete for datasets or experiments, and removing their children (dataset items, traces) leaves
    the experiment listed with null inputs/observations -- observed live -- so the trace/dataset
    residue is left intact and *named* instead of half-deleted. Everything is namespaced
    ``wp31b_*`` per session; ``docker compose -f tests/contract/docker-compose.langfuse.yml down -v``
    is the reset.
    """
    try:
        client = _client()
    except Exception:  # noqa: BLE001 - the stack may be gone already
        client = None
    if client is not None:
        for name in _PROMPTS:
            with contextlib.suppress(Exception):
                client.api.prompts.delete(name)
    _teardown_compose()


def _teardown_compose() -> None:
    """`docker compose -f docker-compose.langfuse.yml down`, only for a stack we booted (E3).

    Two guards, both required: ``_COMPOSE_STARTED`` (the boot helper ran the Langfuse ``up -d``
    itself) and the absence of ``EVALOPS_TEST_LANGFUSE_BASE_URL`` (an externally managed stack is
    never ours to stop, even when ``postgres_url`` pulled the compose files up for its container).

    The command names the Langfuse file, and that file declares ``name:
    evalops-contract-langfuse``: compose ``down`` is project-scoped, so it removes only that project.
    The shared ledger Postgres/MLflow project is untouched -- the exact reason this stack is a
    separate project rather than a ``--profile`` of the shared file, where ``down`` ignored the
    filter and took the whole project with it. A teardown failure is recorded as a warning, never
    raised into the session.
    """
    global _COMPOSE_STARTED
    if not _COMPOSE_STARTED:
        return
    if os.getenv("EVALOPS_TEST_LANGFUSE_BASE_URL"):
        return
    command = ["docker", "compose", "-f", str(LANGFUSE_COMPOSE), "down"]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True, timeout=COMPOSE_DOWN_SECONDS)
    except Exception as exc:  # noqa: BLE001 - best-effort teardown, a failure is a warning
        warnings.warn(f"could not tear the Langfuse compose stack down: {type(exc).__name__}: {exc}", stacklevel=2)
    else:
        _COMPOSE_STARTED = False


# ---------------------------------------------------------------------------
# config / project helpers
# ---------------------------------------------------------------------------
_PLUGIN_CONFIG = Template(
    """
project: wp31b-contract
default_env: local
plugins: {prompts: langfuse, datasets: langfuse, ledger: sql, tracer: langfuse, evaluator: langfuse}
plugin_config:
  langfuse: {}
  sql: {url: "$postgres_url"}
app:
  behavior_config: $module:behavior_config
  predict_fn: $module:build_predict_fn
  scorers: $module:build_scorers
  judge_meta: $module:judge_meta
"""
)

#: `wiring` is the plugin map under test: the full langfuse wiring (B2), the local-data VERIFY
#: branch (`datasets: git`), or the tracer-only wiring (core evaluator + langfuse tracer, which is
#: the only way the tracer role's `wrap`/`usage` are actually invoked -- ADR 0008).
_WIRINGS = {
    "langfuse": "{prompts: langfuse, datasets: langfuse, ledger: sql, tracer: langfuse, evaluator: langfuse}",
    "local-data": "{prompts: langfuse, datasets: git, ledger: sql, tracer: langfuse, evaluator: langfuse}",
    "tracer": "{prompts: git, datasets: git, ledger: sql, tracer: langfuse, evaluator: core}",
}

_PROJECT_CONFIG = Template(
    """
project: wp31b-contract
default_env: local
plugins: $plugins
plugin_config:
  langfuse: {}
  sql: {url: "$postgres_url"}
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

#: A deterministic app: no network, no sleeps, no vendor calls. `predict` reports fixed usage so
#: WP3.0 B2 (`report_usage` under a *vendor* evaluator) is exercised instead of assumed; `match`
#: compares the lookup answer and `shout` (a second, plain scorer) exists so the judge-only pass can
#: re-score with a scorer the recorded run did not aggregate.
_APP_SOURCE = """
import evalops

ANSWERS = {"what is one plus one?": "two", "what is two plus two?": "four"}


def behavior_config():
    return {"llm_endpoint": "lookup-table@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        evalops.report_usage(input_tokens=11, output_tokens=7)
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

#: Per-row usage the fixture app reports (11 in / 7 out), so assertions can prove the stash path.
IN_TOKENS, OUT_TOKENS = 11, 7


def _records() -> list[dict]:
    """The two-row dataset, freshly built each call (records are caller-owned)."""
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


def _plugins(tmp_path: pathlib.Path) -> dict[str, Any]:
    """The role plugins on a plain langfuse config (no project files needed for C1/C2/C6)."""
    from evalops.registry import Registry

    root = _project_root(tmp_path, "plugins")
    # The sql URL is inert for the roles this helper resolves (prompts/datasets); it only has to
    # validate so the config loads.
    (root / "evalops.yaml").write_text(
        _PLUGIN_CONFIG.substitute(postgres_url=DEFAULT_POSTGRES_URL, module="wp31b_noop")
    )
    registry = Registry(_load(root))
    return {role: registry.get(role) for role in ("prompts", "datasets")}


def _app_module() -> str:
    return f"wp31b_app_{TOKEN}_{uuid.uuid4().hex[:6]}"


def _project(
    tmp_path: pathlib.Path, name: str, *, wiring: str = "langfuse", postgres_url: str | None = None
) -> tuple[pathlib.Path, str, str, str]:
    """A git-clean temp project (synced+committed), like tests/unit/_projects.py's helper.

    Returns `(root, prompt_name, dataset_name, app_module)`. Names carry a per-project `name`, so
    two tests never share a Langfuse dataset (experiment listings are per dataset).
    """
    module, prompt, dataset = _app_module(), f"wp31b_prompt_{name}_{TOKEN}", f"wp31b_rows_{name}_{TOKEN}"
    root = _project_root(tmp_path, name)
    (root / "evalops.yaml").write_text(
        _PROJECT_CONFIG.substitute(
            plugins=_WIRINGS[wiring],
            postgres_url=postgres_url or DEFAULT_POSTGRES_URL,
            module=module,
            dataset=dataset,
        )
    )
    (root / ".gitignore").write_text((REPO / ".gitignore").read_text())
    (root / "prompts").mkdir()
    (root / "prompts" / f"{prompt}.txt").write_text("Answer briefly.\n\nQuestion: {{question}}\n")
    (root / "datasets").mkdir()
    (root / "datasets" / f"{dataset}.jsonl").write_text("".join(json.dumps(record) + "\n" for record in _records()))
    (root / f"{module}.py").write_text(_APP_SOURCE)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "wp31b@example.test")
    _git(root, "config", "user.name", "wp31b")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _sync(root)
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "evalops: sync locks")
    _PROMPTS.append(prompt)
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


def _as_set(records: list[dict]) -> list[str]:
    return sorted(json.dumps(record, sort_keys=True) for record in records)


def _all_rows(ledger, run_id: str) -> list[dict]:
    return [json.loads(line) for line in ledger.get_artifact(run_id, "evalops/rows.jsonl").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# C1: prompts
# ---------------------------------------------------------------------------
def test_prompts_register_load_labels_and_aliases(tmp_path, live):
    store = _plugins(tmp_path)["prompts"]
    name = f"wp31b_prompt_labels_{TOKEN}"
    _PROMPTS.append(name)
    template = "Answer briefly.\n\nQuestion: {{question}}\n"
    tags = {"evalops.source": "tests/contract/test_langfuse_plugin.py"}

    v1 = store.register(name, template, tags)
    assert store.load(name, v1).template == template
    # A second register is a NEW immutable version, never an in-place edit.
    v2 = store.register(name, template, tags)
    assert int(v2) > int(v1)

    assert store.get_alias(name, "prod") is None  # nothing promoted yet
    store.set_alias(name, "prod", v1)
    assert store.get_alias(name, "prod") == str(v1)
    # `update_prompt`'s new_labels REPLACES the version's labels: the second alias must preserve
    # `prod` (and the plugin must not re-add the server-managed `latest`, which lives on v2).
    store.set_alias(name, "staging", v1)
    assert store.get_alias(name, "prod") == str(v1)
    assert store.get_alias(name, "staging") == str(v1)
    assert store.get_alias(name, "no-such-alias") is None

    raw = _client().get_prompt(name, version=int(v1), type="text", cache_ttl_seconds=0)
    assert sorted(raw.labels or []) == ["prod", "staging"]
    # `latest` is the server's pointer to the newest version; v2 keeps it, v1 must not regain it.
    assert "latest" in (_client().get_prompt(name, version=int(v2), type="text", cache_ttl_seconds=0).labels or [])


# ---------------------------------------------------------------------------
# C2: datasets
# ---------------------------------------------------------------------------
def test_datasets_write_exists_read_and_write_idempotence(tmp_path, live):
    store = _plugins(tmp_path)["datasets"]
    name = f"wp31b_dataset_{TOKEN}"

    assert store.exists(name) is False
    store.write(name, _records())
    assert store.exists(name) is True
    # Record *order* is the backend's business (Langfuse returns them newest-first); the content is
    # not (WP3.1a finding #7: the port leaves order undefined).
    assert _as_set(store.read(name)) == _as_set(_records())

    # Write idempotence: the sha256 item ids upsert server-side, so a retried sync cannot duplicate
    # rows (the plugin's own retry story for `datasets sync`).
    store.write(name, _records())
    assert len(store.read(name)) == len(_records())
    assert _as_set(store.read(name)) == _as_set(_records())


# ---------------------------------------------------------------------------
# C3: the tracer role + the `api.trace.get` / usage() VERIFY line
# ---------------------------------------------------------------------------
def test_tracer_evaluate_trace_ids_are_readable_and_usage_is_exercised(tmp_path, live, postgres_url):
    from evalops import run
    from evalops.ports import Ledger, Tracer, Usage
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, "tracer", wiring="tracer", postgres_url=postgres_url)
    cfg = _load(root)
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()

    out = run.evaluate(cfg, dataset, "dev")
    rows = out["_rows"][0]
    assert len(rows) == len(_records())
    trace_ids = [r.trace_id for r in rows]
    assert all(trace_ids), rows  # the tracer role returned a real backend trace id per row
    assert out["runs"][0]["metrics"]["rows_missing_usage"] == 0  # report_usage -> core collector

    # The map's VERIFY line: after flush, a real trace id is readable server-side with the row span
    # name `evalops.row` (the name LangfusePlugin.wrap creates).
    for trace_id in trace_ids:
        names = _poll_observation_names(trace_id)
        assert "evalops.row" in names, (trace_id, names)

    tracer = cast(Tracer, Registry(cfg).get("tracer"))
    usage = tracer.usage(trace_ids)  # the call path runs on every SDK generation (never raises)
    assert set(usage) <= set(trace_ids), usage
    if _trace_get_works(trace_ids[0]):
        # The documented contract, when the stack still serves the legacy read the plugin uses.
        assert set(usage) == set(trace_ids), usage
        assert all(isinstance(item, Usage) for item in usage.values()), usage
    else:
        # LIVE FINDING (WP3.2): SDK 5.0.4 -- the version `langfuse>=3` resolves to -- removed
        # `client.api.trace.get`, and a v4 self-host 404s the legacy trace read; `usage()` therefore
        # silently degrades to `{}`. The row still gets usage because the fixture app reports it
        # (WP3.0 B2); trace-derived usage cannot be read at all on the current supported pairing.
        assert usage == {}, usage


# ---------------------------------------------------------------------------
# C4: the evaluator on the native-dataset path (all roles langfuse)
# ---------------------------------------------------------------------------
def test_evaluator_native_dataset_run_scores_metrics_and_ledger_artifacts(tmp_path, live, postgres_url):
    from evalops import run
    from evalops.contract import formats
    from evalops.ports import Ledger
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, "native", postgres_url=postgres_url)
    cfg = _load(root)
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()

    out = run.evaluate(cfg, dataset, "dev")
    run_id = out["run_ids"][0]
    metrics = out["runs"][0]["metrics"]
    assert metrics["match/mean"] == 1.0
    # WP3.0 A1: the scorer aggregate AND the run-layer error metrics, for a vendor evaluator.
    assert metrics["error_rate"] == 0.0
    assert metrics["budget_exceeded_rate"] == 0.0
    # WP3.0 B2: `report_usage` inside predict is harvested under a vendor evaluator, so the rows are
    # not silently usage-less even though the SDK's trace-derived path is broken (see C3).
    assert metrics["rows_missing_usage"] == 0
    assert metrics["gen_input_tokens"] == IN_TOKENS * len(_records())
    assert metrics["gen_output_tokens"] == OUT_TOKENS * len(_records())

    native_ref = out["runs"][0]["native_ref"]
    assert native_ref and native_ref.startswith("langfuse:"), native_ref

    # Rows + manifest landed in the *Postgres ledger* (ledger: sql, not langfuse).
    rows = _all_rows(ledger, run_id)
    assert len(rows) == len(_records())
    assert [r["scores"]["match"] for r in rows] == [1.0, 1.0]
    assert all(r["trace_id"] for r in rows), rows
    rows_text = ledger.get_artifact(run_id, "evalops/rows.jsonl")
    manifest = json.loads(ledger.get_artifact(run_id, "evalops/manifest.json"))
    assert manifest["artifacts"]["evalops/rows.jsonl"] == formats.sha256(rows_text)
    assert "evalops/prompts.lock.json" in manifest["artifacts"]
    assert "evalops/datasets.lock.json" in manifest["artifacts"]

    # The dataset run is visible server-side: one experiment for this session's dataset, one item
    # per row, each carrying the trace the plugin returned.
    physical = ledger.find_runs(tags={"evalops.group": out["group"]}, limit=1)[0].params["dataset_physical"]
    dataset_id = _client().get_dataset(physical).id
    experiments = _poll_experiments(dataset_id=dataset_id, items=len(_records()))
    assert len(experiments) == 1, [(e.id, e.name) for e in experiments]
    assert experiments[0].id in native_ref
    assert experiments[0].item_count == len(_records())
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    items = _client().api.experiments.list_items(experiment_id=experiments[0].id, from_start_time=since).data
    assert len(items) == len(_records())
    assert all(i.trace_id for i in items), items


# ---------------------------------------------------------------------------
# C4 (continued): the local-data VERIFY branch -- datasets: git, so `run_experiment(data=...)`
# ---------------------------------------------------------------------------
def test_evaluator_local_data_run_experiment_is_visible_server_side(tmp_path, live, postgres_url):
    from evalops import run
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, "localdata", wiring="local-data", postgres_url=postgres_url)
    cfg = _load(root)
    cast(Any, Registry(cfg).get("ledger")).migrate()

    out = run.evaluate(cfg, dataset, "dev")
    metrics = out["runs"][0]["metrics"]
    assert metrics["match/mean"] == 1.0
    assert metrics["error_rate"] == 0.0
    native_ref = out["runs"][0]["native_ref"]
    assert native_ref and native_ref.startswith("langfuse:"), native_ref

    rows = out["_rows"][0]
    assert len(rows) == len(_records())
    assert [r.scores["match"] for r in rows] == [1.0, 1.0]
    assert all(r.trace_id for r in rows), rows

    # The top-level runner's experiment has no dataset: it was fed local rows, not a Langfuse one.
    experiment_id = _experiment_id_from_ref(native_ref)
    experiments = _poll_experiments(experiment_id=experiment_id, items=len(_records()))
    assert [e.id for e in experiments] == [experiment_id]
    assert experiments[0].dataset_id is None
    assert experiments[0].item_count == len(_records())


# ---------------------------------------------------------------------------
# C5: stored_rows judge-only re-score on the mixed wiring (evaluator langfuse + ledger sql/postgres)
# ---------------------------------------------------------------------------
def test_stored_rows_judge_only_rescore_through_the_langfuse_evaluator(tmp_path, live, postgres_url):
    from evalops import run
    from evalops.ports import Ledger
    from evalops.registry import Registry

    root, _prompt, dataset, _module = _project(tmp_path, "judgeonly", postgres_url=postgres_url)
    cfg = _load(root)
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()

    # Source run: the real _evaluate, evaluator=langfuse, ledger=Postgres.
    out = run.evaluate(cfg, dataset, "dev")
    run_id = out["run_ids"][0]
    assert out["runs"][0]["metrics"]["match/mean"] == 1.0
    assert out["runs"][0]["metrics"]["error_rate"] == 0.0
    native_ref = out["runs"][0]["native_ref"]
    assert native_ref.startswith("langfuse:")
    # The stored rows live in the Postgres ledger and are read back through it.
    source_rows = _all_rows(ledger, run_id)
    assert [r["scores"]["match"] for r in source_rows] == [1.0, 1.0]

    # Judge-only: no predict_fn; the langfuse evaluator delegates to the core re-score with `shout`.
    rerun = run.evaluate(cfg, dataset, "dev", judge_only=True, source_group=out["group"], scorer_filter={"shout"})
    rerun_id = rerun["run_ids"][0]
    assert rerun_id != run_id
    rows = _all_rows(ledger, rerun_id)
    assert len(rows) == len(_records())
    assert [r["scores"]["shout"] for r in rows] == [1.0, 1.0]
    assert "match" not in rows[0]["scores"]  # the filter selected only the re-score scorer
    assert rerun["runs"][0]["metrics"]["shout/mean"] == 1.0
    assert rerun["runs"][0]["metrics"]["error_rate"] == 0.0


# ---------------------------------------------------------------------------
# C6: doctor / auth_check against the self-bootstrapped project
# ---------------------------------------------------------------------------
def test_doctor_auth_check_against_the_seeded_project(tmp_path, live):
    plugins = _plugins(tmp_path)
    doctor = plugins["prompts"].doctor()
    assert doctor == {"plugin": "langfuse", "ok": True}, doctor

    client = _client()
    assert client.auth_check() is True
    projects = [p.id for p in client.api.projects.get().data]
    assert projects  # the pinned keys see at least one project
    if live.seeded:
        # The compose fixture's `LANGFUSE_INIT_PROJECT_ID`, i.e. no account was created by hand.
        assert projects == [SEEDED_PROJECT_ID], projects
