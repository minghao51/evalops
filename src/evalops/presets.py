"""The seven presets (decision D8) and the capability matrix generated from code (WP2.3).

``evalops init --preset <name>`` renders exactly one file, ``evalops.yaml``, and this module owns
the data it is rendered from:

* :data:`PRESETS` holds one :class:`PresetSpec` per D8 name. A spec carries the
  ``environments:`` mapping as Python data (every key already exists in ``config.models``), the
  pip extras to install, and ``roles`` (role -> plugin) for the capability matrix. ``roles`` is
  *derived* from the spec's own environment block in ``__post_init__``, so the matrix and the
  rendered file cannot disagree.
* :func:`render` serializes a spec, re-parses the text and validates it through the same pydantic
  models ``config validate`` uses, so ``init`` can never write a yaml the loader rejects. Vendor
  URLs and tokens are rendered as ``${VAR:-placeholder}`` interpolations -- never literals.
* :func:`capabilities_payload` is what ``evalops capabilities --json`` returns, and
  :func:`capabilities_text` renders the same payload as ``docs/CAPABILITIES.md`` (``make
  capabilities``). Plugin roles come from each plugin class's own ``provides`` metadata, never from
  a hand-maintained table; a unit test fails when the committed document drifts from the code.

Core stays vendor-free: plugin classes are imported only to read their ``provides``/``name``
metadata, and those modules already import their SDK lazily (``tests/unit/test_no_vendor_imports``).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import yaml

from .config.models import ConfigIssue, issues_message, validate_sections
from .ports import ROLES
from .util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError

#: The one file ``evalops init`` writes.
CONFIG_NAME = "evalops.yaml"
#: Rendered ``project:``; a starter value the user is invited to edit (no ``--name`` flag exists).
PROJECT_NAME = "evalops"
#: The local ledger DSN, interpolated so a CI run can point at Postgres without editing the file.
LEDGER_URL = "${EVALOPS_LEDGER_URL:-sqlite:///.evalops/ledger.sqlite}"

#: The shipped ``evalops.yaml`` local wiring: files in the repo, SQLite ledger, no external service.
LOCAL_PLUGINS = {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "none", "evaluator": "core"}
_MLFLOW_PLUGINS = {
    "prompts": "mlflow",
    "datasets": "mlflow",
    "ledger": "mlflow",
    "tracer": "mlflow",
    "evaluator": "mlflow",
}
_LANGFUSE_PLUGINS = {
    "prompts": "langfuse",
    "datasets": "langfuse",
    "ledger": "sql",
    "tracer": "langfuse",
    "evaluator": "langfuse",
}
_LANGSMITH_PLUGINS = {
    "prompts": "langsmith",
    "datasets": "langsmith",
    "ledger": "sql",
    "tracer": "langsmith",
    "evaluator": "langsmith",
}
_LOGFIRE_PLUGINS = {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "logfire", "evaluator": "core"}
_MIXED_PLUGINS = {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "logfire", "evaluator": "core"}

_MLFLOW_OPTIONS = {
    "tracking_uri": "${MLFLOW_TRACKING_URI:-http://localhost:5000}",
    "experiment": "${MLFLOW_EXPERIMENT:-evalops}",
}
_DATABRICKS_OPTIONS = {
    "tracking_uri": "${DATABRICKS_TRACKING_URI:-databricks}",
    "registry_uri": "${DATABRICKS_REGISTRY_URI:-databricks-uc}",
    "experiment": "${DATABRICKS_EXPERIMENT:-/Shared/evalops}",
    "namespace": "${DATABRICKS_NAMESPACE:-ml.genai_eval}",
}
_LOGFIRE_OPTIONS = {"service_name": "${LOGFIRE_SERVICE_NAME:-evalops}", "read_token": "${LOGFIRE_READ_TOKEN:-}"}

#: pip extra per built-in plugin that ships behind an optional dependency (``pyproject.toml``).
#: Pinned against ``[project.optional-dependencies]`` by ``tests/unit/test_presets.py``.
PLUGIN_EXTRAS = {"mlflow": "mlflow", "langfuse": "langfuse", "langsmith": "langsmith", "logfire": "logfire"}


def _env(
    plugins: dict[str, str],
    plugin_config: dict[str, dict[str, Any]] | None = None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One ``environments.<name>`` block as Python data (a partial overlay, like the shipped file)."""
    block: dict[str, Any] = {"plugins": dict(plugins)}
    if plugin_config:
        block["plugin_config"] = plugin_config
    if limits:
        block["limits"] = limits
    return block


