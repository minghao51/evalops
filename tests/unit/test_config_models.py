"""WP0.3: ``evalops.yaml`` is typed (pydantic v2) + the committed JSON Schema does not drift.

Falsifies, in order: (a) the shipped ``evalops.yaml`` validates and keeps the v0.3 runtime view,
(b) an unknown key is exit 2 naming the *full dotted path* (``gates.metrics.facts/mean.minn``),
(c) a wrong scalar type is exit 2 naming the key (``eval.retries``), (d) the selected env block is
merged before validation while an unselected block is still validated, (e) ``${VAR}`` expansion can
only substitute text, never structure, (f) the ``Stage``/``ToolMode`` literals cannot drift from
``run.STAGES``/``run.TOOL_MODES``, (g) ``docs/evalops.schema.json`` is byte-identical to the
generated schema, (h) the closed key sets are ``plugins`` (five roles, so ``plugins.promps`` is
exit 2 with a dotted path at the top level and in every env block) and ``eval.row_limits``
(``max_steps``/``max_tokens``/``max_seconds``), plus ``judges.<name>.max_mae`` is a real
calibration setting that reaches ``calibrate``, (i) the schema and the loader agree that a plugin
role is an *optional string* — omitted is fine, an explicit ``null`` is exit 2 — and (j) an explicit
``max_mae: null`` validates and means "no MAE gate" instead of crashing ``calibrate``.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace
from typing import get_args

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))

import yaml  # noqa: E402

from evalops import config as config_mod  # noqa: E402
from evalops import run  # noqa: E402
from evalops.config import models as models_mod  # noqa: E402
from evalops.config import schema as schema_mod  # noqa: E402
from evalops.util import EXIT_USAGE, EvalOpsError  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmpdir() -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix="evalops-config-"))


def _raises(fn):
    """Run ``fn``, expect it to raise, and return the (typed) ``EvalOpsError``."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the assertions below inspect the type and code
        assert isinstance(e, EvalOpsError), e
        return e
    raise AssertionError("expected the call to raise, it returned normally")


def _cfg(data, env=None):
    """Build a Config on a temp root, with EVALOPS_ENV set for the duration of the call."""
    previous = os.environ.get("EVALOPS_ENV")
    if env is None:
        os.environ.pop("EVALOPS_ENV", None)
    else:
        os.environ["EVALOPS_ENV"] = env
    try:
        return config_mod.Config(data, _tmpdir())
    finally:
        if previous is None:
            os.environ.pop("EVALOPS_ENV", None)
        else:
            os.environ["EVALOPS_ENV"] = previous


def _error(data, env=None):
    return _raises(lambda: _cfg(data, env))


def _paths(e):
    return [issue["path"] for issue in (e.details.get("errors") or [])]


def _calibration_checks(rule):
    """Run ``calibrate`` with a stubbed evaluator/ledger and return its ``checks`` list.

    Unit level only: no dataset, no scorer, no ledger write. It proves which threshold checks the
    *resolved* value of ``judges.<name>`` produces (both rows have mae 0.2, agreement 1.0).
    """
    from evalops import calibrate as calibrate_mod

    rows = [
        SimpleNamespace(expectations={"human_labels": {"helpful": 0.8}}, scores={"helpful": 1.0}),
        SimpleNamespace(expectations={"human_labels": {"helpful": 0.8}}, scores={"helpful": 1.0}),
    ]
    cfg = _cfg({**BASE, "app": {"judge_meta": "os:getcwd"}, "judges": {"helpful": rule}})
    ledger = SimpleNamespace(log=lambda *args, **kwargs: None)
    real_evaluate, real_registry = calibrate_mod.evaluate, calibrate_mod.Registry
    calibrate_mod.evaluate = lambda *args, **kwargs: {"run_ids": [], "group": "unit", "_rows": [rows]}
    calibrate_mod.Registry = lambda cfg_: SimpleNamespace(get=lambda role: ledger)
    try:
        out = calibrate_mod.calibrate(cfg, "judge_calibration")
    finally:
        calibrate_mod.evaluate, calibrate_mod.Registry = real_evaluate, real_registry
    assert out["judges"]["helpful"]["mae"] == 0.2, out["judges"]
    return out["checks"]


