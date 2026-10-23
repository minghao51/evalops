"""WP0.3: ``evalops config validate|schema|explain`` — the typed config is reachable from the CLI.

Falsifies, in order: (a) ``config validate`` on the shipped file exits 0 with
``{ok, env, path, errors: []}``, (b) an unknown key exits 2 naming ``gates.metrics.facts/mean.minn``,
(c) a wrong type exits 2 naming ``eval.retries``, (d) ``config schema`` prints JSON, (e) ``config
explain`` reports per-key provenance including an env-block override of ``plugins``, (f) neither
command changes an existing command's exit code or output keys, (g) WP0.3b: a role typo
(``plugins.promps``) is exit 2 with a dotted path at the top level and in selected *and*
unselected env blocks, ``eval.row_limits.max_step`` is rejected while the three real limits pass,
and ``judges.<name>.max_mae`` is accepted, (h) WP0.3c: ``plugins.<role>: null`` is exit 2 with a
dotted path while omitting roles is exit 0, and ``max_mae: null`` validates.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import json
import os
import pathlib
import subprocess
import sys
import tempfile

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent

MINIMAL = """
project: cfg-cli
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
app: {predict_fn: app.hooks:build_predict_fn}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write(text: str) -> pathlib.Path:
    directory = pathlib.Path(tempfile.mkdtemp(prefix="evalops-config-cli-"))
    path = directory / "evalops.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _run(*args: str, cwd: pathlib.Path | None = None, env: dict | None = None) -> tuple[int, dict]:
    full_env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "EVALOPS_ACTOR": "test",
        **(env or {}),
    }
    full_env.pop("EVALOPS_CONFIG", None)
    full_env.pop("EVALOPS_ENV", None)
    full_env.update(env or {})
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(cwd or REPO),
        env=full_env,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise AssertionError(f"non-JSON output (exit {proc.returncode}): {proc.stdout}\n{proc.stderr}") from e
    return proc.returncode, payload


def _run_text(*args: str, cwd: pathlib.Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    full_env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "EVALOPS_ACTOR": "test",
        **(env or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "evalops", *args], cwd=str(cwd or REPO), env=full_env, capture_output=True, text=True
    )


# ---------------------------------------------------------------------------
# (a) the shipped file validates
# ---------------------------------------------------------------------------
def test_config_validate_shipped_file_exits_zero():
    code, out = _run("config", "validate")
    assert code == 0, out
    assert out["ok"] is True
    assert out["env"] == "local"
    assert out["path"].endswith("evalops.yaml")
    assert out["errors"] == []


# ---------------------------------------------------------------------------
# (b)/(c) invalid keys are exit 2 and name the dotted path
# ---------------------------------------------------------------------------
def test_config_validate_unknown_metric_key_exits_two():
    path = _write(MINIMAL + "gates:\n  metrics:\n    facts/mean: {minn: 1}\n")
    code, out = _run("--config", str(path), "config", "validate")
    assert code == 2, out
    assert out["ok"] is False and out["code"] == 2
    assert [issue["path"] for issue in out["errors"]] == ["gates.metrics.facts/mean.minn"]
    assert "gates.metrics.facts/mean.minn" in out["error"]


def test_config_validate_wrong_type_exits_two():
    path = _write(MINIMAL + 'eval: {retries: "many"}\n')
    code, out = _run("--config", str(path), "config", "validate", env={"EVALOPS_CONFIG": str(path)})
    assert code == 2, out
    assert [issue["path"] for issue in out["errors"]] == ["eval.retries"]
    assert out["path"] == str(path) or out["path"].endswith("evalops.yaml")


def test_config_validate_accepts_a_judge_max_mae():
    """WP0.3b (1): ``judges.<name>.max_mae`` is a shipped calibration setting, not a typo."""
    path = _write(MINIMAL + "judges:\n  helpful: {min_agreement: 0.85, max_mae: 0.15, threshold: 0.5}\n")
    code, out = _run("--config", str(path), "config", "validate")
    assert code == 0, out
    assert out["ok"] is True and out["errors"] == []
    # WP0.3c: an explicit `null` is "threshold not set", not a usage error (and not a crash later).
    null_mae = _write(MINIMAL + "judges:\n  helpful: {label: helpful, max_mae: null, threshold: 0.5}\n")
    code, out = _run("--config", str(null_mae), "config", "validate")
    assert code == 0, out
    assert out["ok"] is True and out["errors"] == []


