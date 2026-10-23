"""End-to-end lifecycle on zero-dependency plugins (git prompts/datasets, sql ledger, core evaluator).

Exercises: sync, guard, plan, eval (async + flaky predict with retries), gate, promote, baseline,
regression detection, judge-only re-scoring, budgets, frozen datasets, capability errors, and a
third-party plugin loaded via "module:Class".
"""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

CONFIG = """
project: e2e
default_env: local
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
environments:
  local: {}
  broken: {plugins: {ledger: git}}
  custom: {plugins: {tracer: "custom_tracer:CountingTracer"}}
app:
  behavior_config: fake_app:behavior_config
  predict_fn: fake_app:build_predict_fn
  scorers: fake_app:build_scorers
  judge_meta: fake_app:judge_meta
datasets:
  golden_regression: {frozen: true}
  smoke: {frozen: true}
eval: {max_workers: 3, max_scorer_workers: 2, retries: 2, backoff_seconds: 0.01, repeats: {main: 2}}
gates:
  metrics:
    facts/mean: {min: 0.5, max_drop: 0.1}
    helpful/mean: {min: 0.5}
    error_rate: {max: 0.0}
  cost: {max_cost_per_row_increase_pct: 50}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
"""

CUSTOM_TRACER = '''
from evalops.ports import Plugin, Tracer
class CountingTracer(Plugin, Tracer):
    name = "counting"
    provides = ("tracer",)
    def wrap(self, fn, *, run_id, row_id):
        def traced(**inputs):
            return fn(**inputs), f"count-{row_id}"
        return traced
'''


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def make_project() -> pathlib.Path:
    d = pathlib.Path(tempfile.mkdtemp(prefix="evalops-e2e-"))
    (d / "evalops.yaml").write_text(CONFIG)
    shutil.copytree(ROOT / "prompts", d / "prompts")
    shutil.copytree(ROOT / "datasets", d / "datasets")
    (d / "datasets" / "discovery.jsonl").unlink()
    shutil.copy(ROOT / ".gitignore", d / ".gitignore")
    shutil.copy(ROOT / "tests" / "fixtures" / "fake_app.py", d / "fake_app.py")
    (d / "custom_tracer.py").write_text(CUSTOM_TRACER)
    sh(["git", "init", "-q"], d)
    sh(["git", "config", "user.email", "t@t"], d)
    sh(["git", "config", "user.name", "t"], d)
    commit(d, "init")
    return d


def commit(d, msg):
    sh(["git", "add", "-A"], d)
    subprocess.run(["git", "commit", "-qm", msg], cwd=d, capture_output=True)


def cli(d, *args, env=None):
    e = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{d}", "EVALOPS_ACTOR": "test", **(env or {})}
    p = subprocess.run([sys.executable, "-m", "evalops", "--json", *args], cwd=d, env=e, capture_output=True, text=True)
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise AssertionError(f"non-JSON output (exit {p.returncode}): {p.stdout}\n{p.stderr}")
    return p.returncode, out


