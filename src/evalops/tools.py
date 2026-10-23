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
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import inspect
import json
import threading

from .util import canonical, sha256

_ctx: contextvars.ContextVar = contextvars.ContextVar("evalops_tools", default=None)
_active: "Cassette | None" = None   # process-wide fallback: worker threads don't inherit contextvars


class ToolReplayMiss(RuntimeError):
    pass


class Cassette:
    def __init__(self, path, mode: str, on_miss: str = "error"):
        self.path, self.mode, self.on_miss = path, mode, on_miss
        self.entries: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.dirty = False
        if path is not None and path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.entries[e["key"]] = e
        self.misses: list[str] = []

    def fingerprint(self) -> str:
        return sha256("\n".join(sorted(canonical(e) for e in self.entries.values())))[:12] if self.entries else "none"

    def save(self) -> None:
        if not self.dirty or self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("".join(json.dumps(e, sort_keys=True, default=str) + "\n"
                                     for _, e in sorted(self.entries.items())))
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
                cas.misses.append(f"{tool}({canonical(args)[:200]})")
                if cas.on_miss != "live":
                    raise ToolReplayMiss(f"no recording for {tool}({canonical(args)[:200]}); "
                                         f"re-record with `evalops eval --tool-mode record`")
            return key, None, cas

        def replay(hit):
            if hit.get("error"):
                raise RuntimeError(hit["error"])
            return hit["response"]

        def record(cas, key, args, response=None, error=None):
            if cas is not None and cas.mode == "record":
                with cas.lock:
                    cas.entries[key] = {"key": key, "tool": tool, "args": args, "response": response,
                                        "error": error}
                    cas.dirty = True

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapper(*a, **kw):
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
        else:
            @functools.wraps(fn)
            def wrapper(*a, **kw):
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
        wrapper.evalops_tool = tool
        return wrapper
    return deco