def _calibration_check(rule):
    """The single ``max_mae`` check ``calibrate`` emits for ``rule`` (exactly one, or the test fails)."""
    checks = [c for c in _calibration_checks(rule) if c["check"].startswith("helpful: mae")]
    assert len(checks) == 1, checks
    return checks[0]


BASE = {"project": "cfg-test", "app": {"predict_fn": "app.hooks:build_predict_fn"}}


# ---------------------------------------------------------------------------
# (a) the shipped file validates and the runtime view is unchanged
# ---------------------------------------------------------------------------
def test_shipped_evalops_yaml_validates():
    cfg = config_mod.load(str(REPO / "evalops.yaml"))
    assert cfg.env in ("local", os.getenv("EVALOPS_ENV", "local"))
    assert cfg.plugins == {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "none", "evaluator": "core"}
    assert "environments" not in cfg.data  # removed from the runtime view, as in v0.3
    assert cfg.data["project"] == "support-agent"
    assert cfg.section("eval")["retries"] == 3
    assert cfg.section("gates")["metrics"]["correctness/mean"] == {"min": 0.8, "max_drop": 0.03}
    assert cfg.plugin_options("sql")["url"].startswith("sqlite:///")
    assert cfg.path("locks") == cfg.root / ".evalops"
    assert isinstance(cfg.data, dict)


def test_legacy_config_shim_is_intact():
    """`from .config import load, Config` and the v0.3 helpers must keep working verbatim."""
    assert callable(config_mod.load) and callable(config_mod.import_attr)
    assert config_mod._expand({"a": "${EVALOPS_TEST_X:-dflt}"}) == {"a": "dflt"}
    assert config_mod._merge({"p": {"a": 1, "b": 2}}, {"p": {"b": 3}}) == {"p": {"a": 1, "b": 3}}
    cfg = _cfg({**BASE, "paths": {"prompts": "p2"}})
    assert cfg.section("paths") == {"prompts": "p2"}
    assert cfg.path("prompts") == cfg.root / "p2"
    assert cfg.path("cassettes") == cfg.root / "cassettes"  # DEFAULT_PATHS, untouched
    assert cfg.lock_dir.is_dir()
    assert cfg.data["app"] == {"predict_fn": "app.hooks:build_predict_fn"}


def test_unknown_plugin_role_is_a_dotted_path_error():
    """WP0.3b: the role names are a closed set, so a typo is a *structured* config error.

    Superseded the v0.3 "unknown plugin roles [...]" message: same exit code (2), but the
    offending role now has a dotted path the user can edit, like every other validation issue.
    """
    e = _error({**BASE, "plugins": {"prompts": "git", "promps": "git"}})
    assert isinstance(e, EvalOpsError) and e.code == EXIT_USAGE
    assert _paths(e) == ["plugins.promps"]
    assert "promps" in str(e)


# ---------------------------------------------------------------------------
# (b) unknown key -> exit 2 with the full dotted path
# ---------------------------------------------------------------------------
def test_unknown_metric_key_names_the_dotted_path():
    e = _error({**BASE, "gates": {"metrics": {"facts/mean": {"minn": 1}}}})
    assert e.code == EXIT_USAGE
    assert "gates.metrics.facts/mean.minn" in str(e)
    assert _paths(e) == ["gates.metrics.facts/mean.minn"]


def test_unknown_top_level_and_nested_keys_name_their_path():
    assert _paths(_error({**BASE, "gaets": {}})) == ["gaets"]
    assert _paths(_error({**BASE, "cost": {"budgets": {"per_run_usd": 1, "per_week_usd": 2}}})) == [
        "cost.budgets.per_week_usd"
    ]
    assert _paths(_error({**BASE, "judges": {"helpful": {"min_agreement": 0.8, "min_kappaa": 0.5}}})) == [
        "judges.helpful.min_kappaa"
    ]


