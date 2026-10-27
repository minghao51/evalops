"""WP3b.5 F: the nightly server-side judge settings check (`evalops judges --check-settings`).

Falsifies, group by group:

(a) two **lazy-import** settings readers beside the native adapters -- Langfuse's project default
    judge model and pydantic-evals' own default judge model -- tested offline against fake SDK
    modules (the `test_native_judges.py` style): the config pin is compared with the server default
    and the payload is the fixed `{"evaluators": [...], "drifted": bool}` shape;
(b) the flag's semantics: agreement -> `drifted: false`, exit 0; drift -> the finding names the
    config model and the server default, `drifted: true`, exit 1 (`GATE_FAILED`); a missing SDK ->
    exit 6 naming the pip package; an evaluator without a reader (mlflow, langsmith, the litellm
    fallback) -> `no-settings-surface` (visible, not hidden); no declared spec -> `evaluators: []`;
(c) the plain `evalops judges` listing is unchanged by the flag's existence (no `evaluators` key);
(d) the nightly stage runs the check right after `baseline` and before the baseline checkout, lists
    it in `--dry-run`, and a real drift run fails the stage with the step's exit 1.

Every test is a zero-argument function (the standalone runner calls them too) and offline: the fake
SDK modules are installed per test and removed in its `finally`.
"""

import contextlib
import pathlib
import sys
import types
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

SPEC = """
judges:
  correctness:
    model: fake-judge@1
    rubric: |
      Score it 0 or 1.
    prompt_version: "1"
    scale: {kind: binary, min: 0.0, max: 1.0}
"""

LITELLM_SPEC = SPEC.replace("    scale:", "    judge: litellm\n    scale:")

CORE_CONFIG = proj.BASE_CONFIG
LANGFUSE_CONFIG = proj.BASE_CONFIG.replace("evaluator: core", "evaluator: langfuse")
MLFLOW_CONFIG = proj.BASE_CONFIG.replace("evaluator: core", "evaluator: mlflow")
LANGSMITH_CONFIG = proj.BASE_CONFIG.replace("evaluator: core", "evaluator: langsmith")


