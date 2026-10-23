"""Configuration: load, validate, explain ``evalops.yaml``.

``evalops.yaml`` is typed by ``models.EvalopsConfig`` (pydantic v2, decision D4) and
``schema.py`` generates ``docs/evalops.schema.json`` from those models. This module keeps the
**v0.3 runtime view** exactly as it was, so every call site and plugin is unaffected:

* ``Config.data`` is the plain merged dict (top level, then the selected ``environments.<env>``
  block deep-merged over it, ``environments`` removed). It is never replaced by a model dump.
* ``Config.plugins`` is ``DEFAULT_PLUGINS`` merged with ``plugins``, then validated against
  ``ports.ROLES``; ``.section()``, ``.plugin_options()``, ``.path()``, ``.lock_dir`` and
  ``.hook()`` behave as before.
* ``${VAR}`` / ``${VAR:-default}`` expansion happens before validation and can only substitute
  text: an env var can never inject a mapping or a list, because ``_expand`` only rewrites inside
  existing scalar nodes.

Validation runs on the merged document (env blocks are partial by design); every *other*
``environments.<name>`` block is validated by merging it over the top level too, so a typo in a
block you are not using is still an error. Problems are reported as ``path: message`` with the
full dotted key path and raise ``EvalOpsError(EXIT_USAGE)``; the details carry
``{"env", "path", "errors": [{"path", "message"}]}`` so ``--json`` callers get structured output
even before ``dispatch`` runs.

``explain`` provenance is deliberately simple and total over the top-level sections: a section the
env block sets is ``env:<name>``, a section that comes from the file is ``top``, a section (or a
``plugins.<role>`` / ``paths.<name>`` default) the models supply is ``default``, and every dict key
the env block sets directly is reported as ``<section>.<key>: env:<name>`` with the untouched
siblings of that section as ``top`` -- so an overridden ``plugins.ledger`` is visible, not just its
section. Sub-key entries exist only for overridden sections and model defaults; a plain section
carries a single ``top`` entry.
"""

from __future__ import annotations

import copy
import importlib
import os
import pathlib
import re
import sys
from collections.abc import Callable
from typing import Any, cast

import yaml

from ..ports import ROLES
from ..util import EXIT_USAGE, EvalOpsError
from .models import ConfigIssue, EvalopsConfig, issues_message, validate_sections
from .schema import SCHEMA_PATH, config_schema, schema_text, write_schema

__all__ = [
    "DEFAULT_PATHS",
    "DEFAULT_PLUGINS",
    "Config",
    "ConfigIssue",
    "EvalopsConfig",
    "SCHEMA_PATH",
    "config_schema",
    "import_attr",
    "load",
    "run_config_command",
    "schema_text",
    "validate_sections",
    "write_schema",
]

_ENV_RE = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")
DEFAULT_PATHS: dict[str, str] = {
    "prompts": "prompts",
    "datasets": "datasets",
    "locks": ".evalops",
    "cassettes": "cassettes",
}
DEFAULT_PLUGINS: dict[str, str] = {
    "prompts": "git",
    "datasets": "git",
    "ledger": "sql",
    "tracer": "none",
    "evaluator": "core",
}


