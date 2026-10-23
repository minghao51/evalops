"""Pure-logic tests (no backends)."""
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from evalops import config, cost, datasets, gate, scoring  # noqa: E402
from evalops.ports import RowResult, Usage  # noqa: E402
from evalops.util import render  # noqa: E402


class StubCfg:
    def __init__(self, data):
        self.data = data

    def section(self, key):
        return self.data.get(key) or {}


def test_env_expansion_and_merge():
    os.environ["X_URI"] = "http://x"
    assert config._expand({"a": "${X_URI}", "b": "${NOPE_NOT_SET:-dflt}"}) == {"a": "http://x", "b": "dflt"}
    assert config._merge({"p": {"a": 1, "b": 2}}, {"p": {"b": 3}}) == {"p": {"a": 1, "b": 3}}


def test_render_text_and_chat():
    assert render("Hi {{ name }}", {"name": "A"}) == "Hi A"
    assert render([{"role": "user", "content": "{{q}}?"}], {"q": "x"}) == [{"role": "user", "content": "x?"}]


def test_fingerprint_order_independent():
    a = [{"inputs": {"q": 1}}, {"inputs": {"q": 2}, "expectations": {"e": "x"}}]
    assert datasets.fingerprint(a) == datasets.fingerprint(list(reversed(a)))
    assert datasets.fingerprint(a) != datasets.fingerprint(a[:1])


def test_score_normalization():
    class Feedback:  # MLflow-like
        value = "yes"

    class Evaluation:  # Langfuse-like
        value = 0.25
    assert scoring.to_float(Feedback()) == 1.0
    assert scoring.to_float(Evaluation()) == 0.25
    assert scoring.to_float({"key": "k", "score": False}) == 0.0
    assert scoring.to_float("fail") == 0.0 and scoring.to_float(None) is None

    @scoring.scorer("multi")
    def multi(*, outputs):
        return [{"key": "a", "score": 1}, {"key": "b", "score": 0}]
    assert scoring.call_scorer(multi, inputs={}, outputs="x", expectations=None) == {"a": 1.0, "b": 0.0}


def test_estimate_and_measure():
    cfg = StubCfg({"cost": {"prices_per_1m_tokens": {"generation": {"input": 1, "output": 2},
                                                     "judge": {"input": 1, "output": 1}},
                            "defaults": {"gen_input_tokens_per_row": 1000, "gen_output_tokens_per_row": 500,
                                         "judge_input_tokens_per_call": 1000, "judge_output_tokens_per_call": 0}}})
    est = cost.estimate(cfg, rows=100, repeats=2, llm_judges=3, judge_only=False, hist={})
    assert (est["gen_usd"], est["judge_usd"], est["total_usd"]) == (0.4, 0.6, 1.0)
    assert est["llm_calls"] == 800
    rows = [RowResult("0", {}, "o", usage=Usage(1000, 500)), RowResult("1", {}, "o", usage=Usage(0, 0, 0.5)),
            RowResult("2", {}, "o")]
    m = cost.measure(cfg, rows)
    assert m["gen_cost_usd"] == 0.502 and m["rows_missing_usage"] == 1
    assert m["gen_cost_source"] == "price_table+reported"


def test_gate_rules():
    rules = {"metrics": {"q/mean": {"min": 0.8, "max_drop": 0.03}, "error_rate": {"max": 0.0}},
             "cost": {"max_cost_per_row_increase_pct": 20}}
    base = gate.aggregate([{"q/mean": 0.90, "cost_per_row_usd": 0.010, "error_rate": 0}])
    ok = gate.aggregate([{"q/mean": 0.88, "cost_per_row_usd": 0.011, "error_rate": 0}] * 2)
    assert gate.check(ok, base, rules)[0]
    assert not gate.check(gate.aggregate([{"q/mean": 0.85, "cost_per_row_usd": 0.01, "error_rate": 0}]), base, rules)[0]
    assert not gate.check(gate.aggregate([{"q/mean": 0.9, "cost_per_row_usd": 0.013, "error_rate": 0}]), base, rules)[0]
    assert not gate.check(gate.aggregate([{"q/mean": 0.9, "cost_per_row_usd": 0.01, "error_rate": 0.1}]), base, rules)[0]
    assert gate.check(ok, None, rules)[0]
