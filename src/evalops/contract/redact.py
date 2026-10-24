"""Payload redaction: one hook in front of the three outbound sinks (CONTRACT §5.1).

Every payload evalops persists used to be written verbatim. This module owns the single redaction
hook, and it is applied at exactly three boundaries, always **after** identity hashing:

* ``run._put_artifact`` -- ``evalops/cost_estimate.json``, the two locks, ``evalops/rows.jsonl``;
  the bodies are buffered and written only after every one of them is verified, and
  ``evalops/manifest.json`` therefore hashes the *stored* (redacted) bytes;
* ``AgentTrace.to_dict`` -- a redacted deep copy; the in-memory trace (scorers, ``render``, replay)
  is never touched;
* ``Cassette.save`` -- a redacted deep copy of the entries; ``Cassette.entries`` and
  ``Cassette.fingerprint()`` are unchanged by ``save()``.

Rule kinds are applied **in this order**, each over the whole payload before the next one starts:

a. ``fields``  -- dotted-path globs; the matched value becomes ``[REDACTED:<glob>]``.
b. ``patterns`` -- ``{name, regex, replace?}``; a regex substitution on every string leaf; the
   default replacement is ``[REDACTED:<name>]``.
c. ``hash``    -- dotted-path globs; the matched value becomes ``sha256:<first 12 hex of
   sha256(canonical(value))>`` (``evalops.util.canonical``), so it is deterministic across runs.
d. ``hook``    -- ``module:attribute``, same import semantics as ``Config.hook`` (repo-root
   importable), called as ``(path, value) -> Any | None`` for **every** node in document order;
   ``None`` (or an equal value) leaves the node unchanged and the return value replaces it.

Path syntax (``fields``, ``hash``, and the ``path`` handed to a hook):

* ``inputs.password`` -- dictionary keys, dot-separated;
* ``*`` -- exactly one segment (one dictionary key or one list element);
* ``**`` -- any depth, including zero segments;
* ``[]`` -- every list element (``spans[].inputs.authorization`` is key ``spans``, then each list
  element, then ``inputs.authorization``). A list element is rendered as ``[]`` in a hook's path, so
  the vocabulary a hook sees is the vocabulary the config uses.

**Idempotence is part of the contract**: ``redact(redact(x)) == redact(x)`` for rules a, b and c,
always. That is achieved by self-recognising markers -- a value already shaped like
``[REDACTED:<rule>]`` is never re-replaced, and a value already shaped like ``sha256:<12 hex>`` is
never re-hashed, whatever rule matched it. A **hook must be idempotent itself**: it has to return
``None`` (or the input) for the value it produced, otherwise ``mode: strict`` refuses to persist the
payload (exit 5) because the re-scan still matches.

Modes (``redact.mode`` in ``evalops.yaml``, overridden by ``EVALOPS_REDACT``):

* ``off`` (the default) -- **nothing is redacted**; every byte is what evalops wrote before this
  module existed. Rules may be configured while the feature is off; ``--check`` reports nothing.
* ``strict`` -- redact, then re-scan the produced payload with the same rules. Anything that still
  matches (a hook returning its input, a ``replace`` that the ``regex`` matches again, ...) aborts
  the write with **exit 5** before a single byte is persisted. It is a real write-path refusal, not
  a warning.

``evalops redact`` (with an optional, no-op ``--check``) is the dry run: it reads ledger artifacts of
a run group and/or ``--file`` JSON/JSONL documents, renders a unified diff of the values that *would*
change (human mode) or a capped count/path report (``--json``), and **never writes** -- no ledger
row, no file, no ``last_eval.json``. It exits **0 even when it finds differences**: the 0-7 exit
matrix is frozen (CONTRACT §1) and "found a secret" is not a gate outcome.

The active rule set is config-scoped (a `ContextVar` plus a thread fallback, see `use`/`activate`),
and every entry point normalizes a payload with `normalize_json` first, so the tree that is scanned
is bit-for-bit the tree the sinks serialize.
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import dataclasses
import difflib
import json
import os
import pathlib
import re
import sys
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, cast

from ..util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError, canonical, sha256

MODE_ENV = "EVALOPS_REDACT"
MODES: tuple[str, ...] = ("strict", "off")
MARKER = "[REDACTED:{rule}]"
HASH_PREFIX = "sha256:"
#: A `--check` report lists at most this many changed paths per sink (diffs can be huge).
PATH_CAP = 50

_MARKER_RE = re.compile(r"^\[REDACTED:.*\]$")
_HASHED_RE = re.compile(r"^sha256:[0-9a-f]{12}$")
_TOKEN_RE = re.compile(r"\[\]|\*|[^\[\]*]+")

# Path tokens. `is` comparisons are used, so a key token can never be confused with a control token.
Token = tuple[str, str]
ANY: Token = ("**", "")
STAR: Token = ("*", "")
LIST: Token = ("[]", "")
Transform = Callable[[str, Any], tuple[Any, bool]]

_UNPARSED = object()


class _Rules:
    """Marker base class so `Rules` and a plain config dict are interchangeable inputs."""


@dataclasses.dataclass(frozen=True)
class Pattern:
    """One `patterns:` rule: `regex` is substituted on every string leaf with `replace`."""

    name: str
    regex: str
    replace: str | None = None

    @property
    def replacement(self) -> str:
        return self.replace if self.replace is not None else MARKER.format(rule=self.name)


@dataclasses.dataclass(frozen=True)
class Rules:
    """The `redact:` section of `evalops.yaml`, as an immutable value."""

    mode: str = "off"
    fields: tuple[str, ...] = ()
    hash: tuple[str, ...] = ()
    patterns: tuple[Pattern, ...] = ()
    hook: str | None = None

    @property
    def active(self) -> bool:
        """True when at least one rule is configured (an empty rule set is a no-op)."""
        return bool(self.fields or self.hash or self.patterns or self.hook)

    def as_dict(self) -> dict[str, Any]:
        """The rules as a plain dict (what `canonical()` hashes for the rules identity)."""
        return {
            "mode": self.mode,
            "fields": list(self.fields),
            "hash": list(self.hash),
            "patterns": [
                {"name": pattern.name, "regex": pattern.regex, "replace": pattern.replace} for pattern in self.patterns
            ],
            "hook": self.hook,
        }

    def identity(self) -> str:
        """`sha256(canonical(rules))[:12]`: the value recorded in a cassette's `_meta`."""
        return sha256(canonical({key: value for key, value in self.as_dict().items() if key != "mode"}))[:12]


