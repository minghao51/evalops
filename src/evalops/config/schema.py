"""Generated JSON Schema for ``evalops.yaml`` (decision D4).

``make schema`` rewrites ``docs/evalops.schema.json`` from :func:`config_schema`; the committed
file is a build artifact and a unit test fails when it drifts from the models, so a config change
and its schema change land in the same work package.

Two transformations are applied on top of pydantic's output:

* ``EvalopsConfig.environments`` is a free-form mapping in the models because an
  ``environments.<name>`` block is a *partial* overlay (validated by merging over the top level,
  see ``models.validate_sections``). The schema therefore replaces that property with
  ``dict[str, EnvironmentBlock]``, where ``EnvironmentBlock`` reuses the top-level property schemas
  with no ``required`` list — every section is optional there.
* ``plugins.<role>`` is ``str | None`` in the model because an omitted role falls back to
  ``DEFAULT_PLUGINS``, but ``PluginsConfig`` rejects an explicit ``null`` (a role must *name* a
  plugin). pydantic emits the annotation, so the naive schema advertises an ``anyOf [string, null]``
  arm the loader refuses — see :func:`_plugins_are_string_only`. Omitted is the only way to leave a
  role unset, so the schema says exactly that.
"""

from __future__ import annotations

import pathlib
from typing import Any

from .models import EvalopsConfig, PluginsConfig, canonical_json

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_PATH = pathlib.Path(__file__).resolve().parents[3] / "docs" / "evalops.schema.json"


def _environment_block(properties: dict[str, Any]) -> dict[str, Any]:
    """All-optional overlay definition for ``environments.<name>`` (no ``required`` key)."""
    return {
        "title": "EnvironmentBlock",
        "description": "Partial overlay for one environments.<name> block: every section is optional.",
        "type": "object",
        "additionalProperties": False,
        "properties": {key: value for key, value in properties.items() if key != "environments"},
    }


def _plugins_are_string_only(definition: dict[str, Any]) -> None:
    """Make every ``plugins.<role>`` an *optional* string with no ``null`` arm.

    ``PluginsConfig`` names a plugin or stays unset: the models keep rejecting an explicit ``null``
    (the value never reaches ``DEFAULT_PLUGINS``), so a schema advertising ``string | null`` would be
    a trap for anyone validating with it — a document the schema accepts must not be a config error.
    The property stays out of ``required`` and drops the ``null`` default, which leaves "omitted" as
    the one way to say "no override".
    """
    properties: dict[str, Any] = definition["properties"]
    for role in PluginsConfig.model_fields:
        properties[role] = {"title": role.title(), "type": "string"}


def config_schema() -> dict[str, Any]:
    """The JSON Schema (draft 2020-12) for a merged ``evalops.yaml``."""
    document: dict[str, Any] = EvalopsConfig.model_json_schema()
    properties: dict[str, Any] = document["properties"]
    definitions: dict[str, Any] = document.setdefault("$defs", {})
    _plugins_are_string_only(definitions["PluginsConfig"])
    definitions["EnvironmentBlock"] = _environment_block(properties)
    properties["environments"] = {
        "title": "Environments",
        "description": "Named overlays selected with EVALOPS_ENV; each is merged over the top level.",
        "type": "object",
        "default": {},
        "additionalProperties": {"$ref": "#/$defs/EnvironmentBlock"},
    }
    document["$schema"] = SCHEMA_DIALECT
    return document


def schema_text() -> str:
    """The exact bytes of ``docs/evalops.schema.json`` (sorted keys, 2-space indent, trailing newline)."""
    return canonical_json(config_schema())


def write_schema(path: pathlib.Path | None = None) -> pathlib.Path:
    """Write the schema to ``path`` (default: the committed ``docs/evalops.schema.json``)."""
    target = path or SCHEMA_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(schema_text(), encoding="utf-8")
    return target
