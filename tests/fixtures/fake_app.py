"""Deterministic fake app for end-to-end tests: async predict, flaky first call, app-reported usage."""
import asyncio

import evalops

KB = {
    "How do I reset my password?": "Use the Forgot password link, then check your email.",
    "Can I get a refund after 30 days?": "Refunds are only available within 30 days.",
    "What are your business hours?": "We are open 9am-5pm, Monday to Friday.",
}
_seen = set()


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    system = prompts["support_system"]

    async def predict(question: str) -> str:
        await asyncio.sleep(0)
        if question not in _seen:            # first call per question fails like a 429
            _seen.add(question)
            raise RuntimeError("429 Too Many Requests (simulated)")
        text = system.format(question=question)
        evalops.report_usage(input_tokens=len(text.split()) * 10, output_tokens=50)
        return "I don't know." if "BE_UNHELPFUL" in text else KB.get(question, "I don't know.")

    return predict


@evalops.scorer("facts")
def facts(*, outputs, expectations):
    want = (expectations or {}).get("expected_facts") or []
    return sum(f.lower() in str(outputs).lower() for f in want) / len(want) if want else None


@evalops.scorer("helpful", llm=True)
def helpful(*, inputs, outputs):
    return "no" if "don't know" in str(outputs) else "yes"     # string verdicts are normalized


def build_scorers(evaluator="core"):
    return [facts, helpful]


def judge_meta():
    return {"judge_model": "fake-judge@1", "judge_prompt_version": "1", "llm_judge_count": 1}