@dataclasses.dataclass(frozen=True)
class Change:
    """One redacted location: the dotted path, the value before and the value after."""

    path: str
    before: Any
    after: Any


# ---------------------------------------------------------------------------
# rules plumbing
# ---------------------------------------------------------------------------
def _usage(message: str) -> EvalOpsError:
    return EvalOpsError(message, EXIT_USAGE)


def _check_mode(mode: str, label: str = "redact.mode") -> str:
    if mode not in MODES:
        raise _usage(f"{label} must be one of {', '.join(MODES)} (got {mode!r})")
    return mode


def resolve_mode(config_mode: str) -> str:
    """`EVALOPS_REDACT` (strict|off) overrides `redact.mode`; any other value is exit 2.

    An unset *or empty* variable means "use the configured mode": empty is what templated CI
    environments produce for an unset optional variable, and it cannot be a deliberate request for
    one of the two modes.
    """
    override = os.environ.get(MODE_ENV, "")
    if override == "":
        return _check_mode(config_mode)
    if override not in MODES:
        raise EvalOpsError(
            f"{MODE_ENV}={override!r} is not a legal redaction mode; use one of: {', '.join(MODES)}",
            EXIT_USAGE,
            {"variable": MODE_ENV, "value": override, "modes": list(MODES)},
        )
    return override


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list | tuple):
        raise _usage(f"{label} must be a list of dotted paths (got {type(value).__name__})")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise _usage(f"{label} must be a list of dotted paths (got {item!r})")
        out.append(item)
    return tuple(out)