@dataclass(frozen=True)
class PresetSpec:
    """One D8 preset. ``roles`` is derived from :attr:`environments`, never restated by hand."""

    name: str
    description: str
    environments: dict[str, dict[str, Any]]
    extras: tuple[str, ...] = ()
    roles: dict[str, str] = field(init=False, repr=False, default_factory=dict)

    def __post_init__(self) -> None:
        block = self.environments.get(self.name)
        if not isinstance(block, dict):
            raise ValueError(f"preset '{self.name}' has no environments.{self.name} block")
        wiring = block.get("plugins")
        if not isinstance(wiring, dict):
            raise ValueError(f"preset '{self.name}' does not wire any plugin role")
        missing = [role for role in ROLES if role not in wiring]
        if missing:
            raise ValueError(f"preset '{self.name}' does not wire {missing}")
        unknown = sorted(set(wiring) - set(ROLES))
        if unknown:
            raise ValueError(f"preset '{self.name}' wires unknown roles {unknown}")
        # Frozen dataclass: the derived field is set through object.__setattr__.
        object.__setattr__(self, "roles", {role: str(wiring[role]) for role in ROLES})


PRESETS: dict[str, PresetSpec] = {
    "local": PresetSpec(
        name="local",
        description="zero external services: prompts/datasets are repo files and the ledger is SQLite",
        environments={"local": _env(LOCAL_PLUGINS)},
    ),
    "mlflow": PresetSpec(
        name="mlflow",
        description="one self-hosted MLflow server serves all five roles (tracking server + genai registry)",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "mlflow": _env(_MLFLOW_PLUGINS, {"mlflow": dict(_MLFLOW_OPTIONS)}),
        },
        extras=("mlflow",),
    ),
    "databricks": PresetSpec(
        name="databricks",
        description="MLflow on a Databricks workspace: Unity Catalog prompts, datasets and run ledger",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "databricks": _env(
                _MLFLOW_PLUGINS,
                {"mlflow": dict(_DATABRICKS_OPTIONS)},
                {"max_dataset_rows": 2000},
            ),
        },
        extras=("databricks",),
    ),
    "langfuse": PresetSpec(
        name="langfuse",
        description="Langfuse prompts, datasets, tracing and native evaluation; SQL run ledger",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "langfuse": _env(_LANGFUSE_PLUGINS),
        },
        extras=("langfuse",),
    ),
    "langsmith": PresetSpec(
        name="langsmith",
        description="LangSmith prompts, datasets, tracing and native evaluation; SQL run ledger",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "langsmith": _env(_LANGSMITH_PLUGINS),
        },
        extras=("langsmith",),
    ),
    "logfire": PresetSpec(
        name="logfire",
        description="Logfire traces every row; prompts/datasets stay in git and the core evaluator runs the eval",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "logfire": _env(_LOGFIRE_PLUGINS, {"logfire": dict(_LOGFIRE_OPTIONS)}),
        },
        extras=("logfire",),
    ),
    "mixed": PresetSpec(
        name="mixed",
        description="roles compose: git prompts/datasets, SQL ledger, Logfire traces, core evaluator",
        environments={
            "local": _env(LOCAL_PLUGINS),
            "mixed": _env(_MIXED_PLUGINS, {"logfire": dict(_LOGFIRE_OPTIONS)}),
        },
        extras=("logfire",),
    ),
}

#: D8 canonical order (not alphabetical): the order ``init --preset`` and the matrix list them in.
PRESET_NAMES: tuple[str, ...] = tuple(PRESETS)

