"""Fake app that records a manual agent trace containing a planted secret.

Used by the ``evalops traces`` tests: the core evaluator still opens a per-row capture context with
``tracer: none``, so the predict hook adds spans directly. The secret sits in a tool output and an
LLM output, so the stored rows artifact must be redacted before `traces` can render it.
"""

import evalops
from evalops.traces import capture
from evalops.traces.model import Span

PLANTED = "sk-live-PLANTED-TRACE"


def behavior_config():
    return {"llm_endpoint": "fake-llm@1"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        cap = capture.current()
        if cap is not None:
            cap.add(Span(span_id="s-agent", name="supervisor", kind="agent"), "test")
            cap.add(
                Span(
                    span_id="s-tool",
                    name="lookup",
                    kind="tool",
                    parent_id="s-agent",
                    tool_name="lookup_order",
                    inputs={"q": question},
                    outputs={"api_key": PLANTED},
                ),
                "test",
            )
            cap.add(
                Span(
                    span_id="s-llm",
                    name="llm",
                    kind="llm",
                    parent_id="s-agent",
                    model="fake-gpt",
                    inputs={"prompt": question},
                    outputs={"text": "key " + PLANTED},
                    input_tokens=10,
                    output_tokens=5,
                ),
                "test",
            )
        return "ok"

    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [noop]


def judge_meta():
    return {"llm_judge_count": 0}
