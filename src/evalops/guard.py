"""Static guard: keep versioning un-bypassable by people, CI scripts and coding agents.

Static only (safe to run before `sync`): lock freshness is checked by `evalops sync all --check`.

Flags direct calls to any backend's write APIs for prompts/datasets/evaluations in project code.
Extend or override the list with guard.forbidden_calls in evalops.yaml.
"""
import re

from . import datasets as ds_mod
from .util import EXIT_GUARD, EvalOpsError

DEFAULT_FORBIDDEN = [
    # MLflow
    "register_prompt(", "set_prompt_alias(", "delete_prompt(", "merge_records(", "genai.evaluate(",
    # Langfuse
    "create_prompt(", "update_prompt(", "create_dataset_item(", "run_experiment(",
    # LangSmith
    "push_prompt(", "create_commit(", "create_examples(", "create_example(", "langsmith.evaluate(",
    "client.evaluate(", "aevaluate(",
    # shared names
    "create_dataset(",
]
ALIAS_LOAD = [
    re.compile(r"""load_prompt\(\s*(?:name_or_uri\s*=\s*)?f?["']prompts:/[^"']*@"""),  # MLflow alias
    re.compile(r"""get_prompt\([^)]*label\s*="""),                                     # Langfuse label
    re.compile(r"""pull_prompt\(\s*f?["'][^"':]+:[A-Za-z_-]+["']"""),                    # LangSmith tag
]


def run_guard(cfg) -> dict:
    g = cfg.section("guard")
    exclude = set(g.get("exclude", [".venv", "venv", ".git", "node_modules"]))
    eval_dirs = tuple(d.rstrip("/") + "/" for d in g.get("eval_dirs", ["eval"]))
    forbidden = g.get("forbidden_calls", DEFAULT_FORBIDDEN)
    allowed = {k: set(v) for k, v in (g.get("allow") or {}).items()}  # call -> files allowed to use it
    errors = []

    for path in cfg.root.rglob("*.py"):
        rel = path.relative_to(cfg.root).as_posix()
        if any(rel == e or rel.startswith(e.rstrip("/") + "/") for e in exclude) or set(path.parts) & exclude:
            continue
        text = path.read_text(errors="ignore")
        for call in forbidden:
            if call in text and rel not in allowed.get(call, set()):
                errors.append(f"{rel}: '{call[:-1]}' must go through the evalops CLI")
        if rel.startswith(eval_dirs) and any(p.search(text) for p in ALIAS_LOAD):
            errors.append(f"{rel}: evaluation code must not load prompts by alias/label/tag")

    _, _, frozen_changed = ds_mod.status(cfg)
    if frozen_changed and not ds_mod.frozen_changes_approved():
        errors.append(f"frozen dataset(s) changed without approval: {frozen_changed}")
    if errors:
        raise EvalOpsError(f"guard found {len(errors)} violation(s)", EXIT_GUARD, {"violations": errors})
    return {"summary": "guard passed", "violations": []}
