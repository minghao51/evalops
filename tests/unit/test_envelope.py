"""WP0.5: the frozen ``--json`` envelope (CONTRACT.md §2) and the exit-code matrix (§1).

Falsifies, in order: (a) every envelope has exactly the seven contract keys and ``ok`` iff
``code == 0``, (b) ``from_error`` surfaces ``CapabilityError``'s hint and an ``EvalOpsError``'s
details, (c) ``render`` writes exactly ONE JSON document on ``--json``, (d) every deterministic
command matches a golden snapshot under ``tests/fixtures/envelope/`` produced from one reproducible
temp project (WP0.5b D4), so a new top-level key, a dropped key or a reworded message fails,
(e) the goldens are stable across two runs in two different temp directories and hold no absolute
path or temp-dir name, (f) one case per exit code 0–7 asserts both the process code and
``envelope["code"]``, (g) ``--version`` exits 0 without a config file and honours ``--json``,
(h) an ``argparse`` error is exit 2 *with* a code-2 envelope naming the offending argument (D1),
(i) a malformed YAML document is a usage error (exit 2), not a runtime error (D2), (j) a hook that
prints cannot corrupt the document: stdout stays one document and the text lands on stderr (D3),
including an in-process embedder whose `sys.stdout` is a separate stream, as in pytest `capsys`
(WP0.5d D1), (k) ``env`` is pinned by the golden rather than masked (D5), (l) envelopes built before the config
loads (`--version`, argparse errors) report the **resolved** env, honouring `EVALOPS_ENV`, `--config`
and `default_env` (WP0.5c D2) and never raise on a recursive alias or a non-string `default_env`
(WP0.5d D2), (m) the cost envelope's money fields keep their types so a masked
amount cannot hide a type regression, (n) an ``--config`` value that happens to spell a sub-command
name is consumed as a value, so `--config rollback ... --version` is still the top-level flag
(WP0.5d D3), (o) the new ``judges`` command (WP3b.1) has an *additive* golden of its own: one
full judge spec and one threshold-only rule, so both list branches are pinned and no rubric prose
is emitted.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
Regenerate the goldens (review the diff!) with ``EVALOPS_UPDATE_GOLDENS=1 pytest -q -k goldens``.
"""

import contextlib
import io
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

from evalops import __version__ as VERSION  # noqa: E402
from evalops import cli as cli_mod  # noqa: E402
from evalops import envelope  # noqa: E402
from evalops.util import EXIT_CAPABILITY, EXIT_RUNTIME, EXIT_USAGE, CapabilityError  # noqa: E402

GOLDENS = TESTS_DIR / "fixtures" / "envelope"
ENVELOPE_KEYS = {"ok", "code", "schema_version", "command", "env", "data", "errors"}

CONFIG = """
project: envelope
default_env: local
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
app:
  behavior_config: fake_app:behavior_config
  predict_fn: fake_app:build_predict_fn
  scorers: fake_app:build_scorers
  judge_meta: fake_app:judge_meta
datasets: {smoke: {frozen: true}}
eval: {max_workers: 2, retries: 1, backoff_seconds: 0.01}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
"""

# A gate that reads one fabricated run: `facts/mean` 0.0 misses `min: 0.5` -> exit 1.
GATE_CONFIG = """
project: envelope-gate
default_env: local
plugins: {prompts: git, datasets: git, ledger: "envelope_fake_ledger:FakeLedger", tracer: none, evaluator: core}
app: {predict_fn: fake_app:build_predict_fn}
gates: {metrics: {facts/mean: {min: 0.5}}}
environments:
  local: {}
  broken: {plugins: {ledger: git}}
"""

FAKE_LEDGER = '''
from evalops.ports import Ledger, Plugin, RunRecord


class FakeLedger(Plugin, Ledger):
    """One run with a failing metric and no artifacts, so the gate stays on metric rules only."""

    name = "fake"
    provides = ("ledger",)

    def start_run(self, name, tags):
        return "r1"

    def end_run(self, run_id, status="FINISHED"):
        pass

    def log(self, run_id, params=None, metrics=None, tags=None):
        pass

    def put_artifact(self, run_id, name, text):
        pass

    def get_artifact(self, run_id, name):
        raise KeyError(name)

    def find_runs(self, tags=None, since_ms=None, limit=None):
        if (tags or {}).get("evalops.group") == "g1":
            return [RunRecord("r1", {"evalops.group": "g1"}, {}, {"facts/mean": 0.0}, 1)]
        return []
'''

# Goldens (D4): one fabricated run with `facts/mean` 0.9, so a gate PASS and a gate FAIL are both
# reproducible offline from the same temp project (only the configured `min` differs).
GOLDEN_LEDGER = '''
from evalops.ports import Ledger, Plugin, RunRecord


class GoldenLedger(Plugin, Ledger):
    """One fabricated run for the gate goldens; no artifacts, so only metric rules are checked."""

    name = "golden"
    provides = ("ledger",)

    def start_run(self, name, tags):
        return "g1-run"

    def end_run(self, run_id, status="FINISHED"):
        pass

    def log(self, run_id, params=None, metrics=None, tags=None):
        pass

    def put_artifact(self, run_id, name, text):
        pass

    def get_artifact(self, run_id, name):
        raise KeyError(name)

    def find_runs(self, tags=None, since_ms=None, limit=None):
        if (tags or {}).get("evalops.group") == "g1":
            return [RunRecord("g1-run", {"evalops.group": "g1"}, {}, {"facts/mean": 0.9}, 1)]
        return []
'''

# `eval` refuses a dirty tree, so every extra file the golden scenario needs is committed up front.
GATE_PASS_CONFIG = """
project: envelope-gate-pass
default_env: local
plugins: {prompts: git, datasets: git, ledger: "envelope_golden_ledger:GoldenLedger", tracer: none, evaluator: core}
app: {predict_fn: fake_app:build_predict_fn}
gates: {metrics: {facts/mean: {min: 0.5}}}
"""
GATE_FAIL_CONFIG = GATE_PASS_CONFIG.replace("-pass", "-fail").replace("{min: 0.5}", "{min: 0.95}")
BROKEN_CONFIG = CONFIG + "gates:\n  metrics:\n    facts/mean: {minn: 1}\n"

