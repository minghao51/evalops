"""Shared helpers for the WP1.4 CLI-path tests: temp projects, the real CLI, in-memory ledger runs.

Not a test module (no ``test_`` prefix), so pytest and ``tests/run_without_pytest.py`` both skip it.
A test module imports it as ``import _projects as proj`` after putting this directory on ``sys.path``.

Two rules the helpers enforce:

* every CLI run asserts stdout is exactly ONE JSON document (the ``--json`` contract, §2);
* every temp project is git-init'd with ``.evalops/`` ignored, so a run is clean unless the test
  deliberately edits a tracked file (that is how ``eval``'s exit 4 is reached).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
MEMORY_LEDGER_SOURCE = FIXTURES / "formats" / "memory_ledger.py"

#: A complete local project: git for prompts+datasets, SQLite for the ledger, the fake app hooks.
BASE_CONFIG = """
project: wp14
default_env: local
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
app:
  behavior_config: fake_app:behavior_config
  predict_fn: fake_app:build_predict_fn
  scorers: fake_app:build_scorers
  judge_meta: fake_app:judge_meta
datasets: {smoke: {}}
eval: {max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
"""

_COUNTER = itertools.count()


# ---------------------------------------------------------------------------
# temp projects
# ---------------------------------------------------------------------------
def tmpdir(prefix: str = "evalops-wp14-") -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def commit(root: pathlib.Path, message: str = "test") -> None:
    """Stage everything and commit (a clean tree is what `eval` requires)."""
    git(root, "add", "-A")
    subprocess.run(
        ["git", "commit", "-qm", message, "--allow-empty"], cwd=root, capture_output=True, text=True, check=False
    )


def project(
    config: str = BASE_CONFIG, files: dict[str, str] | None = None, dataset_text: str | None = None
) -> pathlib.Path:
    """A git-init'd temp project with the shared prompt/dataset sources and the fake app."""
    root = tmpdir()
    (root / "evalops.yaml").write_text(config)
    (root / ".gitignore").write_text(".evalops/\n__pycache__/\n")
    (root / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "support_system.txt", root / "prompts" / "support_system.txt")
    (root / "datasets").mkdir()
    shutil.copy(REPO / "datasets" / "smoke.jsonl", root / "datasets" / "smoke.jsonl")
    if dataset_text is not None:
        (root / "datasets" / "smoke.jsonl").write_text(dataset_text)
    shutil.copy(FIXTURES / "fake_app.py", root / "fake_app.py")
    for name, text in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(root, "init", "-q")
    git(root, "config", "user.email", "wp14@example.test")
    git(root, "config", "user.name", "wp14")
    commit(root, "init")
    return root


def pipeline_project(
    config: str = BASE_CONFIG, datasets: dict[str, str] | None = None, files: dict[str, str] | None = None
) -> pathlib.Path:
    """Like `project`, but with the repo's real .gitignore: locks/store are tracked, `.evalops/out` is not.

    The pipeline's `commit_state` is meaningful only in a repo that versions its state (the bash
    script commits `.evalops/locks` and `.evalops/store`), and `eval` refuses a dirty tree, so the
    pipeline tests need exactly the rules this repo ships. `datasets` writes extra dataset files
    (declare every one of them in the config's `datasets:` block).
    """
    root = project(config, files=files)
    (root / ".gitignore").write_text((REPO / ".gitignore").read_text())
    for name, text in (datasets or {}).items():
        (root / "datasets" / f"{name}.jsonl").write_text(text)
    commit(root, "pipeline: versioned state tracked")
    return root


def bare_origin(root: pathlib.Path) -> pathlib.Path:
    """A local **bare** repo registered as `origin` (never a remote): state pushes stay offline."""
    origin = tmpdir("evalops-origin-")
    git(origin, "init", "--bare", "-q")
    git(root, "remote", "add", "origin", str(origin))
    return origin


def git_out(root: pathlib.Path, *args: str) -> str:
    """Stdout of a read-only git command (``rev-parse``, ``status``, ``log``, ...)."""
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


# ---------------------------------------------------------------------------
# running the CLI
# ---------------------------------------------------------------------------
def env(extra: dict | None = None) -> dict:
    values = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
        **(extra or {}),
    }
    for stale in ("EVALOPS_CONFIG", "EVALOPS_REDACT", "EVALOPS_ALLOW_FROZEN_CHANGE"):
        if stale not in (extra or {}):
            values.pop(stale, None)
    for name, value in (extra or {}).items():
        if value is None:  # an explicit None means "this variable is not set"
            values.pop(name, None)
    return values


