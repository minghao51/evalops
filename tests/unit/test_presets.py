"""WP2.3: the seven presets, ``evalops init --preset``, and the capability matrix generated from code.

Falsifies, in order: (a) there are exactly the seven D8 presets and each one wires the §A2 plugin
table, (b) every preset renders a yaml that re-parses to its own data and passes
``evalops config validate`` in both its own environment and ``local`` (H1, no hand-written yaml in
this module), (c) ``init`` writes exactly one file and refuses an existing one with exit 2 unless
``--force``, is byte-deterministic, and an unknown preset is exit 2 naming all seven (H2), (d) the
offline ``evalops doctor`` outcome is pinned per preset -- ``local`` healthy, every vendor preset
unavailable with the error naming the plugin and the missing SDK (D1), (e) the ``local`` doctor pin
is mutation-sensitive: flipping the preset table's ledger wiring turns it red (H3), (f)
``evalops capabilities --json`` is deterministic, golden-pinned and derived from the plugin classes'
own ``provides`` (C1), (g) ``docs/CAPABILITIES.md`` regenerates with zero diff, is produced by
``make capabilities``, and a wiring or extras change without regeneration turns the drift probe red
(H4/C2), (h) no preset renders a literal URL or credential -- every one is a ``${VAR:-placeholder}``
interpolation, (i) every extra a preset or the matrix names is a real ``pyproject.toml`` extra.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
Regenerate the capabilities golden with ``EVALOPS_UPDATE_GOLDENS=1 pytest -q -k capabilities``.
"""

import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Any

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(TESTS_DIR / "unit"))

import _projects as proj  # noqa: E402
import yaml  # noqa: E402

from evalops import presets  # noqa: E402
from evalops.ports import ROLES  # noqa: E402

#: D8 (docs/IMPLEMENTATION_MAP.md), in decision order.
D8 = ("local", "mlflow", "databricks", "langfuse", "langsmith", "logfire", "mixed")
#: Plugins that need no external service, so offline ``doctor`` reports them healthy.
NO_SERVICE = frozenset({"git", "sql", "core", "none"})
GOLDENS = TESTS_DIR / "fixtures" / "envelope"
CAPABILITIES_DOC = REPO / "docs" / "CAPABILITIES.md"
SRC = REPO / "src"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmpdir(prefix: str = "evalops-wp23-") -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def _env(pythonpath: pathlib.Path | None = None, extra: dict | None = None) -> dict:
    """A subprocess environment: this repo's ``src`` on PYTHONPATH, no stale evalops variables."""
    values = {
        **os.environ,
        "PYTHONPATH": str(pythonpath or SRC),
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
        **(extra or {}),
    }
    for stale in ("EVALOPS_CONFIG", "EVALOPS_REDACT", "EVALOPS_ALLOW_FROZEN_CHANGE"):
        if stale not in (extra or {}):
            values.pop(stale, None)
    for name, value in (extra or {}).items():
        if value is None:  # an explicit None means "this variable is not set"
            values.pop(name, None)
    return values


def _run_raw(
    root: pathlib.Path,
    *args: str,
    pythonpath: pathlib.Path | None = None,
    extra_env: dict | None = None,
    as_json: bool = True,
) -> subprocess.CompletedProcess:
    argv = [sys.executable, "-m", "evalops"] + (["--json"] if as_json else []) + list(args)
    return subprocess.run(argv, cwd=str(root), env=_env(pythonpath, extra_env), capture_output=True, text=True)


def _run(
    root: pathlib.Path, *args: str, pythonpath: pathlib.Path | None = None, extra_env: dict | None = None
) -> tuple[int, dict[str, Any]]:
    """Run the real CLI as a subprocess -> (exit code, envelope); stdout must be one document."""
    proc = _run_raw(root, *args, pythonpath=pythonpath, extra_env=extra_env)
    return proc.returncode, proj.one_document(proc.stdout)


def _strings(value: Any) -> list[str]:
    if isinstance(value, dict):
        return [item for entry in value.values() for item in _strings(entry)]
    if isinstance(value, list):
        return [item for entry in value for item in _strings(entry)]
    return [value] if isinstance(value, str) else []


def _mutated_tree(mutate) -> pathlib.Path:
    """A /tmp copy of ``src`` with ``presets.py`` mutated by ``mutate(text) -> text``."""
    source = (SRC / "evalops" / "presets.py").read_text()
    mutated = mutate(source)
    assert mutated != source, "the mutation changed nothing: the preset table layout moved"
    root = _tmpdir("evalops-wp23-mutant-")
    shutil.copytree(SRC, root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "src" / "evalops" / "presets.py").write_text(mutated)
    return root


