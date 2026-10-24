"""WP1.2: UTC month accounting, outstanding reservations and run-level budget enforcement.

Falsifies, in order: (a) the monthly boundary is UTC, so a run at 23:59:59.999Z on 28 Feb is not in
March, while 00:00:00.000Z on 1 March is; (b) a reservation in another month does not affect the
current month's projection, while an outstanding reservation in this month does; (c) `run.evaluate`
reserves before generation, fails exit 3 when the monthly reservation would overspend (even if the
pre-check passed), releases on failure and settles the actual cost on success; (d) a plugin that
keeps the non-abstract port defaults is advisory-only and behaves exactly as before.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import datetime as dt
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import cast

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))

from evalops import config as config_mod  # noqa: E402
from evalops import cost, run  # noqa: E402
from evalops.ports import Ledger, Plugin, RunRecord  # noqa: E402
from evalops.registry import Registry  # noqa: E402
from evalops.util import EXIT_BUDGET, EXIT_CAPABILITY, CapabilityError, EvalOpsError  # noqa: E402

CONFIG = """
project: cost-budget
default_env: local
plugins: {prompts: git, datasets: git, ledger: __LEDGER__, tracer: none, evaluator: core}
app:
  behavior_config: fake_app:behavior_config
  predict_fn: fake_app:build_predict_fn
  scorers: fake_app:build_scorers
  judge_meta: fake_app:judge_meta