def one_document(text: str) -> dict:
    """Assert `text` is exactly one JSON object and return it (nothing before it, nothing after)."""
    stripped = text.strip()
    assert stripped.startswith("{"), f"stdout is not a JSON document: {text!r}"
    document, end = json.JSONDecoder().raw_decode(stripped)
    assert end == len(stripped), f"extra output after the JSON document: {stripped[end:]!r}"
    assert isinstance(document, dict), document
    return document


def run_cli(root: pathlib.Path, *args: str, env_extra: dict | None = None) -> tuple[int, dict]:
    """Run the real CLI as a subprocess -> (process exit code, envelope)."""
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=env(env_extra),
        capture_output=True,
        text=True,
    )
    return proc.returncode, one_document(proc.stdout)


def run_cli_in_process(root: pathlib.Path, *args: str) -> tuple[int, dict]:
    """Run `cli.main` in this process (needed when a fixture plugin lives in this process)."""
    from evalops import cli as cli_mod

    cwd, buf = os.getcwd(), io.StringIO()
    os.chdir(root)
    try:
        with contextlib.redirect_stdout(buf):
            code = cli_mod.main(["--json", *args])
    finally:
        os.chdir(cwd)
    return code, one_document(buf.getvalue())


# ---------------------------------------------------------------------------
# the in-memory ledger fixture (fabricated runs, no backend)
# ---------------------------------------------------------------------------
def ledger_config(name: str) -> str:
    """`BASE_CONFIG` with the ledger role pointed at the in-memory fixture module `name`."""
    old = "plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}"
    new = f'plugins: {{prompts: git, datasets: git, ledger: "{name}:MemoryLedger", tracer: none, evaluator: core}}'
    assert old in BASE_CONFIG
    return BASE_CONFIG.replace(old, new)


def ledger_name() -> str:
    """A process-unique module name, so two test projects never share `sys.modules` state."""
    return f"_wp14_ledger_{os.getpid()}_{next(_COUNTER)}"


def install_ledger(root: pathlib.Path, name: str) -> Any:
    """Write the memory-ledger fixture into `root` under `name` and import it (same object the CLI gets)."""
    path = root / f"{name}.py"
    path.write_text(MEMORY_LEDGER_SOURCE.read_text())
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def ledger_project(files: dict[str, str] | None = None) -> tuple[pathlib.Path, Any]:
    """A temp project whose ledger is a fresh in-memory fixture module; state starts empty."""
    name = ledger_name()
    root = project(ledger_config(name), files=files)
    module = install_ledger(root, name)
    ledger_clear(module)
    return root, module


def ledger_clear(module: Any) -> None:
    module.MemoryLedger.runs.clear()
    module.MemoryLedger.artifacts.clear()


def add_run(
    module: Any,
    run_id: str,
    *,
    tags: dict | None = None,
    params: dict | None = None,
    metrics: dict | None = None,
    artifacts: dict[str, str] | None = None,
    start_ms: int = 1,
) -> None:
    """One fabricated run in the memory ledger, in exactly the shape `find_runs` re-reads."""
    module.MemoryLedger.runs[run_id] = {
        "name": run_id,
        "tags": dict(tags or {}),
        "params": dict(params or {}),
        "metrics": dict(metrics or {}),
        "start_ms": start_ms,
    }
    module.MemoryLedger.artifacts[run_id] = dict(artifacts or {})


# ---------------------------------------------------------------------------
# small file helpers
# ---------------------------------------------------------------------------
def read_json(path: pathlib.Path) -> dict:
    return json.loads(path.read_text())


def write_json(path: pathlib.Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")


def last_eval_path(root: pathlib.Path) -> pathlib.Path:
    return root / ".evalops" / "out" / "last_eval.json"


def fixture_text(name: str) -> str:
    """The source of one committed test fixture (for copying it into a temp project)."""
    return (FIXTURES / name).read_text()
