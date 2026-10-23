"""Plugin discovery: built-ins, installed entry points (group 'evalops.plugins'), or 'module:Class'."""
from __future__ import annotations

from importlib.metadata import entry_points

from .config import import_attr
from .ports import ROLES, DatasetStore, Evaluator, Ledger, Plugin, PromptStore, Tracer
from .util import EXIT_USAGE, CapabilityError, EvalOpsError

BUILTIN = {
    "git": "evalops.plugins.git:GitPlugin",
    "sql": "evalops.plugins.sql:SqlPlugin",
    "core": "evalops.plugins.core:CorePlugin",
    "none": "evalops.plugins.core:CorePlugin",
    "mlflow": "evalops.plugins.mlflow:MlflowPlugin",
    "langfuse": "evalops.plugins.langfuse:LangfusePlugin",
    "langsmith": "evalops.plugins.langsmith:LangsmithPlugin",
    "logfire": "evalops.plugins.logfire:LogfirePlugin",
}
ROLE_TYPES = {"prompts": PromptStore, "datasets": DatasetStore, "ledger": Ledger, "tracer": Tracer,
              "evaluator": Evaluator}


def available() -> dict[str, str]:
    found = dict(BUILTIN)
    try:
        for ep in entry_points(group="evalops.plugins"):
            found[ep.name] = ep.value
    except Exception:  # noqa: BLE001
        pass
    return found


class Registry:
    """Instantiates each configured plugin once; one plugin may serve several roles."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._instances: dict[str, Plugin] = {}

    def _instance(self, plugin_name: str) -> Plugin:
        if plugin_name not in self._instances:
            spec = available().get(plugin_name, plugin_name if ":" in plugin_name else None)
            if not spec:
                raise EvalOpsError(f"unknown plugin '{plugin_name}' (available: {sorted(available())})", EXIT_USAGE)
            cls = import_attr(spec, self.cfg.root, f"plugin {plugin_name}")
            self._instances[plugin_name] = cls(self.cfg, self.cfg.plugin_options(plugin_name))
        return self._instances[plugin_name]

    def get(self, role: str):
        if role not in ROLES:
            raise EvalOpsError(f"unknown role {role}", EXIT_USAGE)
        plugin_name = self.cfg.plugins[role]
        inst = self._instance(plugin_name)
        if role not in inst.provides or not isinstance(inst, ROLE_TYPES[role]):
            raise CapabilityError(inst.name, f"role '{role}'",
                                  f"It provides {list(inst.provides)}. Choose another plugin for '{role}'.")
        return inst

    def describe(self) -> dict:
        return {role: self.cfg.plugins[role] for role in ROLES}