def test_config_validate_rejects_a_null_plugin_role_but_accepts_omitted_roles():
    """WP0.3c: the schema says `type: string`; the loader agrees — omitted ok, explicit null is exit 2."""
    null_role = _write(MINIMAL.replace("prompts: git", "prompts: null"))
    code, out = _run("--config", str(null_role), "config", "validate")
    assert code == 2, out
    assert out["ok"] is False and out["code"] == 2
    assert [issue["path"] for issue in out["errors"]] == ["plugins.prompts"]
    assert "plugins.prompts" in out["error"]
    # an empty `plugins` block, or none at all, is still exit 0: DEFAULT_PLUGINS fills the roles
    empty = MINIMAL.replace(
        "plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}", "plugins: {}"
    )
    absent = "project: cfg-cli\napp: {predict_fn: app.hooks:build_predict_fn}\n"
    for text in (empty, absent):
        code, out = _run("--config", str(_write(text)), "config", "validate")
        assert code == 0, out
        assert out["ok"] is True and out["errors"] == []


def test_config_validate_rejects_a_plugin_role_typo_with_a_dotted_path():
    """WP0.3b (2a): the role names are a closed set; the typo is a structured exit-2 error."""
    path = _write(MINIMAL.replace("prompts: git", "promps: git"))
    code, out = _run("--config", str(path), "config", "validate")
    assert code == 2, out
    assert out["ok"] is False and out["code"] == 2
    assert [issue["path"] for issue in out["errors"]] == ["plugins.promps"]
    assert "plugins.promps" in out["error"]


def test_config_validate_rejects_a_plugin_role_typo_in_every_env_block():
    """WP0.3b (2b/2c): the selected block and an unselected block both report a dotted path."""
    selected = MINIMAL + (
        "environments:\n  local: {plugins: {ledger: sql, promps: git}}\n  other: {plugins: {promps: git}}\n"
    )
    code, out = _run("--config", str(_write(selected)), "config", "validate", env={"EVALOPS_ENV": "local"})
    assert code == 2, out
    assert [issue["path"] for issue in out["errors"]] == ["plugins.promps", "environments.other.plugins.promps"]
    unselected = MINIMAL + "environments:\n  local: {}\n  other: {plugins: {promps: git}}\n"
    code, out = _run("--config", str(_write(unselected)), "config", "validate", env={"EVALOPS_ENV": "local"})
    assert code == 2, out
    assert [issue["path"] for issue in out["errors"]] == ["environments.other.plugins.promps"]


def test_config_validate_rejects_a_row_limits_typo():
    """WP0.3b (3): ``max_step`` must not silently disable the per-row circuit breaker."""
    path = _write(MINIMAL + "eval: {row_limits: {max_step: 25}}\n")
    code, out = _run("--config", str(path), "config", "validate")
    assert code == 2, out
    assert [issue["path"] for issue in out["errors"]] == ["eval.row_limits.max_step"]
    ok = _write(MINIMAL + "eval: {row_limits: {max_steps: 25, max_tokens: 100000, max_seconds: 180}}\n")
    code, out = _run("--config", str(ok), "config", "validate")
    assert code == 0, out


def test_config_validate_reports_every_problem_at_once():
    path = _write(MINIMAL + 'gates:\n  metrics:\n    facts/mean: {minn: 1}\neval: {retries: "many"}\n')
    code, out = _run("--config", str(path), "config", "validate")
    assert code == 2
    assert sorted(issue["path"] for issue in out["errors"]) == ["eval.retries", "gates.metrics.facts/mean.minn"]


def test_existing_commands_also_refuse_an_invalid_config():
    """The typed model is not a `config`-only gate: `plugins` fails the same way."""
    path = _write(MINIMAL + 'eval: {retries: "many"}\n')
    code, out = _run("--config", str(path), "plugins")
    assert code == 2 and out["code"] == 2
    assert [issue["path"] for issue in out["errors"]] == ["eval.retries"]


