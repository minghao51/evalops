"""Judge specs: the pinned, recorded identity of one LLM judge (CONTRACT §6; WP3b.1).

Before this module a "judge" was an anonymous callable plus a free-form ``judge_meta()`` dict, so
nothing stopped a project from shipping a judge whose model was ``latest``, or no model at all.
A :class:`JudgeSpec` is the value object that makes an **unpinned judge unrepresentable**: it can
only be built through :meth:`JudgeSpec.from_rule` / :func:`specs`, and every failure is
``EvalOpsError(EXIT_USAGE)`` naming the dotted config path (``judges.<name>.<key>``) a user has to
edit.

What a rule declares (the discriminator, packet A2)
--------------------------------------------------
``judges.<name>`` has two jobs, and only one of them declares a judge:

* a **spec** -- at least one of :data:`SPEC_KEYS` (``rubric``, ``scale``, ``prompt_version``,
  ``sampling``) is present. The spec is validated at load time, so ``plan``/``eval``/``calibrate``
  reject it before a cost estimate or a ledger row exists.
* **calibration thresholds only** -- ``min_agreement`` / ``min_kappa`` / ``max_mae`` / ``label`` /
  ``threshold`` (and/or ``model``). That is the shipped ``evalops.yaml`` shape and it declares *no*
  spec: :func:`specs` yields ``None`` for it, :func:`load` omits it, and behaviour is byte-identical
  to before. Those names are reported under ``payload()["unpinned"]``: the WP3b.5 worklist, surfaced
  early instead of inferred here.

A rule that names only ``model`` is deliberately *not* a spec either (packet A2 names it), and it is
not on the ``unpinned`` worklist either (A4 defines that list as the rules that carry calibration
thresholds): a lone pin with no rubric is a no-op key today and is the pin WP3b.5 validates for
*every* judge. Neither list silently pretends it was validated.

Two numeric validation layers, both exit 2
------------------------------------------
A judge number that is a bool, non-finite (``.inf``/``.nan``) or too large for a float
(``10**400``) is rejected by whichever layer owns the value, and neither layer may ever render
``Infinity``/``NaN`` in an envelope: the value is exit 2 before it is serialized.

* the **config model** (``config.models.JudgeNumber``) rejects it while validating ``evalops.yaml``
  and names the full model key -- ``judges.<name>.scale.<min|max>``, ``judges.<name>.threshold``,
  ``judges.<name>.sampling.temperature``;
* :meth:`JudgeSpec.validate` rejects it for direct API use (a caller builds ``Scale`` / ``Sampling``
  without YAML) and names the owning field -- ``judges.<name>.scale``, ``judges.<name>.threshold``,
  ``judges.<name>.sampling.temperature``.

For the same bad value the model path is therefore *more* specific (``.scale.max``) than the spec
path (``.scale``); a test that only checks a substring cannot tell the two layers apart.

Which executor runs the judge (packet A1/A2, WP3b.3)
---------------------------------------------------
``judges.<name>.judge`` selects *how* a declared spec is executed, from a closed set:

* absent or ``null`` or ``"native"`` -- the evaluator backend's own judge (the WP3b.2 adapter
  registry in :mod:`evalops.scorers.native`, selected by the ``evaluator`` plugin name);
* ``"litellm"`` -- the evalops-owned rubric fallback (:mod:`evalops.scorers.litellm_judge`),
  which is evaluator-independent.

``judge`` is **not** one of :data:`SPEC_KEYS`: it is an executor choice, not a declaration. A rule
carrying only thresholds plus ``judge: litellm`` still declares no spec and stays a no-op key;
only a *declared* spec can select an executor. An unknown value is exit 2 at
``judges.<name>.judge`` (the config model at the exact key, :meth:`JudgeSpec.validate` at the dotted
path), never a silent default.

Global "every judge must be pinned" enforcement is **WP3b.5** (gate refusal, resolved-model
recording per verdict); this module only makes a *declared* spec validatable and load-rejected.

No vendor import lives here (decision D4): a model id is recorded, never resolved.
"""