def _sub_once(pattern: str, replacement: str):
    """A mutation that must match exactly once (a silent no-op would fake a passing pin)."""

    def mutate(text: str) -> str:
        mutated, count = re.subn(pattern, replacement, text, count=1)
        assert count == 1, f"mutation pattern {pattern!r} matched {count} times"
        return mutated

    return mutate


def _doc_tree(mutate=None) -> pathlib.Path:
    """A /tmp copy of ``src`` **and** the committed ``docs/CAPABILITIES.md``, optionally mutated."""
    root = _mutated_tree(mutate) if mutate else _tmpdir("evalops-wp23-docs-")
    if not (root / "src").exists():
        shutil.copytree(SRC, root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "docs").mkdir(exist_ok=True)
    shutil.copy(CAPABILITIES_DOC, root / "docs" / "CAPABILITIES.md")
    return root


def _drift_probe(root: pathlib.Path) -> int:
    """The drift test itself on ``root``: 0 when ``docs/CAPABILITIES.md`` matches the generator."""
    script = (
        "import pathlib, sys\n"
        "from evalops.presets import capabilities_text\n"
        "sys.exit(0 if pathlib.Path(sys.argv[1]).read_text() == capabilities_text() else 1)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script, str(root / "docs" / "CAPABILITIES.md")],
        cwd=str(root),
        env=_env(root / "src"),
        capture_output=True,
        text=True,
    )
    return proc.returncode


def _golden_matches(tree: pathlib.Path) -> bool:
    """True when ``tree``'s ``capabilities`` envelope still equals the committed golden."""
    proc = _run_raw(_tmpdir(), "capabilities", pythonpath=tree / "src", extra_env={"EVALOPS_ENV": "local"})
    assert proc.returncode == 0, proc.stderr
    golden = GOLDENS / "capabilities.json"
    assert golden.exists(), "regenerate the capabilities golden first"
    return json.loads(proc.stdout) == json.loads(golden.read_text())


def _pyproject_extras() -> set[str]:
    """The extras declared in ``[project.optional-dependencies]`` (no tomllib: the floor is 3.10)."""
    text = (REPO / "pyproject.toml").read_text()
    section = text.split("[project.optional-dependencies]", 1)[1].split("\n[", 1)[0]
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*=", section, re.MULTILINE))


# ---------------------------------------------------------------------------
# (a) the D8 table
# ---------------------------------------------------------------------------
def test_the_seven_presets_are_exactly_the_d8_names():
    assert tuple(presets.PRESETS) == D8
    assert presets.PRESET_NAMES == D8
    assert len(presets.PRESETS) == 7
    for name, spec in presets.PRESETS.items():
        assert spec.name == name
        assert spec.description and not spec.description.startswith("/")
        assert "local" in spec.environments, f"{name} must always render a local environment"
        assert tuple(spec.roles) == ROLES
        # `roles` is derived from the spec's own environment block, never restated by hand
        assert spec.roles == spec.environments[name]["plugins"]


def test_each_preset_wires_the_a2_plugin_table():
    expected = {
        "local": ("git", "git", "sql", "none", "core"),
        "mlflow": ("mlflow", "mlflow", "mlflow", "mlflow", "mlflow"),
        "databricks": ("mlflow", "mlflow", "mlflow", "mlflow", "mlflow"),
        "langfuse": ("langfuse", "langfuse", "sql", "langfuse", "langfuse"),
        "langsmith": ("langsmith", "langsmith", "sql", "langsmith", "langsmith"),
        "logfire": ("git", "git", "sql", "logfire", "core"),
        "mixed": ("git", "git", "sql", "logfire", "core"),
    }
    assert tuple(expected) == D8
    for name, wiring in expected.items():
        assert tuple(presets.PRESETS[name].roles[role] for role in ROLES) == wiring, name
    assert {name: presets.PRESETS[name].extras for name in D8} == {
        "local": (),
        "mlflow": ("mlflow",),
        "databricks": ("databricks",),
        "langfuse": ("langfuse",),
        "langsmith": ("langsmith",),
        "logfire": ("logfire",),
        "mixed": ("logfire",),
    }