# WP1.1: `evalops redact --check` gets its own additive golden (a *new* command, so the §2 list of
# pre-existing goldens is unchanged). The document holds a planted secret, the rules redact it, and
# the command reports the would-be changes without writing -- a read-only command is deterministic.
REDACT_CONFIG = (
    CONFIG
    + """
redact:
  mode: strict
  fields: ["inputs.password"]
  patterns:
    - {name: email, regex: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}'}
"""
)
SECRETS_JSON = (
    '{\n  "inputs": {\n    "password": "hunter2-PLANTED",\n    "note": "mail alice@example.com"\n  },\n  "keep": 1\n}\n'
)

# D3: a predict hook that prints. With --json that text is a diagnostic and must land on stderr.
NOISY_APP = """
from fake_app import build_predict_fn as fake_predict


def build_predict_fn(prompts):
    inner = fake_predict(prompts)

    async def predict(question):
        print("noisy hook: generating an answer")
        return await inner(question)

    return predict
"""
NOISY_CONFIG = CONFIG.replace("predict_fn: fake_app:build_predict_fn", "predict_fn: noisy_app:build_predict_fn")

# WP3b.1: `evalops judges` gets its own additive golden (a *new* command after the envelope
# freeze). The project declares one full spec and one threshold-only rule, so both list branches of
# `payload()` appear (a spec identity and the `unpinned` worklist) and the rubric text is provably
# absent: only its length and its 12-hex identity are emitted.
JUDGES_RUBRIC = """Score 1.0 when every claim matches the retrieved context; 0.0 otherwise.
Penalize invented citations at most harshly.
"""
JUDGES_CONFIG = (
    CONFIG
    + "judges:\n"
    + "  correctness:\n"
    + "    model: fake-judge-2024-11-20\n"
    + "    rubric: |\n"
    + "".join(f"      {line}\n" for line in JUDGES_RUBRIC.splitlines())
    + '    prompt_version: "3"\n'
    + "    scale: {kind: binary, min: 0.0, max: 1.0}\n"
    + "    sampling: {temperature: 0.0, max_tokens: 256, seed: 7}\n"
    + "  helpful: {min_agreement: 0.85, min_kappa: 0.6}\n"
)

# WP1.2: a ledger plugin that keeps the non-abstract port defaults (`migrate`/`verify` raise
# CapabilityError), so `ledger verify` pins the exit-6 mapping without adding a new exit code.
MEMORY_LEDGER = (TESTS_DIR / "fixtures" / "formats" / "memory_ledger.py").read_text()
LEDGER_CONFIG = """
project: envelope-ledger
default_env: local
plugins: {prompts: git, datasets: git, ledger: "memory_ledger:MemoryLedger", tracer: none, evaluator: core}
app: {predict_fn: fake_app:build_predict_fn}
"""

# D1: two ways a hook bypasses `sys.stdout` -- writing to the interpreter's real stdout object and
# spawning a child that inherits file descriptor 1. `contextlib.redirect_stdout` catches neither, so
# the capture has to be at the descriptor level. Both texts must land on stderr, in order.
BYPASS_APP = """
import subprocess, sys

from fake_app import build_predict_fn as fake_predict


def build_predict_fn(prompts):
    inner = fake_predict(prompts)

    async def predict(question):
        sys.__stdout__.write("bypass: wrote sys.__stdout__\\n")
        sys.__stdout__.flush()
        subprocess.run([sys.executable, "-c", "print('bypass: child process stdout')"], check=True)
        return await inner(question)

    return predict
"""
BYPASS_CONFIG = CONFIG.replace("predict_fn: fake_app:build_predict_fn", "predict_fn: bypass_app:build_predict_fn")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _project(config: str = CONFIG, files: dict | None = None) -> pathlib.Path:
    """A git-init'd temp project. The full app is only needed for the budget (eval) case."""
    root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-envelope-"))
    (root / "evalops.yaml").write_text(config)
    (root / ".gitignore").write_text(".evalops/\n__pycache__/\n")
    shutil.copy(REPO / "prompts" / "support_system.txt", _mkdir(root / "prompts") / "support_system.txt")
    shutil.copy(REPO / "datasets" / "smoke.jsonl", _mkdir(root / "datasets") / "smoke.jsonl")
    shutil.copy(TESTS_DIR / "fixtures" / "fake_app.py", root / "fake_app.py")
    for name, text in (files or {}).items():
        (root / name).write_text(text)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    _commit(root, "init")
    return root


def _mkdir(path: pathlib.Path) -> pathlib.Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _last_eval(root: pathlib.Path, doc: dict) -> None:
    (_mkdir(root / ".evalops" / "out")).joinpath("last_eval.json").write_text(json.dumps(doc))


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: pathlib.Path, message: str) -> None:
    _git(root, "add", "-A")
    subprocess.run(["git", "commit", "-qm", message], cwd=root, capture_output=True, text=True)


def _env(extra: dict | None = None) -> dict:
    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
        **(extra or {}),
    }
    for key, value in (extra or {}).items():
        if value is None:  # an explicit `None` means "this variable is not set"
            env.pop(key, None)
    for stale in ("EVALOPS_CONFIG", "EVALOPS_ALLOW_FROZEN_CHANGE"):
        if stale not in (extra or {}):
            env.pop(stale, None)
    return env


def _run(root: pathlib.Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
    """Run the real CLI as a subprocess -> (exit code, envelope). Stdout must be one document."""
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=_env(env),
        capture_output=True,
        text=True,
    )
    return proc.returncode, _one_document(proc.stdout)


def _run_in_process(root: pathlib.Path, *args: str) -> tuple[int, dict]:
    """Run ``cli.main`` in this process (for monkeypatching); still asserts one JSON document."""
    cwd, buf = os.getcwd(), io.StringIO()
    os.chdir(root)
    try:
        with contextlib.redirect_stdout(buf):
            code = cli_mod.main(["--json", *args])
    finally:
        os.chdir(cwd)
    return code, _one_document(buf.getvalue())


def _one_document(text: str) -> dict:
    """Assert stdout is exactly one JSON document (nothing before it, nothing after it)."""
    stripped = text.strip()
    assert stripped.startswith("{"), f"stdout is not a JSON document: {text!r}"
    document, end = json.JSONDecoder().raw_decode(stripped)
    assert end == len(stripped), f"extra output after the JSON document: {stripped[end:]!r}"
    assert isinstance(document, dict), document
    return document