# ---------------------------------------------------------------------------
# (d) schema
# ---------------------------------------------------------------------------
def test_config_schema_prints_the_generated_schema():
    code, out = _run("config", "schema")
    assert code == 0, out
    schema = out["schema"]
    assert schema["title"] == "evalops configuration"
    assert schema["additionalProperties"] is False
    committed = json.loads((REPO / "docs" / "evalops.schema.json").read_text(encoding="utf-8"))
    assert schema == committed
    # `--json` is the contract (human output is free-form per CONTRACT §2); it stays one document.
    assert out["ok"] is True and out["env"] == "local"


def test_config_schema_is_pipeable():
    """`evalops config schema | head -5` must print the schema, not a crash or an empty stream."""
    proc = _run_text("config", "schema")
    assert proc.returncode == 0, proc.stderr
    head = proc.stdout.splitlines()[:5]
    assert any("$defs" in line or "additionalProperties" in line for line in head), head


# ---------------------------------------------------------------------------
# (e) explain: resolved data + provenance
# ---------------------------------------------------------------------------
def test_config_explain_reports_env_override_provenance():
    text = (
        MINIMAL
        + "paths: {prompts: prompts}\n"
        + "environments:\n  local: {}\n  mlflow:\n    plugins: {ledger: mlflow}\n"
        + "    cost: {budgets: {per_run_usd: 2}}\n"
    )
    path = _write(text)
    code, out = _run("--config", str(path), "config", "explain", env={"EVALOPS_ENV": "mlflow"})
    assert code == 0, out
    assert out["env"] == "mlflow"
    provenance = out["provenance"]
    assert provenance["plugins.ledger"] == "env:mlflow"  # the key the env block moved
    assert provenance["plugins"] == "env:mlflow"  # the section it lives in
    assert provenance["plugins.prompts"] == "top"  # a sibling the env block did not touch
    assert provenance["project"] == "top"
    assert provenance["paths"] == "top"  # a whole section from the file (sub-keys only when moved)
    assert provenance["paths.locks"] == "default"  # not in the file at all
    assert out["data"]["plugins"]["ledger"] == "mlflow"
    assert out["data"]["plugins"]["prompts"] == "git"  # untouched sibling from the top level
    assert "environments" not in out["data"]
    # local (no env block) sees every section as coming from the file, or from the model defaults
    code, local = _run("--config", str(path), "config", "explain", env={"EVALOPS_ENV": "local"})
    assert code == 0 and local["provenance"]["plugins"] == "top"
    assert local["provenance"]["cost"] == "default"  # only the mlflow block sets `cost`
    assert "plugins.ledger" not in local["provenance"]  # no sub-key entries when the env block is empty


def test_config_explain_default_provenance_covers_defaulted_keys():
    path = _write("project: cfg-cli\napp: {predict_fn: app.hooks:build_predict_fn}\n")
    code, out = _run("--config", str(path), "config", "explain")
    assert code == 0, out
    for role in ("prompts", "datasets", "ledger", "tracer", "evaluator"):
        assert out["provenance"][f"plugins.{role}"] == "default"
        assert out["provenance"][f"paths.{role}" if role in ("prompts", "datasets") else "paths.locks"] == "default"
    assert all(v in ("top", "default") for v in out["provenance"].values())
    assert out["data"]["app"] == {"predict_fn": "app.hooks:build_predict_fn"}


# ---------------------------------------------------------------------------
# (f) --help / usage stays exit 2 for a missing sub-command, and old commands are untouched
# ---------------------------------------------------------------------------
def test_config_without_a_subcommand_is_a_usage_error():
    proc = _run_text("config")
    assert proc.returncode == 2


def test_existing_plugins_command_shape_is_unchanged():
    code, out = _run("plugins")
    assert code == 0
    assert out["ok"] is True and out["env"] == "local"
    assert set(out["roles"]) == {"prompts", "datasets", "ledger", "tracer", "evaluator"}
    assert "available" in out and "summary" in out
