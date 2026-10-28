# WP3.1b packet — contract slice: the Langfuse stack (six-container v3) behind a compose profile

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **503 offline tests, 6 contract tests**
(WP1.2 postgres + WP3.1a's five mlflow tests; `make contract` = 6 green with docker).
**Dispatch order: after WP3.0 (landed `dc617ee`) — independent of WP3.1a, which is also landed**
(`25943b4`): this WP adds a NEW compose profile + a NEW test module; the only shared file is
`tests/contract/docker-compose.yml` (additions only, existing services byte-identical).
Read `tests/contract/test_ledger_postgres.py` and `tests/contract/test_mlflow_plugin.py` (the two
pattern predecessors), `tests/contract/docker-compose.yml` (the profile goes here),
`src/evalops/plugins/langfuse.py` (the surface under test — four roles: prompts, datasets, tracer,
evaluator), `docs/adr/0005-contract-testing-scope.md`, and `docs/adr/0008` (parity behaviors are
asserted here too).

## Environment

```bash
cd /home/howt/work/evalops_universal/evalops
docker --version && docker compose version
make check            # 503 passed, 6 deselected — must stay exactly this offline
# the langfuse slice needs the langfuse extra + drivers OUTSIDE the repo .venv (forbidden):
uv venv /tmp/lf-contract-venv
uv pip install --python /tmp/lf-contract-venv/bin/python '/home/howt/work/evalops_universal/evalops[langfuse,sql]' pytest
make contract PYTHON=/tmp/lf-contract-venv/bin/python
```

**Why six containers:** the plugin pins SDK `langfuse>=3`, whose server is microservices: `web`,
`worker`, `postgres` (Langfuse metadata — separate from our ledger), `clickhouse` (traces/scores
OLAP — this is what `api.trace.get` and score read-back query), `redis` (queue), `minio` (blob).
Langfuse v2 would dodge ClickHouse but speaks an API SDK v3 doesn't target — out of scope.
~2.5–3.5GB RAM, ~2GB one-time image pull, first boot runs migrations (budget the readiness wait).

## Goal

Prove the four-role Langfuse plugin against a live, self-contained Langfuse v3 stack — **no
external credentials**: the stack bootstraps its own org/project/user and pins API keys via
Langfuse's documented `LANGFUSE_INIT_*` self-host seeding envs. Everything runs behind a compose
profile so the default `make contract` path is byte-for-byte today's.

The design decisions below are **fixed**; deviations are reported, not invented.

### A. Compose (`tests/contract/docker-compose.yml`) — additions only, profiled

- **A1** Every new service carries `profiles: ["langfuse"]`. Default `make contract` (no profile)
  starts exactly today's two services; the langfuse slice runs
  `docker compose --profile langfuse up -d`. Existing services stay byte-identical.
- **A2** Vendor Langfuse's own self-hosting compose shape with **pinned image tags** (record the
  upstream source + tag in a comment): `langfuse-web` (host port **33000** → container 3000),
  `langfuse-worker`, `postgres` (internal), `clickhouse` + its init container (bucket/setup),
  `minio` + bucket-init, `redis`. Only `web` needs a host port. Required web/worker envs
  (`NEXTAUTH_SECRET`, `SALT`, `ENCRYPT_KEY`, `DATABASE_URL`, `CLICKHOUSE_*`, `REDIS_*`,
  `S3_*`) get fixed dev-only values in the file.
- **A3** Bootstrap (keys are part of the fixture, not secrets): `LANGFUSE_INIT_ORG_*`,
  `LANGFUSE_INIT_PROJECT_*`, `LANGFUSE_INIT_USER_*` with
  `LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-e2e-contract` /
  `LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-e2e-contract`. These env names are documented for
  self-host seeding; if the running server ignores or renames any of them, that is a **live
  finding** — adapt within the compose (e.g. seed via the public API after boot) and report.
- **A4** Healthcheck on web (`/api/public/health`); readiness budget **300s** for first boot
  (migrations), then the standard skip.

### B. The test module (`tests/contract/test_langfuse_plugin.py`, new)

