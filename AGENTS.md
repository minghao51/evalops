# AGENTS.md: evalops contract for coding agents

The `evalops` CLI is the **only** interface for prompts, datasets, evaluations, and promotions in this repo, whatever the backend (MLflow, Langfuse, LangSmith, Logfire, git/SQL). Never call those SDKs' write APIs directly. `evalops guard` fails the build if you do.

## Always
- Pass `--json` to every command and branch on the exit code.
  - `0` ok
  - `1` gate failed
  - `2` usage/config error
  - `3` budget exceeded
  - `4` guard violation
  - `5` runtime error
  - `6` unsupported by the configured plugin
- Set `EVALOPS_ACTOR=agent:<your-name>`. Use `EVALOPS_ENV=local` unless the user says otherwise. The `local` env needs no external services.
- Run `evalops plan` before `evalops eval`, and tell the user the estimated cost. On exit 3, stop and ask the user.
- Iterate on the `smoke` dataset. Use `golden_regression` only when asked.
- Read `.evalops/out/last_eval.json` and `.evalops/out/gate_report.md`, and summarize them for the user.
- If you get exit 6, report the plugin's hint to the user. Don't work around it.

## To change behavior
| Goal | Do this |
|---|---|
| Change a prompt | Edit `prompts/<name>.txt` (or `.json` for chat), commit, then run `evalops sync prompts` |
| Add test cases | Append to `datasets/discovery.jsonl`, commit, then run `evalops sync datasets` |
| Change model, tools, index, or flags | Edit what `behavior_config()` reads, then commit |
| Change a judge | Edit `build_scorers`/`judge_meta` and bump `judge_prompt_version`. Never in the same PR as an app change |
| Re-score old outputs | `evalops eval --dataset <d> --judge-only --source-group <group>`. This has no generation cost |
| Write a scorer | Use `@evalops.scorer("name", llm=...)` with the signature `fn(*, inputs, outputs, expectations)` |
| Report tokens when the tracer can't | Call `evalops.report_usage(input_tokens=..., output_tokens=...)` inside predict |
| Debug an agent failure | `evalops --json traces --row <id>`, then read the tree. Fix the cause (prompt, tool, or routing), not the expectation |
| Add agent test cases | Use `expected_tools` / `expected_agents` / `max_steps` / `expected_state` in `datasets/discovery.jsonl` |
| Change a judge | Then run `evalops calibrate --dataset judge_calibration` and report the agreement and kappa |

## Never
- Never edit `.evalops/locks/**` or `.evalops/store/**` by hand.
- Never modify frozen datasets (`golden_regression`, `smoke`). Propose the change; it needs an approval label.
- Never run `promote` or `rollback`, and never pass `--override-budget`.
- Never run `--tool-mode record` or `live` without asking the user first. These call real tools, which may have side effects and cost money. Use replay.
- Never raise `row_limits` or the framework's own hard stops (`recursion_limit`, `max_turns`, ...) to make a looping agent pass.
- Never weaken `gates`, `budgets`, `guard`, or `plugins` in `evalops.yaml` to make a check pass.

## Tests (dev loop)
- `make check` runs lint + typecheck + tests. Run it before reporting a change; it is offline (no docker, no network, no vendor SDK).
- `make test` runs pytest over `tests/unit` and `tests/integration`; `make test-unit` / `make test-integration` narrow it.
- `python tests/run_without_pytest.py` is the dependency-free fallback runner; both it and pytest must stay green.
- Put pure tests in `tests/unit` (offline, <10 s) and lifecycle tests in `tests/integration` (temp-dir repos). Mark docker/credential tests `@pytest.mark.contract`; they are excluded by default.
- Never weaken, skip, or delete a test to go green. Core (anything outside `plugins/` and `traces/`) must not import a vendor SDK; `tests/unit/test_no_vendor_imports.py` enforces it.

## Typical loop
```bash
export EVALOPS_ACTOR=agent:claude EVALOPS_ENV=local
evalops --json guard
git commit -am "prompt: clarify refund policy" && evalops --json sync prompts && git commit -am "evalops locks"
evalops --json plan --dataset smoke --stage dev
evalops --json eval --dataset smoke --stage dev
evalops --json gate
```