# ---------------------------------------------------------------------------
# (a)-(c) envelope shape, mapping and rendering
# ---------------------------------------------------------------------------
def test_exit_matrix_is_the_single_source_of_truth_for_codes_zero_to_seven():
    assert sorted(envelope.EXIT_MATRIX) == list(range(8))
    assert envelope.EXIT_MATRIX[0] == "OK" and envelope.EXIT_MATRIX[7] == "CONTRACT"
    assert envelope.SCHEMA_VERSION == 1


def test_success_envelope_has_exactly_the_seven_contract_keys():
    env = envelope.success("plugins", "local", {"summary": "env 'local'"})
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is True and env["code"] == 0 and env["errors"] == []
    assert env["schema_version"] == envelope.SCHEMA_VERSION == 1
    assert env["data"] == {"summary": "env 'local'"}
    # data is always an object, never null, even when the command has no payload
    assert envelope.success("guard", "local")["data"] == {}


def test_failure_envelope_carries_code_message_hint_and_details():
    env = envelope.failure("eval", "local", 3, "budget exceeded", hint="raise it", details={"cap_usd": 0.1})
    assert set(env) == ENVELOPE_KEYS
    assert env["ok"] is False and env["code"] == 3
    assert env["data"] == {} and len(env["errors"]) == 1
    assert env["errors"][0] == {
        "code": 3,
        "message": "budget exceeded",
        "hint": "raise it",
        "details": {"cap_usd": 0.1},
    }
    # a bare failure carries no empty hint/details keys (the contract makes them optional)
    assert set(envelope.failure("guard", "local", 4, "nope")["errors"][0]) == {"code", "message"}


def test_success_is_the_only_ok_envelope_and_failure_rejects_code_zero():
    assert envelope.success("plugins", "local")["ok"] is True
    for code in range(1, 8):
        assert envelope.failure("plugins", "local", code, "x")["ok"] is False
    try:
        envelope.failure("plugins", "local", 0, "x")
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("failure() accepted exit code 0: ok/code would contradict each other")


def test_from_result_keeps_summary_in_data_and_maps_ok_false_to_gate_failed():
    ok = envelope.from_result("sync", "local", {"summary": "datasets in sync", "datasets": {"changed": []}})
    assert ok["ok"] is True and ok["code"] == 0
    assert ok["data"] == {"summary": "datasets in sync", "datasets": {"changed": []}}

    failed = envelope.from_result("gate", "local", {"ok": False, "summary": "gate FAILED for group g1", "checks": []})
    assert failed["ok"] is False and failed["code"] == 1 and envelope.EXIT_MATRIX[1] == "GATE_FAILED"
    assert failed["errors"][0]["message"] == "gate FAILED for group g1"
    assert failed["data"]["summary"] == "gate FAILED for group g1" and failed["data"]["checks"] == []
    assert "ok" not in failed["data"]  # the command's own ok flag lives at the top level only


def test_from_error_surfaces_the_capability_hint_and_the_details():
    exc = CapabilityError("git", "role 'ledger'", "It provides ['prompts', 'datasets'].")
    env = envelope.from_error("baseline", "broken", exc)
    assert env["code"] == EXIT_CAPABILITY == 6 and env["ok"] is False
    assert env["errors"][0]["hint"] == "It provides ['prompts', 'datasets']."
    assert "does not support role 'ledger'" in env["errors"][0]["message"]

    from evalops.util import EvalOpsError

    usage = envelope.from_error("config", "local", EvalOpsError("bad key", EXIT_USAGE, {"errors": [{"path": "a.b"}]}))
    assert usage["code"] == 2 and usage["errors"][0]["details"] == {"errors": [{"path": "a.b"}]}
    assert "hint" not in usage["errors"][0]

    # an unexpected exception is exit 5, never an unmapped code
    plain = envelope.from_error("guard", "local", ValueError("boom"))
    assert plain["code"] == EXIT_RUNTIME == 5 and plain["errors"][0]["message"] == "boom"


def test_render_json_writes_exactly_one_document_and_nothing_else():
    buf = io.StringIO()
    env = envelope.success("plugins", "local", {"summary": "env 'local'"})
    envelope.render(env, True, buf)
    text = buf.getvalue()
    assert text.endswith("}\n") and text.count("\n{") == 0  # one document, one trailing newline
    assert _one_document(text) == env


def test_render_human_prints_the_summary_and_the_error_hint():
    buf = io.StringIO()
    envelope.render(envelope.success("sync", "local", {"summary": "datasets in sync", "changed": []}), False, buf)
    assert buf.getvalue().startswith("datasets in sync\n")
    assert "changed: []" in buf.getvalue()

    buf = io.StringIO()
    envelope.render(envelope.failure("baseline", "broken", 6, "no ledger", hint="choose another plugin"), False, buf)
    assert "baseline failed (CAPABILITY): no ledger" in buf.getvalue()
    assert "hint: choose another plugin" in buf.getvalue()


