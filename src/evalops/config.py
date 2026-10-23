import copy
import importlib
import os
import pathlib
import re
import sys

import yaml

from .ports import ROLES
from .util import EXIT_USAGE, EvalOpsError

_ENV_RE = re.compile(r"\$\{([A-Za-z0-9_]+)(?::-([^}]*))?\}")
DEFAULT_PATHS = {"prompts": "prompts", "datasets": "datasets", "locks": ".evalops", "cassettes": "cassettes"}
DEFAULT_PLUGINS = {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "none", "evaluator": "core"}


def _expand(value):
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


class Config:
    def __init__(self, data: dict, root: pathlib.Path):
        self.root = root
        self.env = os.getenv("EVALOPS_ENV", data.get("default_env", "local"))
        envs = data.get("environments") or {}
        if envs and self.env not in envs:
            raise EvalOpsError(f"unknown environment '{self.env}' (have: {sorted(envs)})", EXIT_USAGE)
        # Environment blocks override top-level keys (plugins, plugin_config, budgets, ...).
        self.data = _merge({k: v for k, v in data.items() if k != "environments"}, envs.get(self.env, {}))
        for key in ("project", "app"):
            if key not in self.data:
                raise EvalOpsError(f"evalops.yaml missing required key '{key}'", EXIT_USAGE)
        self.plugins = {**DEFAULT_PLUGINS, **(self.data.get("plugins") or {})}
        unknown = set(self.plugins) - set(ROLES)
        if unknown:
            raise EvalOpsError(f"unknown plugin roles {sorted(unknown)}; valid: {ROLES}", EXIT_USAGE)

    def section(self, key: str) -> dict:
        return self.data.get(key) or {}

    def plugin_options(self, plugin_name: str) -> dict:
        return (self.data.get("plugin_config") or {}).get(plugin_name) or {}

    def path(self, key: str) -> pathlib.Path:
        return self.root / self.section("paths").get(key, DEFAULT_PATHS[key])

    @property
    def lock_dir(self) -> pathlib.Path:
        p = self.path("locks")
        p.mkdir(parents=True, exist_ok=True)
        return p

    def hook(self, key: str):
        spec = self.section("app").get(key)
        if not spec or ":" not in spec:
            raise EvalOpsError(f"app.{key} must be 'module:attribute'", EXIT_USAGE)
        return import_attr(spec, self.root, f"app.{key}")


def import_attr(spec: str, root: pathlib.Path, label: str):
    module, attr = spec.split(":", 1)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as e:
        raise EvalOpsError(f"cannot load {label}='{spec}': {e}", EXIT_USAGE) from e


def load(path: str | None = None) -> Config:
    p = path or os.getenv("EVALOPS_CONFIG")
    if p:
        cfg_path = pathlib.Path(p)
    else:
        here = pathlib.Path.cwd().resolve()
        cfg_path = next((d / "evalops.yaml" for d in [here, *here.parents] if (d / "evalops.yaml").exists()), None)
    if not cfg_path or not cfg_path.exists():
        raise EvalOpsError("evalops.yaml not found (set --config or EVALOPS_CONFIG)", EXIT_USAGE)
    data = _expand(yaml.safe_load(cfg_path.read_text()) or {})
    return Config(data, cfg_path.parent.resolve())
