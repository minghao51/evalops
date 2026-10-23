"""Lock files are per environment: the same git source maps to different backend versions per env.

Reserved key `_meta`: every lock document carries `{"_meta": {schema_version, evalops_version,
created_at}}` next to the `name -> entry` mapping (the mapping stays flat so `lock.get(name)` keeps
working). `_meta` — and any other key starting with `_` — is never a prompt or dataset name, so
readers always iterate through `entries()` and writers always go through `write()`/`document()`.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

from .contract import formats
from .contract.formats import is_meta, meta


def path(cfg: Any, kind: str) -> pathlib.Path:
    d: pathlib.Path = cfg.lock_dir / "locks" / cfg.env
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{kind}.lock.json"


def entries(lock: dict[str, Any] | None) -> dict[str, Any]:
    """The `name -> entry` view of a lock document: reserved/metadata keys are dropped."""
    return {k: v for k, v in (lock or {}).items() if not is_meta(k)}


def document(data: dict[str, Any]) -> dict[str, Any]:
    """The on-disk shape: the entries plus the reserved `_meta` block."""
    return {**entries(data), formats.META_KEY: meta()}


def read(cfg: Any, kind: str) -> dict[str, Any]:
    """The whole lock document as stored (including `_meta`; absent for v0 files). Never iterate it."""
    p = path(cfg, kind)
    if not p.exists():
        return {}
    doc = formats.load_json(p)
    formats.check_version(doc.get(formats.META_KEY) or doc, p)  # v0: no _meta -> unversioned -> ok
    return doc


def write(cfg: Any, kind: str, data: dict[str, Any]) -> None:
    path(cfg, kind).write_text(json.dumps(document(data), indent=2, sort_keys=True) + "\n")


def scratch(cfg: Any, name: str) -> pathlib.Path:
    """Generated, git-ignored outputs (last_eval.json, reports)."""
    d: pathlib.Path = cfg.lock_dir / "out"
    d.mkdir(parents=True, exist_ok=True)
    return d / name