# ---------------------------------------------------------------------------
# (d)-(e) golden snapshots for the deterministic commands
# ---------------------------------------------------------------------------
# Only per-run identity is masked *by key*. `env` is **pinned** to the selected environment (D5) and
# `version` stays contract-visible, so neither is masked: masking them let a wrong environment or a
# wrong version match. Filesystem paths are masked by *value* (they are always absolute), so a dotted
# config-key path such as `gates.metrics.facts/mean.minn` -- and a relative file name such as
# `broken_config.yaml` -- stay visible: naming the offending key/file is part of the contract.
_MASKS = {
    "group": "<id>",
    "run_id": "<id>",
    "trace_id": "<id>",
    "git_sha": "<id>",
    "app_version": "<id>",
}
_ID_KEYS = frozenset(_MASKS)
#: Cost and timing numbers follow prices and the clock, not the contract shape.
_NUMERIC_KEYS = (
    "_ms",
    "elapsed",
    "duration",
    "latency",
    "timestamp",
    "start_ms",
    "end_ms",
    "timing",
    "cost",
    "_usd",
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_URL = re.compile(r"^[a-z][a-z0-9+.-]*://")
_HEX = re.compile(r"^[0-9a-f]{8,64}$")
#: Values that look like an id but *are* the contract: `evalops judges` publishes a rubric's
#: identity, so masking its 12-hex digest would let any rubric pass the golden. Only these keys
#: skip the hex-by-value mask; no pre-existing golden has one, so no other snapshot moves.
_PINNED_KEYS = frozenset({"rubric_hash"})
#: A 12+ hex run inside free text is a run id / group id (`traces` embeds one in its summary).
_HEX_IN_TEXT = re.compile(r"\b[0-9a-f]{12,64}\b")
_MONEY = re.compile(r"\$\d+(?:\.\d+)?")
#: Every golden is produced with this environment selected; `_normalized` asserts it.
GOLDEN_ENV = "local"


def _volatile_tokens(document: Any) -> tuple[str, ...]:
    """Per-run identities that also appear inside free text (`eval`/`gate` summaries embed them)."""
    tokens: set[str] = set()

    def walk(value, key: str = "") -> None:
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                walk(sub_value, sub_key)
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and key in _ID_KEYS and len(value) >= 4:
            tokens.add(value)

    walk(document)
    return tuple(sorted(tokens, key=len, reverse=True))


def _normalize(value: Any, key: str = "", tokens: tuple[str, ...] = ()) -> Any:
    """Mask genuinely volatile fields so a golden survives a new temp dir, env or clock."""
    if isinstance(value, dict):
        return {k: _normalize(v, k, tokens) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v, key, tokens) for v in value]
    if key in _MASKS:
        return _MASKS[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value
        if key in _PINNED_KEYS:
            return text
        for token in tokens:
            text = text.replace(token, "<id>")
        text = _HEX_IN_TEXT.sub("<id>", text)
        text = _MONEY.sub("$<n>", text)
        if _URL.match(text):
            return "<url>"
        if text.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", text):
            return "<path>"
        if _UUID.search(text):
            return _UUID.sub("<id>", text)
        return "<id>" if _HEX.match(text) else text
    if isinstance(value, (int, float)) and any(part in key for part in _NUMERIC_KEYS):
        return "<n>"
    return value


def _normalized(env_dict: dict, expected_env: str = GOLDEN_ENV) -> dict:
    """Pin ``env`` to the environment the scenario selected, then normalize (D5)."""
    assert env_dict["env"] == expected_env, (
        f"envelope env {env_dict['env']!r} != the selected env {expected_env!r}: the golden pins env, "
        "so this is a real difference, not noise"
    )
    return _normalize(env_dict, tokens=_volatile_tokens(env_dict))


def _golden_path(name: str) -> pathlib.Path:
    return GOLDENS / f"{name}.json"


def _diff(name: str, actual: dict, expected_env: str = GOLDEN_ENV) -> str | None:
    """Compare a normalized envelope with its golden; return a readable diff or None."""
    expected = json.loads(_golden_path(name).read_text())
    normalized = _normalized(actual, expected_env)
    if normalized == expected:
        return None
    return (
        f"{name}: the --json envelope drifted from its golden snapshot\n"
        f"--- expected (tests/fixtures/envelope/{name}.json)\n{json.dumps(expected, indent=2)}\n"
        f"--- actual\n{json.dumps(normalized, indent=2)}"
    )


def _check_golden(name: str, actual: dict, expected_env: str = GOLDEN_ENV) -> str | None:
    if os.environ.get("EVALOPS_UPDATE_GOLDENS") == "1":
        GOLDENS.mkdir(parents=True, exist_ok=True)
        _golden_path(name).write_text(json.dumps(_normalized(actual, expected_env), indent=2) + "\n")
        return None
    return _diff(name, actual, expected_env)


#: Every deterministic command a golden pins (D4). `calibrate`, `promote`, `rollback` and
#: vendor-backed evals are stateful/credentialed: the exit-code matrix (f) and the integration tests
#: cover them instead of a snapshot (docs/CONTRACT.md §2).
GOLDEN_NAMES = (
    "version",
    "plugins",
    "doctor",
    "guard",
    "baseline",
    "cost",
    "config_validate_ok",
    "config_validate_failure",
    "config_schema",
    "config_explain",
    "sync_ok",
    "plan",
    "eval",
    "traces",
    "gate_pass",
    "gate_failure",
    "sync_check_failure",
    "ledger_verify",
    "redact",
    "judges",
)


def _golden_project() -> pathlib.Path:
    """The one reproducible temp project every golden is produced from (D4).

    Every file the scenario needs is committed before the first command (`eval` refuses a dirty
    tree); the gate configs use a fabricated ledger, so PASS and FAIL need no backend at all.
    """
    return _project(
        CONFIG,
        {
            "envelope_golden_ledger.py": GOLDEN_LEDGER,
            "gate_pass.yaml": GATE_PASS_CONFIG,
            "gate_fail.yaml": GATE_FAIL_CONFIG,
            "broken_config.yaml": BROKEN_CONFIG,
            "redact_config.yaml": REDACT_CONFIG,
            "secrets.json": SECRETS_JSON,
            "ledger_config.yaml": LEDGER_CONFIG,
            "memory_ledger.py": MEMORY_LEDGER,
            "judges_config.yaml": JUDGES_CONFIG,
        },
    )


def _golden_envelopes(root: pathlib.Path) -> dict[str, tuple[int, dict]]:
    """Run the shared scenario in one project -> ``{golden name: (exit code, envelope)}``.

    The order is part of the scenario: `sync all` writes the locks `plan`/`eval` need, `eval` writes
    the `last_eval.json` that `traces`/`gate` read (and refuses a dirty tree), and the very last step
    edits a prompt source so `sync --check` reports the out-of-sync failure without disturbing any
    earlier golden.
    """
    out: dict[str, tuple[int, dict]] = {}

    def run(name: str, *args: str) -> None:
        out[name] = _run(root, *args)

    run("version", "--version")
    run("plugins", "plugins")
    run("doctor", "doctor")
    run("guard", "guard")
    run("baseline", "baseline")
    run("cost", "cost")
    run("config_validate_ok", "config", "validate")
    run("config_validate_failure", "--config", "broken_config.yaml", "config", "validate")
    run("config_schema", "config", "schema")
    run("config_explain", "config", "explain")
    run("sync_ok", "sync", "all")
    run("plan", "plan", "--dataset", "smoke", "--stage", "dev")
    run("eval", "eval", "--dataset", "smoke", "--stage", "dev")
    run("traces", "traces")
    run("gate_pass", "--config", "gate_pass.yaml", "gate", "--group", "g1")
    run("gate_failure", "--config", "gate_fail.yaml", "gate", "--group", "g1")
    prompt = root / "prompts" / "support_system.txt"
    prompt.write_text(prompt.read_text() + "\nAlways be extra polite.\n")
    run("sync_check_failure", "sync", "prompts", "--check")
    # WP1.2: a read-only capability error; it cannot disturb any earlier golden.
    run("ledger_verify", "--config", "ledger_config.yaml", "ledger", "verify")
    # WP1.1: read-only, so it cannot disturb any earlier golden; it is deliberately last.
    run("redact", "--config", "redact_config.yaml", "redact", "--check", "--file", "secrets.json")
    # WP3b.1: the new `judges` command (additive golden); read-only, so it disturbs nothing else.
    run("judges", "--config", "judges_config.yaml", "judges")
    return out


def test_envelope_goldens_cover_the_deterministic_commands():
    """D4: one shared temp project, two runs in two directories, one golden per command."""
    first = _golden_envelopes(_golden_project())
    second = _golden_envelopes(_golden_project())
    assert tuple(first) == GOLDEN_NAMES, tuple(first)
    failures = []
    for name in GOLDEN_NAMES:
        code, env_dict = first[name]
        assert code == env_dict["code"], (name, code, env_dict)
        assert set(env_dict) == ENVELOPE_KEYS, (name, sorted(env_dict))
        # a golden that moves on the second identical scenario in another temp dir is not a golden
        assert _normalized(second[name][1]) == _normalized(env_dict), (name, "output is not stable")
        diff = _check_golden(name, env_dict)
        if diff:
            failures.append(diff)
    print("\ngolden                 | exit | command")
    for name in GOLDEN_NAMES:
        print(f"{name:22} |  {first[name][0]}  | {first[name][1]['command']}")
    assert not failures, "\n\n".join(failures)


def test_goldens_hold_no_absolute_paths_or_temp_dirs():
    """A golden must be platform-independent: no temp dir, no home directory, no absolute path."""
    forbidden = (str(REPO), tempfile.gettempdir(), str(pathlib.Path.home()))

    def strings(value):
        if isinstance(value, dict):
            for item in value.values():
                yield from strings(item)
        elif isinstance(value, list):
            for item in value:
                yield from strings(item)
        elif isinstance(value, str):
            yield value

    for path in sorted(GOLDENS.glob("*.json")):
        for value in strings(json.loads(path.read_text())):
            assert not value.startswith("/"), (path.name, value)
            assert not any(part in value for part in forbidden), (path.name, value)


def test_a_new_top_level_key_fails_the_goldens():
    root = _project()
    env_dict = _run(root, "plugins")[1]
    assert _diff("plugins", env_dict) is None
    forged = {**env_dict, "timings": {"total_ms": 3}}
    diff = _diff("plugins", forged)
    assert diff is not None and "timings" in diff, diff
    # dropping a contract key is caught the same way
    missing = {k: v for k, v in env_dict.items() if k != "schema_version"}
    assert _diff("plugins", missing) is not None


def test_a_wrong_environment_fails_the_golden():
    """D5: `env` is pinned, not masked, so selecting a different env changes the snapshot."""
    root = _project()
    env_dict = _run(root, "plugins")[1]
    assert env_dict["env"] == "local"
    assert _diff("plugins", env_dict) is None
    # pinning to the env the envelope claims passes (the pin is about *which* env was selected) ...
    forged = {**env_dict, "env": "broken"}
    # ... and the golden still catches the change, which the old `<env>` mask hid.
    diff = _diff("plugins", forged, expected_env="broken")
    assert diff is not None and '"env": "broken"' in diff, diff
    try:
        _normalized(forged, "local")
    except AssertionError as e:
        assert "broken" in str(e) and "local" in str(e), str(e)
    else:  # pragma: no cover
        raise AssertionError("a wrong env passed the pin")


def test_the_evalops_version_is_not_masked_in_the_version_golden():
    """D5: `version` is contract-visible, so the golden records the real value."""
    env_dict = _run(_project(), "--version")[1]
    assert _normalized(env_dict)["data"]["version"] == VERSION
    assert _diff("version", env_dict) is None
    assert _diff("version", {**env_dict, "data": {"version": "0.0.0"}}) is not None


# ---------------------------------------------------------------------------
# (f) the exit-code matrix 0-7
# ---------------------------------------------------------------------------
def test_exit_code_matrix_zero_to_seven():
    # 1) gate failed: a metric rule the fabricated run misses (in-process: no service, ~5 ms)
    gate_root = _project(GATE_CONFIG, {"envelope_fake_ledger.py": FAKE_LEDGER})
    _last_eval(gate_root, {"group": "g1", "schema_version": 1})
    # 5) runtime: an unexpected exception inside dispatch
    boom_root = _project(GATE_CONFIG, {"envelope_fake_ledger.py": FAKE_LEDGER})

    def boom(args, cfg):
        raise ValueError("dispatch exploded")

    saved, cli_mod.dispatch = cli_mod.dispatch, boom
    try:
        runtime = _run_in_process(boom_root, "plugins")
    finally:
        cli_mod.dispatch = saved

    # 3) budget: a real eval whose estimate blows a 1-micro-dollar per-run budget
    budget_root = _project(CONFIG.replace("per_run_usd: 5", "per_run_usd: 0.000001"))
    assert _run(budget_root, "sync", "all")[0] == 0
    _commit(budget_root, "sync")

    # 4) guard: a direct backend write in project code
    guard_root = _project(CONFIG)
    (guard_root / "sneaky.py").write_text(
        "from langfuse import get_client\nget_client().create_prompt(name='x', prompt='y')\n"
    )
    # 6) capability: git cannot be a ledger
    capability_root = _project(GATE_CONFIG)
    # 7) contract: last_eval.json declares a future schema_version
    contract_root = _project(GATE_CONFIG)
    _last_eval(contract_root, {"group": "g1", "schema_version": 2})
    # 2) usage: an unknown key in evalops.yaml (the dotted path must be named)
    usage_root = _project()
    broken = usage_root / "broken.yaml"
    broken.write_text(CONFIG + "gates:\n  metrics:\n    facts/mean: {minn: 1}\n")

    cases = [
        (0, "plugins (valid project)", lambda: _run(_project(), "plugins")),
        (1, "gate (metric below min)", lambda: _run_in_process(gate_root, "gate")),
        (2, "config validate (unknown key)", lambda: _run(usage_root, "--config", str(broken), "config", "validate")),
        (3, "eval (per_run_usd 0.000001)", lambda: _run(budget_root, "eval", "--dataset", "smoke", "--stage", "dev")),
        (4, "guard (direct create_prompt call)", lambda: _run(guard_root, "guard")),
        (5, "plugins (dispatch raises ValueError)", lambda: runtime),
        (6, "baseline (ledger: git)", lambda: _run(capability_root, "baseline", env={"EVALOPS_ENV": "broken"})),
        (7, "traces (last_eval schema_version 2)", lambda: _run(contract_root, "traces")),
    ]

    observed = []
    for expected, label, case in cases:
        code, env_dict = case()
        assert code == expected, (label, code, env_dict)
        assert env_dict["code"] == expected == code, (label, code, env_dict["code"])
        assert env_dict["ok"] is (expected == 0), (label, env_dict)
        assert set(env_dict) == ENVELOPE_KEYS, (label, sorted(env_dict))
        observed.append((expected, label, env_dict["code"], env_dict["ok"]))

    print("\nexit | envelope | ok    | path")
    for code, label, env_code, ok in observed:
        print(f"  {code}  |    {env_code}     | {str(ok):5} | {label}")
    assert [row[0] for row in observed] == list(range(8))

    # the messages/detail shapes callers branch on
    assert "facts/mean >= 0.5" in json.dumps(_run_in_process(gate_root, "gate")[1]["data"]["checks"])
    assert (
        _run(usage_root, "--config", str(broken), "config", "validate")[1]["errors"][0]["details"]["errors"][0]["path"]
        == "gates.metrics.facts/mean.minn"
    )
    assert (
        "budget exceeded"
        in _run(budget_root, "eval", "--dataset", "smoke", "--stage", "dev")[1]["errors"][0]["message"]
    )
    guard_env = _run(guard_root, "guard")[1]
    assert any("sneaky.py" in v for v in guard_env["errors"][0]["details"]["violations"])
    assert (
        "Choose another plugin"
        in _run(capability_root, "baseline", env={"EVALOPS_ENV": "broken"})[1]["errors"][0]["hint"]
    )
    assert "last_eval.json" in _run(contract_root, "traces")[1]["errors"][0]["message"]
    assert "dispatch exploded" in runtime[1]["errors"][0]["message"]


def test_json_mode_keeps_stdout_a_single_document_and_never_an_envelope_on_stderr():
    """Diagnostics go to stderr; stdout is the envelope and nothing else, on success and failure."""
    root, broken_root = _project(), _project()
    broken = broken_root / "broken.yaml"
    broken.write_text(CONFIG + "gates:\n  metrics:\n    facts/mean: {minn: 1}\n")
    for args in (["plugins"], ["--config", str(broken), "config", "validate"]):
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "--json", *args],
            cwd=str(root),
            env=_env(),
            capture_output=True,
            text=True,
        )
        assert set(_one_document(proc.stdout)) == ENVELOPE_KEYS
        assert '"schema_version"' not in proc.stderr, proc.stderr


