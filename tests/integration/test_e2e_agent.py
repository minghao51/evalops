"""End-to-end agentic lifecycle: a LangGraph-style multi-agent app captured via callbacks.

Covers: trace capture, trajectory + route + args scorers, tool record/replay (offline CI), traces CLI,
reliability gating (pass^k) catching a flaky router, the circuit breaker catching a runaway loop,
trace-derived per-model cost, judge-only re-scoring of stored traces, and judge calibration.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
REFUND = {
    "expected_tools": ["lookup_order", {"name": "issue_refund", "args": {"order_id": "A1", "amount": 89.9}}],
    "forbidden_tools": ["delete_account"],
    "max_steps": 6,
    "expected_agents": ["supervisor", "refund_agent"],
    "expected_state": {"status": "refunded"},
}
GOLDEN = [
    {"inputs": {"question": "I want a refund for order A1"}, "expectations": REFUND},
    {"inputs": {"question": "Please refund my last order"}, "expectations": REFUND},
    {
        "inputs": {"question": "What are your hours?"},
        "expectations": {
            "expected_tools": ["policy_search"],
            "expected_agents": ["supervisor", "info_agent"],
            "expected_state": {"status": "answered"},
            "max_steps": 4,
        },
    },
]
CALIBRATION = [
    {"inputs": {"question": "q1"}, "outputs": {"status": "refunded"}, "expectations": {"human_labels": {"helpful": 1}}},
    {"inputs": {"question": "q2"}, "outputs": {"status": "error"}, "expectations": {"human_labels": {"helpful": 0}}},
    {"inputs": {"question": "q3"}, "outputs": "Sorry, I cannot help", "expectations": {"human_labels": {"helpful": 0}}},
    {"inputs": {"question": "q4"}, "outputs": "Here is your answer", "expectations": {"human_labels": {"helpful": 1}}},
]
CONFIG = """
project: agent-e2e
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
app:
  behavior_config: fake_agent_app:behavior_config
  predict_fn: fake_agent_app:build_predict_fn
  scorers: fake_agent_app:build_scorers
  judge_meta: fake_agent_app:judge_meta
datasets:
  agent_golden: {frozen: true}
  judge_calibration: {frozen: true}
traces: {capture: true}
tools: {mode_by_stage: {dev: live, pr: replay, main: replay, nightly: live}, on_miss: error}
eval:
  max_workers: 3
  retries: 1
  backoff_seconds: 0.01
  repeats: {main: 3}
  row_limits: {max_steps: 8}
gates:
  metrics:
    tool_trajectory/mean: {min: 0.9}
    agent_route/mean: {min: 0.9}
    tool_args/mean: {min: 0.9}
    error_rate: {max: 0.0}
    trace.tool_calls/mean: {max_increase_pct: 50}
  reliability:
    success: [{metric: tool_trajectory/mean, min: 1.0}, {metric: agent_route/mean, min: 1.0}]
    min_pass_hat_k: 0.9
judges:
  helpful: {min_agreement: 0.75, min_kappa: 0.5}