def _expand(value: Any) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` in strings, recursively over mappings and lists."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _merge(base: dict[str, Any], over: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _nested_environment_issues(name: str, block: dict[str, Any]) -> list[ConfigIssue]:
    if "environments" not in block:
        return []
    return [
        ConfigIssue(path=f"environments.{name}.environments", message="nested 'environments' blocks are not allowed")
    ]


def _contributed(prefix: tuple[str, ...], keys: set[str], issue: ConfigIssue) -> bool:
    """True when ``issue`` is caused by a top-level key the env block itself sets."""
    dotted = ".".join(prefix) + "."
    rest = issue["path"][len(dotted) :] if issue["path"].startswith(dotted) else ""
    return rest.split(".", 1)[0] in keys


def find_config(path: str | None = None) -> pathlib.Path:
    """Resolve the config file: ``--config`` / ``EVALOPS_CONFIG``, else ``evalops.yaml`` upward."""
    explicit = path or os.getenv("EVALOPS_CONFIG")
    if explicit:
        candidate = pathlib.Path(explicit)
        if not candidate.exists():
            raise EvalOpsError(f"evalops.yaml not found at {candidate} (from --config/EVALOPS_CONFIG)", EXIT_USAGE)
        return candidate
    here = pathlib.Path.cwd().resolve()
    found = next((d / "evalops.yaml" for d in [here, *here.parents] if (d / "evalops.yaml").exists()), None)
    if found is None:
        raise EvalOpsError("evalops.yaml not found (set --config or EVALOPS_CONFIG)", EXIT_USAGE)
    return found


class Config:
    """The resolved configuration. Validates on construction; keeps the v0.3 plain-dict view."""

    def __init__(self, data: dict[str, Any], root: pathlib.Path, *, source: pathlib.Path | None = None) -> None:
        self.root: pathlib.Path = root
        self.source: pathlib.Path | None = source
        raw_env = os.getenv("EVALOPS_ENV", data.get("default_env", "local"))
        self.env: str = raw_env if isinstance(raw_env, str) else str(raw_env)
        envs = data.get("environments") or {}
        if not isinstance(envs, dict):
            raise EvalOpsError("evalops.yaml: 'environments' must be a mapping of name -> block", EXIT_USAGE)
        if envs and self.env not in envs:
            raise EvalOpsError(f"unknown environment '{self.env}' (have: {sorted(envs)})", EXIT_USAGE)
        # Environment blocks override top-level keys (plugins, plugin_config, budgets, ...).
        self._top: dict[str, Any] = {k: v for k, v in data.items() if k != "environments"}
        block = envs.get(self.env) or {}
        if not isinstance(block, dict):
            raise EvalOpsError(f"environments.{self.env} must be a mapping", EXIT_USAGE)
        self._block: dict[str, Any] = block
        self.data: dict[str, Any] = _merge(self._top, block)
        for key in ("project", "app"):
            if key not in self.data:
                raise EvalOpsError(f"evalops.yaml missing required key '{key}'", EXIT_USAGE)
        self._check(envs, block)
        self.plugins: dict[str, str] = {**DEFAULT_PLUGINS, **(self.data.get("plugins") or {})}
        # Defence in depth: `models.PluginsConfig` already closed the key set, so a typo is reported
        # above as `plugins.<role>`; this guard also covers a drift in DEFAULT_PLUGINS itself.
        unknown = set(self.plugins) - set(ROLES)
        if unknown:
            raise EvalOpsError(f"unknown plugin roles {sorted(unknown)}; valid: {ROLES}", EXIT_USAGE)

    def _check(self, envs: dict[str, Any], block: dict[str, Any]) -> None:
        """Validate the merged document and every env block; raise exit 2 naming each key path."""
        issues: list[ConfigIssue] = _nested_environment_issues(self.env, block)
        issues.extend(validate_sections([((), self.data)]))
        others = {name: other for name, other in envs.items() if name != self.env}
        for name, other in others.items():
            if not isinstance(other, dict):
                issues.append(ConfigIssue(path=f"environments.{name}", message="Input should be a valid mapping"))
                continue
            prefix = ("environments", name)
            # Partial by design: merged over the top level, the full model applies. Only the
            # problems this block contributes are reported -- an inherited top-level typo is
            # already reported once by the merged pass above, not once per environment.
            merged = validate_sections([(prefix, _merge(self._top, other))])
            issues.extend(issue for issue in merged if _contributed(prefix, set(other), issue))
            issues.extend(_nested_environment_issues(name, other))
        if issues:
            raise EvalOpsError(
                issues_message(issues),
                EXIT_USAGE,
                {"env": self.env, "path": str(self.source or ""), "errors": issues},
            )

    def section(self, key: str) -> dict[str, Any]:
        return cast("dict[str, Any]", self.data.get(key) or {})

    def plugin_options(self, plugin_name: str) -> dict[str, Any]:
        return cast("dict[str, Any]", (self.data.get("plugin_config") or {}).get(plugin_name) or {})

    def path(self, key: str) -> pathlib.Path:
        return self.root / cast("str", self.section("paths").get(key, DEFAULT_PATHS[key]))

    @property
    def lock_dir(self) -> pathlib.Path:
        p = self.path("locks")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def hook(self, key: str) -> Callable[..., Any]:
        spec = self.section("app").get(key)
        if not spec or ":" not in spec:
            raise EvalOpsError(f"app.{key} must be 'module:attribute'", EXIT_USAGE)
        return import_attr(spec, self.root, f"app.{key}")

    def provenance(self) -> dict[str, str]:
        """``{"<key>": "top" | "env:<name>" | "default"}``; see the module docstring."""
        tag = f"env:{self.env}"
        out: dict[str, str] = {key: tag if key in self._block else "top" for key in self.data}
        for key in EvalopsConfig.model_fields:
            if key != "environments" and key not in self.data:
                out[key] = "default"  # the models supply the section
        for key, value in self._block.items():
            if not isinstance(value, dict):
                continue
            merged = self.data.get(key)
            merged_keys = merged if isinstance(merged, dict) else {}
            for sub in value:
                out[f"{key}.{sub}"] = tag
            for sub in merged_keys:
                out.setdefault(f"{key}.{sub}", "top")
        for role in ROLES:
            if role not in (self.data.get("plugins") or {}):
                out[f"plugins.{role}"] = "default"
        for name in DEFAULT_PATHS:
            if name not in (self.data.get("paths") or {}):
                out[f"paths.{name}"] = "default"
        return dict(sorted(out.items()))


def import_attr(spec: str, root: pathlib.Path, label: str) -> Callable[..., Any]:
    module, attr = spec.split(":", 1)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return cast("Callable[..., Any]", getattr(importlib.import_module(module), attr))
    except (ImportError, AttributeError) as e:
        raise EvalOpsError(f"cannot load {label}='{spec}': {e}", EXIT_USAGE) from e


def load(path: str | None = None) -> Config:
    config_path = find_config(path)
    document = _expand(yaml.safe_load(config_path.read_text()) or {})
    if not isinstance(document, dict):
        raise EvalOpsError(f"{config_path}: evalops.yaml must be a mapping of keys to values", EXIT_USAGE)
    return Config(document, config_path.parent.resolve(), source=config_path)


def run_config_command(cfg: Config, action: str) -> dict[str, Any]:
    """Implement ``evalops config validate|schema|explain`` (arg ``action``).

    ``validate``: because ``Config`` validates on construction, an invalid file never reaches this
    function — ``main`` reports exit 2 with ``{"env", "path", "errors": [{"path", "message"}]}``
    in the error details. Reaching here means the file is valid, so the payload is
    ``{"ok": True, "env", "path", "errors": []}``.

    ``schema``: ``{"schema": <JSON Schema draft 2020-12>}`` — keyed so ``main``'s ``ok``/``env``
    wrapper cannot collide with the schema's own keys. ``make schema`` writes the raw schema. Note
    that ``main`` loads the config before dispatching, so ``config schema`` also requires a readable
    ``evalops.yaml`` — the schema is never emitted for a broken repository.

    ``explain``: ``{"data": <merged dict>, "provenance": {...}}``.
    """
    if action == "validate":
        return {"ok": True, "env": cfg.env, "path": str(cfg.source or ""), "errors": []}
    if action == "schema":
        return {"schema": config_schema()}
    if action == "explain":
        return {"data": cfg.data, "provenance": cfg.provenance()}
    raise EvalOpsError(f"unknown config sub-command '{action}'", EXIT_USAGE)
