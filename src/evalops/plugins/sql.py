"""sql plugin: run ledger in any SQL database.

url: sqlite:///path.db (stdlib, zero deps) or any SQLAlchemy URL (postgresql+psycopg://..., mysql://...)
which requires `pip install sqlalchemy` plus the driver. Use a shared DB in CI: ephemeral runners lose
local SQLite files.

The *ledger* table schema has its own version (`LEDGER_SCHEMA_VERSION`), separate from the persisted
artifact `FORMAT_VERSION` (CONTRACT.md §5). `evalops ledger migrate` applies the pending steps; a
database that records a newer version raises `FormatError` (exit 7) rather than being downgraded.

Budget reservations need an upsert, and there is no single portable spelling, so the statement is
chosen per dialect (CONTRACT.md §3.3.1): `sqlite`/`postgresql` use
`INSERT ... ON CONFLICT (month) DO UPDATE` (SQLite >= 3.24), `mysql`/`mariadb` use
`INSERT ... ON DUPLICATE KEY UPDATE`, and any other dialect raises `CapabilityError` (exit 6) before
the first budget write instead of running a guess.

MySQL/MariaDB budget reservation is **untested**: this repo has no MySQL contract service, and
Postgres is the verified non-SQLite backend (`tests/contract/test_ledger_postgres.py`). The other
ledger roles (runs, kv, artifacts, `migrate`, `verify`) use portable SQL and work as before on every
dialect.
"""

import contextlib
import json
import time
import uuid

from ..ports import Ledger, Plugin, RunRecord
from ..util import CapabilityError, FormatError, sha256

LEDGER_SCHEMA_VERSION = 1

# v0 tables (evalops <= 0.3); created by the constructor so existing projects keep working.
SCHEMA = [
    "CREATE TABLE IF NOT EXISTS evalops_runs (run_id VARCHAR(64) PRIMARY KEY, name TEXT, status VARCHAR(32),"
    " start_ms BIGINT, end_ms BIGINT)",
    "CREATE TABLE IF NOT EXISTS evalops_kv (run_id VARCHAR(64), kind VARCHAR(8), k VARCHAR(255), v TEXT,"
    " PRIMARY KEY (run_id, kind, k))",
    "CREATE TABLE IF NOT EXISTS evalops_artifacts (run_id VARCHAR(64), name VARCHAR(255), body TEXT,"
    " PRIMARY KEY (run_id, name))",
]

# Additive ledger tables, one list per schema version. `migrate()` applies the missing steps in order.
LEDGER_STEPS = {
    1: [
        "CREATE TABLE IF NOT EXISTS evalops_meta (key VARCHAR(64) PRIMARY KEY, value TEXT)",
        "CREATE TABLE IF NOT EXISTS evalops_budget (run_id VARCHAR(64) PRIMARY KEY, month VARCHAR(7),"
        " amount_usd REAL, state VARCHAR(16))",
        "CREATE TABLE IF NOT EXISTS evalops_budget_month (month VARCHAR(7) PRIMARY KEY, reserved_usd REAL,"
        " settled_usd REAL)",
    ]
}

# The budget-month upsert, one spelling per dialect. Both forms create the month row and re-apply the
# delta (never "do nothing"): that is what makes N racing first reservations of a fresh month all count.
_UPSERT_ON_CONFLICT = (
    "INSERT INTO evalops_budget_month (month, reserved_usd, settled_usd) VALUES (:m, :r, :s)"
    " ON CONFLICT (month) DO UPDATE SET"
    " reserved_usd = evalops_budget_month.reserved_usd + :r,"
    " settled_usd = evalops_budget_month.settled_usd + :s"
)
# MySQL 8.0.20+ deprecates `VALUES(col)` in the UPDATE clause; the bind parameter is re-used instead.
_UPSERT_ON_DUPLICATE_KEY = (
    "INSERT INTO evalops_budget_month (month, reserved_usd, settled_usd) VALUES (:m, :r, :s)"
    " ON DUPLICATE KEY UPDATE reserved_usd = reserved_usd + :r, settled_usd = settled_usd + :s"
)
_MONTH_UPSERT = {
    "sqlite": _UPSERT_ON_CONFLICT,
    "postgresql": _UPSERT_ON_CONFLICT,
    "mysql": _UPSERT_ON_DUPLICATE_KEY,
    "mariadb": _UPSERT_ON_DUPLICATE_KEY,
}