- **B1** Same ladder as the two predecessors: `pytest.importorskip("langfuse")`,
  `importorskip("sqlalchemy")`, `importorskip("psycopg")`; compose started by the test with the
  profile flag (skip if docker unusable); readiness poll on web `/api/public/health` (300s) → skip;
  `EVALOPS_TEST_LANGFUSE_BASE_URL` (default `http://127.0.0.1:33000`),
  `EVALOPS_TEST_LANGFUSE_PUBLIC_KEY` / `_SECRET_KEY` (defaults = A3's pinned keys) overrides.
- **B2** Config under test: the full langfuse wiring
  (`plugins: {prompts: langfuse, datasets: langfuse, tracer: langfuse, evaluator: langfuse}`,
  `ledger: sql` on the existing compose postgres at 55432) — the `langfuse` preset's shape plus the
  mixed ledger the capability matrix advertises. Credentials flow via the standard
  `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`/`LANGFUSE_BASE_URL` envs the plugin documents.
- **C — what is asserted (each its own focused test):**
  - **C1 prompts**: `register` → `load` returns the exact template; `set_alias` → `get_alias`
    resolves to that version; `set_alias` preserves existing labels and never re-adds `latest`;
    a missing alias → `None`; a second `register` bumps the version.
  - **C2 datasets**: `write` → `exists` → `read` round-trips inputs+expectations; **write
    idempotence** (the sha256 item ids): writing the same records twice does not duplicate rows
    (`read` count stable); order-insensitive compare (see WP3.1a finding #7 — port order is
    undefined).
  - **C3 tracer + the VERIFY line**: through a real `_evaluate`, each row returns a `trace_id`;
    after `flush` + ingestion poll, `lf.api.trace.get(trace_id)` succeeds (the map's
    `langfuse.api.trace.get` VERIFY) and the plugin's `usage()` returns a `Usage` for the id
    (zeros are fine — the fixture app calls no LLM; assert the call path, not tokens). Row spans
    carry the `evalops.row` observation name server-side.
  - **C4 evaluator, native-dataset path**: full `run.evaluate` on a temp project (git-clean, real
    sync) with a lookup-table predict + one scorer → rows with scores, `native_ref` =
    `langfuse:<id-or-url>`; metrics carry the scorer aggregate **and** `error_rate` (WP3.0);
    the dataset run is visible server-side; rows.jsonl + manifest artifacts land in the Postgres
    ledger. Then the **local-data VERIFY branch**: same but with `datasets: git` (dataset_store is
    not langfuse) so `evaluate` takes the top-level `run_experiment(data=...)` path — assert it
    works live or record the finding.
  - **C5 stored_rows judge-only**: the mlflow-precedent delegation (langfuse evaluator → core
    re-score) through a real `_evaluate(judge_only=True, source_group=...)` on the mixed wiring.
  - **C6 doctor**: `auth_check` true against the seeded project.
- **D — not asserted**: `budget_exceeded_rate > 0`, langsmith surfaces, cost-accuracy (fixture
  reports usage via `report_usage`; trace-derived amounts are asserted present, not exact),
  archived-dataset filtering beyond a smoke row.

### E. Runner integration

- **E1** `make check` stays exactly: 503 passed, 6 deselected (the new tests are `-m contract`,
  skipped offline).
- **E2** `make contract` (no profile, repo .venv): still 6 passed / skips unchanged. With the
  profile up and a venv carrying the langfuse extra:
  `docker compose --profile langfuse up -d && make contract PYTHON=<venv>` runs all of them. If
  the Makefile needs a `contract-langfuse` convenience target, it is allowed — say why.
- **E3** Teardown is the test's job: `--profile langfuse down` (or `down -v` for a fresh volume
  run) happens via the same finalizer discipline as 3.1a; a full-session cleanup proves the server
  holds only bootstrap leftovers (best-effort deletes via the API where the SDK exposes them —
  report what cannot be deleted).

### F. Honesty rules (identical to 3.1a)

- **F1** Harness only: **zero `src/evalops/**` changes**. Any behavior the live server contradicts
  (SDK/server shape drift, `LANGFUSE_INIT_*` gaps, label semantics, `api.trace.get` payload) is a
  **live finding** with evidence for WP3.2 — reported, never patched here. Adjust the test's own
  expectations only where the packet's assumption (not the plugin) was wrong, and say so.
- **F2** No sleeps beyond readiness/ingestion polls (ingestion is async: poll a documented
  condition with a deadline, as `usage()` itself does — never blanket-sleep around assertions).

## Falsifiable done-when

- **H1** All new services carry `profiles: ["langfuse"]`; existing services byte-identical;
  `make contract` (no profile) is byte-for-byte today's behavior (6 passed / 6 deselected offline).