def test_metric_rule_accepts_exactly_the_six_documented_keys():
    ok = {**BASE, "gates": {"metrics": {"tool_trajectory/mean": {"min": 0.9, "min_ci_lower": 0.85}}}}
    assert _cfg(ok).section("gates")["metrics"]["tool_trajectory/mean"]["min_ci_lower"] == 0.85
    for key in ("min", "max", "max_drop", "max_increase_pct", "min_ci_lower", "min_ci_lower_bootstrap"):
        assert _cfg({**BASE, "gates": {"metrics": {"m": {key: 1}}}}).section("gates")["metrics"]["m"] == {key: 1}
    assert _paths(_error({**BASE, "gates": {"metrics": {"m": {"p95": 1}}}})) == ["gates.metrics.m.p95"]


def test_reliability_rule_accepts_exactly_the_four_documented_keys():
    """``min_pass_hat_k_wilson`` is a real rule; a typo next to it is still exit 2."""
    ok = {**BASE, "gates": {"reliability": {"success": [{"metric": "m", "min": 1.0}], "min_pass_hat_k_wilson": 0.4}}}
    rel = _cfg(ok).section("gates")["reliability"]
    assert rel["min_pass_hat_k_wilson"] == 0.4 and rel["success"] == [{"metric": "m", "min": 1.0}]
    for key in ("min_pass_hat_k", "max_drop_pass_hat_k", "min_pass_hat_k_wilson"):
        assert _cfg({**BASE, "gates": {"reliability": {key: 1}}}).section("gates")["reliability"] == {key: 1}
    assert _paths(_error({**BASE, "gates": {"reliability": {"min_pass_hat_kk": 1}}})) == [
        "gates.reliability.min_pass_hat_kk"
    ]


# ---------------------------------------------------------------------------
# (c) wrong types -> exit 2 naming the key
# ---------------------------------------------------------------------------
def test_wrong_scalar_types_name_their_path():
    e = _error({**BASE, "eval": {"retries": "many"}})
    assert e.code == EXIT_USAGE and "eval.retries" in str(e) and _paths(e) == ["eval.retries"]
    assert _paths(_error({**BASE, "plugins": {"ledger": 5}})) == ["plugins.ledger"]
    assert _paths(_error({**BASE, "tools": {"mode_by_stage": {"dev": "live-ish"}}})) == ["tools.mode_by_stage.dev"]
    assert _paths(_error({**BASE, "app": {"scorers": "no_colon_here"}})) == ["app.scorers"]


def test_wrong_stage_and_mode_keys_are_config_errors():
    assert _paths(_error({**BASE, "eval": {"repeats": {"prod": 1}}})) == ["eval.repeats.prod"]
    assert _paths(_error({**BASE, "tools": {"on_miss": "ignore"}})) == ["tools.on_miss"]


# ---------------------------------------------------------------------------
# (d) env blocks: merged before validation, and validated even when unselected
# ---------------------------------------------------------------------------
def test_env_block_is_merged_over_the_top_level():
    data = {
        **BASE,
        "default_env": "local",
        "plugins": {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "none", "evaluator": "core"},
        "environments": {
            "local": {},
            "mlflow": {"plugins": {"prompts": "mlflow", "ledger": "mlflow"}},
        },
    }
    assert _cfg(data).env == "local"
    mlflow = _cfg(data, env="mlflow")
    assert mlflow.env == "mlflow"
    assert mlflow.plugins["ledger"] == "mlflow" and mlflow.plugins["datasets"] == "git"
    assert mlflow.data["plugins"]["prompts"] == "mlflow"


def test_unselected_env_block_is_still_validated():
    data = {**BASE, "environments": {"local": {}, "other": {"eval": {"retires": 1}}}}
    e = _error(data)
    assert e.code == EXIT_USAGE and _paths(e) == ["environments.other.eval.retires"]


def test_selected_env_block_error_is_reported_without_the_prefix():
    data = {**BASE, "environments": {"local": {"eval": {"retires": 1}}}}
    assert _paths(_error(data)) == ["eval.retires"]


def test_nested_environments_block_is_rejected():
    data = {**BASE, "environments": {"local": {}, "other": {"environments": {"deep": {}}}}}
    assert "environments.other.environments" in _paths(_error(data))


