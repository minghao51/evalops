"""Datasets-as-code: datasets/<name>.jsonl -> content-addressed, never-mutated backend datasets."""
import json
import os

from . import locks
from .util import EXIT_GUARD, EXIT_USAGE, EvalOpsError, canonical, git_sha, sha256

KIND = "datasets"


def read_records(path) -> list[dict]:
    records = []
    for i, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if not isinstance(rec.get("inputs"), dict):
            raise EvalOpsError(f"{path.name}:{i} needs an 'inputs' object", EXIT_USAGE)
        out = {"inputs": rec["inputs"]}
        for key in ("expectations", "outputs", "trace"):  # outputs/trace: pre-recorded (calibration sets)
            if rec.get(key) is not None:
                out[key] = rec[key]
        records.append(out)
    return records


def fingerprint(records: list[dict]) -> str:
    rows = sorted(canonical({"inputs": r.get("inputs"), "expectations": r.get("expectations") or None,
                             **{k: r[k] for k in ("outputs", "trace") if r.get(k) is not None}})
                  for r in records)
    return sha256("\n".join(rows))


def status(cfg):
    lock, declared = locks.read(cfg, KIND), cfg.section("datasets")
    plugin = cfg.plugins["datasets"]
    changed, missing, frozen_changed = [], [], []
    for name, opts in declared.items():
        path = cfg.path("datasets") / f"{name}.jsonl"
        if not path.exists():
            missing.append(name)
            continue
        entry = lock.get(name, {})
        if entry.get("fingerprint") != fingerprint(read_records(path)) or entry.get("plugin") != plugin:
            changed.append(name)
            if (opts or {}).get("frozen") and entry.get("fingerprint") not in (None, fingerprint(read_records(path))):
                frozen_changed.append(name)
    return changed, missing, frozen_changed


def frozen_changes_approved() -> bool:
    return os.getenv("EVALOPS_ALLOW_FROZEN_CHANGE", "").lower() == "true"


def sync(cfg, reg, check: bool = False) -> dict:
    changed, missing, frozen_changed = status(cfg)
    if missing:
        raise EvalOpsError(f"declared datasets have no source file: {missing}", EXIT_USAGE)
    if check:
        if changed:
            raise EvalOpsError("datasets out of sync with lock; run `evalops sync datasets`", EXIT_GUARD,
                               {"changed": changed, "env": cfg.env})
        return {"summary": "datasets in sync", "changed": []}
    if frozen_changed and not frozen_changes_approved():
        raise EvalOpsError("frozen dataset content changed; needs approval "
                           "(EVALOPS_ALLOW_FROZEN_CHANGE=true via an approved CI label)",
                           EXIT_GUARD, {"frozen_changed": frozen_changed})
    if not changed:
        return {"summary": "datasets in sync", "synced": []}

    store = reg.get("datasets")
    lock, max_rows, synced = locks.read(cfg, KIND), cfg.section("limits").get("max_dataset_rows"), []
    for name in changed:
        records = read_records(cfg.path("datasets") / f"{name}.jsonl")
        if max_rows and len(records) > int(max_rows):
            raise EvalOpsError(f"{name}: {len(records)} rows exceeds limits.max_dataset_rows={max_rows}", EXIT_USAGE)
        fp = fingerprint(records)
        physical = store.physical_name(f"{name}__{fp[:8]}")
        if not store.exists(physical):  # content-addressed: an existing snapshot is never rewritten
            store.write(physical, records)
        lock[name] = {"plugin": store.name, "physical": physical, "fingerprint": fp, "rows": len(records),
                      "git_sha": git_sha(cwd=cfg.root)}
        synced.append(physical)
    locks.write(cfg, KIND, lock)
    return {"summary": f"synced {len(synced)} dataset(s) to {store.name}; commit the lock", "synced": synced}


def load(cfg, reg, name: str, verify: bool):
    entry = locks.read(cfg, KIND).get(name)
    if not entry:
        raise EvalOpsError(f"dataset '{name}' not synced; run `evalops sync datasets`", EXIT_USAGE)
    store = reg.get("datasets")
    records = store.read(entry["physical"])
    if verify and fingerprint(records) != entry["fingerprint"]:
        raise EvalOpsError(f"stored dataset for '{name}' was modified outside evalops", EXIT_GUARD)
    return records, entry