def coerce_rules(rules: Rules | Mapping[str, Any]) -> Rules:
    """Normalize a `Rules` or the raw `redact:` mapping into `Rules` (unknown keys are exit 2)."""
    if isinstance(rules, Rules):
        return rules
    if not isinstance(rules, Mapping):
        raise _usage(f"redact rules must be a mapping (got {type(rules).__name__})")
    unknown = sorted(set(rules) - {"mode", "fields", "hash", "patterns", "hook"})
    if unknown:
        raise _usage("unknown redact key(s): " + ", ".join(f"redact.{key}" for key in unknown))
    raw_patterns = rules.get("patterns") or []
    if not isinstance(raw_patterns, list | tuple):
        raise _usage("redact.patterns must be a list of {name, regex, replace}")
    patterns: list[Pattern] = []
    for raw in raw_patterns:
        if isinstance(raw, Pattern):
            patterns.append(raw)
            continue
        if not isinstance(raw, Mapping):
            raise _usage(f"redact.patterns entries must be mappings (got {raw!r})")
        extra = sorted(set(raw) - {"name", "regex", "replace"})
        if extra:
            raise _usage("unknown redact.patterns key(s): " + ", ".join(f"redact.patterns.{key}" for key in extra))
        name, regex = raw.get("name"), raw.get("regex")
        if not isinstance(name, str) or not isinstance(regex, str):
            raise _usage("each redact.patterns entry needs a string 'name' and 'regex'")
        replace = raw.get("replace")
        if replace is not None and not isinstance(replace, str):
            raise _usage(f"redact.patterns '{name}': 'replace' must be a string")
        patterns.append(Pattern(name, regex, replace))
    hook = rules.get("hook")
    if hook is not None and not isinstance(hook, str):
        raise _usage("redact.hook must be a 'module:attribute' string")
    return Rules(
        mode=str(rules.get("mode") or "off"),
        fields=_string_tuple(rules.get("fields"), "redact.fields"),
        hash=_string_tuple(rules.get("hash"), "redact.hash"),
        patterns=tuple(patterns),
        hook=hook,
    )


def _tokens(path: str, label: str = "redact path") -> tuple[Token, ...]:
    """Parse a dotted-path glob into tokens; an empty or malformed segment is exit 2."""
    if not isinstance(path, str) or not path:
        raise _usage(f"{label}: a dotted path must be a non-empty string (got {path!r})")
    out: list[Token] = []
    for segment in path.split("."):
        if segment == "**":
            out.append(ANY)
            continue
        parts = _TOKEN_RE.findall(segment)
        if not parts or "".join(parts) != segment:
            raise _usage(f"{label}: invalid path {path!r} (segment {segment!r})")
        for part in parts:
            if part == "[]":
                out.append(LIST)
            elif part == "*":
                out.append(STAR)
            else:
                out.append(("key", part))
    return tuple(out)


def _resolve_hook(spec: str | None, root: pathlib.Path | None) -> Callable[[str, Any], Any] | None:
    if not spec:
        return None
    if ":" not in spec:
        raise _usage(f"redact.hook must be 'module:attribute' (got {spec!r})")
    from ..config import import_attr

    return import_attr(spec, root if root is not None else pathlib.Path.cwd(), "redact.hook")


def _compile(pattern: Pattern) -> re.Pattern[str]:
    try:
        return re.compile(pattern.regex)
    except re.error as exc:
        raise _usage(f"redact.patterns '{pattern.name}': invalid regex: {exc}") from exc


# ---------------------------------------------------------------------------
# transforms: one per rule kind, an `apply` one and a `scan` (has-it-fired?) one
# ---------------------------------------------------------------------------
def _is_marker(value: Any) -> bool:
    return isinstance(value, str) and bool(_MARKER_RE.match(value))


def _is_hashed(value: Any) -> bool:
    return isinstance(value, str) and bool(_HASHED_RE.match(value))


def _same(left: Any, right: Any) -> bool:
    if left is right:
        return True
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001 - a value whose __eq__ raises is "not the same"
        return False


def _field_apply(glob: str) -> Transform:
    marker = MARKER.format(rule=glob)

    def apply(path: str, value: Any) -> tuple[Any, bool]:
        if _is_marker(value):  # self-recognising: an already-redacted value stays as it is
            return value, False
        return marker, True

    return apply


def _field_scan(glob: str) -> Transform:
    def scan(path: str, value: Any) -> tuple[Any, bool]:
        return value, not _is_marker(value)

    return scan