def test_version_flag_exits_zero_without_a_config_file():
    empty = pathlib.Path(tempfile.mkdtemp(prefix="evalops-noconfig-"))
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--version"], cwd=str(empty), env=_env(), capture_output=True, text=True
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("evalops ") and "\n" in proc.stdout, proc.stdout
    assert VERSION in proc.stdout

    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", "--version"],
        cwd=str(empty),
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env_dict = _one_document(proc.stdout)
    assert set(env_dict) == ENVELOPE_KEYS and env_dict["ok"] is True and env_dict["code"] == 0
    assert env_dict["command"] == "version" and env_dict["data"]["version"] in proc.stdout


def test_a_subcommands_version_option_is_not_the_top_level_version_flag():
    """`rollback --version N` names a prompt version: it must run, not print the evalops version."""
    root = _project()
    assert _run(root, "sync", "all")[0] == 0
    _commit(root, "sync")
    code, env_dict = _run(root, "rollback", "--alias", "production", "--prompt", "support_system", "--version", "1")
    assert code == 0 and env_dict["command"] == "rollback", env_dict
    assert env_dict["data"]["summary"] == "support_system@production -> 1 (rollback via git)", env_dict

    # the real top-level flag still wins before any sub-command
    code, env_dict = _run(root, "--version")
    assert code == 0 and env_dict["command"] == "version" and env_dict["data"]["version"] == VERSION, env_dict