#: Sections every preset renders unchanged; all of them are pre-existing ``config.models`` fields.
_COMMON_SECTIONS: dict[str, Any] = {
    "paths": {"prompts": "prompts", "datasets": "datasets", "locks": ".evalops"},
    "datasets": {"smoke": {"frozen": False}},
    "gates": {"metrics": {"correctness/mean": {"min": 0.8, "max_drop": 0.03}, "error_rate": {"max": 0.02}}},
    "cost": {"budgets": {"per_run_usd": 25, "per_month_usd": 500}},
}
_APP_HOOKS: dict[str, str] = {
    "behavior_config": "app.evalops_hooks:behavior_config",
    "predict_fn": "app.evalops_hooks:build_predict_fn",
    "scorers": "app.evalops_hooks:build_scorers",
    "judge_meta": "app.evalops_hooks:judge_meta",
}

_LOG_FIRE_CAVEAT = [
    "    # Logfire usage lookup runs a private SQL query against your project: the highest-risk",
    "    # VERIFY in docs/STATE.md (compromise 1). Without a read token it reports no usage, so call",
    "    # evalops.report_usage(...) inside predict_fn when you need cost accounting.",
]
#: Environment-specific notes rendered as YAML comments (prose, never wiring metadata).
_ENV_NOTES: dict[str, list[str]] = {
    "databricks": [
        "    # Databricks credentials come from DATABRICKS_HOST (workspace URL) and DATABRICKS_TOKEN in",
        "    # the environment, never from this file; DATABRICKS_TRACKING_URI only overrides the mode",
        "    # keyword `databricks`, and `namespace` must be <catalog>.<schema> for Unity Catalog prompts.",
    ],
    "langsmith": [
        "    # LangSmith commit tags are not self-serve, so evalops cannot move an alias automatically:",
        "    # `evalops promote` exits 6 here. Move the tag in the LangSmith UI, or keep prompts in git.",
    ],
}


# ---------------------------------------------------------------------------
# rendering (A2/A3): deterministic text, re-parsed and validated before it can be written
# ---------------------------------------------------------------------------
def document(spec: PresetSpec) -> dict[str, Any]:
    """The data the rendered file must parse back to (the scaffolding around ``spec.environments``).

    Keys are in the order the file renders them, so a reader can diff :func:`document` against the
    text without re-sorting; ``**_COMMON_SECTIONS`` is spread where it lands in the file.
    """
    return {
        "project": PROJECT_NAME,
        "default_env": spec.name,
        "plugins": dict(LOCAL_PLUGINS),
        "plugin_config": {"sql": {"url": LEDGER_URL}},
        "environments": {name: dict(block) for name, block in spec.environments.items()},
        "paths": dict(_COMMON_SECTIONS["paths"]),
        "app": dict(_APP_HOOKS),
        "datasets": dict(_COMMON_SECTIONS["datasets"]),
        "gates": dict(_COMMON_SECTIONS["gates"]),
        "cost": dict(_COMMON_SECTIONS["cost"]),
    }


_YAML_KEYWORDS = frozenset({"true", "false", "null", "yes", "no", "on", "off", "~"})
_PLAIN = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.+-]*")


def _scalar(value: Any) -> str:
    """One YAML scalar: plain when unambiguous, JSON-quoted otherwise (JSON quoting is valid YAML)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        if _PLAIN.fullmatch(value) and value.lower() not in _YAML_KEYWORDS:
            return value
        return json.dumps(value)
    return json.dumps(value)


def _flow(value: Any) -> str:
    """Flow-style YAML for a scalar, list or mapping (keeps the rendered preset readable)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{_scalar(key)}: {_flow(item)}" for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(_flow(item) for item in value) + "]"
    return _scalar(value)


_HEADER = """\
# evalops.yaml -- starter configuration for the '{name}' preset.
# Generated by `evalops init --preset {name}`; edit it freely from here on.
# ${{VAR}} and ${{VAR:-default}} are expanded when this file loads, so no credential is committed.
# EVALOPS_ENV selects an environment; every preset also renders `local`, which needs no service."""


