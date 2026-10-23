"""sql plugin: run ledger in any SQL database.

url: sqlite:///path.db (stdlib, zero deps) or any SQLAlchemy URL (postgresql+psycopg://..., mysql://...)
which requires `pip install sqlalchemy` plus the driver. Use a shared DB in CI: ephemeral runners lose
local SQLite files.
"""
import time
import uuid

from ..ports import Ledger, Plugin, RunRecord

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS evalops_runs (run_id VARCHAR(64) PRIMARY KEY, name TEXT, status VARCHAR(32),"
    " start_ms BIGINT, end_ms BIGINT)",
    "CREATE TABLE IF NOT EXISTS evalops_kv (run_id VARCHAR(64), kind VARCHAR(8), k VARCHAR(255), v TEXT,"
    " PRIMARY KEY (run_id, kind, k))",
    "CREATE TABLE IF NOT EXISTS evalops_artifacts (run_id VARCHAR(64), name VARCHAR(255), body TEXT,"
    " PRIMARY KEY (run_id, name))",
]


class SqlPlugin(Plugin, Ledger):
    name = "sql"
    provides = ("ledger",)

    def __init__(self, cfg, options):
        super().__init__(cfg, options)
        url = options.get("url") or f"sqlite:///{cfg.lock_dir / 'ledger.sqlite'}"
        if url.startswith("sqlite:///"):
            import sqlite3

            import pathlib

            path = url[len("sqlite:///"):]
            db = pathlib.Path(path) if path.startswith("/") else cfg.root / path
            db.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(db))
            self._sa = None
        else:
            import sqlalchemy as sa  # optional dependency

            self._sa = sa
            self._engine = sa.create_engine(url, pool_pre_ping=True)
        for stmt in SCHEMA:
            self._exec(stmt)

    def _exec(self, sql, params=None, fetch=False):
        if self._sa is None:
            cur = self._conn.execute(sql, params or {})
            rows = cur.fetchall() if fetch else None
            self._conn.commit()
            return rows
        with self._engine.begin() as conn:
            res = conn.execute(self._sa.text(sql), params or {})
            return [tuple(r) for r in res.fetchall()] if fetch else None

    def _put_kv(self, run_id, kind, values):
        for k, v in (values or {}).items():
            self._exec("DELETE FROM evalops_kv WHERE run_id=:r AND kind=:t AND k=:k", {"r": run_id, "t": kind, "k": k})
            self._exec("INSERT INTO evalops_kv (run_id, kind, k, v) VALUES (:r, :t, :k, :v)",
                       {"r": run_id, "t": kind, "k": str(k), "v": str(v)})

    _last_ms = 0

    def start_run(self, name, tags):
        run_id = uuid.uuid4().hex
        now = max(int(time.time() * 1000), SqlPlugin._last_ms + 1)  # strictly increasing for ordering
        SqlPlugin._last_ms = now
        self._exec("INSERT INTO evalops_runs (run_id, name, status, start_ms) VALUES (:r, :n, 'RUNNING', :s)",
                   {"r": run_id, "n": name, "s": now})
        self._put_kv(run_id, "tag", tags)
        return run_id

    def end_run(self, run_id, status="FINISHED"):
        self._exec("UPDATE evalops_runs SET status=:st, end_ms=:e WHERE run_id=:r",
                   {"st": status, "e": int(time.time() * 1000), "r": run_id})

    def log(self, run_id, params=None, metrics=None, tags=None):
        self._put_kv(run_id, "param", params)
        self._put_kv(run_id, "metric", metrics)
        self._put_kv(run_id, "tag", tags)

    def put_artifact(self, run_id, name, text):
        self._exec("DELETE FROM evalops_artifacts WHERE run_id=:r AND name=:n", {"r": run_id, "n": name})
        self._exec("INSERT INTO evalops_artifacts (run_id, name, body) VALUES (:r, :n, :b)",
                   {"r": run_id, "n": name, "b": text})

    def get_artifact(self, run_id, name):
        rows = self._exec("SELECT body FROM evalops_artifacts WHERE run_id=:r AND name=:n",
                          {"r": run_id, "n": name}, fetch=True)
        if not rows:
            raise KeyError(f"artifact {name} not found for run {run_id}")
        return rows[0][0]

    def find_runs(self, tags=None, since_ms=None, limit=None):
        sql, params = "SELECT run_id, start_ms FROM evalops_runs WHERE 1=1", {}
        for i, (k, v) in enumerate((tags or {}).items()):
            sql += (f" AND EXISTS (SELECT 1 FROM evalops_kv kv WHERE kv.run_id=evalops_runs.run_id"
                    f" AND kv.kind='tag' AND kv.k=:k{i} AND kv.v=:v{i})")
            params[f"k{i}"], params[f"v{i}"] = k, str(v)
        if since_ms is not None:
            sql += " AND start_ms >= :since"
            params["since"] = since_ms
        sql += " ORDER BY start_ms DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        out = []
        for run_id, start_ms in self._exec(sql, params, fetch=True):
            kv = {"tag": {}, "param": {}, "metric": {}}
            for kind, k, v in self._exec("SELECT kind, k, v FROM evalops_kv WHERE run_id=:r", {"r": run_id}, fetch=True):
                kv[kind][k] = float(v) if kind == "metric" else v
            out.append(RunRecord(run_id, kv["tag"], kv["param"], kv["metric"], int(start_ms)))
        return out
