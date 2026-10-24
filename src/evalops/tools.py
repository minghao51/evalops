"""Tool record/replay ("cassettes") for deterministic, side-effect-free agent evals.

    @tool                                   # LangChain/LangGraph, or ADK FunctionTool, CrewAI tool, ...
    @evalops.replayable("lookup_order")     # innermost, so the framework sees the real signature
    def lookup_order(order_id: str) -> dict: ...

Modes (set per stage in evalops.yaml `tools.mode_by_stage`, or `evalops eval --tool-mode`):
  live    call the real tool (nightly / integration)
  record  call the real tool and save the response into cassettes/<dataset>.jsonl (commit it)
  replay  return the recorded response; a miss is an error (or `live` with tools.on_miss: live)
Outside an evalops row the decorator is a no-op passthrough, so production is unaffected.
Cassettes live in git; their fingerprint is logged and part of the app version hash.
A cassette file is versioned: the writer emits a `_meta` line first ({schema_version,
  evalops_version, created_at}), the reader skips it, and a file without `_meta` is v0 and still
loads. The fingerprint covers only the semantic entry fields (key, tool, args, response, error), so
adding `_meta` or re-recording metadata never changes it.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import pathlib
import threading

from .contract import formats, redact
from .util import EXIT_RUNTIME, EvalOpsError, FormatError, canonical, sha256

_ctx: contextvars.ContextVar = contextvars.ContextVar("evalops_tools", default=None)
_active: "Cassette | None" = None  # process-wide fallback: worker threads don't inherit contextvars

# Identity fields of a cassette entry: what the fingerprint hashes. Metadata is never included.
ENTRY_FIELDS = ("key", "tool", "args", "response", "error")


class ToolReplayMiss(RuntimeError):
    pass


class Cassette:
    def __init__(self, path: pathlib.Path | None, mode: str, on_miss: str = "error"):
        self.path, self.mode, self.on_miss = path, mode, on_miss
        self.entries: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.dirty = False
        if path is not None and path.exists():
            self._load(path)
        self.misses: list[str] = []

    def _load(self, path: pathlib.Path) -> None:
        """v0 files (no `_meta` line) and v1 files (a leading `_meta` line) both load.

        A line that is not a JSON object is a contract mismatch (exit 7), never a TypeError from
        indexing a string/list.
        """
        if self.path is None:  # pragma: no cover - defensive
            return
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EvalOpsError(f"{path}:{i}: not valid JSON: {exc}", EXIT_RUNTIME) from exc
            if not isinstance(e, dict):
                raise FormatError(f"{path}:{i}", "not-an-object", formats.FORMAT_VERSION)
            if formats.META_KEY in e and "key" not in e:
                formats.check_version(e[formats.META_KEY] or e, path)
                continue
            self.entries[e["key"]] = e

    def fingerprint(self) -> str:
        """sha256 over the semantic entry fields only: metadata cannot change an app version."""
        if not self.entries:
            return "none"
        rows = sorted(canonical(formats.semantic(e, ENTRY_FIELDS)) for e in self.entries.values())
        return sha256("\n".join(rows))[:12]

    def save(self) -> None:
        """Write the cassette; the entries written are a redacted *deep copy* (CONTRACT §5.1).

        `self.entries` and `fingerprint()` are identity inputs (`app_version` hashes the
        fingerprint), so `save()` must not touch them: redaction happens on the copy that goes to
        disk. The copy is normalized first (``json.dumps(default=str)`` round-trip), so a tuple,
        set, dataclass or custom object cannot escape the walk while its ``str()`` is persisted.
        `_meta` gains `redacted`/`redaction_rules` only when a rule actually changed something, and
        those keys are metadata: the fingerprint never covers `_meta`.
        """
        if not self.dirty or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        redactor = redact.current()
        payload = redact.normalize_json(dict(self.entries))
        entries, changes = redactor.redact(payload)
        extra = {"redacted": True, "redaction_rules": redactor.fingerprint()} if changes else {}
        meta_line = json.dumps({formats.META_KEY: formats.meta(**extra)}, sort_keys=True, default=str) + "\n"
        text = meta_line + "".join(
            json.dumps(e, sort_keys=True, default=str) + "\n" for _, e in sorted(entries.items())
        )
        if redactor.mode == "strict":  # refuse to persist a payload a rule still matches (exit 5)
            residual = redactor.residual(entries)
            if residual:
                raise EvalOpsError(
                    f"strict redaction: {self.path} would still contain {len(residual)} value(s) matching a "
                    f"redaction rule (e.g. {residual[0]}); nothing was written",
                    EXIT_RUNTIME,
                    {"path": str(self.path), "paths": residual[:50]},
                    hint="a redaction hook must be idempotent and return None for already-redacted input",
                )
        self.path.write_text(text)
        self.dirty = False


@contextlib.contextmanager
def use(cassette: Cassette | None):
    global _active
    token, previous = _ctx.set(cassette), _active
    _active = cassette
    try:
        yield cassette
    finally:
        _ctx.reset(token)
        _active = previous


def _current():
    return _ctx.get() or _active


def _key(tool: str, args: dict) -> str:
    return sha256(canonical({"tool": tool, "args": args}))[:24]


def replayable(name: str | None = None):
    def deco(fn):
        tool = name or fn.__name__
        sig = inspect.signature(fn)

        def bound(*a, **kw):
            try:
                b = sig.bind(*a, **kw)
                b.apply_defaults()
                return dict(b.arguments)
            except TypeError:
                return {"args": list(a), "kwargs": kw}

        def before(args):
            cas = _current()
            if cas is None or cas.mode == "live":
                return None, None, cas
            key = _key(tool, args)
            if cas.mode == "replay":
                hit = cas.entries.get(key)
                if hit is not None:
                    return key, hit, cas
                # The replay failed, and the diagnostic is the only place the arguments can reach
                # a sink (`misses` -> last_eval.json, `--json` data, the miss error message). It is
                # therefore redacted here, at capture time, before any sink -- not at a boundary
                # (CONTRACT §5.1). `fields`/`hash` cannot match a flat string; `patterns`/`hook` can.
                label = redact.redact_string(f"{tool}({canonical(args)[:200]})")
                redactor = redact.current()
                if redactor.mode == "strict":  # never hand a still-matching value to the caller
                    residual = redactor.residual(label)
                    if residual:
                        raise EvalOpsError(
                            f"strict redaction: the replay-miss label for {tool} still matches "
                            f"{len(residual)} rule(s) after redaction (e.g. {residual[0] or '<root>'}); "
                            f"nothing was written",
                            EXIT_RUNTIME,
                            {
                                "tool": tool,
                                "source": "tool_replay_misses",
                                "paths": residual[: redact.PATH_CAP],
                            },
                            hint="a redaction hook must be idempotent and return None for already-redacted input",
                        )
                cas.misses.append(label)
                if cas.on_miss != "live":
                    raise ToolReplayMiss(f"no recording for {label}; re-record with `evalops eval --tool-mode record`")
            return key, None, cas

        def replay(hit):
            if hit.get("error"):
                raise RuntimeError(hit["error"])
            return hit["response"]

        def record(cas, key, args, response=None, error=None):
            if cas is not None and cas.mode == "record":
                with cas.lock:
                    cas.entries[key] = {"key": key, "tool": tool, "args": args, "response": response, "error": error}
                    cas.dirty = True

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_wrapper(*a, **kw):
                args = bound(*a, **kw)
                key, hit, cas = before(args)
                if hit is not None:
                    return replay(hit)
                try:
                    out = await fn(*a, **kw)
                except Exception as e:  # noqa: BLE001
                    record(cas, key, args, error=f"{type(e).__name__}: {e}")
                    raise
                record(cas, key, args, response=out)
                return out

            wrapper = async_wrapper
        else:

            @functools.wraps(fn)
            def sync_wrapper(*a, **kw):
                args = bound(*a, **kw)
                key, hit, cas = before(args)
                if hit is not None:
                    return replay(hit)
                try:
                    out = fn(*a, **kw)
                except Exception as e:  # noqa: BLE001
                    record(cas, key, args, error=f"{type(e).__name__}: {e}")
                    raise
                record(cas, key, args, response=out)
                return out

            wrapper = sync_wrapper
        setattr(wrapper, "evalops_tool", tool)  # noqa: B010 - marker read by frameworks/diagnostics
        return wrapper

    return deco