from __future__ import annotations

import dataclasses
import math
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, NoReturn, TextIO, cast

from .util import EXIT_USAGE, EvalOpsError, sha256

#: The scale kinds a spec may declare (packet A1/A3). ``binary`` is fixed to ``(0.0, 1.0)``.
SCALE_KINDS: tuple[str, ...] = ("binary", "ordinal", "continuous")

#: The keys that make a ``judges.<name>`` rule *declare* a spec (packet A2).
#: ``judge`` is deliberately absent: it selects an executor and never declares a spec (WP3b.3 A1).
SPEC_KEYS: tuple[str, ...] = ("rubric", "scale", "prompt_version", "sampling")

#: The closed set of executors a *declared* spec may name (packet A1). ``native`` is the default
#: and covers an absent/``null`` key; ``litellm`` is the evalops rubric fallback.
JUDGE_EXECUTORS: tuple[str, ...] = ("native", "litellm")

#: The calibration thresholds ``calibrate`` reads; they never declare a spec on their own.
THRESHOLD_KEYS: tuple[str, ...] = ("min_agreement", "min_kappa", "max_mae", "label", "threshold")

#: Every key a ``judges.<name>`` rule may carry (the config model is the enforcing surface).
RULE_KEYS: tuple[str, ...] = (*THRESHOLD_KEYS, "model", "judge", *SPEC_KEYS)

#: Alias / default forms that are **not** a pin (CONTRACT §6). Compared case-insensitively.
UNPINNED_ALIASES: frozenset[str] = frozenset({"default", "latest", "auto", "project-default"})

_HASH_CHARS = 12


def _fail(path: str, message: str) -> NoReturn:
    """Raise the load-time rejection: exit 2, naming the dotted config path it is about."""
    raise EvalOpsError(f"{path}: {message}", EXIT_USAGE, {"errors": [{"path": path, "message": message}]})


def is_pinned_model(model: Any) -> bool:
    """True iff ``model`` is a concrete pin rather than an alias or a default (CONTRACT §6).

    The check is deliberately vendor-agnostic: evalops *records* the resolved model, it does not
    resolve it, so any other non-empty string is accepted. A pin may not be
    ``default``/``latest``/``auto``/``project-default``, a ``*-latest`` alias (``gpt-latest``),
    a ``default/<model>`` default or a ``<vendor>/<project>/default`` server-side default.
    """
    if not isinstance(model, str):
        return False
    value = model.strip().lower()
    if not value or value in UNPINNED_ALIASES:
        return False
    if value.endswith("-latest"):
        return False
    if value.startswith("default/"):
        return False
    return not (value.endswith("/default") and value.count("/") >= 2)


def rubric_hash(rubric: str) -> str:
    """The stable 12-hex identity of a rubric (the text itself is never emitted)."""
    return sha256(rubric)[:_HASH_CHARS]


@dataclass(frozen=True)
class Scale:
    """The score range a judge emits. ``binary`` is exactly ``(0.0, 1.0)``."""

    max: float
    min: float = 0.0
    kind: str = "binary"


@dataclass(frozen=True)
class Sampling:
    """How the judge is called. The defaults are the reproducible ones (temperature 0)."""

    temperature: float = 0.0
    max_tokens: int = 512
    seed: int | None = None