class SqlPlugin(Plugin, Ledger):
    name = "sql"
    provides = ("ledger",)

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        url = options.get("url") or f"sqlite:///{cfg.lock_dir / 'ledger.sqlite'}"
        self._url = url
        if url.startswith("sqlite:///"):
            import pathlib
            import sqlite3

            path = url[len("sqlite:///") :]
            db = pathlib.Path(path) if path.startswith("/") else cfg.root / path
            db.parent.mkdir(parents=True, exist_ok=True)
            self._db_label = f"{db} (evalops_meta)"
            # isolation_level=None puts sqlite in autocommit mode; budget transactions issue an
            # explicit BEGIN IMMEDIATE. The timeout/busy_timeout serializes concurrent writers.
            self._conn = sqlite3.connect(str(db), timeout=10.0, isolation_level=None)
            self._conn.execute("PRAGMA busy_timeout=10000")
            self._sa = None  # no engine on the stdlib path; `_dialect()` reports "sqlite" without one
        else:
            import importlib

            sa = importlib.import_module("sqlalchemy")  # optional dependency, lazily imported
            self._sa = sa
            self._db_label = f"{url} (evalops_meta)"
            self._engine = sa.create_engine(url, pool_pre_ping=True)
        for stmt in SCHEMA:
            self._exec(stmt)

    # ---- low-level execution -------------------------------------------------
    def _execute(self, conn, sql, params=None, fetch=False):
        """Run one statement on a sqlite connection or SQLAlchemy transaction -> (rows, rowcount)."""
        bound = params or {}
        execute = conn.execute  # one seam for the (sqlite|SQLAlchemy) driver call
        if self._sa is None:
            cur = execute(sql, bound)
            return (cur.fetchall() if fetch else None), cur.rowcount
        res = execute(self._sa.text(sql), bound)
        rows = [tuple(r) for r in res.fetchall()] if fetch else None
        return rows, res.rowcount

    def _exec(self, sql, params=None, fetch=False):
        if self._sa is None:
            rows, _ = self._execute(self._conn, sql, params, fetch)
            self._conn.commit()
            return rows
        with self._engine.begin() as conn:
            rows, _ = self._execute(conn, sql, params, fetch)
            return rows

    @contextlib.contextmanager
    def _transaction(self):
        """One write transaction; yields ``(sql, params, fetch) -> (rows, rowcount)``.

        SQLite takes the write lock up front (`BEGIN IMMEDIATE`), so a concurrent reservation
        serializes instead of racing. SQLAlchemy uses a normal transaction; the budget updates are
        atomic row updates, so Postgres serializes them on the row lock.
        """
        if self._sa is None:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield lambda sql, params=None, fetch=False: self._execute(self._conn, sql, params, fetch)
                self._conn.execute("COMMIT")
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        else:
            with self._engine.begin() as conn:
                yield lambda sql, params=None, fetch=False: self._execute(conn, sql, params, fetch)

    # ---- schema version + migration/verification -----------------------------
    def _missing_table(self, exc: BaseException) -> bool:
        text = str(exc).lower()
        return "no such table" in text or "does not exist" in text or "undefined table" in text

    def _maybe_fetch(self, sql, params=None):
        try:
            return self._exec(sql, params, fetch=True) or []
        except Exception as exc:  # noqa: BLE001 - an unmigrated DB has no new tables yet
            if self._missing_table(exc):
                return []
            raise

    def _meta_table_exists(self) -> bool:
        """True when `evalops_meta` exists; checked before a transaction so Postgres is not aborted."""
        if self._sa is None:
            rows = self._exec("SELECT name FROM sqlite_master WHERE type='table' AND name='evalops_meta'", None, True)
            return bool(rows)
        return bool(self._sa.inspect(self._engine).has_table("evalops_meta"))

    def _schema_version(self, execute=None) -> int:
        """Read the recorded ledger schema version; a DB without the row/table is version 0."""
        run = execute or (lambda sql, params=None, fetch=False: (self._exec(sql, params, fetch), 0))
        try:
            rows, _ = run("SELECT value FROM evalops_meta WHERE key='schema_version'", None, True)
        except Exception as exc:  # noqa: BLE001 - the legacy DB predates evalops_meta
            if self._missing_table(exc):
                return 0
            raise
        if not rows:
            return 0
        try:
            return int(rows[0][0])
        except (TypeError, ValueError) as exc:
            raise FormatError(self._db_label, rows[0][0], LEDGER_SCHEMA_VERSION) from exc

    def _check_schema(self, version: int) -> None:
        if version > LEDGER_SCHEMA_VERSION:
            raise FormatError(self._db_label, version, LEDGER_SCHEMA_VERSION)

    def migrate(self) -> dict:
        """Apply pending ledger schema steps idempotently in one transaction; preserves all rows."""
        # Probe the meta table *before* BEGIN: a failed SELECT inside a Postgres transaction would
        # abort it. Version 0 is then the legacy database with no recorded schema.
        meta_exists = self._meta_table_exists()
        with self._transaction() as run:
            version = self._schema_version(run) if meta_exists else 0
            self._check_schema(version)
            applied = []
            for target in range(version + 1, LEDGER_SCHEMA_VERSION + 1):
                for stmt in LEDGER_STEPS.get(target, []):
                    run(stmt)
                run("DELETE FROM evalops_meta WHERE key='schema_version'")
                run(
                    "INSERT INTO evalops_meta (key, value) VALUES ('schema_version', :v)",
                    {"v": str(target)},
                )
                applied.append(target)
        return {"from": version, "to": LEDGER_SCHEMA_VERSION if applied else version, "applied": applied}

    def verify(self) -> dict:
        """Read-only integrity report. Tampering is reported as typed `problems`; never repaired."""
        version = self._schema_version()
        self._check_schema(version)
        problems = []

        runs = {row[0] for row in self._maybe_fetch("SELECT run_id FROM evalops_runs")}
        kv_rows = self._maybe_fetch("SELECT run_id FROM evalops_kv")
        artifact_rows = self._maybe_fetch("SELECT run_id, name, body FROM evalops_artifacts")
        budget_rows = self._maybe_fetch("SELECT run_id, month, amount_usd, state FROM evalops_budget")
        month_rows = self._maybe_fetch("SELECT month, reserved_usd, settled_usd FROM evalops_budget_month")

        orphan_ids = sorted(
            {
                row[0]
                for row in [
                    *((run_id,) for run_id in [r[0] for r in kv_rows]),
                    *((run_id,) for run_id, _, _ in artifact_rows),
                    *((run_id,) for run_id, _, _, _ in budget_rows),
                ]
                if row[0] not in runs
            }
        )
        if orphan_ids:
            problems.append(
                {
                    "kind": "orphan",
                    "detail": f"{len(orphan_ids)} row(s) reference a missing run: {orphan_ids[:5]}",
                }
            )

        artifacts_by_run: dict[str, dict[str, str]] = {}
        for run_id, name, body in artifact_rows:
            artifacts_by_run.setdefault(run_id, {})[name] = body
        for run_id, stored in sorted(artifacts_by_run.items()):
            manifest_text = stored.get("evalops/manifest.json")
            if manifest_text is None:
                continue
            try:
                listed = json.loads(manifest_text).get("artifacts") or {}
            except (json.JSONDecodeError, AttributeError):
                listed = {}
                problems.append(
                    {"kind": "artifact_hash", "detail": f"run {run_id}: manifest is not valid JSON metadata"}
                )
                continue
            for name, expected in sorted(listed.items()):
                if name not in stored:
                    problems.append({"kind": "artifact_hash", "detail": f"run {run_id}: artifact {name} is missing"})
                elif sha256(stored[name]) != expected:
                    problems.append({"kind": "artifact_hash", "detail": f"run {run_id}: artifact {name} hash mismatch"})

        expected_by_month: dict[str, dict[str, float]] = {}
        for _run_id, month, amount_usd, state in budget_rows:
            if month is None or state not in ("reserved", "settled"):
                continue
            bucket = expected_by_month.setdefault(month, {"reserved": 0.0, "settled": 0.0})
            bucket[state] += float(amount_usd or 0.0)
        recorded_months = {month for month, _, _ in month_rows} | set(expected_by_month)
        for month in sorted(recorded_months):
            expected = expected_by_month.get(month, {"reserved": 0.0, "settled": 0.0})
            recorded = next(
                ((float(reserved or 0.0), float(settled or 0.0)) for m, reserved, settled in month_rows if m == month),
                (0.0, 0.0),
            )
            if abs(expected["reserved"] - recorded[0]) > 1e-6 or abs(expected["settled"] - recorded[1]) > 1e-6:
                problems.append(
                    {
                        "kind": "budget_mismatch",
                        "detail": f"month {month}: rows reserve/settle {expected['reserved']}/{expected['settled']}"
                        f" but totals record {recorded[0]}/{recorded[1]}",
                    }
                )

        return {
            "ok": not problems,
            "schema_version": version,
            "problems": problems,
            "counts": {
                "runs": len(runs),
                "kv": len(kv_rows),
                "artifacts": len(artifact_rows),
                "budget": len(budget_rows),
                "months": len(month_rows),
            },
        }

    # ---- budget reservations -------------------------------------------------
    def _ensure_ledger_tables(self) -> None:
        # Budgets must work on a legacy DB even before an explicit `ledger migrate`; the CREATEs are
        # idempotent and do not record a schema version (migrate still reports that step as pending).
        for target in range(1, LEDGER_SCHEMA_VERSION + 1):
            for stmt in LEDGER_STEPS.get(target, []):
                self._exec(stmt)

    def _dialect(self) -> str:
        """The ledger dialect name that selects the budget upsert (stdlib sqlite path -> 'sqlite')."""
        engine = getattr(self, "_engine", None)
        if engine is None:
            return "sqlite"
        return str(engine.dialect.name)

    def _month_upsert(self) -> str:
        """The dialect-correct month upsert statement; resolved once before any budget write.

        An unverified dialect is refused (exit 6) rather than guessed: on MySQL the `ON CONFLICT`
        spelling is a syntax error, which would turn every capped eval into a runtime failure.
        """
        dialect = self._dialect()
        try:
            return _MONTH_UPSERT[dialect]
        except KeyError:
            raise CapabilityError(
                self.name, "atomic budget reservation", hint="the ledger dialect is not verified"
            ) from None

    def _adjust_month(self, run, month, reserved_delta, settled_delta, upsert=None):
        # One upsert statement, not UPDATE-then-INSERT: on a fresh month the old sequence had every
        # concurrent transaction see "no row" and all INSERT, so the losers died with a unique
        # violation after the winner committed (Postgres READ COMMITTED). Creating the row and
        # applying the delta are the same statement, so the second writer conflicts and re-applies
        # its delta to the committed row instead of failing or dropping it. The spelling depends on
        # the dialect (`sqlite`/`postgresql` vs `mysql`/`mariadb`), so callers resolve it up front
        # with `self._month_upsert()` before their first write.
        run(
            upsert or self._month_upsert(),
            {"r": reserved_delta, "s": settled_delta, "m": month},
            False,
        )

    def budget_reserve(self, run_id: str, amount_usd: float, month: str, limit_usd: float | None) -> bool:
        upsert = self._month_upsert()  # refuse an unverified dialect before any read/write
        version = self._schema_version()
        self._check_schema(version)
        self._ensure_ledger_tables()
        amount = float(amount_usd)
        try:
            with self._transaction() as run:
                existing, _ = run(
                    "SELECT month, amount_usd, state FROM evalops_budget WHERE run_id=:r", {"r": run_id}, True
                )
                if existing:
                    month, _amount, state = existing[0]
                    if state != "reserved":
                        return True  # settled/released runs never re-reserve; idempotent no-op
                    rows, _ = run(
                        "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=:m",
                        {"m": month},
                        True,
                    )
                    if limit_usd is None or not rows:
                        return True
                    return float(rows[0][0] or 0.0) + float(rows[0][1] or 0.0) <= float(limit_usd) + 1e-9

                self._adjust_month(run, month, amount, 0.0, upsert)
                rows, _ = run(
                    "SELECT reserved_usd, settled_usd FROM evalops_budget_month WHERE month=:m",
                    {"m": month},
                    True,
                )
                reserved, settled = (float(rows[0][0] or 0.0), float(rows[0][1] or 0.0)) if rows else (0.0, 0.0)
                if limit_usd is not None and reserved + settled > float(limit_usd) + 1e-9:
                    raise _Rollback(False)  # rolled back below: no partial month increment survives
                run(
                    "INSERT INTO evalops_budget (run_id, month, amount_usd, state) VALUES (:r, :m, :a, 'reserved')",
                    {"r": run_id, "m": month, "a": amount},
                    False,
                )
                return True
        except _Rollback as stop:
            return stop.verdict

    def budget_settle(self, run_id: str, actual_usd: float) -> None:
        upsert = self._month_upsert()  # refuse an unverified dialect before any write
        self._check_schema(self._schema_version())
        self._ensure_ledger_tables()
        with self._transaction() as run:
            rows, _ = run("SELECT month, amount_usd, state FROM evalops_budget WHERE run_id=:r", {"r": run_id}, True)
            if not rows or rows[0][2] != "reserved":
                return  # double settle and settle-after-release are no-ops
            month, amount, _state = rows[0]
            actual = float(actual_usd)
            run(
                "UPDATE evalops_budget SET amount_usd=:a, state='settled' WHERE run_id=:r",
                {"a": actual, "r": run_id},
                False,
            )
            self._adjust_month(run, month, -float(amount or 0.0), actual, upsert)

    def budget_release(self, run_id: str) -> None:
        upsert = self._month_upsert()  # refuse an unverified dialect before any write
        self._check_schema(self._schema_version())
        self._ensure_ledger_tables()
        with self._transaction() as run:
            rows, _ = run("SELECT month, amount_usd, state FROM evalops_budget WHERE run_id=:r", {"r": run_id}, True)
            if not rows or rows[0][2] != "reserved":
                return  # double release is a no-op
            month, amount, _state = rows[0]
            run("UPDATE evalops_budget SET state='released' WHERE run_id=:r", {"r": run_id}, False)
            self._adjust_month(run, month, -float(amount or 0.0), 0.0, upsert)

    def budget_reserved(self, month: str) -> float:
        self._check_schema(self._schema_version())
        rows = self._maybe_fetch("SELECT reserved_usd FROM evalops_budget_month WHERE month=:m", {"m": month})
        return float(rows[0][0] or 0.0) if rows else 0.0

    # ---- runs ----------------------------------------------------------------
    def _put_kv(self, run_id, kind, values):
        for k, v in (values or {}).items():
            self._exec("DELETE FROM evalops_kv WHERE run_id=:r AND kind=:t AND k=:k", {"r": run_id, "t": kind, "k": k})
            self._exec(
                "INSERT INTO evalops_kv (run_id, kind, k, v) VALUES (:r, :t, :k, :v)",
                {"r": run_id, "t": kind, "k": str(k), "v": str(v)},
            )

    _last_ms = 0

    def start_run(self, name, tags):
        run_id = uuid.uuid4().hex
        now = max(int(time.time() * 1000), SqlPlugin._last_ms + 1)  # strictly increasing for ordering
        SqlPlugin._last_ms = now
        self._exec(
            "INSERT INTO evalops_runs (run_id, name, status, start_ms) VALUES (:r, :n, 'RUNNING', :s)",
            {"r": run_id, "n": name, "s": now},
        )
        self._put_kv(run_id, "tag", tags)
        return run_id

    def end_run(self, run_id, status="FINISHED"):
        self._exec(
            "UPDATE evalops_runs SET status=:st, end_ms=:e WHERE run_id=:r",
            {"st": status, "e": int(time.time() * 1000), "r": run_id},
        )

    def log(self, run_id, params=None, metrics=None, tags=None):
        self._put_kv(run_id, "param", params)
        self._put_kv(run_id, "metric", metrics)
        self._put_kv(run_id, "tag", tags)

    def put_artifact(self, run_id, name, text):
        self._exec("DELETE FROM evalops_artifacts WHERE run_id=:r AND name=:n", {"r": run_id, "n": name})
        self._exec(
            "INSERT INTO evalops_artifacts (run_id, name, body) VALUES (:r, :n, :b)",
            {"r": run_id, "n": name, "b": text},
        )

    def get_artifact(self, run_id, name):
        rows = self._exec(
            "SELECT body FROM evalops_artifacts WHERE run_id=:r AND name=:n", {"r": run_id, "n": name}, fetch=True
        )
        if not rows:
            raise KeyError(f"artifact {name} not found for run {run_id}")
        return rows[0][0]

    def find_runs(self, tags=None, since_ms=None, limit=None):
        sql, params = "SELECT run_id, start_ms FROM evalops_runs WHERE 1=1", {}
        for i, (k, v) in enumerate((tags or {}).items()):
            sql += (
                f" AND EXISTS (SELECT 1 FROM evalops_kv kv WHERE kv.run_id=evalops_runs.run_id"
                f" AND kv.kind='tag' AND kv.k=:k{i} AND kv.v=:v{i})"
            )
            params[f"k{i}"], params[f"v{i}"] = k, str(v)
        if since_ms is not None:
            sql += " AND start_ms >= :since"
            params["since"] = since_ms
        sql += " ORDER BY start_ms DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        runs = self._exec(sql, params, fetch=True) or []
        if not runs:
            return []

        # One runs query + one kv query per 500 run ids, never one per run (the old N+1).
        kv_by_run: dict[str, dict[str, dict[str, object]]] = {
            run_id: {"tag": {}, "param": {}, "metric": {}} for run_id, _start_ms in runs
        }
        run_ids = [run_id for run_id, _start_ms in runs]
        for start in range(0, len(run_ids), 500):
            chunk = run_ids[start : start + 500]
            chunk_params = {f"r{i}": run_id for i, run_id in enumerate(chunk)}
            placeholders = ", ".join(f":r{i}" for i in range(len(chunk)))
            rows = self._exec(
                f"SELECT run_id, kind, k, v FROM evalops_kv WHERE run_id IN ({placeholders})",
                chunk_params,
                fetch=True,
            )
            for run_id, kind, k, v in rows or []:
                kv_by_run[run_id][kind][k] = float(v) if kind == "metric" else v

        out = []
        for run_id, start_ms in runs:
            kv = kv_by_run[run_id]
            out.append(RunRecord(run_id, kv["tag"], kv["param"], kv["metric"], int(start_ms)))
        return out


class _Rollback(Exception):
    """Internal sentinel: abort a reservation transaction and report ``False`` to the caller."""

    def __init__(self, verdict: bool):
        super().__init__()
        self.verdict = verdict
