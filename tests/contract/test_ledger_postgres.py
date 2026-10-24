"""WP1.2 contract: the SQL ledger's migrate/verify/reserve race on a real Postgres.

Excluded from the offline suite by the ``contract`` marker. Skips with a clear reason when the
optional drivers are missing, docker is unavailable, or the compose image cannot be pulled/started.
Set ``EVALOPS_TEST_POSTGRES_URL`` (a SQLAlchemy URL) to use an already-running database instead.
"""

import importlib
import os
import pathlib
import random
import shutil
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.contract

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_URL = "postgresql+psycopg://evalops:evalops@127.0.0.1:55432/evalops_test"


class _Cfg:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.lock_dir = self.root / ".evalops" / "locks" / "local"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.env = "local"


def _drivers():
    pytest.importorskip("sqlalchemy", reason="the sql extra is not installed (pip install 'evalops[sql]')")
    pytest.importorskip("psycopg", reason="the Postgres driver is not installed (pip install 'evalops[sql]')")


def _start_compose():
    if not shutil.which("docker"):
        pytest.skip("docker is not available and EVALOPS_TEST_POSTGRES_URL is unset")
    try:
        subprocess.run(["docker", "info"], check=True, capture_output=True, timeout=20)
    except Exception as exc:  # noqa: BLE001 - any docker problem means skip this optional contract test
        pytest.skip(f"docker is not usable ({type(exc).__name__}); set EVALOPS_TEST_POSTGRES_URL to run it")
    try:
        subprocess.run(
            ["docker", "compose", "-f", str(HERE / "docker-compose.yml"), "up", "-d"],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001 - image pull/start failures are an environment skip
        pytest.skip(f"could not start the Postgres compose service: {type(exc).__name__}: {exc}")


def _wait_until_ready(url):
    sa = importlib.import_module("sqlalchemy")

    engine = sa.create_engine(url)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except Exception:  # noqa: BLE001 - the container is still booting
            time.sleep(1)
    pytest.skip("Postgres did not become ready within 60s")


def test_ledger_postgres_migrate_verify_reserve_race_and_settle(tmp_path):
    _drivers()
    url = os.getenv("EVALOPS_TEST_POSTGRES_URL")
    if not url:
        _start_compose()
        url = DEFAULT_URL
    _wait_until_ready(url)

    from evalops.plugins.sql import SqlPlugin

    cfg = _Cfg(tmp_path)
    plugin = SqlPlugin(cfg, {"url": url})
    plugin.migrate()
    # The database outlives one test run (compose or EVALOPS_TEST_POSTGRES_URL): drop the budget rows
    # a previous attempt left behind, so a rerun starts from the same state as a fresh container.
    for table in ("evalops_budget", "evalops_budget_month"):
        plugin._exec(f"DELETE FROM {table}")
    assert plugin.verify()["ok"] is True

    month = f"9{random.randint(0, 999):03d}-01"  # unique VARCHAR(7) month so reruns do not collide
    amount, cap = 0.25, 0.75
    # Real runs for the reservations: `verify` reports a budget row whose run does not exist as an
    # orphan, so the reservations must hang off runs that were actually started.
    run_ids = [plugin.start_run(f"pg-{month}-{i}", {}) for i in range(8)]
    barrier = threading.Barrier(8)
    results, errors = [], []

    def worker(i):
        local = SqlPlugin(cfg, {"url": url})
        try:
            barrier.wait(timeout=20)
            results.append((i, local.budget_reserve(run_ids[i], amount, month, cap)))
        except BaseException as exc:  # noqa: BLE001 - report every thread failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors
    verdicts = [verdict for _i, verdict in results]
    assert (verdicts.count(True), verdicts.count(False)) == (3, 5), results
    assert abs(plugin.budget_reserved(month) - 3 * amount) < 1e-9

    winner = next(i for i, verdict in results if verdict)
    plugin.budget_settle(run_ids[winner], 0.1)
    assert plugin.budget_reserved(month) <= 3 * amount
    assert plugin.verify()["ok"] is True