def _pattern_apply(pattern: Pattern, compiled: re.Pattern[str]) -> Transform:
    replacement = pattern.replacement

    def apply(path: str, value: Any) -> tuple[Any, bool]:
        if not isinstance(value, str) or _is_marker(value):
            return value, False
        # A callable replacement keeps backslashes in `replace` literal instead of escapes.
        new = compiled.sub(lambda _match: replacement, value)
        return (new, True) if new != value else (value, False)

    return apply


def _pattern_scan(compiled: re.Pattern[str]) -> Transform:
    def scan(path: str, value: Any) -> tuple[Any, bool]:
        if not isinstance(value, str) or _is_marker(value):
            return value, False
        return value, bool(compiled.search(value))

    return scan


def _hash_apply() -> Transform:
    def apply(path: str, value: Any) -> tuple[Any, bool]:
        if _is_marker(value) or _is_hashed(value):  # self-recognising: never hash a marker/hash
            return value, False
        return HASH_PREFIX + sha256(canonical(value))[:12], True

    return apply


def _hash_scan() -> Transform:
    def scan(path: str, value: Any) -> tuple[Any, bool]:
        return value, not _is_marker(value) and not _is_hashed(value)

    return scan


def _hook_apply(hook: Callable[[str, Any], Any]) -> Transform:
    def apply(path: str, value: Any) -> tuple[Any, bool]:
        try:
            new = hook(path, value)
        except EvalOpsError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken hook is a runtime failure, never a silent skip
            raise EvalOpsError(
                f"redact hook raised at {path or '<root>'}: {type(exc).__name__}: {exc}", EXIT_RUNTIME
            ) from exc
        if new is None or _same(new, value):
            return value, False
        return new, True

    return apply


def _hook_scan(hook: Callable[[str, Any], Any]) -> Transform:
    def scan(path: str, value: Any) -> tuple[Any, bool]:
        new = hook(path, value)
        return value, new is not None and not _same(new, value)

    return scan


# ---------------------------------------------------------------------------
# traversal
# ---------------------------------------------------------------------------
def render_path(path: Sequence[Any]) -> str:
    """Render a component path: `inputs.password`, `spans[].outputs.token` (root is `“”`)."""
    out = ""
    for part in path:
        if isinstance(part, int):
            out += "[]"
        else:
            out += ("." if out else "") + str(part)
    return out


def _children(value: Any) -> Iterable[tuple[Any, Any]]:
    if isinstance(value, dict):
        return list(value.items())
    if isinstance(value, list):
        return list(enumerate(value))
    return ()


def _walk_into(
    tokens: tuple[Token, ...], value: Any, path: tuple[Any, ...], fn: Transform, changes: list[Change]
) -> Any:
    """Apply `tokens` to every child of `value`, replacing each child with the result."""
    if isinstance(value, dict):
        for key in list(value):
            value[key] = _walk(tokens, value[key], (*path, key), fn, changes)
    elif isinstance(value, list):
        for index in range(len(value)):
            value[index] = _walk(tokens, value[index], (*path, index), fn, changes)
    return value


def _walk(tokens: tuple[Token, ...], value: Any, path: tuple[Any, ...], fn: Transform, changes: list[Change]) -> Any:
    """Apply `fn` to every value matched by `tokens`; returns the (possibly replaced) value."""
    if not tokens:
        rendered = render_path(path)
        new, changed = fn(rendered, value)
        if changed:
            changes.append(Change(rendered, value, new))
        return new
    head, rest = tokens[0], tokens[1:]
    if head is ANY:
        value = _walk(rest, value, path, fn, changes)  # zero segments
        return _walk_into(tokens, value, path, fn, changes)  # one or more segments
    if head is LIST:
        if isinstance(value, list):
            for index in range(len(value)):
                value[index] = _walk(rest, value[index], (*path, index), fn, changes)
        return value
    if head is STAR:
        return _walk_into(rest, value, path, fn, changes)
    key = head[1]
    if isinstance(value, dict) and key in value:
        value[key] = _walk(rest, value[key], (*path, key), fn, changes)
    return value


def _parse(text: Any) -> Any:
    """Parse JSON text; any other input (or malformed text) comes back as the `_UNPARSED` sentinel."""
    if not isinstance(text, str):
        return _UNPARSED
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return _UNPARSED


