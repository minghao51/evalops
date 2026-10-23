"""Versioned persisted formats: one schema_version for every artifact evalops writes.

Persisted shapes (public contract; additive changes only, see docs/IMPLEMENTATION_MAP.md D12):

  .evalops/locks/<env>/prompts.lock.json   {<name>: {...}, "_meta": {schema_version, ...}}
  .evalops/locks/<env>/datasets.lock.json  {<name>: {...}, "_meta": {schema_version, ...}}
  cassettes/<dataset>.jsonl                {"_meta": {...}} first line, then one entry per line
  evalops/rows.jsonl artifact              per line: row fields + schema_version + evalops_version
  evalops/cost_estimate.json artifact      estimate + schema_version + evalops_version at top level
  .evalops/out/last_eval.json              result + schema_version + evalops_version at top level
  evalops/manifest.json artifact           {schema_version, evalops_version, artifacts: {name: sha}}

Readers accept v0 (an unversioned file written by evalops <= 0.3) for one minor release. A file
that declares a schema_version newer than FORMAT_VERSION is a contract mismatch: it raises
`FormatError` (exit 7) rather than being misread.

Identity hashes (cassette fingerprint, dataset fingerprint, `app_version`) are computed over
*semantic* fields only, via `semantic()`. Metadata (`_meta`, `schema_version`, `evalops_version`,
`created_at`, backend ids...) must never reach a fingerprint, so stamping an artifact does not
invalidate recorded runs and does not change `app_version` (risk R1).
"""

from __future__ import annotations

import json
import pathlib
import time
from collections.abc import Iterable
from typing import Any

from ..util import EXIT_RUNTIME, EvalOpsError, FormatError, sha256

FORMAT_VERSION = 1
META_KEY = "_meta"  # reserved lock/cassette key; never a prompt, dataset or tool name
VERSION_FIELDS = ("schema_version", "evalops_version")


def evalops_version() -> str:
    """The evalops version that wrote an artifact (imported lazily: `evalops/__init__` imports us)."""
    import evalops

    return getattr(evalops, "__version__", "unknown")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # UTC, seconds, no datetime tz dep


def meta(**extra: Any) -> dict[str, Any]:
    """The reserved `_meta` block for mapping-shaped and line-shaped formats."""
    return {"schema_version": FORMAT_VERSION, "evalops_version": evalops_version(), "created_at": _now(), **extra}


def is_meta(key: Any) -> bool:
    """True for a reserved/private key: `_meta` and any other key starting with `_`."""
    return isinstance(key, str) and key.startswith("_")


def semantic(obj: dict[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    """The identity-relevant subset of `obj`: only `keys` that are present. Never includes metadata."""
    return {k: obj[k] for k in keys if k in obj}


def stamp(payload: dict[str, Any]) -> dict[str, Any]:
    """Add the version fields that per-line/top-level formats carry (rows.jsonl, last_eval.json)."""
    return {**payload, "schema_version": FORMAT_VERSION, "evalops_version": evalops_version()}


def check_version(payload: Any, source: Any) -> int:
    """Return `payload`'s schema version (0 when unversioned); raise FormatError on a future one.

    A payload that is not a JSON object cannot be a versioned artifact of any version: it is a
    contract mismatch (exit 7), never silently read as v0.
    """
    if not isinstance(payload, dict):
        raise FormatError(str(source), "not-an-object", FORMAT_VERSION)
    found = payload.get("schema_version", 0)
    if not isinstance(found, int) or isinstance(found, bool) or found > FORMAT_VERSION:
        raise FormatError(str(source), found, FORMAT_VERSION)
    return found


def write_versioned(path: Any, payload: dict[str, Any], indent: int = 2) -> dict[str, Any]:
    """Write `payload` plus the version fields as JSON; returns what was written."""
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    doc = stamp(payload)
    p.write_text(json.dumps(doc, indent=indent, default=str) + "\n")
    return doc


def load_json(path: Any) -> dict[str, Any]:
    """Parse a JSON object from `path`; a corrupt artifact is a runtime error that names the file."""
    p = pathlib.Path(path)
    try:
        doc = json.loads(p.read_text())
    except json.JSONDecodeError as e:
        raise EvalOpsError(f"{p}: not valid JSON: {e}", EXIT_RUNTIME) from e
    if not isinstance(doc, dict):
        raise FormatError(str(p), "not-an-object", FORMAT_VERSION)
    return doc


def read_versioned(path: Any) -> dict[str, Any]:
    """Read a versioned JSON document; v0 (no schema_version) reads fine, v>FORMAT_VERSION raises."""
    doc = load_json(path)
    check_version(doc, pathlib.Path(path))
    return doc


def manifest(artifacts: dict[str, str]) -> dict[str, Any]:
    """The artifact manifest: {schema_version, evalops_version, artifacts: {name: sha256hex}}.

    `artifacts` maps the artifact name as stored in the ledger to its exact text, so the hashes can
    be verified against what was actually written. The manifest does not hash itself.
    """
    return {
        "schema_version": FORMAT_VERSION,
        "evalops_version": evalops_version(),
        "artifacts": {name: sha256(text) for name, text in sorted(artifacts.items())},
    }