# ---------------------------------------------------------------------------
# (b) H1: every preset renders a valid evalops.yaml
# ---------------------------------------------------------------------------
def test_every_preset_renders_a_config_that_validates():
    for name in D8:
        spec = presets.PRESETS[name]
        text = presets.render(spec)  # renders, re-parses and validates through the pydantic models
        assert yaml.safe_load(text) == presets.document(spec), name
        root = _tmpdir()
        code, env_dict = _run(root, "init", "--preset", name)
        assert code == 0, (name, env_dict)
        assert (root / "evalops.yaml").read_text() == text, f"{name}: the CLI wrote something else"
        # exactly one file, and `init` scaffolds nothing else (B1)
        assert sorted(path.name for path in root.iterdir()) == ["evalops.yaml"], name
        # `config validate` passes in the preset's own env *and* in the offline fallback
        for selected in dict.fromkeys(("local", name)):
            code, env_dict = _run(root, "config", "validate", extra_env={"EVALOPS_ENV": selected})
            assert code == 0, (name, selected, env_dict)


def test_rendered_yaml_comments_list_the_legal_plugins_for_the_composition_preset():
    text = presets.render(presets.PRESETS["mixed"])
    for role in ROLES:
        legal = presets.plugins_for_role(role)
        assert legal, role
        line = next(line for line in text.splitlines() if line.strip().startswith("#") and f"{role}:" in line)
        assert all(plugin in line for plugin in legal), (role, line)
    # the Logfire private-SQL caveat (STATE compromise 1) is in every Logfire preset's comments
    for name in ("logfire", "mixed"):
        rendered = presets.render(presets.PRESETS[name])
        assert "compromise 1" in rendered and "private SQL query" in rendered, name
    # vendor-specific notes are rendered where they apply, not in a separate doc
    assert "DATABRICKS_HOST" in presets.render(presets.PRESETS["databricks"])
    assert "exits 6" in presets.render(presets.PRESETS["langsmith"])


# ---------------------------------------------------------------------------
# (c) H2: refusal paths, --force, determinism, unknown preset
# ---------------------------------------------------------------------------
def test_init_refuses_an_existing_file_and_force_overwrites_deterministically():
    root = _tmpdir()
    code, env_dict = _run(root, "init", "--preset", "mixed")
    assert code == 0, env_dict
    assert env_dict["data"]["files_written"] == ["evalops.yaml"]
    original = (root / "evalops.yaml").read_text()

    code, env_dict = _run(root, "init", "--preset", "local")
    assert code == 2 == env_dict["code"], env_dict
    assert "already exists" in env_dict["errors"][0]["message"]
    assert (root / "evalops.yaml").read_text() == original, "the refused write touched the file"

    code, env_dict = _run(root, "init", "--preset", "local", "--force")
    assert code == 0, env_dict
    assert (root / "evalops.yaml").read_text() == presets.render(presets.PRESETS["local"])

    # determinism: the same preset renders byte-identically in two different directories
    other = _tmpdir()
    assert _run(other, "init", "--preset", "local")[0] == 0
    assert (other / "evalops.yaml").read_text() == (root / "evalops.yaml").read_text()
    assert sorted(path.name for path in root.iterdir()) == ["evalops.yaml"]


def test_init_reports_the_extras_and_the_next_three_commands():
    root = _tmpdir()
    code, env_dict = _run(root, "init", "--preset", "logfire")
    assert code == 0, env_dict
    data = env_dict["data"]
    assert data["preset"] == "logfire"
    assert data["files_written"] == ["evalops.yaml"]
    assert data["extras"] == ["logfire"]
    assert len(data["next_steps"]) == 3
    assert "config validate" in data["next_steps"][0]
    assert "doctor" in data["next_steps"][1]
    assert "sync all" in data["next_steps"][2]
    # `init` runs before any config exists, so the envelope reports the resolved (fallback) env
    assert env_dict["env"] == "local"
    assert env_dict["command"] == "init"

    human_root = _tmpdir()
    proc = _run_raw(human_root, "init", "--preset", "logfire", as_json=False)
    assert proc.returncode == 0, proc.stderr
    assert "logfire" in proc.stdout and "config validate" in proc.stdout
    assert all(step in proc.stdout for step in data["next_steps"])


def test_init_unknown_preset_is_exit_two_naming_all_seven():
    root = _tmpdir()
    code, env_dict = _run(root, "init", "--preset", "nope")
    assert code == 2 == env_dict["code"], env_dict
    text = json.dumps(env_dict)
    for name in D8:
        assert name in text, (name, text)
    assert sorted(path.name for path in root.iterdir()) == [], "a rejected init wrote something"