def test_plugin_role_typo_fails_in_selected_and_unselected_env_blocks():
    """WP0.3b (2c): the role constraint must participate in the env-block passes too."""
    baseline = {"prompts": "git", "datasets": "git", "ledger": "sql", "tracer": "none", "evaluator": "core"}
    in_selected = {**BASE, "environments": {"local": {"plugins": {**baseline, "promps": "git"}}}}
    assert _paths(_error(in_selected)) == ["plugins.promps"]
    in_unselected = {**BASE, "environments": {"local": {}, "other": {"plugins": {"promps": "git"}}}}
    assert _paths(_error(in_unselected)) == ["environments.other.plugins.promps"]
    # a *partial* env block stays legal: the roles it does not name inherit the top level
    partial = {**BASE, "plugins": baseline, "environments": {"local": {}, "mlflow": {"plugins": {"ledger": "mlflow"}}}}
    assert _cfg(partial, env="mlflow").plugins["ledger"] == "mlflow"


def test_plugin_roles_and_the_registry_roles_cannot_drift():
    """The five role names are spelled out in ``models.py`` (no import cycle); pin them to ports."""
    from evalops.ports import ROLES

    assert tuple(models_mod.PluginsConfig.model_fields) == ROLES
    assert all(field.is_required() is False for field in models_mod.PluginsConfig.model_fields.values())


def test_unknown_environment_keeps_the_legacy_message():
    e = _error({**BASE, "environments": {"local": {}}}, env="nope")
    assert e.code == EXIT_USAGE and "unknown environment 'nope'" in str(e)


# ---------------------------------------------------------------------------
# (e) expansion: text only, before validation
# ---------------------------------------------------------------------------
def test_env_expansion_cannot_inject_structure():
    os.environ["EVALOPS_TEST_RETRIES"] = "7"
    os.environ["EVALOPS_TEST_LIST"] = "[1, 2]"
    try:
        # a scalar placeholder coerces into a scalar field, as it did in v0.3
        expanded = config_mod._expand({**BASE, "eval": {"retries": "${EVALOPS_TEST_RETRIES}"}})
        assert expanded["eval"]["retries"] == "7"
        assert _cfg(expanded).section("eval")["retries"] == "7"  # the runtime view keeps the raw text
        assert config_mod.EvalopsConfig.model_validate(expanded).eval.retries == 7
        # a placeholder never becomes a list/dict, so a list field rejects the substituted text
        injected = config_mod._expand({**BASE, "traces": {"instrument": "${EVALOPS_TEST_LIST}"}})
        assert injected["traces"]["instrument"] == "[1, 2]"
        assert _paths(_error(injected)) == ["traces.instrument"]
    finally:
        del os.environ["EVALOPS_TEST_RETRIES"]
        del os.environ["EVALOPS_TEST_LIST"]


# ---------------------------------------------------------------------------
# (f) literals cannot drift from the runtime constants
# ---------------------------------------------------------------------------
def test_stage_and_tool_mode_literals_match_run():
    assert get_args(models_mod.Stage) == run.STAGES
    assert get_args(models_mod.ToolMode) == run.TOOL_MODES


# ---------------------------------------------------------------------------
# (g) the committed schema does not drift
# ---------------------------------------------------------------------------
def test_committed_schema_is_byte_identical_to_the_generated_one():
    path = REPO / "docs" / "evalops.schema.json"
    assert path.exists(), "docs/evalops.schema.json is missing; run `make schema`"
    assert path.read_text(encoding="utf-8") == schema_mod.schema_text(), "schema drift: run `make schema`"