def test_an_option_value_named_like_a_subcommand_is_not_the_subcommand():
    """D3: `--config rollback` names a file, so the top-level `--version` must still be an envelope.

    The raw-token scan used to stop at any token that spelled a sub-command name, so a project with
    a config file called `rollback` turned `--config rollback --json --version` into argparse's own
    plain-text version (exit 0, no envelope). The value is consumed, not scanned.
    """
    root = _project(CONFIG, {"rollback": CONFIG.replace("project: envelope", "project: envelope-rollback")})
    for args in (["--config", "rollback"], ["--config=rollback"]):
        code, version = _run(root, *args, "--version")
        assert code == 0, (args, version)
        assert version["command"] == "version" and version["ok"] is True, (args, version)
        assert version["data"]["version"] == VERSION, (args, version)
        assert version["env"] == "local", (args, version)  # resolved *from* the file that was consumed

    # the same scan names the real sub-command in a code-2 envelope: a value is never a command
    code, usage = _run(root, "--config", "rollback", "sync", "bogus")
    assert code == 2 and usage["command"] == "sync", usage

    # and a sub-command's own --version option still reaches the sub-command
    assert _run(root, "sync", "all")[0] == 0
    _commit(root, "sync")
    code, rolled = _run(root, "rollback", "--alias", "production", "--prompt", "support_system", "--version", "1")
    assert code == 0 and rolled["command"] == "rollback", rolled


def test_every_offline_command_uses_the_exit_code_as_its_envelope_code():
    """The matrix above is one case per code; this one sweeps the daily commands on one project."""
    root = _project()
    for args in (
        ["plugins"],
        ["doctor"],
        ["guard"],
        ["baseline"],
        ["cost"],
        ["config", "validate"],
        ["config", "schema"],
        ["config", "explain"],
        ["sync", "all", "--check"],
    ):
        code, env_dict = _run(root, *args)
        assert env_dict["code"] == code, (args, code, env_dict["code"])