- **H2** `docker compose --profile langfuse up -d` on an empty volume brings up all six + init
  containers healthy; web answers `/api/public/health` within 300s of first boot; the pinned
  `pk-lf-e2e-contract`/`sk-lf-e2e-contract` keys authenticate (`auth_check` true).
- **H3** C1–C6 pass against the live stack, including the local-data `run_experiment` branch and
  the `api.trace.get` VERIFY line exercised with real trace ids.
- **H4** Skip ladder proven: dead web URL → clean skips (no hang); no drivers → clean skips;
  `make check` counts untouched.
- **H5** Blast radius: `git status --short` lists only the allowed paths; zero `src/evalops/**`,
  `docs/**`, `ci/**`, golden, or Makefile changes (unless E2's target, declared).
- **H6** Gates: `make check` green, standalone runner green, `make contract` green both with and
  without the profile (per B/E2), schema/capabilities/ci-adapters zero diff.

## Allowed paths

`tests/contract/docker-compose.yml` (additions + profile flags),
`tests/contract/test_langfuse_plugin.py` (new), `Makefile` (only if E2's target — declare why).

## Forbidden

`src/evalops/**` (all of it — findings are reported, not patched), `docs/**` (map updates are the
orchestrator's), `pyproject.toml`, `ci/**`, `evalops.yaml`, `tests/unit/**`,
`tests/contract/test_ledger_postgres.py`, `tests/contract/test_mlflow_plugin.py` (read them, don't
touch them), any golden, deleting/skipping/loosening tests, `git commit`/`push`/branch, installing
anything into the repo `.venv` or the system (the throwaway venv + docker pulls are expected).

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format tests
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q                       # 503 passed, 6 deselected
.venv/bin/python tests/run_without_pytest.py        # 503/503
make check
docker compose -f tests/contract/docker-compose.yml --profile langfuse up -d
make contract PYTHON=/tmp/lf-contract-venv/bin/python          # per-test tails, twice
docker compose -f tests/contract/docker-compose.yml --profile langfuse ps
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Contract surface
<what each test proves against the live stack; profile/flag mechanics; bootstrap used; cleanup>

## Verification
<exact commands + tail output; PASS/FAIL each — include make contract per-test lines, twice>

## Live findings
<any server/SDK behavior that contradicts the plugin or this packet, with evidence — or 'none'>

## Claims
<6-10 falsifiable statements>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3.1b (langfuse six-container contract slice) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. Packet: `docs/packets/WP3.1b.md`. The stack should
still be up (`docker compose -f tests/contract/docker-compose.yml --profile langfuse ps`) — use it.

- **H1 profile isolation.** Existing services byte-identical (diff the compose); every new service
  profiled; `make contract` WITHOUT the profile runs exactly the pre-WP tree's behavior (6 passed
  offline-skipped as before); `--profile langfuse up -d` from an empty volume brings up all six +
  init containers; web health within the stated budget on first boot.
- **H2 self-contained credentials.** The keys authenticate because of the compose-seeded bootstrap,
  not a manually created account: recreate from scratch (down -v, up) and watch C6 pass without
  any manual step. If the worker fell back to API-seeding, verify it is fully automated in the
  test/compose, not a manual step they ran once.
- **H3 coverage honesty.** C1–C6 assert server state (api.trace.get payloads, dataset-run
  visibility, ledger artifacts), not just absence of exceptions; the local-data `run_experiment`
  VERIFY branch is exercised (or its finding recorded); C3 proves `api.trace.get` with a REAL
  trace id from a real `_evaluate`, not a fabricated one.
- **H4 skip ladder.** Dead web URL → clean skips without hanging; no-drivers → clean skips;
  `make check` counts untouched (503 passed, 6 deselected).
- **H5 harness only.** `git diff --stat`: compose + new test module only (plus a declared Makefile
  target if any); zero `src/evalops/**`; live findings reported with evidence, not patched; unit
  suite untouched.
- **H6 determinism.** Two consecutive full `make contract` (profiled, venv) runs green; fresh
  -volumes run green; cleanup leaves no session residue (or the residue is documented as
  undeletable via the API).
- **Live findings audit.** Spot-check the report's findings against the live server yourself —
  WP3.2's langfuse packet will be written from them, so wrong evidence here poisons the next WP.

End with the gate decision in exactly this shape:

```text
## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
```