@dataclass(frozen=True)
class JudgeSpec:
    """One judge, pinned: rubric + concrete model + prompt version + scale + sampling.

    Build it through :meth:`from_rule` (or :func:`specs`/:func:`load`), which validate. The
    dataclass constructor cannot intercept an argument, so :meth:`validate` is public as well: it
    is the one rule set, and the classmethods are thin callers of it.
    """

    name: str
    rubric: str
    model: str
    prompt_version: str
    #: A spec without an explicit scale is a binary judge over ``0..1`` (``Scale`` needs ``max``).
    scale: Scale = field(default_factory=lambda: Scale(max=1.0))
    sampling: Sampling = field(default_factory=Sampling)
    #: The executor: ``native`` (the evaluator's own judge, the default) or ``litellm`` (fallback).
    judge: str = "native"

    @classmethod
    def from_rule(cls, name: str, rule: Mapping[str, Any]) -> JudgeSpec:
        """Build and validate one ``judges.<name>`` rule that *declares* a spec (packet A2)."""
        spec = cls(
            name=name,
            # The three identity fields are typed as `str` but validate() rejects a missing/blank/
            # non-string value with the dotted path, so a raw rule value is passed through as-is.
            rubric=cast(str, rule.get("rubric")),
            model=cast(str, rule.get("model")),
            prompt_version=cast(str, rule.get("prompt_version")),
            scale=_scale_from(name, rule.get("scale")),
            sampling=_sampling_from(name, rule.get("sampling")),
            # absent and an explicit ``null`` both mean the evaluator's native adapter (A1)
            judge=_executor_from(rule.get("judge")),
        )
        spec.validate(threshold=_as_float(rule.get("threshold")))
        return spec

    def validate(self, *, threshold: float | None = None) -> None:
        """Reject an invalid spec with exit 2 naming ``judges.<name>.<key>`` (packet A3).

        ``threshold`` is the rule's binarization cut-off: it is not part of the spec's identity (it
        is a calibration setting that ``calibrate`` reads), so it is passed in when a caller has the
        rule and checked against the scale here.
        """
        path = self.config_path

        if not isinstance(self.rubric, str) or not self.rubric.strip():
            _fail(f"{path}.rubric", "a judge spec needs a non-empty rubric")
        if not is_pinned_model(self.model):
            _fail(
                f"{path}.model",
                f"{self.model!r} is not a concrete model pin: no empty value, no alias "
                f"(latest, *-latest, auto, default, project-default, default/*, */*/default) "
                f"and no default is accepted (CONTRACT §6)",
            )
        if not isinstance(self.prompt_version, str) or not self.prompt_version.strip():
            _fail(f"{path}.prompt_version", "a judge spec needs a non-empty prompt_version")
        if self.prompt_version.strip().lower() == "latest":
            _fail(f"{path}.prompt_version", "'latest' is not a prompt version pin; name the version")

        if self.judge not in JUDGE_EXECUTORS:
            _fail(
                f"{path}.judge",
                f"unknown judge executor {_show(self.judge)}; use one of {list(JUDGE_EXECUTORS)} "
                "(omit the key for the evaluator's native judge)",
            )

        self._validate_scale(threshold)
        self._validate_sampling()

    def _validate_scale(self, threshold: Any) -> None:
        path = f"{self.config_path}.scale"
        scale = self.scale
        if scale.kind not in SCALE_KINDS:
            _fail(f"{path}.kind", f"unknown scale kind {scale.kind!r}; use one of {list(SCALE_KINDS)}")
        # A non-finite bound (``inf``/``-inf``/``NaN``, or an int too large for a float) is exit 2
        # here, at the scale's own dotted path: ``Infinity`` is not valid JSON, so it must never
        # reach the CLI envelope (``evalops judges --json``) in the first place.
        low, high = _number(scale.min, path), _number(scale.max, path)
        if low is None or high is None:
            _fail(path, f"scale bounds must be numbers, got min={_show(scale.min)} max={_show(scale.max)}")
        if not low < high:
            _fail(path, f"scale min ({_show(scale.min)}) must be below max ({_show(scale.max)})")
        if scale.kind == "binary" and (low, high) != (0.0, 1.0):
            _fail(path, f"a binary scale is exactly 0.0..1.0, got {_show(scale.min)}..{_show(scale.max)}")
        if threshold is not None:
            cut_off = _number(threshold, f"{self.config_path}.threshold")
            if cut_off is None:
                _fail(f"{self.config_path}.threshold", f"threshold must be a number, got {_show(threshold)}")
            if not low <= cut_off <= high:
                _fail(
                    f"{self.config_path}.threshold",
                    f"threshold {cut_off} lies outside the scale {_show(scale.min)}..{_show(scale.max)}",
                )

    def _validate_sampling(self) -> None:
        path = f"{self.config_path}.sampling"
        sampling = self.sampling
        temperature = _number(sampling.temperature, f"{path}.temperature")
        if temperature is None or not 0.0 <= temperature <= 2.0:
            _fail(f"{path}.temperature", f"temperature must be in [0, 2], got {_show(sampling.temperature)}")
        tokens = sampling.max_tokens
        if not _integer(tokens) or tokens <= 0:
            _fail(f"{path}.max_tokens", f"max_tokens must be a positive integer, got {tokens!r}")
        if sampling.seed is not None and not _integer(sampling.seed):
            _fail(f"{path}.seed", f"seed must be an integer or null, got {sampling.seed!r}")

    @property
    def config_path(self) -> str:
        """The dotted ``evalops.yaml`` path a rejection names (and a reviewer greps for)."""
        return f"judges.{self.name}"

    def payload(self) -> dict[str, Any]:
        """The JSON-safe surface of one spec: identity, never the rubric text (packet A4)."""
        return {
            "name": self.name,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "scale": dataclasses.asdict(self.scale),
            "sampling": dataclasses.asdict(self.sampling),
            "judge": self.judge,
            "rubric_chars": len(self.rubric),
            "rubric_hash": rubric_hash(self.rubric),
        }