def test_init_refuses_a_missing_target_directory_and_creates_nothing():
    root = _tmpdir()
    missing = root / "nested" / "project"
    code, env_dict = _run(root, "init", "--preset", "local", "--dir", str(missing))
    assert code == 2 == env_dict["code"], env_dict
    assert "does not exist" in env_dict["errors"][0]["message"]
    assert not missing.exists(), "init created a directory it was told to write into"


# ---------------------------------------------------------------------------
# (d) D1 + H3: the offline doctor pins, and their mutation sensitivity
# ---------------------------------------------------------------------------
def test_doctor_pins_the_actual_offline_outcome_of_every_preset():
    declared = _pyproject_extras()
    for name in D8:
        root = _tmpdir()
        assert _run(root, "init", "--preset", name)[0] == 0
        # no EVALOPS_ENV: the rendered `default_env` must select the preset's own environment
        code, env_dict = _run(root, "doctor", extra_env={"EVALOPS_ENV": None})
        assert env_dict["env"] == name, (name, env_dict["env"])
        roles = env_dict["data"]["roles"]
        assert tuple(roles) == ROLES
        # the pip extra to install for this preset is a real one (drift-checked against pyproject)
        assert set(presets.PRESETS[name].extras) <= declared, name
        if name == "local":
            assert code == 0 and env_dict["ok"] is True, env_dict
            assert all(entry["ok"] is True for entry in roles.values()), env_dict
            continue
        assert code == 1 and env_dict["ok"] is False, (name, env_dict)
        for role in ROLES:
            entry = roles[role]
            plugin = presets.PRESETS[name].roles[role]
            assert entry["plugin"] == plugin, (name, role, entry)
            if plugin in NO_SERVICE:
                assert entry["ok"] is True, (name, role, entry)
                continue
            # doctor's real unavailable outcome: the role is down and the error names the SDK
            assert entry["ok"] is False, (name, role, entry)
            assert f"No module named '{plugin}'" in entry["error"], (name, role, entry)
            assert presets.PLUGIN_EXTRAS[plugin] in declared, (name, role)


def test_the_local_doctor_pin_is_mutation_sensitive():
    healthy = _tmpdir()
    assert _run(healthy, "init", "--preset", "local", pythonpath=SRC)[0] == 0
    code, env_dict = _run(healthy, "doctor", pythonpath=SRC, extra_env={"EVALOPS_ENV": None})
    assert code == 0, env_dict  # the control: the unmutated preset is healthy offline

    mutated = _mutated_tree(_sub_once(r'(LOCAL_PLUGINS = \{[^}]*"ledger":\s*)"sql"', r'\1"no_such_plugin"'))
    broken = _tmpdir()
    code, env_dict = _run(broken, "init", "--preset", "local", pythonpath=mutated / "src")
    assert code == 0, env_dict
    wiring = yaml.safe_load((broken / "evalops.yaml").read_text())["environments"]["local"]["plugins"]
    assert wiring["ledger"] == "no_such_plugin", wiring
    code, env_dict = _run(broken, "doctor", pythonpath=mutated / "src", extra_env={"EVALOPS_ENV": None})
    assert code == 1 and env_dict["ok"] is False, env_dict
    ledger = env_dict["data"]["roles"]["ledger"]
    assert ledger["ok"] is False and "no_such_plugin" in ledger["error"], ledger


# ---------------------------------------------------------------------------
# (f) C1: the capability matrix, derived from the plugin classes
# ---------------------------------------------------------------------------
def test_capabilities_envelope_is_deterministic_and_golden_pinned():
    first = _run(_tmpdir(), "capabilities")
    second = _run(_tmpdir(), "capabilities")
    assert first[0] == 0 == second[0]
    assert first[1] == second[1], "capabilities output is not deterministic"
    env_dict = first[1]
    assert env_dict["command"] == "capabilities" and env_dict["env"] == "local"
    payload = env_dict["data"]
    assert tuple(payload["presets"]) == D8
    assert list(payload["plugins"]) == sorted(payload["plugins"])
    assert tuple(payload["roles"]) == ROLES

    golden = GOLDENS / "capabilities.json"
    if os.environ.get("EVALOPS_UPDATE_GOLDENS") == "1":
        GOLDENS.mkdir(parents=True, exist_ok=True)
        golden.write_text(json.dumps(env_dict, indent=2) + "\n")
    assert golden.exists(), "tests/fixtures/envelope/capabilities.json is missing; regenerate the goldens"
    assert json.loads(golden.read_text()) == env_dict, "capabilities drifted from its golden snapshot"