def normalize_json(value: Any) -> Any:
    """Round-trip a value through the *same* serialization every sink writes with.

    Sinks serialize with ``json.dumps(..., default=str)``, so a tuple becomes a list, a set becomes
    ``str(set)`` and a custom object becomes ``str(obj)``. Walking the raw Python object would step
    past exactly those values (``_children`` only knows ``dict``/``list``) while ``default=str``
    persisted their secret verbatim -- and a ``strict`` re-scan would miss it too. Normalizing first
    makes the scanned tree bit-for-bit the tree that is serialized. A root that JSON cannot
    round-trip at all (e.g. a tuple used as a dict key) degrades to ``str(value)``.
    """
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return str(value)


# ---------------------------------------------------------------------------
# the Redactor
# ---------------------------------------------------------------------------
class Redactor:
    """The compiled rule set. Build it once per process/config (`from_config`) and reuse it.

    `mode` resolves `EVALOPS_REDACT` when it is not passed explicitly, so an invalid value is
    reported (exit 2) wherever a redactor is built -- before a sink writes anything.
    """

    def __init__(
        self,
        rules: Rules | Mapping[str, Any],
        *,
        root: pathlib.Path | None = None,
        mode: str | None = None,
    ) -> None:
        self.rules = coerce_rules(rules)
        self.mode = _check_mode(mode) if mode is not None else resolve_mode(self.rules.mode)
        self._hook = _resolve_hook(self.rules.hook, root)
        stages: list[tuple[tuple[Token, ...], Transform, Transform]] = []
        for glob in self.rules.fields:
            stages.append((_tokens(glob, "redact.fields"), _field_apply(glob), _field_scan(glob)))
        for pattern in self.rules.patterns:
            compiled = _compile(pattern)
            stages.append(((ANY,), _pattern_apply(pattern, compiled), _pattern_scan(compiled)))
        for glob in self.rules.hash:
            stages.append((_tokens(glob, "redact.hash"), _hash_apply(), _hash_scan()))
        if self._hook is not None:
            stages.append(((ANY,), _hook_apply(self._hook), _hook_scan(self._hook)))
        self._stages = tuple(stages)

    # ---------- identity ----------
    def fingerprint(self) -> str:
        """`sha256(canonical(rules))[:12]`: recorded in a cassette's `_meta.redaction_rules`."""
        return self.rules.identity()

    @property
    def enabled(self) -> bool:
        """True when this redactor actually changes bytes (`mode: off` is always a no-op)."""
        return self.mode != "off" and self.rules.active

    # ---------- value API ----------
    def redact(self, value: Any) -> tuple[Any, list[Change]]:
        """Return `(redacted deep copy, changes)`; the input object is never mutated.

        When enabled, the payload is first normalized through `normalize_json`, so a tuple/set/
        dataclass/custom object is scanned as the value the sinks would actually serialize. When the
        redactor is inactive the value is a plain deep copy: `off` stays byte-compatible and nothing
        is re-serialized.
        """
        changes: list[Change] = []
        if not self.enabled:
            return copy.deepcopy(value), changes
        current = normalize_json(value)
        for tokens, apply_fn, _scan_fn in self._stages:
            current = _walk(tokens, current, (), apply_fn, changes)
        return current, changes

    def residual(self, value: Any) -> list[str]:
        """Paths that still match a rule -- the `mode: strict` re-scan of an already-redacted value.

        Normalized first, for the same reason as `redact`: a value that `json.dumps(default=str)`
        would persist as a secret must be refused even when it is not a native JSON container.
        """
        if not self.enabled:
            return []
        found: list[Change] = []
        normalized = normalize_json(value)
        for tokens, _apply_fn, scan_fn in self._stages:
            _walk(tokens, normalized, (), scan_fn, found)
        return list(dict.fromkeys(change.path for change in found))

    # ---------- text API ----------
    def redact_value(self, value: Any) -> tuple[Any, list[Change]]:
        return self.redact(value)

    def redact_json_text(self, text: str) -> tuple[str, list[Change]]:
        """Redact one JSON document; anything that is not parseable JSON comes back unchanged."""
        parsed = _parse(text)
        if parsed is _UNPARSED:
            return text, []
        redacted, changes = self.redact(parsed)
        if not changes:
            return text, []
        return json.dumps(redacted, sort_keys=True, default=str), changes

    def redact_jsonl_text(self, text: str) -> tuple[str, list[Change]]:
        """Redact line-delimited JSON; unchanged lines keep their exact bytes."""
        if not isinstance(text, str):
            return text, []
        changes: list[Change] = []
        out: list[str] = []
        for line in text.splitlines():
            parsed = _parse(line) if line.strip() else _UNPARSED
            if parsed is _UNPARSED:  # a blank/foreign line is never a payload shape error
                out.append(line)
                continue
            redacted, line_changes = self.redact(parsed)
            if line_changes:
                changes.extend(line_changes)
                out.append(json.dumps(redacted, default=str))
            else:
                out.append(line)
        if not changes:
            return text, []
        joined = "\n".join(out)
        return (joined + "\n" if text.endswith("\n") else joined), changes

    # ---------- write boundary ----------
    def residual_text(self, text: str, *, jsonl: bool = False) -> list[str]:
        """Paths that still match in already-redacted text (empty when the mode is `off`)."""
        if jsonl:
            paths: list[str] = []
            for line in text.splitlines():
                parsed = _parse(line)
                if parsed is not _UNPARSED:
                    paths.extend(self.residual(parsed))
            return list(dict.fromkeys(paths))
        parsed = _parse(text)
        return [] if parsed is _UNPARSED else self.residual(parsed)

    def enforce_text(self, text: str, source: str = "") -> str:
        """Redact an outbound payload; `strict` refuses to return text that still matches (exit 5).

        Text needs no `normalize_json`: every JSON/JSONL line is parsed with `json.loads` before the
        walk, so the tree scanned is already the JSON tree that will be re-serialized.
        """
        if not self.enabled:
            return text
        jsonl = source.endswith(".jsonl")
        redacted, _changes = self.redact_jsonl_text(text) if jsonl else self.redact_json_text(text)
        if self.mode != "strict":
            return redacted
        residual = self.residual_text(redacted, jsonl=jsonl)
        if residual:
            raise EvalOpsError(
                f"strict redaction: {source or 'the payload'} still matches {len(residual)} rule(s) "
                f"after redaction (e.g. {residual[0]}); nothing was written",
                EXIT_RUNTIME,
                {"source": source, "paths": residual[:PATH_CAP]},
                hint="a redaction hook must be idempotent and return None for already-redacted input",
            )
        return redacted


