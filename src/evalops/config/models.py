"""Typed models for ``evalops.yaml`` (pydantic v2; decision D4).

The models mirror the shipped configuration **exactly**: every key the CLI reads is a field, and
``extra="forbid"`` everywhere means a typo is an error instead of a silently ignored key. The
models only validate; the runtime view (``Config.data``) stays the plain merged ``dict`` so
plugins and evaluators keep reading the same shape they always did.

Three shapes are worth knowing:

* ``Stage`` / ``ToolMode`` are ``Literal`` mirrors of ``run.STAGES`` / ``run.TOOL_MODES``. The
  duplication is pinned by a unit test (``tests/unit/test_config_models.py``) because importing
  ``run`` from here would be a cycle (``run`` -> ``registry`` -> ``config``).
* ``environments`` is declared as a free-form mapping on purpose: an env block is a *partial*
  overlay, so it is validated by merging it over the top level and running the full model over the
  result (see :func:`validate_sections`). ``schema.py`` replaces the field in the generated JSON
  Schema with an all-optional ``EnvironmentBlock`` definition.
* Optional/optional-ish sections default to the same values the runtime falls back to, so the
  generated schema doubles as documentation of the effective defaults.

Two sections are **closed key sets** rather than free-form mappings, because their keys are read
literally by the runtime and a typo would silently disable behaviour: ``plugins`` (the five
``ports.ROLES``) and ``eval.row_limits`` (the circuit-breaker keys ``traces/capture.py`` reads).
Both are models, so a bad key is a dotted-path error (``plugins.promps``,
``eval.row_limits.max_step``) instead of an ignored setting.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from typing import Annotated, Any, Literal, TypedDict

from pydantic import AfterValidator, BaseModel, BeforeValidator, ConfigDict, Field, ValidationError, field_validator

Stage = Literal["dev", "pr", "main", "nightly", "control"]
ToolMode = Literal["live", "record", "replay"]


def _hook_spec(value: str) -> str:
    """Accept only ``module:attribute`` — the one shape ``Config.hook`` can import."""
    module, _, attribute = value.partition(":")
    if not module or not attribute:
        raise ValueError("must be a 'module:attribute' hook spec")
    return value


HookSpec = Annotated[str, AfterValidator(_hook_spec)]


def _not_boolean(value: Any) -> Any:
    """Reject a JSON boolean where a number is expected (a Python ``bool`` is an ``int``)."""
    if isinstance(value, bool):
        raise ValueError("Input should be a finite number, not a boolean")
    return value


#: A judge's numeric setting: a real, finite JSON number -- never a bool, ``inf`` or ``NaN``.
#: ``Infinity``/``NaN`` are not valid JSON, so they must be exit 2 before any envelope is rendered.
JudgeNumber = Annotated[float, BeforeValidator(_not_boolean), Field(allow_inf_nan=False)]


class _Strict(BaseModel):
    """Unknown keys are configuration errors, in every section."""

    model_config = ConfigDict(extra="forbid")


class TokenPrice(_Strict):
    input: float = 0.0
    output: float = 0.0


class PricesPerMillionTokens(_Strict):
    generation: TokenPrice = Field(default_factory=TokenPrice)
    judge: TokenPrice = Field(default_factory=TokenPrice)
    models: dict[str, TokenPrice] = Field(default_factory=dict)


class CostDefaults(_Strict):
    gen_calls_per_row: int = 1
    gen_input_tokens_per_row: int = 1500
    gen_output_tokens_per_row: int = 400
    judge_input_tokens_per_call: int = 2000
    judge_output_tokens_per_call: int = 200


class CostBudgets(_Strict):
    per_run_usd: float | None = None
    per_month_usd: float | None = None


class CostConfig(_Strict):
    currency: str = "USD"
    prices_per_1m_tokens: PricesPerMillionTokens = Field(default_factory=PricesPerMillionTokens)
    defaults: CostDefaults = Field(default_factory=CostDefaults)
    budgets: CostBudgets = Field(default_factory=CostBudgets)


class MetricRule(_Strict):
    """A gate rule for one metric. Six keys now; ``min_ci_lower`` is the normal-approximation lower
    bound, ``min_ci_lower_bootstrap`` the deterministic bootstrap-percentile lower bound. Anything
    else is a typo."""

    min: float | None = None
    max: float | None = None
    max_drop: float | None = None
    max_increase_pct: float | None = None
    min_ci_lower: float | None = None
    min_ci_lower_bootstrap: float | None = None


class GateCostRule(_Strict):
    max_cost_per_row_increase_pct: float | None = None
    max_cost_per_row_usd: float | None = None


class ReliabilitySuccess(_Strict):
    metric: str
    min: float = 1.0


class ReliabilityRule(_Strict):
    """pass^k rules: the point estimate (``min_pass_hat_k`` / ``max_drop_pass_hat_k``) and the
    Wilson lower bound of ``pass_hat_k`` (``min_pass_hat_k_wilson``)."""

    success: list[ReliabilitySuccess] = Field(default_factory=list)
    min_pass_hat_k: float | None = None
    max_drop_pass_hat_k: float | None = None
    min_pass_hat_k_wilson: float | None = None


class GatesConfig(_Strict):
    metrics: dict[str, MetricRule] = Field(default_factory=dict)
    cost: GateCostRule | None = None
    reliability: ReliabilityRule | None = None


class ScaleRule(_Strict):
    """``judges.<name>.scale``: the score range a judge emits.

    The *semantic* checks (``kind`` is one of ``binary|ordinal|continuous``, ``min < max``,
    ``binary`` is exactly ``0..1``) live in ``evalops.judges.JudgeSpec.validate()``, which is also
    what ``evalops judges`` and ``plan``/``eval``/``calibrate`` run: this model only pins the key set
    and the scalar types, so an unknown nested key is a dotted-path exit 2 (CONTRACT §1.1).
    """

    min: JudgeNumber = 0.0
    max: JudgeNumber = 1.0
    kind: str = "binary"


class SamplingRule(_Strict):
    """``judges.<name>.sampling``: how the judge is called (reproducible defaults).

    Ranges (``temperature`` in ``[0, 2]``, positive ``max_tokens``, integer ``seed``) are checked by
    ``evalops.judges.JudgeSpec.validate()``; the key set and the types are checked here.
    """

    temperature: JudgeNumber = 0.0
    max_tokens: int = 512
    seed: int | None = None


class JudgeRule(_Strict):
    """Calibration thresholds for one judge, plus the pin ``judges.<name>.model`` of CONTRACT §6.

    ``max_mae`` is checked by ``calibrate`` (``mae <= max_mae``) for scalar/ordinal judges; it is
    part of the calibration contract, so it must be a field here (see ``calibrate``'s docstring).
    Every threshold is *optional*: an omitted key and an explicit ``null`` both mean "no such gate",
    and ``calibrate`` emits no check for it instead of comparing a number to nothing.

    A rule **declares a judge spec** iff it carries at least one of ``rubric`` / ``scale`` /
    ``prompt_version`` / ``sampling`` (``evalops.judges.SPEC_KEYS``); those four keys make it
    validated at load time and load-rejected when unpinned. A rule with only thresholds is the
    pre-3b shape: it declares no spec and behaves exactly as before (global pin enforcement is
    WP3b.5).
    """

    min_agreement: float | None = None
    min_kappa: float | None = None
    max_mae: float | None = None
    label: str | None = None
    threshold: JudgeNumber | None = None
    model: str | None = None
    # `judge` selects the executor of a *declared* spec: absent/null/"native" is the evaluator
    # backend's own judge, "litellm" is the evalops rubric fallback (WP3b.3); anything else is exit 2
    # at `judges.<name>.judge`. The closed set is the annotation below (no prose in the class
    # docstring: that docstring *is* the generated schema `description`, and the WP3b.3 schema diff
    # has to stay the one property). It never declares a spec on its own, so a threshold-only rule
    # with `judge: litellm` stays a no-op key.
    judge: Literal["native", "litellm"] | None = None
    rubric: str | None = None
    scale: ScaleRule | None = None
    prompt_version: str | None = None
    sampling: SamplingRule | None = None


class DatasetDecl(_Strict):
    frozen: bool = False


class RowLimits(_Strict):
    """The per-row circuit breaker: exactly the keys the runtime consumes.

    ``traces/capture.py`` reads ``max_steps`` / ``max_tokens`` / ``max_seconds`` literally and
    ``plugins/core.py`` uses ``max_seconds`` as the async timeout, so ``max_step`` (a typo) used to
    be accepted and silently disabled the limit. Adding a limit means adding a field here and in
    the runtime (CONTRACT §1.1).
    """

    max_steps: float | None = None
    max_tokens: float | None = None
    max_seconds: float | None = None


class PluginsConfig(_Strict):
    """``plugins.<role>``: the role names are the closed set ``ports.ROLES``.

    A role the registry cannot serve used to reach ``Config`` as an unstructured
    "unknown plugin roles" error; as a model it is a dotted-path validation issue like any other
    (``plugins.promps``, ``environments.<env>.plugins.promps``).
    """

    prompts: str | None = None
    datasets: str | None = None
    ledger: str | None = None
    tracer: str | None = None
    evaluator: str | None = None

    @field_validator("*")
    @classmethod
    def _names_a_plugin(cls, value: str | None) -> str | None:
        """An absent role falls back to ``DEFAULT_PLUGINS``; an explicit null is a typo."""
        if value is None:
            raise ValueError("a role must name a plugin, not null")
        return value


class EvalConfig(_Strict):
    max_workers: int = 4
    max_scorer_workers: int = 2
    retries: int = 3
    backoff_seconds: float = 1.0
    requests_per_minute: int | None = None
    async_timeout: float = 300.0
    repeats: dict[Stage, int] = Field(default_factory=dict)
    row_limits: RowLimits = Field(default_factory=RowLimits)


class ToolsConfig(_Strict):
    mode_by_stage: dict[Stage, ToolMode] = Field(default_factory=dict)
    on_miss: Literal["error", "live"] = "error"


class TracesConfig(_Strict):
    capture: bool = True
    instrument: list[str] = Field(default_factory=list)
    max_payload_chars: int = 4000


class PathsConfig(_Strict):
    prompts: str = "prompts"
    datasets: str = "datasets"
    locks: str = ".evalops"
    cassettes: str = "cassettes"


class AppHooks(_Strict):
    """The only app-specific code: four ``module:attribute`` hooks."""

    behavior_config: HookSpec | None = None
    predict_fn: HookSpec | None = None
    scorers: HookSpec | None = None
    judge_meta: HookSpec | None = None


class GuardConfig(_Strict):
    exclude: list[str] | None = None
    eval_dirs: list[str] | None = None
    allow: dict[str, list[str]] | None = None
    forbidden_calls: list[str] | None = None


class LimitsConfig(_Strict):
    max_dataset_rows: int | None = None


class RedactPattern(_Strict):
    """One `redact.patterns` rule: `regex` is substituted on every string leaf with `replace`.

    The regex is compiled at validation time, so an invalid one is exit 2 naming the pattern
    instead of a crash on the first payload.
    """

    name: str
    regex: str
    replace: str | None = None


class RedactConfig(_Strict):
    """`redact:` -- the payload redaction hook (CONTRACT §5.1).

    `mode: off` (the default) writes every byte exactly as evalops did before redaction existed;
    `strict` redacts and then re-scans, refusing to persist a payload that still matches a rule
    (exit 5). `EVALOPS_REDACT` overrides `mode` at run time. The glob syntax, the rule order and the
    idempotence contract live in `evalops.contract.redact`.
    """

    mode: Literal["strict", "off"] = "off"
    fields: list[str] = Field(default_factory=list)
    hash: list[str] = Field(default_factory=list)
    patterns: list[RedactPattern] = Field(default_factory=list)
    hook: HookSpec | None = None

    @field_validator("patterns")
    @classmethod
    def _regexes_compile(cls, value: list[RedactPattern]) -> list[RedactPattern]:
        """A bad regex is a config error naming the pattern (exit 2), not a first-payload crash."""
        for pattern in value:
            try:
                re.compile(pattern.regex)
            except re.error as exc:
                raise ValueError(f"pattern '{pattern.name}': invalid regex: {exc}") from exc
        return value


class EvalopsConfig(_Strict):
    """The merged ``evalops.yaml``: top level ⊕ the selected ``environments.<env>`` block."""

    model_config = ConfigDict(extra="forbid", title="evalops configuration")

    project: str
    default_env: str = "local"
    plugins: PluginsConfig = Field(default_factory=PluginsConfig)
    plugin_config: dict[str, dict[str, Any]] = Field(default_factory=dict)
    environments: dict[str, Any] = Field(default_factory=dict)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    app: AppHooks
    datasets: dict[str, DatasetDecl] = Field(default_factory=dict)
    traces: TracesConfig = Field(default_factory=TracesConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    eval: EvalConfig = Field(default_factory=EvalConfig)
    gates: GatesConfig = Field(default_factory=GatesConfig)
    judges: dict[str, JudgeRule] = Field(default_factory=dict)
    redact: RedactConfig = Field(default_factory=RedactConfig)
    cost: CostConfig = Field(default_factory=CostConfig)
    guard: GuardConfig = Field(default_factory=GuardConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)


class ConfigIssue(TypedDict):
    """One validation problem, keyed by the dotted path a user has to edit."""

    path: str
    message: str


def format_path(prefix: tuple[str, ...], loc: tuple[int | str, ...]) -> str:
    """Render a pydantic error location as a dotted key path (``gates.metrics.x.minn``)."""
    parts = [str(part) for part in (*prefix, *loc)]
    if parts and parts[-1] == "[key]":  # pydantic marks a bad *dict key* with a trailing [key]
        parts.pop()
    return ".".join(parts) or "<root>"


def _issues(error: ValidationError, prefix: tuple[str, ...]) -> list[ConfigIssue]:
    return [ConfigIssue(path=format_path(prefix, tuple(err["loc"])), message=err["msg"]) for err in error.errors()]


def validate_sections(sections: Iterable[tuple[tuple[str, ...], Mapping[str, Any]]]) -> list[ConfigIssue]:
    """Validate candidate documents; ``prefix`` is prepended to every reported path.

    Used with ``((), merged)`` for the selected environment and ``(("environments", name), merged)``
    for every other block, which is how a *partial* env block gets validated: merged over the top
    level, every section is present, so "optional" needs no second model.
    """
    issues: list[ConfigIssue] = []
    for prefix, data in sections:
        try:
            EvalopsConfig.model_validate(dict(data))
        except ValidationError as error:
            issues.extend(_issues(error, prefix))
    return issues


def dump_issue(issue: ConfigIssue) -> str:
    """``path: message`` — the form the CLI prints and a reviewer greps for."""
    return f"{issue['path']}: {issue['message']}"


def issues_message(issues: list[ConfigIssue]) -> str:
    return "invalid evalops.yaml: " + "; ".join(dump_issue(issue) for issue in issues)


def canonical_json(obj: Any) -> str:
    """Deterministic JSON text used for the committed schema (byte-stable across runs)."""
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"
