"""Example hooks: the ONLY project-specific code evalops needs. Backend-neutral by design."""
import hashlib
import json
import os

import evalops

TOOLS = [{"name": "lookup_order", "parameters": {"order_id": "string"}}]  # your real tool schemas


def behavior_config() -> dict:
    """Everything that changes behavior but is NOT in a prompt. Any change -> new app version."""
    return {
        "llm_endpoint": os.getenv("APP_LLM_ENDPOINT", "<provider/model>@<pinned-version>"),
        "tools_manifest_sha": hashlib.sha256(json.dumps(TOOLS, sort_keys=True).encode()).hexdigest()[:12],
        "retriever_index_version": os.getenv("APP_INDEX_VERSION", "<index-snapshot-id>"),
        "feature_flags": {"use_reranker": os.getenv("APP_USE_RERANKER", "true")},
    }


def build_predict_fn(prompts: dict):
    """prompts: {name: LoadedPrompt} pinned from the per-env lock. Same code for every backend."""
    system = prompts["support_system"]

    def predict(question: str) -> str:  # parameter names = keys of each record's "inputs"; may be async
        text = system.format(question=question)
        answer, usage = call_llm(text)
        # Optional: report usage yourself when your tracer can't (tracer: none, custom clients).
        evalops.report_usage(input_tokens=usage["in"], output_tokens=usage["out"])
        return answer

    return predict


def call_llm(text: str):
    raise NotImplementedError("call your model; return (answer, {'in': tokens, 'out': tokens})")


@evalops.scorer("mentions_expected_facts")
def mentions_expected_facts(*, outputs, expectations):
    facts = (expectations or {}).get("expected_facts") or []
    return sum(f.lower() in str(outputs).lower() for f in facts) / len(facts) if facts else None


def build_scorers(evaluator: str = "core") -> list:
    """Return neutral scorers (work everywhere) and/or backend-native ones for that evaluator."""
    scorers = [mentions_expected_facts]
    if evaluator in ("mlflow", "core"):
        try:  # MLflow built-in judges are callable with inputs/outputs/expectations -> usable by core too
            from mlflow.genai.scorers import Correctness, Safety
            scorers += [Correctness(inference_params={"temperature": 0.0}), Safety()]
        except ImportError:
            pass
    return scorers


def judge_meta() -> dict:
    """Logged on every run and part of the version hash. llm_judge_count drives cost estimates."""
    return {"judge_model": os.getenv("JUDGE_MODEL", "<judge-model>@<pinned-version>"),
            "judge_prompt_version": "1", "llm_judge_count": 2}