# ---------------------------------------------------------------------------
# (h)-(j) parse errors, broken YAML and a printing hook: stdout stays a contract
# ---------------------------------------------------------------------------
def test_argparse_error_is_a_code_two_envelope_naming_the_offending_argument():
    """D1: a bad flag used to print nothing on stdout and exit 2; now it is a code-2 envelope."""
    root = _project()
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", "plugins", "--made-up-flag"],
        cwd=str(root),
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, proc.stderr
    env_dict = _one_document(proc.stdout)  # exactly one JSON document, nothing else
    assert set(env_dict) == ENVELOPE_KEYS
    assert env_dict["ok"] is False and env_dict["code"] == 2
    assert env_dict["command"] == "plugins"  # the subcommand named on the command line
    assert "--made-up-flag" in env_dict["errors"][0]["message"]
    assert "usage:" in proc.stderr  # humans still get argparse's usage line, on stderr

    # an unknown top-level flag, an invalid choice and a missing subcommand are all code 2
    for args, command, fragment in (
        (["plugins", "--made-up-flag"], "plugins", "--made-up-flag"),
        (["sync", "bogus"], "sync", "bogus"),
        (["eval", "--dataset", "smoke", "--stage", "nope"], "eval", "nope"),
        ([], "cli", "required: cmd"),
    ):
        code, envelope_dict = _run(root, *args)
        assert code == 2 and envelope_dict["code"] == 2, (args, code, envelope_dict)
        assert envelope_dict["command"] == command, (args, envelope_dict["command"])
        assert fragment in envelope_dict["errors"][0]["message"], (args, envelope_dict)
        assert envelope_dict["env"] in ("local", os.environ.get("EVALOPS_ENV", "local"))

    # `--help` keeps argparse's exit 0 and never becomes an envelope
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--help"], cwd=str(root), env=_env(), capture_output=True, text=True
    )
    assert proc.returncode == 0 and proc.stdout.startswith("usage:"), proc.stdout[:200]
    # in-process embedders get the code back instead of a raised SystemExit(2)
    assert cli_mod.main(["--json", "plugins", "--made-up-flag"]) == 2


def test_malformed_yaml_is_a_usage_error_not_a_runtime_error():
    """D2: an unparseable document is exit 2 (config/usage), never the exit 5 a crash maps to."""
    root = _project()
    (root / "evalops.yaml").write_text(CONFIG + "datasets: [smoke\n")  # unterminated flow sequence

    code, env_dict = _run(root, "plugins")
    assert code == 2, env_dict
    assert env_dict["ok"] is False and env_dict["code"] == 2
    assert "invalid YAML" in env_dict["errors"][0]["message"]
    assert env_dict["errors"][0]["details"]["path"].endswith("evalops.yaml")
    assert env_dict["env"] == "local"  # falls back to EVALOPS_ENV when the file cannot be read

    # the same failure is exit 2 without --json (human output stays free-form)
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "plugins"], cwd=str(root), env=_env(), capture_output=True, text=True
    )
    assert proc.returncode == 2 and "invalid YAML" in proc.stdout + proc.stderr


def test_a_printing_hook_cannot_corrupt_the_json_document():
    """D3: in --json mode stdout is the envelope only; whatever dispatch prints goes to stderr."""
    root = _project(NOISY_CONFIG, {"noisy_app.py": NOISY_APP})
    assert _run(root, "sync", "all")[0] == 0
    _commit(root, "sync")
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", "eval", "--dataset", "smoke", "--stage", "dev"],
        cwd=str(root),
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env_dict = _one_document(proc.stdout)  # exactly one document, so the prints did not leak in
    assert env_dict["ok"] is True and env_dict["code"] == 0
    assert "noisy hook: generating an answer" in proc.stderr
    assert "noisy hook" not in proc.stdout


def test_fd_level_capture_stops_a_hook_bypassing_sys_stdout():
    """D1: `sys.__stdout__` and a child process both write to fd 1, so both are captured.

    `contextlib.redirect_stdout` only rebinds `sys.stdout`; the hook here ignores it twice. The
    envelope must stay the only thing on stdout, with the bypassed text forwarded to stderr.
    """
    root = _project(BYPASS_CONFIG, {"bypass_app.py": BYPASS_APP})
    assert _run(root, "sync", "all")[0] == 0
    _commit(root, "sync")
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", "eval", "--dataset", "smoke", "--stage", "dev"],
        cwd=str(root),
        env=_env(),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    env_dict = _one_document(proc.stdout)  # exactly one document: neither bypass reached stdout
    assert env_dict["ok"] is True and env_dict["code"] == 0
    assert env_dict["command"] == "eval"
    assert "bypass: wrote sys.__stdout__" in proc.stderr, proc.stderr
    assert "bypass: child process stdout" in proc.stderr, proc.stderr
    # both arrived in the order the hook wrote them, and never inside the document
    assert proc.stderr.index("sys.__stdout__") < proc.stderr.index("child process stdout")
    assert "bypass: wrote sys.__stdout__" not in proc.stdout and "bypass: child" not in proc.stdout


def test_in_process_capture_also_catches_a_substituted_sys_stdout():
    """D1: an embedder's `sys.stdout` is a *separate stream*, so fd 1 alone misses its writes.

    That is how pytest ``capsys`` and ``contextlib.redirect_stdout`` present stdout: a hook's
    ``print`` never touches file descriptor 1, so the descriptor capture let it land in front of the
    envelope. `--json` must rebind `sys.stdout` to stderr for the dispatch as well, and the envelope
    must still render to the restored `sys.stdout` afterwards.
    """
    root = _project()

    def printing_dispatch(args, cfg):
        print("in-process hook: written to the substituted sys.stdout")
        return {"summary": "in-process"}

    saved_dispatch = cli_mod.dispatch
    out, err = io.StringIO(), io.StringIO()
    cwd, saved_stdout, saved_stderr = os.getcwd(), sys.stdout, sys.stderr
    cli_mod.dispatch = printing_dispatch
    os.chdir(root)
    sys.stdout, sys.stderr = out, err
    try:
        code = cli_mod.main(["--json", "plugins"])
    finally:
        sys.stdout, sys.stderr = saved_stdout, saved_stderr
        os.chdir(cwd)
        cli_mod.dispatch = saved_dispatch

    assert code == 0
    env_dict = _one_document(out.getvalue())  # the envelope rendered to the restored stream ...
    assert env_dict["ok"] is True and env_dict["command"] == "plugins", env_dict
    assert "in-process hook" not in out.getvalue()  # ... and nothing else did
    assert "in-process hook: written to the substituted sys.stdout" in err.getvalue(), err.getvalue()
    assert '"schema_version"' not in err.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# (l) env is the resolved environment, including for envelopes built before the config loads (D2)