def _legal_role_comments() -> list[str]:
    """The legal plugin per role, read from each plugin class's own ``provides`` (``mixed`` only)."""
    lines = ["    # legal plugins per role (each plugin's own `provides` metadata):"]
    for role in ROLES:
        plugins = ", ".join(plugins_for_role(role))
        lines.append(f"    #   {role + ':':<10} {plugins}")
    return lines


def _render_text(spec: PresetSpec) -> str:
    doc = document(spec)
    out = [_HEADER.format(name=spec.name), "", "# Set this to your project name."]
    out.append(f"project: {_scalar(doc['project'])}")
    out.append(f"default_env: {_scalar(doc['default_env'])}")
    out += [
        "",
        "# Which plugin serves each role: prompts | datasets | ledger | tracer | evaluator",
        "# This top level is the `local` wiring; every environments.<name> block below overrides it.",
        f"plugins: {_flow(doc['plugins'])}",
        "plugin_config:",
    ]
    for plugin, options in doc["plugin_config"].items():
        out.append(f"  {_scalar(plugin)}: {_flow(options)}")
    out += ["", "environments:"]
    for env_name, block in doc["environments"].items():
        out.append(f"  {_scalar(env_name)}:")
        if env_name == "local":
            out.append("    # zero external services: prompts/datasets are repo files, ledger is SQLite")
        else:
            description = PRESETS[env_name].description if env_name in PRESETS else ""
            out.append(f"    # {description}")
            out += _ENV_NOTES.get(env_name, [])
            if env_name == "mixed":
                out += _legal_role_comments()
        out.append(f"    plugins: {_flow(block['plugins'])}")
        if "plugin_config" in block:
            out.append("    plugin_config:")
            for plugin, options in block["plugin_config"].items():
                out.append(f"      {_scalar(plugin)}: {_flow(options)}")
        if "limits" in block:
            out.append(f"    limits: {_flow(block['limits'])}")
        if "logfire" in set(block["plugins"].values()):
            out += _LOG_FIRE_CAVEAT
        out.append("")
    out.append(f"paths: {_flow(doc['paths'])}")
    out.append("")
    out.append("app:")
    out += [f"  {_scalar(key)}: {_flow(hook)}" for key, hook in doc["app"].items()]
    out.append("")
    out.append("datasets:")
    out += [f"  {_scalar(name)}: {_flow(decl)}" for name, decl in doc["datasets"].items()]
    out.append("")
    out.append(f"gates: {_flow(doc['gates'])}")
    out.append("")
    out.append(f"cost: {_flow(doc['cost'])}")
    return "\n".join(out) + "\n"