# ---------------------------------------------------------------------------
# process/config plumbing
# ---------------------------------------------------------------------------
_CACHE: dict[tuple[str, str, str], Redactor] = {}
#: The config-scoped binding. A ContextVar (not a module global) is what keeps two configs in one
#: process from sharing rules; `run.evaluate` and `cli.main` bind it with `use`/`activate`.
_ctx: contextvars.ContextVar[Redactor | None] = contextvars.ContextVar("evalops_redact", default=None)
#: Process-wide fallback: worker threads do not inherit a ContextVar (same shape as `tools._ctx`).
_active: Redactor | None = None
_FALLBACK: tuple[str, Redactor] | None = None


def from_config(cfg: Any) -> Redactor:
    """Build (once per config) the redactor for a loaded `Config`; exit 2 on a bad env value."""
    section = dict(cfg.section("redact") or {})
    root = pathlib.Path(getattr(cfg, "root", pathlib.Path.cwd()))
    mode = resolve_mode(str(section.get("mode") or "off"))
    key = (str(root), canonical(section), mode)
    cached = _CACHE.get(key)
    if cached is None:
        cached = Redactor(section, root=root, mode=mode)
        _CACHE[key] = cached
    return cached


def activate(cfg: Any) -> Redactor:
    """Install `cfg`'s redactor as the process default (the sinks that take no `cfg` use it).

    `cli.main` calls this once per command, right after a successful config load, so every sink of
    that command is bound to the config that command loaded. `run.evaluate` uses the scoped `use`
    instead, so a run restores whatever binding existed before it.
    """
    global _active
    redactor = from_config(cfg)
    _ctx.set(redactor)
    _active = redactor
    return redactor


@contextlib.contextmanager
def use(cfg: Any) -> Iterator[Redactor]:
    """Bind `cfg`'s redactor for the duration of the block, then restore the previous binding.

    Token based (`ContextVar.set`/`reset`) so nested or failing runs cannot leave another config's
    rules active; the module fallback is switched with it so worker threads see the same rules.
    """
    global _active
    redactor = from_config(cfg)
    token, previous = _ctx.set(redactor), _active
    _active = redactor
    try:
        yield redactor
    finally:
        _ctx.reset(token)
        _active = previous


