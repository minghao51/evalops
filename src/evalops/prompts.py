"""Prompts-as-code: git files -> immutable backend versions -> per-env prompts.lock.json.

Lock readers use `locks.entries()`: the reserved `_meta` key (and any `_`-prefixed key) is file
metadata, never a prompt name.
"""

import json
import os
from typing import Any

from . import locks
from .util import EXIT_GUARD, EXIT_USAGE, EvalOpsError, git_sha, sha256

KIND = "prompts"


def _sources(cfg: Any) -> dict[str, Any]:
    d = cfg.path("prompts")
    if not d.exists():
        return {}
    return {p.stem: p for p in sorted(d.iterdir()) if p.suffix in {".txt", ".json"}}


def diff(cfg: Any) -> tuple[list[str], list[str]]:
    lock, src = locks.entries(locks.read(cfg, KIND)), _sources(cfg)
    changed = [n for n, p in src.items() if lock.get(n, {}).get("sha256") != sha256(p.read_text())]
    plugin = cfg.plugins["prompts"]
    stale_plugin = [n for n, e in lock.items() if e.get("plugin") != plugin]
    return sorted(set(changed) | set(stale_plugin)), sorted(set(lock) - set(src))


def sync(cfg: Any, reg: Any, check: bool = False) -> dict[str, Any]:
    changed, orphaned = diff(cfg)
    if orphaned:
        raise EvalOpsError(f"lock lists prompts with no source file: {orphaned}", EXIT_GUARD)
    if check:
        if not locks.path(cfg, KIND).exists() and _sources(cfg):
            raise EvalOpsError(f"no prompts lock for env '{cfg.env}'; run `evalops sync prompts`", EXIT_GUARD)
        if changed:
            raise EvalOpsError(
                "prompts out of sync with lock; run `evalops sync prompts`",
                EXIT_GUARD,
                {"changed": changed, "env": cfg.env},
            )
        return {"summary": "prompts in sync", "changed": []}
    if not changed:
        return {"summary": "prompts in sync", "registered": []}

    store = reg.get("prompts")
    lock, src, registered = locks.entries(locks.read(cfg, KIND)), _sources(cfg), []
    for name in changed:
        raw = src[name].read_text()
        template = json.loads(raw) if src[name].suffix == ".json" else raw
        tags = {
            "evalops.sha256": sha256(raw),
            "evalops.git_sha": git_sha(cwd=cfg.root),
            "evalops.source": str(src[name].relative_to(cfg.root)),
        }
        if pr := os.getenv("EVALOPS_PR"):
            tags["evalops.pr"] = pr
        physical = store.physical_name(name)
        version = str(store.register(physical, template, tags))
        lock[name] = {"plugin": store.name, "physical": physical, "version": version, "sha256": tags["evalops.sha256"]}
        registered.append(f"{physical}@{version}")
    locks.write(cfg, KIND, lock)
    return {
        "summary": f"registered {len(registered)} prompt version(s) in {store.name}; commit the lock",
        "registered": registered,
    }


def ref(entry: dict[str, Any]) -> str:
    return f"{entry['plugin']}:{entry['physical']}@{entry['version']}"


def load_locked(cfg: Any, reg: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    store, lock = reg.get("prompts"), locks.entries(locks.read(cfg, KIND))
    return {n: store.load(e["physical"], e["version"]) for n, e in lock.items()}, lock


def promote(cfg: Any, reg: Any, alias: str, evidence_group: str) -> dict[str, Any]:
    """Move `alias` on every locked prompt, only with a passing, promotable evidence group."""
    ledger, store, lock = reg.get("ledger"), reg.get("prompts"), locks.entries(locks.read(cfg, KIND))
    runs = ledger.find_runs(tags={"evalops.group": evidence_group})
    if not runs:
        raise EvalOpsError(f"no runs for evidence group {evidence_group}", EXIT_USAGE)
    for r in runs:
        if r.tags.get("evalops.promotable") != "true":
            raise EvalOpsError("evidence is not promotable (dirty git or non-main stage)", EXIT_GUARD)
        if r.tags.get("evalops.gate") != "passed":
            raise EvalOpsError("evidence has not passed `evalops gate`", EXIT_GUARD)
        mismatched = [n for n, e in lock.items() if r.params.get(f"prompt.{n}") != ref(e)]
        if mismatched:
            raise EvalOpsError(f"evidence used different prompt versions: {mismatched}", EXIT_GUARD)

    moves = []
    for name, entry in lock.items():
        previous = store.get_alias(entry["physical"], alias)
        store.set_alias(entry["physical"], alias, entry["version"])
        moves.append({"prompt": entry["physical"], "from": previous, "to": entry["version"]})

    if alias == "production":  # evidence group becomes the baseline for gates and control runs
        for r in ledger.find_runs(tags={"evalops.baseline": "true"}):
            ledger.log(r.run_id, tags={"evalops.baseline": "false"})
        for r in runs:
            ledger.log(r.run_id, tags={"evalops.baseline": "true"})
    for r in runs:
        ledger.log(r.run_id, tags={f"evalops.promoted.{alias}": json.dumps(moves)})
    return {"summary": f"moved @{alias} for {len(moves)} prompt(s) via {store.name}", "moves": moves}


def rollback(cfg: Any, reg: Any, alias: str, prompt: str, version: str) -> dict[str, Any]:
    lock = locks.entries(locks.read(cfg, KIND))
    if prompt not in lock:
        raise EvalOpsError(f"unknown prompt '{prompt}'", EXIT_USAGE)
    store = reg.get("prompts")
    store.set_alias(lock[prompt]["physical"], alias, str(version))
    return {"summary": f"{lock[prompt]['physical']}@{alias} -> {version} (rollback via {store.name})"}