def _number(value: Any, path: str) -> float | None:
    """The finite ``float(value)``, or ``None`` when ``value`` is not a number at all.

    Every numeric coercion in spec validation goes through here (scale bounds, threshold,
    temperature), so no caller can leak a raw ``TypeError``/``ValueError``/``OverflowError`` -- a
    huge integer literal (``10**400``) becomes exit 2, not a traceback. A non-finite result is
    rejected at the caller's dotted ``path`` with the same exit 2: ``inf``/``NaN`` serialize as the
    non-JSON tokens ``Infinity``/``NaN``, so they must never reach the envelope. Bools are not
    numbers here (JSON ``true`` is not a scale bound).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        number = math.nan  # one rejection below, with the same message and dotted path
    if not math.isfinite(number):
        _fail(path, f"must be a finite number, got {_show(value)}")
    return number


def _show(value: Any) -> str:
    """A safe ``repr`` for an error message -- ``repr(10**10000)`` itself raises ``ValueError``."""
    try:
        return repr(value)
    except (TypeError, ValueError, OverflowError):  # pragma: no cover - int digit-limit guard
        return f"<unrepresentable {type(value).__name__}>"


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _as_float(value: Any) -> Any:
    """The ``float`` form of a numeric scalar, or ``value`` unchanged when it is not one.

    Construction-time normalization only: a raw YAML integer (``min: 0``) bypasses pydantic's
    float coercion because the runtime reads the merged dict, so the value object would otherwise
    carry ``0`` while the model view says ``0.0``. Every *validated* numeric bound goes through
    here, making the spec surface type-stable regardless of the YAML scalar form. An invalid bound
    (bool, string, ``inf``, an int too large for a float) is left untouched so ``validate()``
    rejects it at the dotted path rather than this helper raising a constructor-time error.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return value


def _executor_from(raw: Any) -> Any:
    """The declared executor, with an absent/``null`` key meaning ``native`` (packet A1).

    Only the two null-ish config forms are normalized here; anything else is passed through so
    :meth:`JudgeSpec.validate` rejects it at ``judges.<name>.judge`` (never a silent default).
    """
    return "native" if raw is None else raw


def _scale_from(name: str, raw: Any) -> Scale:
    """Build a :class:`Scale` from the raw (already pydantic-checked) ``judges.<name>.scale`` block."""
    if raw is None:
        return Scale(max=1.0)
    if isinstance(raw, Scale):
        return raw
    if not isinstance(raw, Mapping):  # pragma: no cover - the config model rejects a non-mapping
        _fail(f"judges.{name}.scale", f"a scale must be a mapping, got {type(raw).__name__}")
    return Scale(
        max=_as_float(raw.get("max", 1.0)),
        min=_as_float(raw.get("min", 0.0)),
        kind=raw.get("kind", "binary"),
    )