def test_full_lifecycle():
    d = make_project()

    code, out = cli(d, "plugins")
    assert code == 0 and all(r["ok"] for r in out["roles"].values()), out

    assert cli(d, "guard")[0] == 0                       # static checks pass before first sync
    code, out = cli(d, "sync", "all", "--check")
    assert code == 4 and "no prompts lock" in out["error"], out

    code, out = cli(d, "sync", "all")
    assert code == 0 and out["prompts"]["registered"] == ["support_system@1"], out
    commit(d, "sync")
    assert cli(d, "guard")[0] == 0
    assert cli(d, "sync", "all", "--check")[0] == 0

    code, out = cli(d, "plan", "--dataset", "golden_regression", "--stage", "main")
    assert code == 0 and out["estimate"]["llm_calls"] == 3 * 2 * 2, out

    # PR-stage smoke eval: async predict, first call per row fails -> retried
    code, out = cli(d, "eval", "--dataset", "smoke", "--stage", "pr")
    assert code == 0, out
    m = out["runs"][0]["metrics"]
    assert m["facts/mean"] == 1.0 and m["helpful/mean"] == 1.0 and m["error_rate"] == 0.0, m
    assert m["gen_cost_usd"] > 0 and m["judge_cost_usd_est"] > 0, m
    assert cli(d, "gate")[0] == 0

    # main: 2 repeats on golden -> gate -> promote candidate + production (sets baseline)
    code, out = cli(d, "eval", "--dataset", "golden_regression", "--stage", "main")
    assert code == 0 and len(out["run_ids"]) == 2, out
    main_group = out["group"]
    assert cli(d, "gate")[0] == 0
    code, out = cli(d, "promote", "--alias", "production", "--evidence-group", main_group)
    assert code == 0 and out["moves"][0]["to"] == "1", out
    aliases = json.loads((d / ".evalops/store/prompts/support_system/aliases.json").read_text())
    assert aliases == {"production": "1"}
    assert cli(d, "eval", "--dataset", "smoke", "--stage", "pr")[0] == 4   # git-plugin alias move = uncommitted
    commit(d, "promote")                                                  # (ci/pipeline.sh commits it)
    code, out = cli(d, "baseline")
    assert out["baseline"]["group"] == main_group

    # PR-stage evidence cannot be promoted
    code, out = cli(d, "eval", "--dataset", "smoke", "--stage", "pr")
    pr_group = out["group"]
    cli(d, "gate")
    assert cli(d, "promote", "--alias", "production", "--evidence-group", pr_group)[0] == 4

    # A worse prompt is caught by the gate (quality drop vs baseline)
    p = d / "prompts" / "support_system.txt"
    p.write_text(p.read_text() + "\nBE_UNHELPFUL\n")
    commit(d, "worse prompt")
    assert cli(d, "eval", "--dataset", "smoke", "--stage", "pr")[0] == 4      # out of sync -> guard
    assert cli(d, "sync", "prompts")[0] == 0
    commit(d, "sync worse")
    code, out = cli(d, "eval", "--dataset", "golden_regression", "--stage", "pr")
    assert code == 0 and out["runs"][0]["metrics"]["facts/mean"] == 0.0
    code, out = cli(d, "gate")
    assert code == 1 and not out["ok"], out
    assert "FAILED" in (d / ".evalops/out/gate_report.md").read_text()

    # Judge-only re-score of the baseline outputs: no generation cost
    code, out = cli(d, "eval", "--dataset", "golden_regression", "--stage", "dev", "--judge-only",
                    "--source-group", main_group)
    assert code == 0, out
    m = out["runs"][0]["metrics"]
    assert m["gen_cost_usd"] == 0 and m["facts/mean"] == 1.0, m

    # Budget enforcement
    cfg = (d / "evalops.yaml").read_text().replace("per_run_usd: 5", "per_run_usd: 0.000001")
    (d / "evalops.yaml").write_text(cfg)
    commit(d, "tight budget")
    assert cli(d, "eval", "--dataset", "smoke", "--stage", "dev")[0] == 3
    assert cli(d, "eval", "--dataset", "smoke", "--stage", "dev", "--dry-run", "--override-budget")[0] == 0
    code, out = cli(d, "cost")
    assert code == 0 and out["month_spent_usd"] > 0

    # Frozen dataset change needs approval; approved change creates a NEW content-addressed snapshot
    g = d / "datasets" / "smoke.jsonl"
    g.write_text(g.read_text() + json.dumps({"inputs": {"question": "What are your business hours?"}}) + "\n")
    commit(d, "edit frozen")
    assert cli(d, "guard")[0] == 4
    assert cli(d, "sync", "datasets")[0] == 4
    old = json.loads((d / ".evalops/locks/local/datasets.lock.json").read_text())["smoke"]["physical"]
    assert cli(d, "sync", "datasets", env={"EVALOPS_ALLOW_FROZEN_CHANGE": "true"})[0] == 0
    new = json.loads((d / ".evalops/locks/local/datasets.lock.json").read_text())["smoke"]["physical"]
    assert old != new and (d / f".evalops/store/datasets/{old}.jsonl").exists()

    # Capability error: git cannot be a ledger
    code, out = cli(d, "plugins", env={"EVALOPS_ENV": "broken"})
    assert out["roles"]["ledger"]["ok"] is False
    assert cli(d, "baseline", env={"EVALOPS_ENV": "broken"})[0] == 6

    # Guard catches direct backend calls in project code
    (d / "sneaky.py").write_text("from langfuse import get_client\nget_client().create_prompt(name='x', prompt='y')\n")
    code, out = cli(d, "guard", env={"EVALOPS_ALLOW_FROZEN_CHANGE": "true"})
    assert code == 4 and any("sneaky.py" in v for v in out["violations"]), out
    (d / "sneaky.py").unlink()
    commit(d, "frozen approved")

    # Third-party plugin via module:Class (separate env -> its own locks)
    env = {"EVALOPS_ENV": "custom"}
    assert cli(d, "sync", "all", env=env)[0] == 0
    commit(d, "custom locks")
    code, out = cli(d, "eval", "--dataset", "smoke", "--stage", "dev", "--override-budget", env=env)
    assert code == 0 and out["plugins"]["tracer"] == "custom_tracer:CountingTracer", out
    shutil.rmtree(d, ignore_errors=True)