def _merged(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    """Deep merge of one env block over the top level (the shape ``Config`` validates)."""
    out = dict(base)
    for key, value in over.items():
        out[key] = _merged(out[key], value) if isinstance(value, dict) and isinstance(out.get(key), dict) else value
    return out


def rendered_issues(parsed: dict[str, Any]) -> list[ConfigIssue]:
    """Validate a parsed preset the way ``config validate`` does: top level plus every env merge."""
    top = {key: value for key, value in parsed.items() if key != "environments"}
    sections: list[tuple[tuple[str, ...], dict[str, Any]]] = [((), top)]
    for name, block in (parsed.get("environments") or {}).items():
        sections.append((("environments", name), _merged(top, block)))
    return validate_sections(sections)


def render(spec: PresetSpec) -> str:
    """The ``evalops.yaml`` text for ``spec``, guaranteed to re-parse and to validate (A3)."""
    text = _render_text(spec)
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:  # pragma: no cover - a rendering bug, not a user path
        raise EvalOpsError(f"preset '{spec.name}' rendered invalid YAML: {exc}", EXIT_RUNTIME) from exc
    if parsed != document(spec):  # pragma: no cover - a quoting bug would be silent otherwise
        raise EvalOpsError(f"preset '{spec.name}' does not round-trip to its own data", EXIT_RUNTIME)
    issues = rendered_issues(parsed)
    if issues:  # pragma: no cover - a preset that cannot render is a code defect, not a user error
        raise EvalOpsError(
            f"preset '{spec.name}' would write an invalid evalops.yaml: {issues_message(issues)}", EXIT_RUNTIME
        )
    return text


# ---------------------------------------------------------------------------
# `evalops init --preset <p>` (B1/B2)
# ---------------------------------------------------------------------------
def init(preset: str, *, directory: str | pathlib.Path = ".", force: bool = False) -> dict[str, Any]:
    """Write ``<directory>/evalops.yaml`` for ``preset`` and return the CLI payload.

    Exactly one file is written (no ``.gitignore`` edit, no directory scaffold) and the text is
    validated *before* the write, so an existing file is refused without being touched. The
    refusal is exit 2, like every other usage/config error.
    """
    spec = PRESETS.get(preset)
    if spec is None:
        raise EvalOpsError(
            f"unknown preset '{preset}' (valid: {', '.join(PRESET_NAMES)})",
            EXIT_USAGE,
            {"presets": list(PRESET_NAMES)},
        )
    root = pathlib.Path(directory)
    target = root / CONFIG_NAME
    if not root.is_dir():
        # `init` writes one file and creates nothing else -- not even the target directory.
        raise EvalOpsError(
            f"target directory {root} does not exist or is not a directory", EXIT_USAGE, {"path": str(root)}
        )
    if target.exists() and not force:
        raise EvalOpsError(
            f"{target} already exists; pass --force to overwrite it",
            EXIT_USAGE,
            {"path": str(target)},
        )
    text = render(spec)
    target.write_text(text, encoding="utf-8")
    return {
        "summary": f"wrote {target} for preset '{spec.name}'",
        "preset": spec.name,
        "files_written": [str(target)],
        "extras": list(spec.extras),
        # The three commands a fresh project needs, in order (`config validate` first: `doctor`
        # cannot tell a config typo from a missing SDK).
        "next_steps": ["evalops --json config validate", "evalops --json doctor", "evalops --json sync all"],
    }


# ---------------------------------------------------------------------------
# the capability matrix (C1/C2): plugin roles come from the plugin classes themselves
# ---------------------------------------------------------------------------
def _plugin_class(spec: str) -> type:
    module_name, _, attribute = spec.partition(":")
    try:
        return getattr(importlib.import_module(module_name), attribute)
    except (ImportError, AttributeError) as exc:  # pragma: no cover - a broken installation
        raise EvalOpsError(f"cannot load built-in plugin '{spec}': {exc}", EXIT_RUNTIME) from exc


def plugin_matrix() -> dict[str, dict[str, Any]]:
    """``{plugin: {"aliases", "extra", "provides"}}`` read from the built-in plugin classes.

    ``registry.BUILTIN`` maps registry names to classes; a name that differs from the class's own
    ``name`` is an alias (``none`` is ``core``). ``provides`` is filtered through ``ports.ROLES`` so
    the order is the contract's role order; a role the port layer does not know is a defect.
    """
    from .registry import BUILTIN

    rows: dict[str, dict[str, Any]] = {}
    for registry_name in sorted(BUILTIN):
        cls = _plugin_class(BUILTIN[registry_name])
        declared = tuple(getattr(cls, "provides", ()))
        unknown = sorted(set(declared) - set(ROLES))
        if unknown:  # pragma: no cover - a plugin declaring a non-port role
            raise EvalOpsError(f"plugin '{registry_name}' declares unknown roles {unknown}", EXIT_RUNTIME)
        canonical = str(getattr(cls, "name", registry_name))
        provides = [role for role in ROLES if role in declared]
        row = rows.get(canonical)
        if row is None:
            rows[canonical] = {"aliases": [], "extra": PLUGIN_EXTRAS.get(canonical), "provides": provides}
        elif row["provides"] != provides:  # pragma: no cover - alias and class disagree
            raise EvalOpsError(f"plugin alias '{registry_name}' disagrees on the roles of '{canonical}'", EXIT_RUNTIME)
        else:
            row["aliases"].append(registry_name)
    for row in rows.values():
        row["aliases"].sort()
    return {name: rows[name] for name in sorted(rows)}


def plugins_for_role(role: str) -> list[str]:
    """Every registry name that provides ``role`` (``provides`` plus its aliases), sorted.

    Aliases are included because ``plugins.<role>`` accepts a registry name, not a class name:
    ``tracer: none`` is the shipped local wiring and ``none`` is ``core`` (whose ``provides`` is the
    source of truth for both names).
    """
    names: set[str] = set()
    for name, row in plugin_matrix().items():
        if role in row["provides"]:
            names.add(name)
            names.update(row["aliases"])
    return sorted(names)


def capabilities_payload() -> dict[str, Any]:
    """``evalops capabilities --json`` data: presets (D8 order) and plugins/roles (sorted)."""
    return {
        "presets": {
            name: {
                "description": PRESETS[name].description,
                "environments": sorted(PRESETS[name].environments),
                "extras": list(PRESETS[name].extras),
                "roles": {role: PRESETS[name].roles[role] for role in ROLES},
            }
            for name in PRESET_NAMES
        },
        "plugins": plugin_matrix(),
        "roles": {role: plugins_for_role(role) for role in ROLES},
    }


CAPABILITIES_PATH = pathlib.Path(__file__).resolve().parents[2] / "docs" / "CAPABILITIES.md"
_GENERATED_NOTE = [
    "<!-- Generated by `make capabilities` from src/evalops/presets.py and each plugin class's",
    "     own `provides` metadata. Do not edit by hand: tests/unit/test_presets.py fails when",
    "     this file drifts from the code. -->",
]


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def capabilities_text() -> str:
    """``docs/CAPABILITIES.md``: the matrix rendered from :func:`capabilities_payload`."""
    data = capabilities_payload()
    roles = list(ROLES)
    lines = [
        "# evalops capability matrix",
        "",
        *_GENERATED_NOTE,
        "",
        "`evalops init --preset <name>` renders one of the seven D8 presets. Every preset also renders",
        "a `local` environment (prompts/datasets are repo files, the ledger is SQLite, no service), so",
        "a project can always fall back offline. `evalops capabilities --json` emits the same matrix.",
        "",
        "## Presets",
        "",
    ]
    rows = [
        [
            f"`{name}`",
            ", ".join(f"`{env}`" for env in entry["environments"]),
            *(f"`{entry['roles'][role]}`" for role in roles),
            ", ".join(f"`{extra}`" for extra in entry["extras"]) or "—",
        ]
        for name, entry in data["presets"].items()
    ]
    lines += _table(["Preset", "Environments", *roles, "Extras"], rows)
    lines += ["", "## Preset notes", ""]
    lines += [f"- `{name}` — {entry['description']}" for name, entry in data["presets"].items()]
    lines += [
        "",
        "Install a preset's extras with `pip install .[<extra>]` before running `evalops doctor`.",
        "The `Extra` column below is the optional dependency that installs a plugin's SDK; `sql`",
        "needs none for SQLite (the `sql` extra adds the Postgres/MySQL drivers).",
        "",
        "## Built-in plugins",
        "",
    ]
    rows = [
        [
            f"`{name}`",
            *("✓" if role in row["provides"] else "" for role in roles),
            ", ".join(f"`{alias}`" for alias in row["aliases"]) or "—",
            f"`{row['extra']}`" if row["extra"] else "—",
        ]
        for name, row in data["plugins"].items()
    ]
    lines += _table(["Plugin", *roles, "Aliases", "Extra"], rows)
    lines += ["", "## Legal plugins per role", ""]
    rows = [[f"`{role}`", ", ".join(f"`{name}`" for name in data["roles"][role])] for role in roles]
    lines += _table(["Role", "Plugins"], rows)
    lines += [
        "",
        "A third-party plugin (an `evalops.plugins` entry point, or `module:Class`) may serve any",
        "subset of the roles. Asking a plugin for a role it does not provide is exit 6 with a hint",
        "naming what it does provide.",
        "",
    ]
    return "\n".join(lines)


def write_capabilities(path: pathlib.Path | None = None) -> pathlib.Path:
    """Write the matrix to ``path`` (default: the committed ``docs/CAPABILITIES.md``)."""
    target = path or CAPABILITIES_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(capabilities_text(), encoding="utf-8")
    return target