def _sampling_from(name: str, raw: Any) -> Sampling:
    """Build a :class:`Sampling` from the raw ``judges.<name>.sampling`` block (model defaults)."""
    if raw is None:
        return Sampling()
    if isinstance(raw, Sampling):
        return raw
    if not isinstance(raw, Mapping):  # pragma: no cover - the config model rejects a non-mapping
        _fail(f"judges.{name}.sampling", f"sampling must be a mapping, got {type(raw).__name__}")
    return Sampling(
        temperature=_as_float(raw.get("temperature", 0.0)),
        max_tokens=raw.get("max_tokens", 512),
        seed=raw.get("seed"),
    )


def declares_spec(rule: Mapping[str, Any]) -> bool:
    """True iff the rule declares a spec: at least one of :data:`SPEC_KEYS` is present.

    Key *presence* is the declarative act, so an explicit ``rubric: null`` is a declared spec with an
    empty rubric (exit 2 naming ``judges.<name>.rubric``) rather than a silently ignored judge.
    """
    return any(key in rule for key in SPEC_KEYS)


def carries_threshold(rule: Mapping[str, Any]) -> bool:
    """True iff the rule carries a calibration threshold but no spec -- the 3b.5 worklist."""
    return any(key in rule for key in THRESHOLD_KEYS)


def specs(cfg: Any) -> dict[str, JudgeSpec | None]:
    """One entry per ``judges.<name>`` rule: a validated spec, or ``None`` for threshold-only rules.

    Fails fast on the first invalid declared spec in sorted name order (packet A4), so a config with
    two broken specs always reports the same one.
    """
    rules: Mapping[str, Any] = cfg.section("judges") or {}
    out: dict[str, JudgeSpec | None] = {}
    for name in sorted(rules):
        rule = rules[name] or {}
        out[name] = JudgeSpec.from_rule(name, rule) if declares_spec(rule) else None
    return out


def load(cfg: Any) -> dict[str, JudgeSpec]:
    """The declared (and validated) specs only; ``{}`` when no rule declares one.

    This is the load-time rejection point: ``plan``, ``evaluate`` and ``calibrate`` call it before
    any cost estimate, budget reservation or ledger write (packet C1). It is a pure read.
    """
    return {name: spec for name, spec in specs(cfg).items() if spec is not None}


def payload(cfg: Any) -> dict[str, Any]:
    """The deterministic ``--json`` payload of ``evalops judges`` (packet A4/D1).

    ``judges`` is sorted by name and carries a spec's identity plus ``rubric_chars``/``rubric_hash``:
    the rubric text is never emitted (reviewers and CI logs get a stable identity, not prose).
    ``unpinned`` is the sorted list of rules that carry calibration thresholds but declare no spec.
    """
    declared = specs(cfg)
    rules: Mapping[str, Any] = cfg.section("judges") or {}
    return {
        "judges": [spec.payload() for spec in declared.values() if spec is not None],
        "unpinned": sorted(
            name for name, spec in declared.items() if spec is None and carries_threshold(rules.get(name) or {})
        ),
    }


def write_human(data: Mapping[str, Any], stream: TextIO | None = None) -> None:
    """Human-mode rendering of :func:`payload`: one line per spec plus one warning per unpinned rule."""
    out = stream if stream is not None else sys.stdout
    for spec in data["judges"]:
        scale = spec["scale"]
        out.write(
            f"{spec['name']}  {spec['model']}  {spec['prompt_version']}  "
            f"{scale['kind']}({scale['min']}..{scale['max']})  {spec['rubric_hash']}\n"
        )
    for name in data["unpinned"]:
        out.write(
            f"warning: judges.{name} has calibration thresholds but declares no spec (unpinned); "
            "add rubric, prompt_version, scale/sampling and a concrete model\n"
        )
    out.flush()
