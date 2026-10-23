# HANDOFF: evalops v0.3 → implementation project

Start a new thread by uploading this zip, then point the assistant at this file.

## Where things stand
- **Code:** evalops v0.3 in this folder. 17 tests pass (`python tests/run_without_pytest.py`).
- **Plan:** the "evalops — Implementation Map" Claude Doc: https://claude.ai/artifact/RiATH2LUQjon1RZChjEvRE. It has phases 0–6 plus 3b, the gates, the template design, the testing strategy, gaps, risks, and open decisions. The doc can export itself to Markdown; save the export as `docs/IMPLEMENTATION_MAP.md` in the new repo.
- **Background:** `docs/MLFLOW_GENAI_BEST_PRACTICES.md` (agent-facing best practices, sections 1–14).

## What v0.3 contains
| Area | Files | Status |
|---|---|---|
| Core: config, ports (5 roles), registry, CLI, locks, prompts/datasets-as-code, gate, cost, guard | `src/evalops/*.py` | tested end to end |
| Plugins: git, sql, core/none | `src/evalops/plugins/{git,sql,core}.py` | tested end to end |
| Plugins: mlflow, langfuse, langsmith, logfire | `src/evalops/plugins/*.py` | written to current docs, **never run live**; see the `VERIFY` markers |
| Agent traces: model, normalize (OTel GenAI / OpenInference / Traceloop / ADK / Pydantic AI), capture, circuit breaker | `src/evalops/traces/` | tested with synthetic spans; no real framework installed |
| LangGraph callbacks, OpenAI Agents processor | `traces/langchain.py`, `traces/openai_agents.py` | LangGraph path tested end to end (fake graph); OpenAI Agents with synthetic spans |
| Trajectory scorers, judge helpers | `src/evalops/scorers/` | tested |
| Tool cassettes (record/replay) | `src/evalops/tools.py` | tested |
| Reliability gating (pass^k, CI lower bound), judge calibration | `gate.py`, `calibrate.py` | tested |
| CI pipeline + adapters | `ci/pipeline.sh`, `ci/*.yml` | `pipeline.sh` simulated locally (pr → main → release → nightly); YAML adapters not run on real CI |

## Decisions made after v0.3 (the code does NOT reflect these yet)
1. **Wrap and normalize, never rebuild.** evalops delegates execution, tracing, storage, UIs and model calls. It owns only the glue: normalized traces, the plugin contract, version pinning, gates and statistics, cost, CLI and pipeline, cassettes, calibration.
2. **Use built-in judges first.** Each setup wraps its native judges:
   - MLflow scorers and `make_judge`
   - Langfuse managed evaluators, which run server-side; evalops reads the scores back
   - openevals for LangSmith
   - pydantic-evals for Logfire and the local setup

   The fallback is an evalops rubric judge via LiteLLM, used only where a setup has none, plus pairwise (position swap) and panel wrappers. `scorers/judges.py` (span_judge, trajectory_judge) should become adapters/wrappers in phase 3b.
3. **Pin every judge.** The gate refuses judges running on a default model, because built-in defaults change (pydantic-evals across releases; Langfuse's project default applies to existing evaluators). Record the resolved model per verdict, and check server-side judge settings nightly.
4. **Lineage.** Write native backlinks (the Langfuse prompt on generations, the MLflow active model, LangSmith experiment metadata, Logfire span attributes), keep a lineage record per run, add `evalops lineage`, and add change attribution to the gate report.
5. **Template.** Copier, covering all 7 backend presets from phase 2. Support tiers: Generated → Contract-verified → Framework-verified.
6. **Testing.** Hypothesis property tests, plus a stateful contract suite (`RuleBasedStateMachine`) parametrized per backend, plus real span fixtures per framework and version.

## Known limits and bugs to carry forward
- The vendor plugins' API details are unverified (`VERIFY`), so resolve them in phase 3/5 contract jobs.
- Persisted formats (locks, rows, traces, cassettes) have no version fields yet. That is phase 0.
- Traces, rows and cassettes are stored unredacted. A redaction hook is needed (phase 1).
- The SQLite ledger is local only; CI needs Postgres or MLflow.
- The git plugin turns alias moves into commits, so the release job needs push rights.
- `pipeline.sh` is bash-only. Plan: `evalops pipeline <stage>` (phase 2).
- The package name `evalops` may be taken on PyPI. Check it in phase 0.

## Suggested first message for the new thread
> Here is evalops v0.3 (zip) and its HANDOFF.md. Read HANDOFF.md and the Implementation Map doc (link inside). Start phase 0: confirm the open decisions with me (package name, license, GitHub owner, Pydantic in core), then restructure the repo (src/, tests/unit/), add the typed config + JSON schema, and add version fields to the persisted formats. Keep all 17 tests green.
