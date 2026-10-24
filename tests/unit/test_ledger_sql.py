"""WP1.2: the SQL ledger's schema version, integrity report and atomic budget accounting.

Falsifies, in order: (a) concurrent reservations cannot overspend a monthly cap, (b) settle/release
move exactly the right month totals and are idempotent, (c) `migrate` upgrades a legacy DB and
refuses a future schema, (d) `verify` detects an orphan, a tampered manifest artifact and edited
month totals, (e) `find_runs` is a bounded number of statements (no N+1), including >500-run chunks,
and (f) the CLI surfaces verify/migrate with the frozen exit-code matrix.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import json
import pathlib
import sqlite3
import subprocess
import sys
import tempfile
import threading

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))

from evalops.contract import formats  # noqa: E402
from evalops.plugins.sql import LEDGER_SCHEMA_VERSION, SCHEMA, SqlPlugin  # noqa: E402
from evalops.util import EXIT_CONTRACT, FormatError  # noqa: E402


class _Cfg:
    def __init__(self, root):
        self.root = pathlib.Path(root)
        self.lock_dir = self.root / ".evalops" / "locks" / "local"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.env = "local"


def _plugin(root, db):
    return SqlPlugin(_Cfg(root), {"url": f"sqlite:///{db}"})


def _close(plugin):
    conn = getattr(plugin, "_conn", None)
    if conn is not None:
        conn.close()


def _raw(db, sql, params=()):
    conn = sqlite3.connect(str(db))
    try:
        execute = conn.execute  # local alias: the SQL is a test-owned literal/migration query
        rows = execute(sql, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def _tables(db):
    return {row[0] for row in _raw(db, "SELECT name FROM sqlite_master WHERE type='table'")}


def test_concurrent_reserve_cannot_overspend():
    amount, cap, month = 0.25, 0.75, "2026-01"
    successes_total = failures_total = 0
    for round_no in range(20):
        with tempfile.TemporaryDirectory(prefix="evalops-ledger-race-") as tmp:
            db = pathlib.Path(tmp) / "ledger.sqlite"
            seed = _plugin(tmp, db)
            try:
                seed.migrate()
            finally:
                _close(seed)

            barrier = threading.Barrier(8)
            results, errors = [], []

            def worker(
                i,
                db=db,
                barrier=barrier,
                results=results,
                errors=errors,
                round_no=round_no,
            ):
                plugin = _plugin(tmp, db)
                try:
                    barrier.wait(timeout=10)
                    results.append(plugin.budget_reserve(f"r{round_no}-{i}", amount, month, cap))
                except BaseException as exc:  # noqa: BLE001 - the test reports the whole failure set
                    errors.append(exc)
                finally:
                    _close(plugin)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)

            assert not errors, errors
            assert len(results) == 8, results
            successes, failures = results.count(True), results.count(False)
            assert (successes, failures) == (3, 5), (round_no, successes, failures)
            successes_total += successes
            failures_total += failures

            check = _plugin(tmp, db)
            try:
                assert abs(check.budget_reserved(month) - 3 * amount) < 1e-9
            finally:
                _close(check)
            rows = _raw(db, "SELECT amount_usd, state FROM evalops_budget")
            assert sum(float(a) for a, state in rows if state == "reserved") <= cap + 1e-9
            assert len(rows) == 3

    assert (successes_total, failures_total) == (60, 100)


def test_first_reserve_of_a_fresh_month_is_one_re_applying_upsert():
    # Regression for the Postgres first-reserve race: the old UPDATE->SELECT->INSERT sequence let
    # every concurrent transaction see "no row" and INSERT, so all but one lost with a unique
    # violation (exit 5). The fix is one upsert that re-applies the delta on conflict; this test
    # fails on the old code, where a fresh month took three statements (UPDATE, SELECT, INSERT).
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-upsert-") as tmp:
        db = pathlib.Path(tmp) / "upsert.sqlite"
        plugin = _plugin(tmp, db)
        try:
            plugin.migrate()
            statements = []

            def recording_run(sql, params=None, fetch=False):
                rows, count = plugin._execute(plugin._conn, sql, params, fetch)
                statements.append(sql)
                return rows, count

            assert _raw(db, "SELECT month FROM evalops_budget_month WHERE month='2026-09'") == []
            plugin._adjust_month(recording_run, "2026-09", 1.25, 0.0)
            assert len(statements) == 1, statements
            assert "ON CONFLICT (month) DO UPDATE" in statements[0], statements[0]
            assert "DO NOTHING" not in statements[0], statements[0]
            assert _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month='2026-09'") == [
                (1.25, 0.0)
            ]
            # the conflict branch re-applies the delta instead of replacing the row
            plugin._adjust_month(recording_run, "2026-09", 0.0, 0.75)
            assert _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month='2026-09'") == [
                (1.25, 0.75)
            ]

            # two sequential first-reserves of a month that has no row yet: the second must add
            run_a, run_b = plugin.start_run("run-a", {}), plugin.start_run("run-b", {})
            assert plugin.budget_reserve(run_a, 2.5, "2026-10", None) is True
            assert plugin.budget_reserve(run_b, 2.5, "2026-10", None) is True
            assert _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month='2026-10'") == [
                (5.0, 0.0)
            ]
            assert plugin.budget_reserved("2026-10") == 5.0
            rows = _raw(db, "SELECT month, amount_usd, state FROM evalops_budget WHERE state='reserved'")
            assert sorted(r[1] for r in rows if r[0] == "2026-10") == [2.5, 2.5]
            assert sum(float(a) for m, a, _s in rows if m == "2026-10") == plugin.budget_reserved("2026-10")
            # the raw `_adjust_month` above wrote a month total with no budget row behind it, which
            # `verify` flags (and never repairs); drop that fixture row to check the real accounting
            assert [p["kind"] for p in plugin.verify()["problems"]] == ["budget_mismatch"]
            _raw(db, "DELETE FROM evalops_budget_month WHERE month='2026-09'")
            assert plugin.verify()["ok"] is True
        finally:
            _close(plugin)


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeEngine:
    """Just enough of a SQLAlchemy Engine: the budget upsert only reads `.dialect.name`."""

    def __init__(self, name):
        self.dialect = _FakeDialect(name)


def test_budget_month_upsert_is_chosen_per_dialect_and_refuses_unknown_ones():
    # MySQL is advertised (mysql:// URLs, the `sql` extra) but `ON CONFLICT` is a syntax error there,
    # so a monthly budget would die at reserve time. The statement must follow the dialect, and an
    # unverified dialect must be refused (exit 6) before any SQL runs -- never guessed.
    from evalops.util import EXIT_CAPABILITY, CapabilityError

    on_conflict = (
        "INSERT INTO evalops_budget_month (month, reserved_usd, settled_usd) VALUES (:m, :r, :s)"
        " ON CONFLICT (month) DO UPDATE SET"
        " reserved_usd = evalops_budget_month.reserved_usd + :r,"
        " settled_usd = evalops_budget_month.settled_usd + :s"
    )
    on_duplicate_key = (
        "INSERT INTO evalops_budget_month (month, reserved_usd, settled_usd) VALUES (:m, :r, :s)"
        " ON DUPLICATE KEY UPDATE reserved_usd = reserved_usd + :r, settled_usd = settled_usd + :s"
    )
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-dialect-") as tmp:
        db = pathlib.Path(tmp) / "dialect.sqlite"
        plugin = _plugin(tmp, db)
        try:
            plugin.migrate()
            assert plugin._dialect() == "sqlite" and plugin._month_upsert() == on_conflict
            for dialect, expected in (
                ("sqlite", on_conflict),
                ("postgresql", on_conflict),
                ("mysql", on_duplicate_key),
                ("mariadb", on_duplicate_key),
            ):
                plugin._engine = _FakeEngine(dialect)
                assert plugin._dialect() == dialect, dialect
                assert plugin._month_upsert() == expected, (dialect, plugin._month_upsert())
                statements = []

                def recording_run(sql, params=None, fetch=False, statements=statements):
                    statements.append(sql)
                    return None, 0

                plugin._adjust_month(recording_run, "2026-11", 1.5, 0.0)
                assert statements == [expected], (dialect, statements)
                if dialect in ("mysql", "mariadb"):
                    assert "ON CONFLICT" not in statements[0], statements[0]
                    assert "VALUES(reserved_usd)" not in statements[0], statements[0]
                else:  # the sqlite/postgres spelling keeps the round-2 shape
                    assert "ON CONFLICT (month) DO UPDATE" in statements[0], statements[0]
                    assert "DO NOTHING" not in statements[0], statements[0]

            # an unverified dialect is refused once, before any statement reaches the database
            issued = []
            plugin._engine = _FakeEngine("oracle")
            original = plugin._execute

            def counting_execute(conn, sql, params=None, fetch=False):
                issued.append(sql)
                return original(conn, sql, params, fetch)

            plugin._execute = counting_execute
            for call in (
                lambda: plugin.budget_reserve("dialect-run", 1.0, "2026-12", 10.0),
                lambda: plugin.budget_settle("dialect-run", 1.0),
                lambda: plugin.budget_release("dialect-run"),
            ):
                try:
                    call()
                except CapabilityError as exc:
                    assert exc.code == EXIT_CAPABILITY, exc.code
                    assert "atomic budget reservation" in str(exc), str(exc)
                    assert "the ledger dialect is not verified" in str(exc), str(exc)
                else:  # pragma: no cover
                    raise AssertionError("an unknown dialect was not refused")
            assert issued == [], issued
        finally:
            _close(plugin)

        # the stdlib path is untouched: no engine needed, and verify/migrate still work
        check = _plugin(tmp, db)
        try:
            assert not hasattr(check, "_engine"), "the sqlite path must not need a SQLAlchemy engine"
            assert check._dialect() == "sqlite"
            assert check.migrate()["applied"] == []  # idempotent on an already-migrated database
            assert check.verify()["ok"] is True, check.verify()
        finally:
            _close(check)


def test_settle_and_release_accounting_is_exact_and_idempotent():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-settle-") as tmp:
        db = pathlib.Path(tmp) / "ledger.sqlite"
        plugin = _plugin(tmp, db)
        try:
            plugin.migrate()
            month = "2026-04"
            assert plugin.budget_reserve("run-a", 5.0, month, 20.0) is True
            plugin.budget_settle("run-a", 3.0)
            assert plugin.budget_reserved(month) == 0.0
            rows = _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=?", (month,))
            assert rows == [(0.0, 3.0)], rows
            assert _raw(db, "SELECT amount_usd, state FROM evalops_budget WHERE run_id='run-a'") == [(3.0, "settled")]

            # double settle, settle-after-release and double release are no-ops, never double counts
            plugin.budget_settle("run-a", 3.0)
            plugin.budget_settle("run-a", 99.0)
            plugin.budget_release("run-a")
            assert plugin.budget_reserved(month) == 0.0
            assert _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=?", (month,)) == [
                (0.0, 3.0)
            ]

            assert plugin.budget_reserve("run-b", 4.0, month, 20.0) is True
            plugin.budget_release("run-b")
            plugin.budget_release("run-b")
            plugin.budget_settle("run-b", 2.0)
            assert plugin.budget_reserved(month) == 0.0
            assert _raw(db, "SELECT amount_usd, state FROM evalops_budget WHERE run_id='run-b'") == [(4.0, "released")]
            assert _raw(db, "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=?", (month,)) == [
                (0.0, 3.0)
            ]
            # re-reserving a finished run is an idempotent no-op, not a second charge
            assert plugin.budget_reserve("run-b", 4.0, month, 20.0) is True
            assert plugin.budget_reserved(month) == 0.0
        finally:
            _close(plugin)


def test_migrate_upgrades_a_legacy_db_and_rejects_a_future_schema():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-migrate-") as tmp:
        db = pathlib.Path(tmp) / "legacy.sqlite"
        conn = sqlite3.connect(str(db))
        try:
            for stmt in SCHEMA:
                conn.execute(stmt)
            conn.execute("INSERT INTO evalops_runs (run_id, name, status, start_ms) VALUES ('old', 'n', 'FINISHED', 1)")
            conn.execute("INSERT INTO evalops_kv VALUES ('old', 'tag', 'evalops.group', 'g')")
            conn.execute("INSERT INTO evalops_artifacts VALUES ('old', 'note.txt', 'hello')")
            conn.commit()
        finally:
            conn.close()

        plugin = _plugin(tmp, db)
        try:
            assert _tables(db) == {"evalops_runs", "evalops_kv", "evalops_artifacts"}
            first = plugin.migrate()
            assert first == {"from": 0, "to": LEDGER_SCHEMA_VERSION, "applied": [1]}, first
            assert {"evalops_meta", "evalops_budget", "evalops_budget_month"} <= _tables(db)
            assert _raw(db, "SELECT value FROM evalops_meta WHERE key='schema_version'") == [
                (str(LEDGER_SCHEMA_VERSION),)
            ]
            assert _raw(db, "SELECT run_id FROM evalops_runs") == [("old",)]
            assert _raw(db, "SELECT body FROM evalops_artifacts WHERE run_id='old' AND name='note.txt'") == [("hello",)]
            assert plugin.migrate() == {"from": LEDGER_SCHEMA_VERSION, "to": LEDGER_SCHEMA_VERSION, "applied": []}

            _raw(db, "UPDATE evalops_meta SET value='99' WHERE key='schema_version'")
            for operation in (plugin.migrate, plugin.verify):
                try:
                    operation()
                except FormatError as exc:
                    assert exc.code == EXIT_CONTRACT
                    assert "99" in str(exc) and "schema_version" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError(f"{operation.__name__} accepted a future ledger schema")
        finally:
            _close(plugin)


def _clean_db(root, db):
    plugin = _plugin(root, db)
    plugin.migrate()
    run_id = plugin.start_run("run", {"evalops.group": "g1"})
    artifacts = {"note.txt": "hello"}
    plugin.put_artifact(run_id, "note.txt", artifacts["note.txt"])
    plugin.put_artifact(run_id, "evalops/manifest.json", json.dumps(formats.manifest(artifacts)))
    return plugin, run_id


def test_verify_detects_orphans_artifact_tampering_and_budget_mismatch():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-verify-") as tmp:
        root = pathlib.Path(tmp)
        plugin, run_id = _clean_db(root, root / "clean.sqlite")
        try:
            report = plugin.verify()
            assert report["ok"] is True and report["problems"] == [], report
            assert report["schema_version"] == LEDGER_SCHEMA_VERSION
        finally:
            _close(plugin)

        # a listed artifact whose stored bytes no longer hash to the manifest value
        db = root / "artifact.sqlite"
        plugin, _run_id = _clean_db(root, db)
        try:
            _raw(db, "UPDATE evalops_artifacts SET body='tampered' WHERE name='note.txt'")
            report = plugin.verify()
            assert report["ok"] is False
            assert [p["kind"] for p in report["problems"]] == ["artifact_hash"], report
        finally:
            _close(plugin)

        # a kv row (and a budget row) whose run does not exist
        db = root / "orphan.sqlite"
        plugin = _plugin(root, db)
        try:
            plugin.migrate()
            _raw(db, "INSERT INTO evalops_kv VALUES ('missing', 'tag', 'x', 'y')")
            _raw(db, "INSERT INTO evalops_budget VALUES ('missing', '2026-01', 1.0, 'reserved')")
            _raw(db, "INSERT INTO evalops_budget_month VALUES ('2026-01', 1.0, 0.0)")
            report = plugin.verify()
            kinds = {p["kind"] for p in report["problems"]}
            assert "orphan" in kinds, report
        finally:
            _close(plugin)

        # hand-edited month totals no longer match the per-run budget rows
        db = root / "budget.sqlite"
        plugin = _plugin(root, db)
        try:
            plugin.migrate()
            run_c = plugin.start_run("run-c", {})
            plugin.budget_reserve(run_c, 2.0, "2026-05", 10.0)
            plugin.budget_settle(run_c, 1.5)
            _raw(db, "UPDATE evalops_budget_month SET settled_usd=999.0 WHERE month='2026-05'")
            report = plugin.verify()
            assert report["ok"] is False
            assert [p["kind"] for p in report["problems"]] == ["budget_mismatch"], report
        finally:
            _close(plugin)


def test_find_runs_is_bounded_and_preserves_the_run_record_shape():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-nplus1-") as tmp:
        db = pathlib.Path(tmp) / "runs.sqlite"
        plugin = _plugin(tmp, db)
        try:
            plugin.migrate()
            for i in range(20):
                run_id = plugin.start_run(f"run-{i}", {})
                plugin.log(run_id, params={"row": i}, metrics={"score": float(i)}, tags={"bucket": str(i % 2)})
            calls = {"n": 0}
            original = plugin._exec

            def counted(sql, params=None, fetch=False):
                calls["n"] += 1
                return original(sql, params, fetch)

            plugin._exec = counted
            runs = plugin.find_runs()
            assert calls["n"] <= 3, calls["n"]
            assert len(runs) == 20
            assert [r.tags["bucket"] for r in runs[:2]] == ["1", "0"]  # newest first, i=19 then 18
            assert runs[0].params == {"row": "19"}  # kv values are stored as text, as before
            assert runs[0].metrics == {"score": 19.0}
            assert isinstance(runs[0].metrics["score"], float)
        finally:
            _close(plugin)


def test_find_runs_chunks_more_than_500_ids():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-chunks-") as tmp:
        db = pathlib.Path(tmp) / "many.sqlite"
        plugin = _plugin(tmp, db)
        try:
            plugin.migrate()
            conn = sqlite3.connect(str(db))
            try:
                conn.executemany(
                    "INSERT INTO evalops_runs (run_id, name, status, start_ms) VALUES (?, 'run', 'FINISHED', ?)",
                    [(f"r{i:04d}", 10_000 + i) for i in range(501)],
                )
                conn.executemany(
                    "INSERT INTO evalops_kv (run_id, kind, k, v) VALUES (?, 'tag', 'n', ?)",
                    [(f"r{i:04d}", str(i)) for i in range(501)],
                )
                conn.commit()
            finally:
                conn.close()

            calls = {"n": 0}
            original = plugin._exec

            def counted(sql, params=None, fetch=False):
                calls["n"] += 1
                return original(sql, params, fetch)

            plugin._exec = counted
            runs = plugin.find_runs()
            assert len(runs) == 501
            assert calls["n"] == 1 + (501 + 499) // 500, calls["n"]
            assert runs[0].tags == {"n": "500"}
        finally:
            _close(plugin)


# ---------------------------------------------------------------------------
# CLI surface: migrate/verify use the frozen exit-code matrix (§1), no new codes.
# ---------------------------------------------------------------------------
def _run_cli(root, *args, env_extra=None):
    env = {
        **__import__("os").environ,
        "PYTHONPATH": f"{REPO / 'src'}{__import__('os').pathsep}{root}",
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
        **(env_extra or {}),
    }
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode, json.loads(proc.stdout)


def _cli_project(db, ledger="sql", extra_files=None):
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-ledger-cli-"))
    (root / "evalops.yaml").write_text(
        f"""project: ledger-cli