def test_schema_is_regenerable_and_describes_the_shipped_config():
    schema = schema_mod.config_schema()
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["additionalProperties"] is False
    assert schema["required"] == ["project", "app"]
    assert "gates.metrics.facts/mean.minn" not in schema  # keys are user data, not schema properties
    assert schema["$defs"]["MetricRule"]["additionalProperties"] is False
    assert sorted(schema["$defs"]["MetricRule"]["properties"]) == [
        "max",
        "max_drop",
        "max_increase_pct",
        "min",
        "min_ci_lower",
        "min_ci_lower_bootstrap",
    ]
    assert sorted(schema["$defs"]["ReliabilityRule"]["properties"]) == [
        "max_drop_pass_hat_k",
        "min_pass_hat_k",
        "min_pass_hat_k_wilson",
        "success",
    ]
    # WP0.3b: the closed key sets are in the schema as objects with exactly these properties
    plugins = schema["$defs"]["PluginsConfig"]
    assert plugins["additionalProperties"] is False
    assert sorted(plugins["properties"]) == ["datasets", "evaluator", "ledger", "prompts", "tracer"]
    assert schema["properties"]["plugins"] == {"$ref": "#/$defs/PluginsConfig"}
    limits = schema["$defs"]["RowLimits"]
    assert limits["additionalProperties"] is False
    assert sorted(limits["properties"]) == ["max_seconds", "max_steps", "max_tokens"]
    assert schema["$defs"]["JudgeRule"]["properties"]["max_mae"]["default"] is None
    # env blocks are partial overlays: same properties as the top level, but nothing required
    block = schema["$defs"]["EnvironmentBlock"]
    assert "required" not in block and block["additionalProperties"] is False
    assert block["properties"] == {k: v for k, v in schema["properties"].items() if k != "environments"}
    assert schema["properties"]["environments"]["additionalProperties"] == {"$ref": "#/$defs/EnvironmentBlock"}

    tmp = _tmpdir() / "nested" / "schema.json"
    assert schema_mod.write_schema(tmp) == tmp
    assert tmp.read_text(encoding="utf-8") == schema_mod.schema_text()


def test_shipped_config_is_a_fixed_point_of_the_models():
    """The shipped file must satisfy the models *without* a single acceptance warning."""
    data = yaml.safe_load((REPO / "evalops.yaml").read_text(encoding="utf-8"))
    merged = config_mod._merge({k: v for k, v in data.items() if k != "environments"}, data["environments"]["local"])
    assert models_mod.validate_sections([((), merged)]) == []


# ---------------------------------------------------------------------------
# (h) WP0.3b: calibration thresholds and the row-limits circuit breaker
# ---------------------------------------------------------------------------
def test_judge_max_mae_is_accepted_and_gates_calibration():
    """``judges.<name>.max_mae`` is a real setting (``calibrate`` reads it); it must not be exit 2."""
    cfg = _cfg({**BASE, "judges": {"helpful": {"label": "helpful", "max_mae": 0.15, "threshold": 0.5}}})
    assert cfg.section("judges")["helpful"]["max_mae"] == 0.15  # accepted, and the raw value is kept
    # the resolved value reaches calibrate's comparison: mae 0.2 passes at 0.25 and fails at 0.15
    assert _calibration_check({"label": "helpful", "max_mae": 0.25}) == {
        "check": "helpful: mae <= 0.25",
        "passed": True,
        "candidate": 0.2,
    }
    failed = _calibration_check({"label": "helpful", "max_mae": 0.15})
    assert failed["check"] == "helpful: mae <= 0.15" and failed["passed"] is False
    assert _paths(_error({**BASE, "judges": {"helpful": {"max_mae": 0.8, "max_maee": 0.8}}})) == [
        "judges.helpful.max_maee"
    ]


def test_row_limits_accepts_only_the_three_keys_the_runtime_reads():
    """``max_step`` (typo for ``max_steps``) used to silently disable the circuit breaker."""
    ok = _cfg({**BASE, "eval": {"row_limits": {"max_steps": 25, "max_tokens": 100000, "max_seconds": 180}}})
    assert ok.section("eval")["row_limits"] == {"max_steps": 25, "max_tokens": 100000, "max_seconds": 180}
    for key in ("max_steps", "max_tokens", "max_seconds"):
        assert _cfg({**BASE, "eval": {"row_limits": {key: 1}}}).section("eval")["row_limits"] == {key: 1}
    assert tuple(models_mod.RowLimits.model_fields) == ("max_steps", "max_tokens", "max_seconds")
    e = _error({**BASE, "eval": {"row_limits": {"max_step": 25}}})
    assert e.code == EXIT_USAGE and _paths(e) == ["eval.row_limits.max_step"]
    assert "eval.row_limits.max_step" in str(e)