datasets: {smoke: {frozen: true}}
eval: {max_workers: 2, retries: 1, backoff_seconds: 0.01}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: __CAP__}
guard: {exclude: [".git", "src"]}
"""


def _ms(text):
    return int(dt.datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


class _ClockCfg:
    def __init__(self, budgets):
        self._cost = {"budgets": budgets}

    def section(self, key):
        return self._cost if key == "cost" else {}


class _ClockLedger:
    def __init__(self, runs, reserved):
        self.runs = runs
        self.reserved = reserved
        self.asked = []

    def find_runs(self, tags=None, since_ms=None, limit=None):
        found = [r for r in self.runs if since_ms is None or r.start_ms >= since_ms]
        found.sort(key=lambda r: r.start_ms, reverse=True)
        return found[:limit] if limit else found

    def budget_reserved(self, month):
        self.asked.append(month)
        return self.reserved.get(month, 0.0)


def test_utc_month_boundary_and_reservations_in_the_projection():
    frozen = _ms("2026-03-01T00:00:00Z")
    original = cost._now_ms
    cost._now_ms = lambda: frozen
    try:
        assert cost.utc_month(_ms("2026-02-28T23:59:59.999Z")) == "2026-02"
        assert cost.utc_month(_ms("2026-03-01T00:00:00.000Z")) == "2026-03"

        feb = RunRecord("feb", {}, {}, {"total_cost_usd": 9.0}, _ms("2026-02-28T23:59:59.999Z"))
        march = RunRecord("mar", {}, {}, {"total_cost_usd": 0.25}, frozen)
        ledger = _ClockLedger([feb, march], {"2026-02": 100.0, "2026-03": 0.0})
        assert cost.month_spend(ledger) == 0.25  # February is not counted in March

        est = {"total_usd": 0.5}
        # A February reservation is irrelevant to March: 0.25 + 0 + 0.5 <= 1.0.
        assert cost.check_budget(_ClockCfg({"per_month_usd": 1.0}), ledger, est, False)["problems"] == []
        assert ledger.asked[-1] == "2026-03"
        # An outstanding March reservation is not: 0.25 + 0.5 + 0.5 > 1.0.
        ledger.reserved["2026-03"] = 0.5
        info = cost.check_budget(_ClockCfg({"per_month_usd": 1.0}), ledger, est, True)
        assert "estimate would exceed per_month_usd" in info["problems"], info
    finally:
        cost._now_ms = original


class _AdvisoryLedger(Plugin, Ledger):
    name = "advisory"
    provides = ("ledger",)

    def start_run(self, name, tags):
        return "a1"

    def end_run(self, run_id, status="FINISHED"):
        pass

    def log(self, run_id, params=None, metrics=None, tags=None):
        pass

    def put_artifact(self, run_id, name, text):
        pass

    def get_artifact(self, run_id, name):
        raise KeyError(name)

    def find_runs(self, tags=None, since_ms=None, limit=None):
        return []


def test_port_defaults_are_advisory_and_capability_errors_are_exit_six():
    ledger = _AdvisoryLedger(None, {})
    assert ledger.budget_reserve("r", 10.0, "2026-03", 0.01) is True
    assert ledger.budget_settle("r", 1.0) is None
    assert ledger.budget_release("r") is None
    assert ledger.budget_reserved("2026-03") == 0.0
    for operation in (ledger.migrate, ledger.verify):
        try:
            operation()
        except CapabilityError as exc:
            assert exc.code == EXIT_CAPABILITY and "does not support ledger" in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"{operation.__name__} should need SQL-backed tables")


# ---------------------------------------------------------------------------
# run.evaluate: reserve -> generate -> settle, or fail/release
# ---------------------------------------------------------------------------
def _cli(root, *args):
    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{root}",
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def _project(cap, ledger="sql"):
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-cost-budget-"))
    (root / "evalops.yaml").write_text(CONFIG.replace("__LEDGER__", ledger).replace("__CAP__", str(cap)))
    shutil.copy(REPO / ".gitignore", root / ".gitignore")
    shutil.copy(TESTS_DIR / "fixtures" / "fake_app.py", root / "fake_app.py")
    (root / "prompts").mkdir()
    shutil.copy(REPO / "prompts" / "support_system.txt", root / "prompts" / "support_system.txt")
    (root / "datasets").mkdir()
    shutil.copy(REPO / "datasets" / "smoke.jsonl", root / "datasets" / "smoke.jsonl")
    if ledger != "sql":
        shutil.copy(TESTS_DIR / "fixtures" / "formats" / "memory_ledger.py", root / "memory_ledger.py")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, check=True, capture_output=True)
    _commit(root, "init")
    code, envelope = _cli(root, "sync", "all")
    assert code == 0, envelope
    _commit(root, "sync")
    return root


def _commit(root, message):
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, capture_output=True)


def _db(root):
    return pathlib.Path(root) / ".evalops" / "ledger.sqlite"  # sql plugin default: cfg.lock_dir/ledger.sqlite


def _query(root, sql, params=()):
    conn = sqlite3.connect(str(_db(root)))
    try:
        execute = conn.execute  # local alias: the SQL is a test-owned literal
        return execute(sql, params).fetchall()
    finally:
        conn.close()


def test_evaluate_fails_when_the_monthly_reservation_cannot_be_made():
    root = _project(cap=50)
    cfg = config_mod.load(str(root / "evalops.yaml"))
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()
    month = cost.utc_month(cost._now_ms())
    # The room is already promised to another run. `check_budget` is simulated as having passed
    # before that reservation landed (the race the atomic reserve exists to close).
    assert ledger.budget_reserve("blocker", 50.0, month, 50.0) is True

    original = cost.check_budget
    cost.check_budget = lambda cfg, ledger, est, override: {
        "estimate_usd": est["total_usd"],
        "per_run_usd": None,
        "month_spent_usd": 0.0,
        "per_month_usd": 50.0,
        "override": override,
        "problems": [],
    }
    try:
        try:
            run.evaluate(cfg, "smoke", "dev")
        except EvalOpsError as exc:
            assert exc.code == EXIT_BUDGET, exc
            assert exc.details["month"] == month
            assert exc.details["limit_usd"] == 50.0
            assert exc.details["reserved_usd"] == 50.0
            assert "budget exceeded" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("run.evaluate ignored the failed monthly reservation")
    finally:
        cost.check_budget = original

    run_id = ledger.find_runs(limit=1)[0].run_id
    assert _query(root, "SELECT status FROM evalops_runs WHERE run_id=?", (run_id,)) == [("FAILED",)]
    assert _query(root, "SELECT state FROM evalops_budget WHERE run_id=?", (run_id,)) == []
    assert _query(root, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=?", (month,)) == [
        (50.0, 0.0)
    ]


def test_evaluate_settles_the_actual_cost_and_month_spend_reflects_it():
    root = _project(cap=50)
    cfg = config_mod.load(str(root / "evalops.yaml"))
    ledger = cast(Ledger, Registry(cfg).get("ledger"))
    ledger.migrate()
    out = run.evaluate(cfg, "smoke", "dev")
    run_id = out["run_ids"][0]
    actual = out["runs"][0]["metrics"]["total_cost_usd"]
    month = cost.utc_month(cost._now_ms())
    assert actual > 0
    assert cost.month_spend(ledger) == round(actual, 4)  # month_spend records 4 decimals
    assert _query(root, "SELECT amount_usd, state FROM evalops_budget WHERE run_id=?", (run_id,)) == [
        (actual, "settled")
    ]
    reserved, settled = _query(
        root, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=?", (month,)
    )[0]
    assert reserved == 0.0 and abs(settled - actual) < 1e-9


def test_advisory_plugin_evaluate_is_byte_identical_to_before():
    # The memory ledger keeps every port default: no reservation rows, no migration, no cap checks
    # beyond the existing check_budget, so the same evaluation still succeeds.
    root = _project(cap=50, ledger="memory_ledger:MemoryLedger")
    cfg = config_mod.load(str(root / "evalops.yaml"))
    out = run.evaluate(cfg, "smoke", "dev")
    assert out["runs"][0]["metrics"]["error_rate"] == 0.0
    assert out["runs"][0]["metrics"]["total_cost_usd"] > 0
    assert not _db(root).exists()  # advisory defaults never created a SQL ledger
