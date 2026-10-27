# WP3.1a packet — contract slice: the MLflow evaluator + Postgres ledger against live services

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **482 tests + WP3.0's additions**
(+1 contract test, deselected by default). **Dispatch order: AFTER WP3.0 lands** — one of your
assertions (`error_rate` under the mlflow evaluator) is false on a tree without it.
Read `docs/IMPLEMENTATION_MAP.md`'s phase-3 rows (3.0/3.1a/3.2 — this WP is the first 3.1 slice),
`tests/contract/test_ledger_postgres.py` (the pattern you follow: compose managed inside the test,
skip ladder, env-override), `tests/contract/docker-compose.yml`, `src/evalops/plugins/mlflow.py`
(the surface under test), `docs/adr/0005-contract-testing-scope.md` (what the contract suite is
for), and `docs/adr/0008-evaluator-owned-tracing.md` (the parity behaviors you assert).

This WP is **harness only**: live-service tests for what already exists. It is the in-repo,
CI-runnable form of the manual e2e platform that stood up MLflow+Postgres+Logfire on 2026-10-27
(findings in that run's report are 3.2 material — you do NOT fix plugins here).

## Environment

```bash
cd /home/howt/work/evalops_universal/evalops
docker --version && docker compose version   # available here
make check                                   # must stay: <482+> passed, 1 deselected — offline untouched
make contract                                # starts the compose services; runs the contract suite
```

The mlflow image (`ghcr.io/mlflow/mlflow`) pulls on first use (~400MB). The suite must **skip
cleanly** at every step where the environment lacks something: no docker → skip; docker unusable →
skip; service not ready in the wait budget → skip; `mlflow`/`sqlalchemy`/`psycopg` not importable →
`pytest.importorskip`. Exactly the `test_ledger_postgres.py` ladder — a missing piece is an
environment skip, never a red suite.

## Goal

The map's phase-3 row 3.1, first slice: the five-role MLflow plugin and the SQL (Postgres) ledger
proved against **live services**, so the fake-SDK unit tests and `VERIFY` markers get a real
execution path long before credentials exist. Langfuse+clickhouse, recorded HTTP fixtures, and the
CI nightly wiring are the **deferred** 3.1b slice — out of scope here.

### A. Compose (`tests/contract/docker-compose.yml`)

- **A1** Add an `mlflow` service to the EXISTING compose file (the postgres service and its port
  `55432` stay byte-identical — the WP1.2 test and `make contract` depend on them):
  image `ghcr.io/mlflow/mlflow:latest`, command
  `mlflow server --host 0.0.0.0 --port 5000 --backend-store-uri sqlite:////mlflow/mlflow.db
  --default-artifact-root /mlflow/artifacts`, port `55000:5000` (55000, so a developer's local
  mlflow on 5000 never collides), a named volume for `/mlflow`, and a healthcheck probing
  `http://localhost:5000/health`.
- **A2** `EVALOPS_TEST_MLFLOW_TRACKING_URI` overrides the URI (default
  `http://127.0.0.1:55000`), the same escape hatch as `EVALOPS_TEST_POSTGRES_URL`.

### B. The test module (`tests/contract/test_mlflow_plugin.py`, new)

- **B1** Same shape as `test_ledger_postgres.py`: one contract test (or a small class), compose
  started by the test, readiness waited by polling `GET {uri}/health` (60s budget, then skip),
  everything `pytest.importorskip`'d (`mlflow`, `sqlalchemy`, `psycopg`).
- **B2** Real-registry plugins, real config: build a `Config` over a temp project dir with
  `plugins: {prompts: mlflow, datasets: mlflow, ledger: mlflow, tracer: mlflow, evaluator: mlflow}`
  pointed at the live server (the `mlflow` preset's wiring), plus a second config variant with
  `ledger: sql` at `postgresql+psycopg://evalops:evalops@127.0.0.1:55432/evalops_test` (the
  postgres service already in the compose file) — the mixed wiring the capability matrix
  advertises. Use `EVALOPS_TEST_POSTGRES_URL` when set, same as the WP1.2 test.
- **C — what is asserted (each as its own focused test):**
  - **C1 prompts**: `register` → `load` returns the template; `set_alias` → `get_alias` resolves;
    a missing alias returns `None`.
  - **C2 datasets**: `write` → `exists` → `read` round-trips records (inputs + expectations).
  - **C3 ledger**: `start_run` → `log` (params/metrics/tags) → `find_runs` by tag →
    `put_artifact`/`get_artifact` byte-identical round-trip → `end_run`.
  - **C4 evaluator, live**: a 2-row dataset, a deterministic `predict_fn` (no external calls: a
    lookup table) and a plain `@evalops.scorer` — through the REAL `run.py::_evaluate` on a temp
    project (the `_projects.py` helper pattern, mlflow wiring) asserting: run FINISHED server-side
    (`MlflowClient.search_runs`), rows carry scores + `trace_id`, metrics carry the scorer aggregate
    AND `error_rate` (WP3.0), `evalops/rows.jsonl` + manifest artifacts retrievable from the ledger.
  - **C5 judge-only/live-re-score**: the `stored_rows` delegation path (mlflow evaluator → core
    re-score) executes against the live server without error.
- **D — what is deliberately NOT asserted**: `budget_exceeded_rate` > 0 (no breaker under vendors),
  langfuse/langsmith surfaces, recorded HTTP fixtures (3.1b/3.2), anything from the e2e findings
  list marked 3.2.

### E. Runner integration

- **E1** `make check` stays exactly as is (contract tests deselected; count = base + WP3.0's total).
- **E2** `make contract` runs the suite with both services up. If the existing `make contract`
  target needs no change (the tests manage their own compose), do not touch the `Makefile`.

### F. Honesty rules

- **F1** If a live-service assertion fails against real MLflow 3.x (a plugin defect, a VERIFY
  marker that turns out wrong), **do not patch `src/evalops/**` in this WP** — capture the failing
  evidence (request/response, version) and report it as a blocker/finding for 3.2. This WP may
  adjust its own assertions only where the packet's expectation was wrong, and must say so.
- **F2** No flaky-tolerance tricks: no sleep-polling beyond the readiness wait, no retry-loops
  around assertions. Ingestion is synchronous for tracking-server reads; if something is genuinely
  asynchronous, poll the documented readiness condition, not the assertion.

## Falsifiable done-when

- **H1** `tests/contract/docker-compose.yml` gains the mlflow service (A1) without touching the
  postgres service; `docker compose -f tests/contract/docker-compose.yml up -d` brings up BOTH,
  healthy, on 55432/55000.
- **H2** `make contract` with docker runs the WP1.2 postgres test AND the new mlflow contract
  tests, all green; without docker (or with the drivers missing) everything skips cleanly with the
  documented skip reasons.
- **H3** C1–C5 pass against the live server; the C4 assertions include `error_rate` (WP3.0) and
  the artifact round-trip.
- **H4** `make check` is untouched: same counts, still offline, zero diff in `docs/`, `ci/`, no
  schema change; `git status --short` lists only the allowed paths.
- **H5** Every plugin behavior discovered broken against the live server is reported as evidence,
  not patched.

## Allowed paths

`tests/contract/docker-compose.yml`, `tests/contract/test_mlflow_plugin.py` (new),
`tests/contract/conftest.py` (only if shared readiness helpers justify it), `Makefile` (only if
E2 forces it — say why in the report).

## Forbidden

`src/evalops/**` (all of it — harness only; findings are reported, not patched),
`docs/**` (map/ADR updates are the orchestrator's), `pyproject.toml`, `ci/**`, `evalops.yaml`,
`tests/unit/**` (unit tests stay offline), any golden, deleting/skipping/loosening tests,
`git commit`/`push`/branch, installing anything into the system or the repo `.venv` (docker images
are pulled by compose, which is expected and fine).

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format tests
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q                       # offline count unchanged
.venv/bin/python tests/run_without_pytest.py
make check
make contract                                       # with docker: tails per test
docker compose -f tests/contract/docker-compose.yml ps
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Contract surface
<what each test proves against the live server; skip ladder; compose changes>

## Verification
<exact commands + tail output; PASS/FAIL each — include the make contract per-test lines>

## Live findings
<any VERIFY marker that the live server contradicted, with evidence — or 'none'>

## Claims
<6-10 falsifiable statements>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3.1a (mlflow+postgres contract slice) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. The packet is `docs/packets/WP3.1a.md`; the pattern to
compare against is `tests/contract/test_ledger_postgres.py`.

- **H1 compose.** The postgres service byte-identical (diff it); mlflow service per A1 (port 55000,
  healthcheck, volume); `docker compose up -d` brings both up healthy; `docker compose down` then
  up works (the tests, not you, own lifecycle — verify the tests start what they need).
- **H2 skip ladder.** Run `make contract` with docker (all green) and then prove the skips: point
  `EVALOPS_TEST_MLFLOW_TRACKING_URI` at a dead port → clean skip, not a hang; simulate the
  no-driver path (uninstall nothing — argue from the importorskip lines, or run in a venv without
  the extras). No assertion retry-loops; readiness polling only.
- **H3 coverage honesty.** Each C1–C5 test asserts real server state (search_runs, artifact
  round-trip), not just "no exception". C4 must go through the real `run.py::_evaluate` on a temp
  project (git-clean, real sync) and assert `error_rate` — confirm this test FAILS on a tree
  without WP3.0 (that is the dispatch-order dependency, prove it on a /tmp copy).
- **H4 harness only.** `git diff --stat` — zero `src/evalops/**` changes; any live finding is in
  the report's "Live findings" section with evidence, not patched. Unit suite untouched and
  offline (`make check` count unchanged).
- **H5 determinism.** Two consecutive `make contract` runs both green; no ordering dependence
  between the new tests; the tests clean up runs/datasets they created in the shared compose
  volumes (or prove non-accumulation doesn't affect assertions).

End with the gate decision in exactly this shape:

```text
## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
```