default_env: local
plugins: {{prompts: git, datasets: git, ledger: {ledger}, tracer: none, evaluator: core}}
app: {{predict_fn: fake_app:build_predict_fn}}
plugin_config:
  sql:
    url: "sqlite:///{db}"
"""
    )
    (root / "fake_app.py").write_text((TESTS_DIR / "fixtures" / "fake_app.py").read_text())
    for name, text in (extra_files or {}).items():
        (root / name).write_text(text)
    return root


def test_ledger_cli_migrate_and_verify_surface_the_exit_codes():
    with tempfile.TemporaryDirectory(prefix="evalops-ledger-cli-db-") as tmp:
        db = pathlib.Path(tmp) / "cli.sqlite"
        plugin, _run_id = _clean_db(tmp, db)
        _close(plugin)
        root = _cli_project(db)

        code, envelope = _run_cli(root, "ledger", "migrate")
        assert code == 0 and envelope["ok"] is True, envelope
        assert envelope["data"]["from"] == LEDGER_SCHEMA_VERSION
        assert envelope["data"]["applied"] == []

        code, envelope = _run_cli(root, "ledger", "verify")
        assert code == 0 and envelope["data"]["ok"] is True, envelope
        assert envelope["data"]["problems"] == []

        _raw(db, "UPDATE evalops_artifacts SET body='tampered' WHERE name='note.txt'")
        code, envelope = _run_cli(root, "ledger", "verify")
        assert code == 5, envelope
        assert envelope["ok"] is False and envelope["data"]["ok"] is False
        assert [p["kind"] for p in envelope["data"]["problems"]] == ["artifact_hash"]
        assert "1 problem" in envelope["errors"][0]["message"]

        memory = TESTS_DIR / "fixtures" / "formats" / "memory_ledger.py"
        memory_root = _cli_project(db, "memory_ledger:MemoryLedger", {"memory_ledger.py": memory.read_text()})
        code, envelope = _run_cli(memory_root, "ledger", "verify")
        assert code == 6 and envelope["ok"] is False, envelope
        assert "does not support ledger verify" in envelope["errors"][0]["message"]