# ---------------------------------------------------------------------------
# (i) WP0.3c: schema and runtime agree on `plugins.<role>` (omitted ok, null is an error)
# ---------------------------------------------------------------------------
def test_schema_plugin_roles_are_optional_strings_without_a_null_arm():
    """The schema must not advertise a ``null`` the loader rejects (a schema-accepted config is invalid)."""
    schema = schema_mod.config_schema()
    plugins = schema["$defs"]["PluginsConfig"]
    assert plugins["type"] == "object" and plugins["additionalProperties"] is False
    assert sorted(plugins["properties"]) == ["datasets", "evaluator", "ledger", "prompts", "tracer"]
    assert "required" not in plugins  # every role stays optional: omitted means "use the default"
    for role, property_schema in plugins["properties"].items():
        # string-only: no anyOf/oneOf arm, no `type: [string, null]`, and no null default
        assert property_schema["type"] == "string", (role, property_schema)
        assert "anyOf" not in property_schema and "oneOf" not in property_schema, (role, property_schema)
        assert "default" not in property_schema, (role, property_schema)
        assert property_schema["title"] == role.title(), (role, property_schema)
    # the same definition is what the top level and every env overlay reference
    assert schema["properties"]["plugins"] == {"$ref": "#/$defs/PluginsConfig"}
    assert schema["$defs"]["EnvironmentBlock"]["properties"]["plugins"] == {"$ref": "#/$defs/PluginsConfig"}
    # ... and the model still refuses the value the schema no longer offers
    assert _paths(_error({**BASE, "plugins": {"prompts": None}})) == ["plugins.prompts"]


def test_explicit_null_plugin_role_is_exit_two_while_omitted_roles_are_fine():
    """``plugins.<role>: null`` names nothing; omitting the role falls back to ``DEFAULT_PLUGINS``."""
    for role in models_mod.PluginsConfig.model_fields:
        e = _error({**BASE, "plugins": {role: None}})
        assert e.code == EXIT_USAGE and _paths(e) == [f"plugins.{role}"], (role, e.details)
        assert f"plugins.{role}" in str(e)
    # a null inside the *selected* env block is merged first, so the path is the merged one
    e = _error({**BASE, "environments": {"local": {"plugins": {"ledger": None}}}}, env="local")
    assert _paths(e) == ["plugins.ledger"]
    # omitted roles (an empty block, or no `plugins` key at all) are the supported way to say default
    assert _cfg({**BASE, "plugins": {}}).plugins["ledger"] == "sql"
    assert _cfg(BASE).plugins["prompts"] == "git"
    assert models_mod.validate_sections([((), BASE)]) == []


# ---------------------------------------------------------------------------
# (j) WP0.3c: `max_mae: null` means "no MAE gate", not a crash
# ---------------------------------------------------------------------------
def test_null_max_mae_validates_and_calibration_skips_the_mae_check():
    """Previously ``float <= None`` TypeError: null/omitted is "threshold not set", so no check."""
    cfg = _cfg({**BASE, "judges": {"helpful": {"label": "helpful", "max_mae": None}}})
    assert cfg.section("judges")["helpful"]["max_mae"] is None  # validates, raw value kept
    assert _calibration_checks({"label": "helpful", "max_mae": None}) == []  # no check, no TypeError
    # numeric max_mae is untouched: still exactly one check, same wording, same verdict
    assert _calibration_check({"label": "helpful", "max_mae": 0.25}) == {
        "check": "helpful: mae <= 0.25",
        "passed": True,
        "candidate": 0.2,
    }
    assert _calibration_check({"label": "helpful", "max_mae": 0.15})["passed"] is False
    # an omitted key behaves the same as an explicit null (both are "not set")
    assert _calibration_checks({"label": "helpful"}) == []


def test_null_threshold_falls_back_to_the_default_instead_of_raising():
    """Same defect class as ``max_mae: null``: ``float(None)`` used to raise from calibration.

    A null threshold means the documented 0.5 default (1.0 vs 0.8 agrees), not a crash; a numeric
    threshold still binarizes as before (0.9 splits 1.0 from 0.8, so agreement drops to 0).
    """
    assert _calibration_checks({"label": "helpful", "threshold": None, "min_agreement": 0.9}) == [
        {"check": "helpful: agreement >= 0.9", "passed": True, "candidate": 1.0}
    ]
    assert _calibration_checks({"label": "helpful", "threshold": 0.9, "min_agreement": 0.9}) == [
        {"check": "helpful: agreement >= 0.9", "passed": False, "candidate": 0.0}
    ]
