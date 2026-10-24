"""Fake app that calls one record/replayable tool: exercises the cassette path through the CLI.

The tool is decorated (so `eval` builds a cassette and `--tool-mode replay` reads it), the scorer is
trivial, and nothing needs a network or a vendor SDK.
"""

import evalops
from evalops.tools import replayable


@replayable("lookup_order")
def lookup_order(order_id: str) -> dict:
    return {"order_id": order_id, "status": "delivered"}


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools": "lookup_order@1"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        return f"{question} -> {lookup_order(question)['status']}"

    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [noop]


def judge_meta():
    return {}