def test_capabilities_matrix_is_read_from_the_plugin_classes():
    from evalops.registry import BUILTIN

    payload = presets.capabilities_payload()
    for registry_name, spec in BUILTIN.items():
        cls = presets._plugin_class(spec)
        row = payload["plugins"][cls.name]
        assert row["provides"] == [role for role in ROLES if role in cls.provides], registry_name
        if registry_name != cls.name:
            assert registry_name in row["aliases"], registry_name
    # spot checks, so a hand-written table wearing a generator's clothes cannot pass
    assert payload["plugins"]["logfire"]["provides"] == ["tracer"]
    assert payload["plugins"]["git"]["provides"] == ["prompts", "datasets"]
    assert payload["plugins"]["core"]["aliases"] == ["none"]
    assert payload["roles"]["ledger"] == ["mlflow", "sql"]
    assert payload["roles"]["tracer"] == ["core", "langfuse", "langsmith", "logfire", "mlflow", "none"]
    # no preset may wire a plugin for a role that plugin does not provide
    for name in D8:
        for role in ROLES:
            assert presets.PRESETS[name].roles[role] in payload["roles"][role], (name, role)


# ---------------------------------------------------------------------------
# (g) C2 + H4: the generated document and its drift sensitivity
# ---------------------------------------------------------------------------
def test_the_capabilities_document_regenerates_with_zero_diff():
    assert CAPABILITIES_DOC.exists(), "docs/CAPABILITIES.md is missing; run `make capabilities`"
    assert CAPABILITIES_DOC.read_text() == presets.capabilities_text(), (
        "capability drift: docs/CAPABILITIES.md does not match the code; run `make capabilities`"
    )
    makefile = (REPO / "Makefile").read_text()
    assert "\ncapabilities:\n" in makefile and "write_capabilities" in makefile
    # the table rows are the payload's rows, not a hand-maintained list
    text = CAPABILITIES_DOC.read_text()
    for plugin in presets.capabilities_payload()["plugins"]:
        assert f"| `{plugin}` |" in text, plugin
    for role in ROLES:
        assert f"| `{role}` |" in text, role


def test_the_capabilities_drift_probe_is_mutation_sensitive():
    # the same probe as the drift test, on an unmutated copy of the committed tree: zero diff
    assert _drift_probe(_doc_tree()) == 0
    assert _golden_matches(_doc_tree())
    # a preset wiring change without regeneration must turn the drift test and the golden red
    wiring = _doc_tree(_sub_once(r'(_MIXED_PLUGINS = \{[^}]*"tracer":\s*)"logfire"', r'\1"none"'))
    assert _drift_probe(wiring) == 1, "a wiring change without regeneration passed the drift test"
    assert not _golden_matches(wiring), "a wiring change matched the committed capabilities golden"
    # so must a preset extras change (the matrix lists them)
    extras = _doc_tree(_sub_once(r'extras=\("logfire",\)', 'extras=("logfire", "sql")'))
    assert _drift_probe(extras) == 1, "an extras change without regeneration passed the drift test"
    assert not _golden_matches(extras), "an extras change matched the committed capabilities golden"


# ---------------------------------------------------------------------------
# (h) no literal credential, (i) every extra really exists
# ---------------------------------------------------------------------------
def test_no_preset_renders_a_literal_url_or_credential():
    for name in D8:
        spec = presets.PRESETS[name]
        text = presets.render(spec)
        doc = yaml.safe_load(text)
        for value in _strings(doc):
            if "://" in value:
                assert value.startswith("${"), (name, "a literal URL is a defect", value)
        for env_name, block in (doc["environments"] or {}).items():
            for plugin, options in (block.get("plugin_config") or {}).items():
                for key, value in options.items():
                    if any(word in key for word in ("uri", "url", "token", "host", "key", "secret")):
                        assert isinstance(value, str) and value.startswith("${"), (name, env_name, plugin, key)
        assert "sk-" not in text and "Bearer " not in text, name
        # the sql ledger DSN is interpolated too, never a hard-coded path
        assert doc["plugin_config"]["sql"]["url"].startswith("${")


def test_every_extra_named_by_a_preset_or_the_matrix_is_a_real_pyproject_extra():
    declared = _pyproject_extras()
    assert {"mlflow", "databricks", "langfuse", "langsmith", "logfire"} <= declared, sorted(declared)
    for name in D8:
        assert set(presets.PRESETS[name].extras) <= declared, name
    for plugin, extra in presets.PLUGIN_EXTRAS.items():
        assert extra in declared, (plugin, extra)