cost:
  prices_per_1m_tokens:
    generation: {input: 1.0, output: 1.0}
    judge: {input: 1.0, output: 1.0}
    models: {fake-gpt: {input: 2.5, output: 10.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git"]}
"""


def sh(cmd, cwd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def commit(d, msg):
    sh(["git", "add", "-A"], d)
    subprocess.run(["git", "commit", "-qm", msg], cwd=d, capture_output=True)


def cli(d, *args, env=None):
    e = {**os.environ, "PYTHONPATH": f"{ROOT / 'src'}{os.pathsep}{d}", "EVALOPS_ACTOR": "test", **(env or {})}
    p = subprocess.run([sys.executable, "-m", "evalops", "--json", *args], cwd=d, env=e, capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(f"non-JSON output (exit {p.returncode}): {p.stdout}\n{p.stderr}") from e


def set_prompt(d, text):
    (d / "prompts" / "router.txt").write_text(text)


def test_agent_lifecycle():
    d = pathlib.Path(tempfile.mkdtemp(prefix="evalops-agent-"))
    (d / "evalops.yaml").write_text(CONFIG)
    (d / "prompts").mkdir()
    set_prompt(d, "Route the request to the right agent: {{question}}")
    (d / "datasets").mkdir()
    (d / "datasets" / "agent_golden.jsonl").write_text("".join(json.dumps(r) + "\n" for r in GOLDEN))
    (d / "datasets" / "judge_calibration.jsonl").write_text("".join(json.dumps(r) + "\n" for r in CALIBRATION))
    shutil.copy(ROOT / ".gitignore", d / ".gitignore")
    shutil.copy(ROOT / "tests" / "fixtures" / "fake_agent_app.py", d / "fake_agent_app.py")
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"], ["git", "config", "user.name", "t"]):
        sh(c, d)
    assert cli(d, "sync", "all")[0] == 0
    commit(d, "init")
    offline = {"TOOLS_OFFLINE": "1"}

    # 1) Record tool cassettes against live tools, then commit them
    code, out = cli(d, "eval", "--dataset", "agent_golden", "--stage", "dev", "--tool-mode", "record")
    assert code == 0, out
    m = out["data"]["runs"][0]["metrics"]
    assert m["tool_trajectory/mean"] == 1.0 and m["agent_route/mean"] == 1.0 and m["tool_args/mean"] == 1.0, m
    assert m["trace.tool_calls/mean"] > 0 and m["gen_cost_usd"] > 0, m
    assert (d / "cassettes" / "agent_golden.jsonl").exists()
    commit(d, "record cassettes")

    # 2) PR stage replays tools: works fully offline
    code, out = cli(d, "eval", "--dataset", "agent_golden", "--stage", "pr", env=offline)
    assert code == 0 and out["data"]["tool_mode"] == "replay", out
    m = out["data"]["runs"][0]["metrics"]
    assert m["error_rate"] == 0.0 and m["tool_replay_misses"] == 0 and m["tool_trajectory/mean"] == 1.0, m
    # cost came from the captured trace with the per-model price: 2 LLM calls x (120 in, 30 out) per row
    rows = 3
    assert abs(m["gen_cost_usd"] - rows * 2 * (120 * 2.5 + 30 * 10.0) / 1e6) < 1e-9, m

    code, out = cli(d, "traces", "--row", "00000")
    tree = out["data"]["rows"][0]["tree"]
    assert "[agent] supervisor" in tree and "[tool] lookup_order" in tree and "[llm] fake-gpt" in tree, tree

    # 3) Main: 3 repeats, reliability gate passes -> promote to production (baseline)
    code, out = cli(d, "eval", "--dataset", "agent_golden", "--stage", "main", env=offline)
    assert code == 0 and len(out["data"]["run_ids"]) == 3, out
    main_group = out["data"]["group"]
    code, out = cli(d, "gate")
    assert code == 0, out
    rel = next(c for c in out["data"]["checks"] if c["check"] == "reliability (info)")
    assert rel["cand_pass_hat_k"] == 1.0 and rel["cand_k"] == 3, rel
    assert cli(d, "promote", "--alias", "production", "--evidence-group", main_group)[0] == 0
    commit(d, "promote")

    # 4) Flaky router: averages look OK-ish, but pass^k exposes the unreliability
    set_prompt(d, "Route the request to the right agent: {{question}} ROUTE_FLAKY")
    commit(d, "flaky")
    assert cli(d, "sync", "prompts")[0] == 0
    commit(d, "sync flaky")
    code, out = cli(d, "eval", "--dataset", "agent_golden", "--stage", "main", "--tool-mode", "live")
    assert code == 0, out
    code, out = cli(d, "gate")
    assert code == 1, out
    failed = [c["check"] for c in out["data"]["checks"] if not c["passed"]]
    assert any(c.startswith("pass_hat_k") for c in failed), failed

    # 5) Runaway loop: the circuit breaker aborts the run mid-graph (before the next tool executes)
    set_prompt(d, "Route the request to the right agent: {{question}} LOOP")
    commit(d, "loop")
    assert cli(d, "sync", "prompts")[0] == 0
    commit(d, "sync loop")
    code, out = cli(d, "eval", "--dataset", "agent_golden", "--stage", "dev", "--tool-mode", "live")
    assert code == 0, out
    m = out["data"]["runs"][0]["metrics"]
    assert abs(m["budget_exceeded_rate"] - 2 / 3) < 1e-9 and m["error_rate"] > 0, m
    assert cli(d, "gate")[0] == 1

    # 6) Judge-only: re-score the baseline's STORED traces (no generation, trajectory scorers still work)
    code, out = cli(
        d, "eval", "--dataset", "agent_golden", "--stage", "dev", "--judge-only", "--source-group", main_group
    )
    assert code == 0, out
    m = out["data"]["runs"][0]["metrics"]
    assert m["gen_cost_usd"] == 0 and m["tool_trajectory/mean"] == 1.0 and m["agent_route/mean"] == 1.0, m

    # 7) Judge calibration against human labels; a broken judge is caught before it can gate anything
    code, out = cli(d, "calibrate", "--dataset", "judge_calibration")
    assert code == 0 and out["data"]["judges"]["helpful"]["agreement"] == 1.0, out
    code, out = cli(d, "calibrate", "--dataset", "judge_calibration", env={"JUDGE_BROKEN": "1"})
    assert code == 1 and out["data"]["judges"]["helpful"]["agreement"] == 0.0, out
    shutil.rmtree(d, ignore_errors=True)