def current() -> Redactor:
    """The active redactor: context binding, then thread fallback, then the project's `evalops.yaml`.

    `AgentTrace.to_dict` and `Cassette.save` have no `Config` parameter, so they ask here. The
    cwd-config fallback keeps a bare script (a sink used outside any command) on its project's rule
    set; an unreadable/unavailable config is a no-op redactor, because the command that owns the
    write reports its own config error and a sink must never invent a new failure on that path.
    """
    global _FALLBACK
    bound = _ctx.get() or _active
    if bound is not None:
        return bound
    cwd = str(pathlib.Path.cwd())
    if _FALLBACK is not None and _FALLBACK[0] == cwd:
        return _FALLBACK[1]
    redactor = _project_redactor()
    _FALLBACK = (cwd, redactor)
    return redactor


def _project_redactor() -> Redactor:
    try:
        from ..config import load

        return from_config(load())
    except Exception:  # noqa: BLE001 - see current(): a sink never invents a failure
        return Redactor({}, mode="off")


def reset() -> None:
    """Forget every binding and the per-cwd cache (tests only)."""
    global _active, _FALLBACK
    _ctx.set(None)
    _active = None
    _FALLBACK = None
    _CACHE.clear()


# ---------------------------------------------------------------------------
# module-level helpers: the three sinks use these (`redact_string` is the capture-time label
# helper of `tools`, applied before any sink exists -- not a fourth boundary)
# ---------------------------------------------------------------------------
def redact_value(value: Any) -> tuple[Any, list[Change]]:
    """Redact a JSON-like value with the process redactor."""
    return current().redact(value)


def redact_json_text(text: str) -> tuple[str, list[Change]]:
    """Redact one JSON document with the process redactor."""
    return current().redact_json_text(text)


def redact_jsonl_text(text: str) -> tuple[str, list[Change]]:
    """Redact line-delimited JSON with the process redactor."""
    return current().redact_jsonl_text(text)


def redact_string(text: str) -> str:
    """Redact one flat string (a diagnostic label) with the process redactor.

    Used at *capture* time by `tools` for a replay-miss label, before any sink exists, so it is not
    a fourth boundary. A string root is matched by `patterns` (a regex substitution on the root
    value) and by `hook` (called with the empty path); `fields`/`hash` name dotted paths inside a
    document and therefore cannot match a flat string -- only `patterns` and `hook` apply here.
    `strict` is enforced by the caller with `Redactor.residual`, never silently here.
    """
    redacted, _changes = current().redact(text)
    return redacted if isinstance(redacted, str) else str(redacted)


def redact_document(document: dict[str, Any]) -> dict[str, Any]:
    """The `AgentTrace.to_dict` boundary: a redacted, normalized deep copy; the input is untouched."""
    redacted, _changes = current().redact(normalize_json(document))
    return cast("dict[str, Any]", redacted)


def redact_text(text: str, source: str = "") -> str:
    """The artifact boundary: redact JSON/JSONL text for `source`, enforcing `strict`."""
    return current().enforce_text(text, source)


def enforce_artifact(cfg: Any, source: str, text: str) -> str:
    """Redact one `run._put_artifact` payload with `cfg`'s rules (no process-wide side effect)."""
    return from_config(cfg).enforce_text(text, source)


# ---------------------------------------------------------------------------
# `evalops redact --check`: a dry run that never writes
# ---------------------------------------------------------------------------
def value_diff(label: str, change: Change) -> list[str]:
    """A line-oriented unified diff of one would-be change (human mode of `--check`)."""

    def lines(value: Any) -> list[str]:
        text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, default=str)
        rendered = text.splitlines() or [""]
        return [line + "\n" for line in rendered]

    return list(
        difflib.unified_diff(
            lines(change.before),
            lines(change.after),
            fromfile=f"{label}:{change.path}",
            tofile=f"{label}:{change.path} (redacted)",
        )
    )