# ---------------------------------------------------------------------------
# fake SDKs
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def fake_modules(modules: dict[str, Any]):
    """Install fake SDK modules for one test, restoring `sys.modules` afterwards."""
    previous = {name: sys.modules.get(name) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def langfuse_fake(default: str) -> dict[str, Any]:
    """A fake `langfuse` whose project settings expose `default_evaluator_model`."""

    class FakeProjects:
        def get(self):
            return types.SimpleNamespace(default_evaluator_model=default)

    class FakeLangfuse:
        def __init__(self, **kwargs):
            self.api = types.SimpleNamespace(projects=FakeProjects())

    return {"langfuse": types.SimpleNamespace(Langfuse=FakeLangfuse)}


def pydantic_evals_fake(default: str) -> dict[str, Any]:
    """A fake `pydantic_evals` whose `LLMJudge` field default is `default`."""
    evaluators = types.ModuleType("pydantic_evals.evaluators")

    class FakeLLMJudge:
        model_fields = {"model": types.SimpleNamespace(default=default)}

    evaluators.LLMJudge = FakeLLMJudge  # type: ignore[attr-defined]
    package = types.ModuleType("pydantic_evals")
    package.evaluators = evaluators  # type: ignore[attr-defined]
    return {"pydantic_evals": package, "pydantic_evals.evaluators": evaluators}


def _project(config: str = CORE_CONFIG) -> pathlib.Path:
    return proj.project(config)


def _clean(*paths: pathlib.Path) -> None:
    import shutil

    for path in paths:
        shutil.rmtree(path, ignore_errors=True)


def _check(root: pathlib.Path) -> tuple[int, dict]:
    return proj.run_cli_in_process(root, "judges", "--check-settings")


def _judge_entry(data: dict, evaluator: str) -> dict:
    return next(entry for entry in data["evaluators"] if entry["evaluator"] == evaluator)


# ---------------------------------------------------------------------------
# (a/b) readers and payload semantics
# ---------------------------------------------------------------------------
def test_an_agreeing_pydantic_evals_default_is_ok_and_exit_zero():
    root = _project(CORE_CONFIG + SPEC)
    try:
        with fake_modules(pydantic_evals_fake("fake-judge@1")):
            code, envelope = _check(root)
        assert code == 0 == envelope["code"], envelope
        # F2 fixes the data shape: exactly `evaluators` + `drifted`
        assert envelope["data"] == {
            "evaluators": [
                {
                    "evaluator": "core",
                    "status": "ok",
                    "judges": [
                        {"name": "correctness", "config_model": "fake-judge@1", "server_default": "fake-judge@1"}
                    ],
                }
            ],
            "drifted": False,
        }, envelope["data"]
    finally:
        _clean(root)


def test_a_drifted_server_default_is_exit_one_and_names_both_models():
    root = _project(CORE_CONFIG + SPEC)
    try:
        with fake_modules(pydantic_evals_fake("openai:gpt-4o")):
            code, envelope = _check(root)
        assert code == 1 == envelope["code"], envelope
        assert envelope["ok"] is False and envelope["data"]["drifted"] is True, envelope
        entry = _judge_entry(envelope["data"], "core")
        assert entry["status"] == "drift", entry
        assert entry["judges"] == [
            {"name": "correctness", "config_model": "fake-judge@1", "server_default": "openai:gpt-4o"}
        ], entry
        message = envelope["errors"][0]["message"]
        assert "judges.correctness.model" in message, message
        assert "fake-judge@1" in message and "openai:gpt-4o" in message, message
    finally:
        _clean(root)


def test_the_langfuse_project_default_reader_compares_the_same_way():
    root = _project(LANGFUSE_CONFIG + SPEC)
    try:
        with fake_modules(langfuse_fake("fake-judge@1")):
            code, envelope = _check(root)
        assert code == 0 == envelope["code"], envelope
        assert _judge_entry(envelope["data"], "langfuse")["status"] == "ok", envelope["data"]

        with fake_modules(langfuse_fake("langfuse-default-judge")):
            code, envelope = _check(root)
        assert code == 1 == envelope["code"], envelope
        entry = _judge_entry(envelope["data"], "langfuse")
        assert entry["status"] == "drift", entry
        assert entry["judges"][0]["server_default"] == "langfuse-default-judge", entry
    finally:
        _clean(root)


def test_a_missing_sdk_is_exit_six_naming_the_pip_package():
    root = _project(CORE_CONFIG + SPEC)
    try:
        if "pydantic_evals" in sys.modules:  # a leaked fake would make this vacuous
            raise AssertionError("pydantic_evals leaked into sys.modules from another test")
        code, envelope = _check(root)
        assert code == 6 == envelope["code"], envelope
        assert envelope["errors"][0]["hint"] == "pip install pydantic-evals", envelope["errors"][0]
        assert "pydantic-evals" in envelope["errors"][0]["message"], envelope["errors"][0]
    finally:
        _clean(root)


def test_evaluators_without_a_reader_are_visible_as_no_settings_surface():
    for config, evaluator in ((MLFLOW_CONFIG, "mlflow"), (LANGSMITH_CONFIG, "langsmith")):
        root = _project(config + SPEC)
        try:
            code, envelope = _check(root)
            assert code == 0 == envelope["code"], (evaluator, envelope)
            entry = _judge_entry(envelope["data"], evaluator)
            assert entry["status"] == "no-settings-surface", (evaluator, entry)
            assert entry["judges"][0]["server_default"] is None, entry
            assert envelope["data"]["drifted"] is False, envelope["data"]
        finally:
            _clean(root)


def test_a_litellm_fallback_spec_has_no_server_side_surface():
    root = _project(CORE_CONFIG + LITELLM_SPEC)
    try:
        code, envelope = _check(root)
        assert code == 0 == envelope["code"], envelope
        entry = _judge_entry(envelope["data"], "litellm")
        assert entry["status"] == "no-settings-surface", entry
        assert entry["judges"] == [{"name": "correctness", "config_model": "fake-judge@1", "server_default": None}], (
            entry
        )
    finally:
        _clean(root)


def test_no_declared_specs_is_an_empty_evaluator_list_and_exit_zero():
    root = _project(CORE_CONFIG)
    try:
        code, envelope = _check(root)
        assert code == 0 == envelope["code"], envelope
        assert envelope["data"] == {"evaluators": [], "drifted": False}, envelope["data"]
    finally:
        _clean(root)


def test_the_plain_listing_is_unchanged_by_the_flag():
    root = _project(CORE_CONFIG + SPEC)
    try:
        code, envelope = proj.run_cli_in_process(root, "judges")
        assert code == 0 == envelope["code"], envelope
        assert set(envelope["data"]) == {"judges", "unpinned"}, envelope["data"]
        assert "evaluators" not in envelope["data"] and "drifted" not in envelope["data"], envelope["data"]
    finally:
        _clean(root)


# ---------------------------------------------------------------------------
# (d) the nightly stage
# ---------------------------------------------------------------------------
def test_the_nightly_dry_run_lists_judge_settings_right_after_baseline():
    root = _project(CORE_CONFIG + SPEC)
    try:
        code, envelope = proj.run_cli(root, "pipeline", "nightly", "--dry-run")
        assert code == 0 == envelope["code"], envelope
        names = [step["name"] for step in envelope["data"]["steps"]]
        assert names[:2] == ["baseline", "judge-settings"], names
        step = envelope["data"]["steps"][1]
        assert step["argv"] == ["judges", "--check-settings"], step
        assert step["command"] == "evalops judges --check-settings", step
        assert step["run"] is True and step["kind"] == "evalops", step
        assert "checkout" in names and names.index("judge-settings") < names.index("checkout"), names
    finally:
        _clean(root)


def test_a_real_nightly_drift_run_fails_the_stage_at_the_settings_step():
    root = _project(CORE_CONFIG + SPEC)
    try:
        with fake_modules(pydantic_evals_fake("drifted-model")):
            code, envelope = proj.run_cli_in_process(root, "pipeline", "nightly")
        assert code == 1 == envelope["code"], envelope
        assert [step["name"] for step in envelope["data"]["steps"]] == ["baseline", "judge-settings"], envelope
        settings = envelope["data"]["steps"][-1]
        assert settings["code"] == 1 and settings["ok"] is False, settings
        message = envelope["errors"][0]["message"]
        assert "judge-settings" in message and "judges.correctness.model" in message, message
        assert "fake-judge@1" in message and "drifted-model" in message, message
    finally:
        _clean(root)