# ---------------------------------------------------------------------------
def test_version_and_usage_envelopes_report_the_projects_default_env():
    """D2: `--version` and a parse error used to hard-code `env: local` (CONTRACT.md §2)."""
    staging = CONFIG.replace("default_env: local", "default_env: staging")
    root = _project(staging)
    # the config is found the same way `load()` finds it, including by scanning upward from a
    # subdirectory; here EVALOPS_ENV is genuinely unset.
    nested = _mkdir(root / "prompts")

    code, version = _run(nested, "--version", env={"EVALOPS_ENV": None})
    assert code == 0 and version["command"] == "version", version
    assert version["env"] == "staging", version

    code, usage = _run(nested, "plugins", "--made-up-flag", env={"EVALOPS_ENV": None})
    assert code == 2 and usage["code"] == 2, usage
    assert usage["env"] == "staging", usage

    # --config / --config= point at the same file explicitly from a directory with no config
    empty = pathlib.Path(tempfile.mkdtemp(prefix="evalops-noconfig-"))
    for args in (["--config", str(root / "evalops.yaml")], [f"--config={root / 'evalops.yaml'}"]):
        code, version = _run(empty, *args, "--version", env={"EVALOPS_ENV": None})
        assert code == 0 and version["env"] == "staging", (args, version)

    # EVALOPS_ENV still wins over default_env
    code, version = _run(root, "--version", env={"EVALOPS_ENV": "other"})
    assert code == 0 and version["env"] == "other", version

    # no readable config -> `local`, and the usage path still reports its own verdict
    code, version = _run(empty, "--version", env={"EVALOPS_ENV": None})
    assert code == 0 and version["env"] == "local", version
    code, usage = _run(empty, "plugins", "--made-up-flag", env={"EVALOPS_ENV": None})
    assert code == 2 and usage["env"] == "local", usage


def test_env_resolution_never_turns_a_version_or_usage_path_into_a_failure():
    """D2: a broken config is still read best-effort, and never makes `--version` fail."""
    root = _project()
    (root / "evalops.yaml").write_text("default_env: [not, a, string]\n")  # unusable document
    code, version = _run(root, "--version", env={"EVALOPS_ENV": None})
    assert code == 0 and version["ok"] is True and version["env"] == "local", version

    (root / "evalops.yaml").write_text(CONFIG)  # a valid file: its default_env is reported
    code, version = _run(root, "--version", env={"EVALOPS_ENV": None})
    assert code == 0 and version["env"] == "local", version


#: D2: `default_env` values that used to make env resolution raise *outside* its handler.
MALFORMED_DEFAULT_ENVS = (
    ("recursive YAML alias", "default_env: &loop [*loop]\n"),  # RecursionError inside `_expand`
    ("non-string mapping", "default_env: {name: [local]}\n"),  # may never be expanded or coerced
    ("non-string scalar", "default_env: 42\n"),
)


def test_env_resolution_is_total_for_an_unusable_default_env():
    """D2: env resolution never raises, so it can never rewrite a version or usage exit code.

    `default_env: &loop [*loop]` made `_expand` recurse until `RecursionError` escaped the handler,
    turning `evalops --version` into exit 1 plus a traceback. The whole resolution (find, load,
    expand) is inside one handler, and only a string is ever expanded.
    """
    for label, tail in MALFORMED_DEFAULT_ENVS:
        root = pathlib.Path(tempfile.mkdtemp(prefix="evalops-bad-default-env-"))
        (root / "evalops.yaml").write_text("project: bad-env\napp: {predict_fn: x:y}\n" + tail)

        code, version = _run(root, "--version", env={"EVALOPS_ENV": None})
        assert code == 0, (label, code, version)
        assert version["ok"] is True and version["command"] == "version", (label, version)
        assert version["env"] == "local", (label, version)

        # the same document feeds the usage path, which keeps its own verdict (exit 2 with envelope)
        code, usage = _run(root, "plugins", "--made-up-flag", env={"EVALOPS_ENV": None})
        assert code == 2 and usage["code"] == 2, (label, code, usage)
        assert usage["env"] == "local", (label, usage)

        # without --json too: `--version` stays exit 0 with no traceback (the reported defect)
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "--version"],
            cwd=str(root),
            env=_env({"EVALOPS_ENV": None}),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (label, proc.stderr)
        assert proc.stdout.startswith(f"evalops {VERSION}"), (label, proc.stdout)
        assert "Traceback" not in proc.stderr, (label, proc.stderr)


# ---------------------------------------------------------------------------
# (m) money fields keep their types in the envelope (masked amounts must not hide a regression)
# ---------------------------------------------------------------------------
def test_the_cost_envelope_types_its_money_fields():
    """`month_spent_usd` is always a float >= 0; `per_month_usd` is null or a positive number."""
    code, cost = _run(_project(), "cost")
    assert code == 0 and cost["ok"] is True, cost
    spent, cap = cost["data"]["month_spent_usd"], cost["data"]["per_month_usd"]
    assert isinstance(spent, float) and spent >= 0, cost["data"]
    assert isinstance(cap, (int, float)) and not isinstance(cap, bool) and cap > 0, cost["data"]

    # an unset monthly cap stays null, and the spent amount keeps its type on a fresh project
    no_cap = _project(CONFIG.replace("budgets: {per_run_usd: 5, per_month_usd: 50}", "budgets: {per_run_usd: 5}"))
    code, cost = _run(no_cap, "cost")
    assert code == 0, cost
    assert cost["data"]["per_month_usd"] is None, cost["data"]
    assert isinstance(cost["data"]["month_spent_usd"], float) and cost["data"]["month_spent_usd"] >= 0, cost["data"]