_MANIFEST_ARTIFACT = "evalops/manifest.json"
_KNOWN_ARTIFACTS = (
    "evalops/rows.jsonl",
    "evalops/cost_estimate.json",
    "evalops/prompts.lock.json",
    "evalops/datasets.lock.json",
)


def _artifact_names(ledger: Any, run_id: str) -> list[str]:
    """Artifact names of one run: from `evalops/manifest.json`, else the four known artifacts."""
    try:
        document = json.loads(ledger.get_artifact(run_id, _MANIFEST_ARTIFACT))
    except Exception:  # noqa: BLE001 - no manifest: fall back to the artifacts evalops always writes
        return list(_KNOWN_ARTIFACTS)
    listed = document.get("artifacts") if isinstance(document, dict) else None
    return sorted(listed) if isinstance(listed, dict) and listed else list(_KNOWN_ARTIFACTS)


def _registry(cfg: Any) -> Any:
    """`Registry(cfg)`, cast to a typed callable: `registry` is a legacy, non-strict module."""
    from ..registry import Registry

    return cast("Callable[[Any], Any]", Registry)(cfg)


def _ledger_sinks(cfg: Any, group: str | None) -> list[tuple[str, str, bool]]:
    from .. import locks
    from ..ports import Ledger

    resolved = group
    if not resolved:
        last = locks.scratch(cfg, "last_eval.json")
        if not last.exists():
            raise EvalOpsError(
                "no --group and no .evalops/out/last_eval.json: nothing to check",
                EXIT_USAGE,
                hint="pass --group G (or --file PATH) explicitly",
            )
        from . import formats

        resolved = str(formats.read_versioned(last).get("group") or "")
        if not resolved:
            raise EvalOpsError(f"{last}: no run group recorded; pass --group", EXIT_USAGE)
    ledger = cast("Ledger", _registry(cfg).get("ledger"))
    runs = ledger.find_runs(tags={"evalops.group": resolved}, limit=1)
    if not runs:
        raise EvalOpsError(f"no runs for group {resolved}", EXIT_USAGE)
    run_id = runs[0].run_id
    sinks: list[tuple[str, str, bool]] = []
    for name in _artifact_names(ledger, run_id):
        try:
            text = ledger.get_artifact(run_id, name)
        except Exception:  # noqa: BLE001 - an artifact this run did not write is not a sink
            continue
        sinks.append((f"ledger:{run_id}:{name}", text, name.endswith(".jsonl")))
    return sinks


def _file_sink(path: str) -> tuple[str, str, bool]:
    target = pathlib.Path(path)
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise _usage(f"--file {path}: cannot read: {exc}") from exc
    suffix = target.suffix.lower()
    if suffix == ".jsonl":
        return f"file:{path}", text, True
    if suffix == ".json":
        return f"file:{path}", text, False
    raise _usage(f"--file {path}: expected a .jsonl (cassette/rows) or a .json document")


def run_check(
    cfg: Any,
    group: str | None = None,
    files: Iterable[str] = (),
    *,
    as_json: bool = False,
    stream: Any = None,
) -> dict[str, Any]:
    """`evalops redact` (optionally `--check`): report what the rules would change; never write, exit 0."""
    redactor = from_config(cfg)
    paths = list(files)
    sinks: list[tuple[str, str, bool]] = []
    if group is not None or not paths:  # no --file -> the ledger group (default: last_eval.json)
        sinks.extend(_ledger_sinks(cfg, group))
    sinks.extend(_file_sink(path) for path in paths)
    out = stream if stream is not None else sys.stdout
    report: list[dict[str, Any]] = []
    total = 0
    for label, text, jsonl in sinks:
        _redacted, changes = redactor.redact_jsonl_text(text) if jsonl else redactor.redact_json_text(text)
        total += len(changes)
        paths = [change.path for change in changes]
        report.append(
            {
                "sink": label,
                "changes": len(changes),
                "paths": paths[:PATH_CAP],
                "truncated": len(paths) > PATH_CAP,
            }
        )
        if as_json:
            continue  # diffs are large: --json carries counts + capped paths only
        for change in changes[:PATH_CAP]:
            for line in value_diff(label, change):
                out.write(line)
    summary = f"redact check ({redactor.mode}): {total} change(s) in {len(report)} sink(s)"
    return {"summary": summary, "mode": redactor.mode, "changed": total, "sinks": report}
