"""The frozen ``--json`` envelope (docs/CONTRACT.md §2) and the exit-code matrix (§1).

``SCHEMA_VERSION`` and ``EXIT_MATRIX`` are the single source of truth for the envelope shape and
for the exit codes: ``cli.main`` builds every payload through this module, and the golden snapshots
plus the exit-code matrix test import the same constants, so a bump or a renumber is one edit.

Contract, enforced by tests/unit/test_envelope.py:

- with ``--json`` stdout is exactly one JSON document plus a newline and nothing else;
- the process exit code always equals ``envelope["code"]``;
- ``ok`` is true iff ``code`` is 0; ``data`` is always an object; ``errors`` is always a list;
- top-level keys are frozen — new fields go inside ``data`` / ``errors[].details``;
- human output is free-form and not a contract.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .util import (
    EXIT_BUDGET,
    EXIT_CAPABILITY,
    EXIT_CONTRACT,
    EXIT_GATE_FAILED,
    EXIT_GUARD,
    EXIT_OK,
    EXIT_RUNTIME,
    EXIT_USAGE,
)

SCHEMA_VERSION = 1

#: `data` keys a command renders as its own human block. ``render`` omits them from the generic
#: ``key: value`` listing so the same content is not printed twice (``evalops doctor`` prints
#: ``beta`` as the ``beta (unverified surfaces):`` block after the roles). This is human output
#: only: the envelope, and therefore every ``--json`` rendering, always carries the key. Add a key
#: here only together with the command that renders it.
HUMAN_SPECIAL_KEYS: frozenset[str] = frozenset({"beta"})

#: Exit code -> name. The names are the ones in docs/CONTRACT.md §1; codes are never renumbered.
EXIT_MATRIX: dict[int, str] = {
    EXIT_OK: "OK",
    EXIT_GATE_FAILED: "GATE_FAILED",
    EXIT_USAGE: "USAGE",
    EXIT_BUDGET: "BUDGET",
    EXIT_GUARD: "GUARD",
    EXIT_RUNTIME: "RUNTIME",
    EXIT_CAPABILITY: "CAPABILITY",
    EXIT_CONTRACT: "CONTRACT",
}


def success(command: str, env: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    """The success envelope: ``ok: true``, ``code: 0``, no errors."""
    return {
        "ok": True,
        "code": EXIT_OK,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "env": env,
        "data": dict(data or {}),
        "errors": [],
    }


def failure(
    command: str,
    env: str,
    code: int,
    message: str,
    hint: str | None = None,
    details: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The failure envelope: one error carrying the message, the optional hint and details."""
    if code not in EXIT_MATRIX or code == EXIT_OK:
        raise ValueError(f"failure() needs an exit code in 1..7, got {code!r}")
    error: dict[str, Any] = {"code": code, "message": str(message)}
    if hint:
        error["hint"] = str(hint)
    if details:
        error["details"] = dict(details)
    return {
        "ok": False,
        "code": code,
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "env": env,
        "data": dict(data or {}),
        "errors": [error],
    }


def from_result(command: str, env: str, result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a ``dispatch()`` payload.

    The command's own ``ok`` flag decides success vs. EXIT_GATE_FAILED (a gate, reliability,
    calibration or doctor verdict), exactly as before the envelope existed. Everything else —
    including ``summary``, which human mode prints — goes into ``data`` unchanged.
    """
    payload = {key: value for key, value in result.items() if key != "ok"}
    if result.get("ok", True):
        return success(command, env, payload)
    message = payload.get("summary") if isinstance(payload.get("summary"), str) else None
    return failure(command, env, EXIT_GATE_FAILED, message or f"{command} reported a failure", data=payload)


def from_error(command: str, env: str, exc: BaseException) -> dict[str, Any]:
    """Wrap an exception: its ``code`` (an ``EvalOpsError``), message, hint and details."""
    code = getattr(exc, "code", EXIT_RUNTIME)
    if not isinstance(code, int) or code == EXIT_OK or code not in EXIT_MATRIX:
        code = EXIT_RUNTIME
    details = getattr(exc, "details", None) or None
    hint = getattr(exc, "hint", None) or None
    message = str(exc) if str(exc) else type(exc).__name__
    return failure(command, env, code, message, hint=hint, details=details)


def render(env_dict: dict[str, Any], as_json: bool, stream: TextIO = sys.stdout) -> None:
    """Write the envelope: one JSON document on ``--json``, a short readable rendering otherwise."""
    if as_json:
        stream.write(json.dumps(env_dict, indent=2, default=str) + "\n")
        stream.flush()
        return
    name = EXIT_MATRIX.get(env_dict["code"], str(env_dict["code"]))
    for error in env_dict["errors"]:
        stream.write(f"evalops {env_dict['command']} failed ({name}): {error['message']}\n")
        if error.get("hint"):
            stream.write(f"  hint: {error['hint']}\n")
        for key, value in (error.get("details") or {}).items():
            stream.write(f"  {key}: {_plain(value)}\n")
    data = env_dict["data"]
    if data.get("summary"):
        stream.write(f"{data['summary']}\n")
    for key, value in data.items():
        if key != "summary" and key not in HUMAN_SPECIAL_KEYS:
            stream.write(f"  {key}: {_plain(value)}\n")
    stream.flush()


def _plain(value: Any) -> str:
    return json.dumps(value, default=str) if isinstance(value, (dict, list)) else str(value)
