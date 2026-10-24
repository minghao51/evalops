"""WP1.1: payload redaction at the three outbound sinks (CONTRACT §5.1).

Falsifies, in order: (a) a planted secret (`sk-live-PLANTED-9f3a`, `alice@example.com`,
`123-45-6789`) never reaches a sink after a full local `evalops eval` -- every ledger artifact
body, the `evalops/rows.jsonl` trace/output payload, `AgentTrace.to_dict()`, the cassette file and
`.evalops/out/last_eval.json` -- while `app_version` is identical to an `EVALOPS_REDACT=off` run of
the same project; (b) `Cassette.save` redacts the copy it writes and leaves `entries`/`fingerprint`
untouched; (c) `redact(redact(x)) == redact(x)` for hypothesis-generated nested payloads with all
four rule kinds; (d) each rule kind has an expected value (`**` depth, `[]` list traversal, `*`
single segment, regex default/custom replacement, hash determinism, hook `None` = unchanged);
(e) `mode: strict` refuses to persist a payload a rule still matches (exit 5) and writes nothing;
(f) `EVALOPS_REDACT=off` persists verbatim and an unknown value is exit 2; (g) `evalops redact
--check` never writes, renders diffs / capped counts, and exits 0 even when it finds differences;
(h) the `redact:` config section is closed (`redact.<key>` is exit 2) and a bad regex names the
pattern.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import copy
import hashlib
import json
import os
import pathlib
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from typing import cast

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from evalops import cli as cli_mod  # noqa: E402
from evalops import config as config_mod  # noqa: E402
from evalops import locks, run  # noqa: E402
from evalops import tools as tools_mod  # noqa: E402
from evalops.contract import formats, redact  # noqa: E402
from evalops.contract.redact import Redactor, Rules  # noqa: E402
from evalops.ports import Ledger  # noqa: E402
from evalops.registry import Registry  # noqa: E402
from evalops.tools import Cassette  # noqa: E402
from evalops.traces.model import AgentTrace, Span  # noqa: E402
from evalops.util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError  # noqa: E402

SECRETS = ("sk-live-PLANTED-9f3a", "alice@example.com", "123-45-6789")

# ---------------------------------------------------------------------------
# the shared strict project: secrets in inputs, in tool traffic, in traces and in outputs
# ---------------------------------------------------------------------------
CONFIG = r"""
project: redact
default_env: local
plugins: {prompts: git, datasets: git, ledger: sql, tracer: none, evaluator: core}
app:
  behavior_config: secret_app:behavior_config
  predict_fn: secret_app:build_predict_fn
  scorers: secret_app:build_scorers
  judge_meta: secret_app:judge_meta
datasets: {smoke: {frozen: true}}
tools: {mode_by_stage: {dev: record}}
eval: {max_workers: 1, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}
traces: {capture: true, max_payload_chars: 4000}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
redact:
  mode: strict
  fields: ["inputs.password", "spans[].inputs.authorization"]
  hash: ["inputs.customer_id"]
  patterns:
    - {name: apikey, regex: 'sk-live-[A-Za-z0-9-]+'}
    - {name: email, regex: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}'}
    - {name: ssn, regex: '[0-9]{3}-[0-9]{2}-[0-9]{4}'}
"""

SECRET_APP = r"""
import evalops
from evalops.tools import replayable
from evalops.traces import capture
from evalops.traces.model import Span

API_KEY = "sk-live-PLANTED-9f3a"
EMAIL = "alice@example.com"
SSN = "123-45-6789"


@replayable("lookup_account")
def lookup_account(email):
    return {"email": email, "api_key": API_KEY, "ssn": SSN}


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools": "lookup_account@1"}


def build_predict_fn(prompts):
    def predict(question, password="", email=EMAIL, ssn=SSN, customer_id=""):
        account = lookup_account(email)
        cap = capture.current()
        if cap is not None:
            cap.add(
                Span(
                    span_id="s-llm",
                    name="llm",
                    kind="llm",
                    inputs={"authorization": "Bearer " + password, "prompt": question},
                    outputs={"text": "account " + account["email"] + " key " + account["api_key"]},
                ),
                "test",
            )
        return "ticket for %s: key=%s ssn=%s" % (email, password or account["api_key"], ssn)

    return predict


@evalops.scorer("echo")
def echo(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [echo]


def judge_meta():
    return {"llm_judge_count": 1}
"""

DATASET = (
    json.dumps(
        {
            "inputs": {
                "question": "Where is my order?",
                "password": "sk-live-PLANTED-9f3a",
                "email": "alice@example.com",
                "ssn": "123-45-6789",
                "customer_id": "acct-98765",
            },
            "expectations": {"expected": "ticket"},
        }
    )
    + "\n"
    + json.dumps(
        {
            "inputs": {
                "question": "Second row",
                "password": "sk-live-PLANTED-9f3a",
                "email": "alice@example.com",
                "ssn": "123-45-6789",
                "customer_id": "acct-98765",
            }
        }
    )
    + "\n"
)

# A hook that "redacts" nothing: it puts the original secret back for a matched field, so `strict`
# must refuse to persist the payload instead of writing it.
LEAKY_HOOK = """
def leaky(path, value):
    if path == "inputs.password":
        return "sk-live-PLANTED-9f3a"
    return None
"""

# The same idea against the *first* artifact (`evalops/cost_estimate.json`): `llm_calls` is a real
# key of the estimate, and a hook that keeps the original value there defeats the rule.
LEAKY_ESTIMATE_HOOK = """
def leaky_estimate(path, value):
    if path == "llm_calls":
        return 4
    return None
"""

# A hook that defeats the field rule for a cassette entry's dotted path.
LEAKY_ENTRY_HOOK = """
def leaky_entry(path, value):
    if path == "k1.args.email":
        return "alice@example.com"
    return None
"""

# An idempotent hook: a string carrying the sentinel becomes a marker; the marker carries no
# sentinel, so a second pass is a no-op.
MASK_HOOK = """
def mask(path, value):
    if isinstance(value, str) and "secret-" in value:
        return "[REDACTED:hook]"
    return None
"""

STRICT_CONFIG = CONFIG
OFF_CONFIG = CONFIG.replace("  mode: strict", "  mode: off")
#: Everything before the `redact:` block, so a test project can carry a different rule set.
CONFIG_PREFIX = CONFIG.split("redact:")[0]
EMAIL_RULE_CONFIG = CONFIG_PREFIX + (
    "redact:\n  mode: strict\n  patterns:\n"
    "    - {name: email, regex: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}'}\n"
)
APIKEY_RULE_CONFIG = CONFIG_PREFIX + (
    "redact:\n  mode: strict\n  patterns:\n    - {name: apikey, regex: 'sk-live-[A-Za-z0-9-]+'}\n"
)

#: A fixed 64-hex digest (a lock entry's `fingerprint`/`sha256` has the same shape).
DIGEST = hashlib.sha256(b"evalops redaction fixture").hexdigest()

#: Strict rules that also match an artifact *digest* (`[0-9a-f]{64}`), plus the `fields:`/`hash:`
#: pair that matches the row payloads. Routing `evalops/manifest.json` through the redaction
#: boundary would rewrite every sha256 it lists -- the round-3 defect this rule set reproduces.
DIGEST_RULE_CONFIG = CONFIG_PREFIX + (
    "redact:\n"
    "  mode: strict\n"
    '  fields: ["inputs.password", "spans[].inputs.authorization"]\n'
    '  hash: ["inputs.customer_id"]\n'
    "  patterns:\n"
    "    - {name: hex, regex: '[0-9a-f]{64}'}\n"
    "    - {name: apikey, regex: 'sk-live-[A-Za-z0-9-]+'}\n"
    "    - {name: email, regex: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}'}\n"
    "    - {name: ssn, regex: '[0-9]{3}-[0-9]{2}-[0-9]{4}'}\n"
)

#: The same rules as a plain dict, for in-process `_cfg` / `_put_artifact` staging checks.
DIGEST_RULES = {
    "mode": "strict",
    "fields": ["inputs.password", "spans[].inputs.authorization"],
    "hash": ["inputs.customer_id"],
    "patterns": [
        {"name": "hex", "regex": "[0-9a-f]{64}"},
        {"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"},
    ],
}

#: The four payloads `_put_artifact` stages (`evalops/manifest.json` is derived metadata, not one).
STAGED_ARTIFACTS = (
    "evalops/cost_estimate.json",
    "evalops/prompts.lock.json",
    "evalops/datasets.lock.json",
    "evalops/rows.jsonl",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmpdir(prefix: str = "evalops-redact-") -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _commit(root: pathlib.Path, message: str) -> None:
    _git(root, "add", "-A")
    subprocess.run(["git", "commit", "-qm", message], cwd=root, capture_output=True, text=True)


def _project(config: str = CONFIG, files: dict[str, str] | None = None) -> pathlib.Path:
    """A self-contained, git-init'd evalops project with secrets in its dataset."""
    root = _tmpdir()
    (root / "evalops.yaml").write_text(config)
    (root / ".gitignore").write_text(".evalops/\n__pycache__/\n")
    (root / "prompts").mkdir()
    (root / "prompts" / "support_system.txt").write_text("You are a support agent.\n")
    (root / "datasets").mkdir()
    (root / "datasets" / "smoke.jsonl").write_text(DATASET)
    (root / "secret_app.py").write_text(SECRET_APP)
    for name, text in (files or {}).items():
        (root / name).write_text(text)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "redact@example.test")
    _git(root, "config", "user.name", "redact")
    _commit(root, "init")
    return root


def _env(extra: dict | None = None) -> dict[str, str]:
    env = {
        **os.environ,
        "PYTHONPATH": f"{REPO / 'src'}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "EVALOPS_ACTOR": "test",
        "EVALOPS_ENV": "local",
        **(extra or {}),
    }
    for stale in ("EVALOPS_CONFIG", "EVALOPS_ALLOW_FROZEN_UPDATE", "EVALOPS_REDACT"):
        if stale not in (extra or {}):
            env.pop(stale, None)
    return env


def _cli(root: pathlib.Path, *args: str, env: dict | None = None) -> tuple[int, dict]:
    """Run the real CLI as a subprocess; --json stdout must be exactly one document."""
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=_env(env),
        capture_output=True,
        text=True,
    )
    text = proc.stdout.strip()
    assert text.startswith("{"), f"stdout is not a JSON document: {proc.stdout!r}\n{proc.stderr}"
    document, end = json.JSONDecoder().raw_decode(text)
    assert end == len(text), f"extra output after the document: {text[end:]!r}"
    return proc.returncode, document


def _cli_human(root: pathlib.Path, *args: str, env: dict | None = None) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", *args], cwd=str(root), env=_env(env), capture_output=True, text=True
    )
    return proc.returncode, proc.stdout + proc.stderr


def _cfg(root: pathlib.Path, section: dict | None = None) -> config_mod.Config:
    data = {"project": "redact", "app": {}}
    if section is not None:
        data["redact"] = section
    return config_mod.Config(data, root)


def _ledger(cfg: config_mod.Config) -> Ledger:
    """The configured ledger plugin, as the port (so `get_artifact` is a typed read)."""
    return cast(Ledger, Registry(cfg).get("ledger"))


def _raises(fn) -> EvalOpsError:
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError, the call returned normally")


def _connect(root: pathlib.Path):
    return sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))


def _artifacts(root: pathlib.Path) -> dict[str, dict[str, str]]:
    conn = _connect(root)
    try:
        rows = conn.execute("SELECT run_id, name, body FROM evalops_artifacts").fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, str]] = {}
    for run_id, name, body in rows:
        out.setdefault(run_id, {})[name] = body
    return out


def _run_ids(root: pathlib.Path) -> list[str]:
    conn = _connect(root)
    try:
        return [row[0] for row in conn.execute("SELECT run_id FROM evalops_runs ORDER BY start_ms").fetchall()]
    finally:
        conn.close()


def _artifact(root: pathlib.Path, run_id: str, name: str) -> str | None:
    """The stored body of one artifact, or None -- what `ledger.get_artifact` raises `KeyError` for."""
    conn = _connect(root)
    try:
        row = conn.execute("SELECT body FROM evalops_artifacts WHERE run_id=? AND name=?", (run_id, name)).fetchone()
    finally:
        conn.close()
    return None if row is None else row[0]


def _assert_no_secrets(text: str, where: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{where}: the planted secret {secret!r} reached a persisted sink"


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# (a) + (f) the planted secret never reaches a sink; `off` is the verbatim baseline
# ---------------------------------------------------------------------------
def test_planted_secrets_never_reach_a_sink():
    """Sinks asserted: every ledger artifact body, `evalops/rows.jsonl`, the persisted trace payload
    (and its `AgentTrace.to_dict` round-trip), the cassette file, `.evalops/out/last_eval.json`, and
    the `evalops traces` output.

    Deliberately *not* asserted (kept honest, see CONTRACT §5.1): ledger run params and configuration
    provenance (`behavior_config`, the app_version hash inputs, run names/tags) stay unredacted by
    design; `gate_report.md` is written by `gate`, which this test does not run; and the stdout of
    arbitrary other commands is out of scope.
    """
    root = _project()
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        code, out = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        strict_version, group = out["data"]["app_version"], out["data"]["group"]

        artifacts = _artifacts(root)
        runs = _run_ids(root)
        assert len(runs) == 1, runs
        bodies = artifacts[runs[0]]
        assert "evalops/rows.jsonl" in bodies and "evalops/cost_estimate.json" in bodies, sorted(bodies)
        for name, body in bodies.items():
            _assert_no_secrets(body, f"ledger artifact {name}")

        rows = bodies["evalops/rows.jsonl"]
        assert "[REDACTED:inputs.password]" in rows
        assert "[REDACTED:email]" in rows and "[REDACTED:ssn]" in rows
        assert "sha256:" in rows  # the `hash:` rule replaced inputs.customer_id deterministically

        # the manifest hashes the *stored* (redacted) bytes, not the pre-redaction text
        manifest = json.loads(bodies["evalops/manifest.json"])
        assert manifest["artifacts"]["evalops/rows.jsonl"] == hashlib.sha256(rows.encode()).hexdigest()

        cassette = root / "cassettes" / "smoke.jsonl"
        assert cassette.exists()
        cassette_text = cassette.read_text()
        _assert_no_secrets(cassette_text, "cassette file")
        assert json.loads(cassette_text.splitlines()[0])["_meta"]["redacted"] is True

        last_eval = root / ".evalops" / "out" / "last_eval.json"
        assert last_eval.exists()
        _assert_no_secrets(last_eval.read_text(), "last_eval.json")

        # `AgentTrace.to_dict()`: the stored trace round-trips through to_dict without a secret
        row = json.loads(rows.splitlines()[0])
        assert row["trace"] is not None and row["trace"]["spans"]
        restored = AgentTrace.from_dict(row["trace"])
        assert restored is not None
        _assert_no_secrets(json.dumps(restored.to_dict()), "AgentTrace.to_dict")

        # `evalops traces` reads the same rows.jsonl, so its rendered output must be clean too
        code, traced = _cli(root, "traces", "--group", group)
        assert code == 0, traced
        _assert_no_secrets(json.dumps(traced), "evalops traces --json output")

        # `evalops redact --check --group` on the already-redacted group is a no-op, twice
        for _ in range(2):
            code, check = _cli(root, "redact", "--check", "--group", group)
            assert code == 0 and check["data"]["changed"] == 0, check
            assert check["data"]["mode"] == "strict"
            assert {sink["sink"].split(":")[-1] for sink in check["data"]["sinks"]} >= {"evalops/rows.jsonl"}

        # an unknown EVALOPS_REDACT is exit 2 with an envelope, before a single sink is touched
        code, bad = _cli(root, "plan", "--dataset", "smoke", "--stage", "dev", env={"EVALOPS_REDACT": "loud"})
        assert code == EXIT_USAGE, bad
        assert "not a legal redaction mode" in bad["errors"][0]["message"], bad

        # `EVALOPS_REDACT=off` in the same repo: identical app_version, verbatim payloads
        cassette.unlink()
        code, off = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev", env={"EVALOPS_REDACT": "off"})
        assert code == 0, off
        assert off["data"]["app_version"] == strict_version, "redaction must not move app_version"
        off_rows = _artifacts(root)[off["data"]["run_ids"][0]]["evalops/rows.jsonl"]
        assert "sk-live-PLANTED-9f3a" in off_rows, "EVALOPS_REDACT=off must persist verbatim"
        assert "sk-live-PLANTED-9f3a" in cassette.read_text()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_off_mode_is_byte_identical_and_unknown_value_is_exit_two():
    root = _tmpdir()
    try:
        cfg = _cfg(root, {"mode": "off", "fields": ["inputs.password"]})
        redactor = redact.from_config(cfg)
        assert redactor.mode == "off" and not redactor.enabled
        text = json.dumps({"inputs": {"password": "sk-live-PLANTED-9f3a"}})
        assert redactor.enforce_text(text) == text  # byte-identical, not even re-serialized
        assert redactor.redact({"inputs": {"password": "x"}})[1] == []

        os.environ["EVALOPS_REDACT"] = "loud"
        try:
            exc = _raises(lambda: redact.from_config(cfg))
            assert exc.code == EXIT_USAGE and "strict, off" in str(exc), str(exc)
        finally:
            os.environ.pop("EVALOPS_REDACT", None)

        # the env switch wins over the configured mode
        os.environ["EVALOPS_REDACT"] = "strict"
        try:
            assert redact.from_config(cfg).mode == "strict"
        finally:
            os.environ.pop("EVALOPS_REDACT", None)
        redact.reset()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b) Cassette.save redacts the copy it writes, never the identity inputs
# ---------------------------------------------------------------------------
def test_cassette_save_redacts_the_file_and_keeps_entries_and_fingerprint():
    root = _tmpdir()
    try:
        section = {
            "mode": "strict",
            "patterns": [
                {"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"},
                {"name": "email", "regex": "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"},
            ],
        }
        path = root / "cassettes" / "smoke.jsonl"
        cas = Cassette(path, "record")
        cas.entries["k1"] = {
            "key": "k1",
            "tool": "lookup_account",
            "args": {"email": "alice@example.com"},
            "response": {"api_key": "sk-live-PLANTED-9f3a"},
            "error": None,
        }
        cas.dirty = True
        entries_before = copy.deepcopy(cas.entries)
        fingerprint_before = cas.fingerprint()

        redact.activate(_cfg(root, section))
        cas.save()

        assert cas.entries == entries_before, "save() must not mutate the in-memory entries"
        assert cas.fingerprint() == fingerprint_before, "save() must not move the fingerprint"
        written = path.read_text()
        _assert_no_secrets(written, "cassette file")
        meta = json.loads(written.splitlines()[0])["_meta"]
        assert meta["schema_version"] == formats.FORMAT_VERSION
        assert meta["redacted"] is True and re.fullmatch(r"[0-9a-f]{12}", meta["redaction_rules"])

        # a second save from the same in-memory entries is byte-identical apart from created_at metadata
        before_hash = [line for line in written.splitlines()[1:]]
        cas.dirty = True
        cas.save()
        assert path.read_text().splitlines()[1:] == before_hash

        # no rule matches -> today's exact output (no `redacted` metadata, verbatim entry)
        plain = Cassette(root / "cassettes" / "other.jsonl", "record")
        plain.entries["k1"] = entries_before["k1"]
        plain.dirty = True
        redact.activate(_cfg(root, {}))
        plain.save()
        text = (root / "cassettes" / "other.jsonl").read_text()
        assert json.loads(text.splitlines()[0])["_meta"].keys() == {"schema_version", "evalops_version", "created_at"}
        assert json.loads(text.splitlines()[1]) == entries_before["k1"]
        assert "sk-live-PLANTED-9f3a" in text
        redact.reset()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b2) non-JSON values are normalized to the serialized tree before scanning
# ---------------------------------------------------------------------------
class _LeakyStr:
    """A custom object whose `str()` carries a secret: `json.dumps(default=str)` would persist it."""

    def __init__(self, secret: str = "sk-live-PLANTED-9f3a") -> None:
        self.secret = secret

    def __str__(self) -> str:
        return self.secret


def _nonjson_entry() -> dict:
    return {
        "key": "k1",
        "tool": "lookup_account",
        "args": {"tuple": ("sk-live-PLANTED-9f3a",)},
        "response": {"obj": _LeakyStr()},
        "error": None,
    }


def test_non_json_values_are_normalized_before_the_walk_and_the_strict_rescan():
    """A tuple and a custom object escape a dict/list walk while `default=str` persists their secret.

    Normalizing to the serialized tree is what makes both the redaction and the strict re-scan see
    them; without it `strict` would persist the secret and never refuse.
    """
    root = _tmpdir()
    try:
        section = {"mode": "strict", "patterns": [{"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"}]}
        path = root / "cassettes" / "nonjson.jsonl"
        cas = Cassette(path, "record")
        cas.entries["k1"] = _nonjson_entry()
        cas.dirty = True
        fingerprint = cas.fingerprint()
        redact.activate(_cfg(root, section))
        cas.save()

        _assert_no_secrets(path.read_text(), "cassette with a tuple and a custom object")
        stored = json.loads(path.read_text().splitlines()[1])
        # the persisted value is the normalized tree: tuple -> list, object -> its str()
        assert stored["args"]["tuple"] == ["[REDACTED:apikey]"]
        assert stored["response"]["obj"] == "[REDACTED:apikey]"
        assert cas.fingerprint() == fingerprint, "save() must not move the identity hash"

        # a hook that maps the normalized list element back is caught by the strict re-scan: after
        # normalization the tuple element is a walkable node, so residual() sees it -> exit 5
        (root / "leaky_json_hook.py").write_text(
            "SECRET = 'sk-live-PLANTED-9f3a'\n\n"
            "def leaky(path, value):\n"
            "    if path == 'k1.args.tuple[]':\n"
            "        return SECRET\n"
            "    return None\n"
        )
        refused = root / "cassettes" / "refused.jsonl"
        cas2 = Cassette(refused, "record")
        cas2.entries["k1"] = _nonjson_entry()
        cas2.dirty = True
        redact.activate(_cfg(root, {**section, "hook": "leaky_json_hook:leaky"}))
        exc = _raises(cas2.save)
        assert exc.code == EXIT_RUNTIME and "strict redaction" in str(exc), str(exc)
        assert not refused.exists(), "a refused save must not create the file"
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b3) a redacted cassette is lossy on reload; `off`/no-match stays byte-identical
# ---------------------------------------------------------------------------
def test_a_redacted_cassette_is_lossy_on_reload_but_off_and_no_match_are_unchanged():
    """(a) replay returns the redacted value, (b) the reloaded fingerprint moves iff redaction
    changed a semantic entry field, (c) `off` / a rule that matches nothing keeps the exact entry
    bytes and the fingerprint."""
    root = _tmpdir()
    try:

        @tools_mod.replayable("lookup_account")
        def lookup_account(email):
            return {"email": email, "api_key": "sk-live-PLANTED-9f3a"}

        def record(path):
            cas = Cassette(path, "record")
            with tools_mod.use(cas):
                lookup_account("alice@example.com")
            assert cas.dirty
            return cas

        def replay(path):
            calls = []

            @tools_mod.replayable("lookup_account")
            def replayed(email):
                calls.append(email)
                return {"api_key": "the real tool must not run"}

            with tools_mod.use(Cassette(path, "replay")):
                response = replayed("alice@example.com")
            assert calls == [], "a replay hit must not call the tool"
            return response

        apikey = {"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"}

        # (a) + (b): redaction changes a semantic field -> no secret on disk, replay is lossy, and
        # the reloaded fingerprint differs exactly because the entry's response changed
        redacted_path = root / "cassettes" / "redacted.jsonl"
        redact.activate(_cfg(root, {"mode": "strict", "patterns": [apikey]}))
        recorded = record(redacted_path)
        before = recorded.fingerprint()
        recorded.save()
        text = redacted_path.read_text()
        assert "sk-live-PLANTED-9f3a" not in text, "the api-key rule must redact the cassette"
        assert "[REDACTED:apikey]" in text
        assert replay(redacted_path)["api_key"] == "[REDACTED:apikey]"
        assert Cassette(redacted_path, "replay").fingerprint() != before, "a redacted field must move the fingerprint"

        # (c) `off` and a no-match rule: the entry line is byte-identical to the legacy writer, the
        # file gains no `redacted` metadata and the reloaded fingerprint is unchanged
        legacy_line = json.dumps(next(iter(recorded.entries.values())), sort_keys=True, default=str)
        for name, section in (
            ("off", {"mode": "off", "patterns": [apikey]}),
            ("nomatch", {"mode": "strict", "patterns": [{"name": "none", "regex": "zzz-[0-9]+"}]}),
        ):
            path = root / "cassettes" / f"{name}.jsonl"
            redact.activate(_cfg(root, section))
            cas = record(path)
            fingerprint = cas.fingerprint()
            cas.save()
            lines = path.read_text().splitlines()
            assert json.loads(lines[0])["_meta"].keys() == {"schema_version", "evalops_version", "created_at"}
            assert lines[1] == legacy_line, f"{name}: the entry bytes must be unchanged"
            assert Cassette(path, "replay").fingerprint() == fingerprint, f"{name}: the fingerprint must not move"
            assert replay(path)["api_key"] == "sk-live-PLANTED-9f3a", f"{name}: replay stays verbatim"
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (c) idempotence, hypothesis-driven, over all four rule kinds
# ---------------------------------------------------------------------------
_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-5, 5),
    st.text(max_size=16),
    st.sampled_from(
        ["secret-1", "a@b.co", "sk-live-X9", "[REDACTED:x]", "sha256:0123456789ab", "", "plain", "x.secret"]
    ),
)
_KEYS = st.sampled_from(["inputs", "items", "password", "api_key", "token", "secret", "note", "x"])
_PAYLOADS = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=2),
        st.dictionaries(_KEYS, children, max_size=3),
    ),
    max_leaves=7,
)

PROPERTY_RULES = {
    "mode": "strict",
    "fields": ["inputs.password", "*.api_key", "items[].token"],
    "hash": ["inputs.secret", "**.secret"],
    "patterns": [
        {"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"},
        {"name": "email", "regex": "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"},
    ],
    "hook": "redact_hooks:mask",
}


@settings(max_examples=40, deadline=None, database=None, suppress_health_check=[HealthCheck.too_slow])
@given(_PAYLOADS)
def test_redaction_is_idempotent(payload):
    root = _tmpdir()
    try:
        (root / "redact_hooks.py").write_text(MASK_HOOK)
        redactor = Redactor(RULES_CACHE.setdefault("rules", PROPERTY_RULES), root=root, mode="strict")
        original = copy.deepcopy(payload)
        once, first = redactor.redact(payload)
        twice, second = redactor.redact(once)
        assert twice == once, (payload, once, twice)
        assert second == [], (payload, second)
        assert payload == original, "redact() must not mutate its input"
        assert redactor.residual(once) == [], redactor.residual(once)
    finally:
        shutil.rmtree(root, ignore_errors=True)


#: One parsed rule set for the property test (the hook is resolved per payload's temp root).
RULES_CACHE: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# (d) one test per rule kind, with an expected value
# ---------------------------------------------------------------------------
def test_field_globs_cover_keys_segments_depth_and_lists():
    redactor = Redactor(
        {
            "mode": "strict",
            "fields": ["inputs.password", "*.api_key", "spans[].outputs.token", "**.secret", "deep.*.leaf"],
        }
    )
    value = {
        "inputs": {"password": "p", "other": "keep"},
        "creds": {"api_key": "k"},
        "nested": {"api_key": "k2"},
        "deeper": {"x": {"api_key": "k3"}},
        "spans": [{"outputs": {"token": "t1"}}, {"outputs": {"token": "t2"}}],
        "a": {"b": {"secret": "s"}},
        "deep": {"any": {"leaf": "l"}},
    }
    out, changes = redactor.redact(value)
    assert out == {
        "inputs": {"password": "[REDACTED:inputs.password]", "other": "keep"},
        "creds": {"api_key": "[REDACTED:*.api_key]"},
        "nested": {"api_key": "[REDACTED:*.api_key]"},
        # `*` is exactly one segment, so a two-level key is not matched by `*.api_key`
        "deeper": {"x": {"api_key": "k3"}},
        "spans": [
            {"outputs": {"token": "[REDACTED:spans[].outputs.token]"}},
            {"outputs": {"token": "[REDACTED:spans[].outputs.token]"}},
        ],
        "a": {"b": {"secret": "[REDACTED:**.secret]"}},
        "deep": {"any": {"leaf": "[REDACTED:deep.*.leaf]"}},
    }
    assert {change.path for change in changes} == {
        "inputs.password",
        "creds.api_key",
        "nested.api_key",
        "spans[].outputs.token",
        "a.b.secret",
        "deep.any.leaf",
    }
    # a glob that matches nothing is legal and touches nothing
    assert Redactor({"mode": "strict", "fields": ["nothing.here"]}).redact(value) == (value, [])
    # nothing matched at all -> the value is an unchanged deep copy, not the same object
    out, changes = Redactor({"mode": "strict", "fields": ["missing"]}).redact(value)
    assert changes == [] and out == value and out is not value


def test_patterns_substitute_string_leaves_with_default_and_custom_replacements():
    redactor = Redactor(
        {
            "mode": "strict",
            "patterns": [
                {"name": "email", "regex": "[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}"},
                {"name": "ssn", "regex": "[0-9]{3}-[0-9]{2}-[0-9]{4}", "replace": "[ssn]"},
            ],
        }
    )
    out, changes = redactor.redact({"a": "mail alice@example.com", "b": {"c": "ssn 123-45-6789"}, "n": 7})
    assert out == {"a": "mail [REDACTED:email]", "b": {"c": "ssn [ssn]"}, "n": 7}
    assert sorted(change.path for change in changes) == ["a", "b.c"]
    # no match -> untouched; a non-string leaf is never a pattern target
    assert redactor.redact({"a": "nothing here", "n": 123456789}) == ({"a": "nothing here", "n": 123456789}, [])
    # dictionary keys are not payload values and stay intact
    assert redactor.redact({"alice@example.com": "v"})[0] == {"alice@example.com": "v"}


def test_hash_rule_is_deterministic_and_marker_shaped():
    rules = {"mode": "strict", "hash": ["inputs.ssn"]}
    first = Redactor(rules).redact({"inputs": {"ssn": "123-45-6789"}})[0]["inputs"]["ssn"]
    second = Redactor(rules).redact({"inputs": {"ssn": "123-45-6789"}})[0]["inputs"]["ssn"]
    assert first == second == "sha256:" + hashlib.sha256(b'"123-45-6789"').hexdigest()[:12]
    assert re.fullmatch(r"sha256:[0-9a-f]{12}", first)
    # a different value hashes differently; a mapping value hashes its canonical form
    other = Redactor(rules).redact({"inputs": {"ssn": "999-99-9999"}})[0]["inputs"]["ssn"]
    assert other != first
    assert Redactor({"mode": "strict", "hash": ["inputs"]}).redact({"inputs": {"b": 1, "a": 2}})[0]["inputs"] == (
        "sha256:" + hashlib.sha256(b'{"a":2,"b":1}').hexdigest()[:12]
    )
    # already hashed -> untouched (idempotence)
    assert Redactor(rules).redact({"inputs": {"ssn": first}})[1] == []


def test_hash_rule_is_deterministic_across_processes():
    script = (
        f"import sys; sys.path.insert(0, {str(REPO / 'src')!r})\n"
        "from evalops.contract.redact import Redactor\n"
        "print(Redactor({'mode': 'strict', 'hash': ['inputs.ssn']}).redact({'inputs': {'ssn': '123-45-6789'}})"
        "[0]['inputs']['ssn'])\n"
    )
    hashes = {
        subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout.strip()
        for _ in range(2)
    }
    assert hashes == {"sha256:4b6f8243a6b4"}, hashes


def test_hook_none_leaves_a_value_unchanged_and_a_return_value_replaces_it():
    root = _tmpdir()
    try:
        (root / "redact_hooks.py").write_text(MASK_HOOK)
        redactor = Redactor({"mode": "strict", "hook": "redact_hooks:mask"}, root=root)
        out, changes = redactor.redact({"a": "secret-1", "b": "keep", "c": {"d": "secret-2"}})
        assert out == {"a": "[REDACTED:hook]", "b": "keep", "c": {"d": "[REDACTED:hook]"}}
        assert sorted(change.path for change in changes) == ["a", "c.d"]
        # None (no change) is not a change and is never a residual
        assert redactor.residual(out) == []
        # a hook spec that cannot be imported is a usage error naming the spec
        exc = _raises(lambda: Redactor({"mode": "strict", "hook": "not_a_module:fn"}, root=root))
        assert exc.code == EXIT_USAGE and "redact.hook" in str(exc)
        # a raising hook is a runtime failure, never a silent skip
        (root / "boom_hook.py").write_text("def boom(path, value):\n    raise RuntimeError('nope')\n")
        with_boom = Redactor({"mode": "strict", "hook": "boom_hook:boom"}, root=root)
        exc = _raises(lambda: with_boom.redact({"a": 1}))
        assert exc.code == EXIT_RUNTIME and "redact hook raised" in str(exc)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_to_dict_redacts_a_deep_copy_and_leaves_the_in_memory_trace_alone():
    root = _tmpdir()
    try:
        redact.activate(_cfg(root, {"mode": "strict", "fields": ["spans[].inputs.password"]}))
        trace = AgentTrace(
            spans=[Span(span_id="s1", name="llm", kind="llm", inputs={"password": "p"}, outputs="ok")],
            trace_id="t1",
        )
        out = trace.to_dict()
        assert out["spans"][0]["inputs"] == {"password": "[REDACTED:spans[].inputs.password]"}
        assert trace.spans[0].inputs == {"password": "p"}, "the in-memory trace must not change"
        assert trace.render()  # the rendered tree still reads the real payload
        redact.reset()
        assert trace.to_dict()["spans"][0]["inputs"] == {"password": "p"}
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


def test_artifact_sink_redacts_before_the_manifest_and_keeps_text_when_off():
    root = _tmpdir()
    try:
        line = json.dumps({"row_id": "0", "inputs": {"password": "sk-live-PLANTED-9f3a"}}) + "\n"
        artifacts: dict[str, str] = {}
        stored = run._put_artifact(_cfg(root, {}), artifacts, "evalops/rows.jsonl", line)
        assert stored == line == artifacts["evalops/rows.jsonl"], "mode off must be byte-identical"

        artifacts = {}
        strict = _cfg(root, {"mode": "strict", "patterns": [{"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"}]})
        stored = run._put_artifact(strict, artifacts, "evalops/rows.jsonl", line)
        assert stored == artifacts["evalops/rows.jsonl"], "the manifest must hash the stored bytes"
        _assert_no_secrets(stored, "rows.jsonl")
        assert json.loads(stored)["row_id"] == "0"  # still one valid JSON document per line
        redact.reset()
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b4) the manifest is derived metadata: never redacted, hashes the stored bytes
# ---------------------------------------------------------------------------
def test_manifest_hashes_survive_a_rule_that_matches_a_digest():
    """Round-3 defect: a rule that matches a sha256 must not rewrite `evalops/manifest.json`.

    With `[0-9a-f]{64}` configured in `mode: strict` (plus a `fields:`/`hash:` pair matching the row
    payloads), a real eval must store a manifest whose every digest is the sha256 of the *stored*
    artifact body, and every stored body must be the redacted one.
    """
    root = _project(DIGEST_RULE_CONFIG)
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        code, out = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        run_id = out["data"]["run_ids"][0]

        cfg = config_mod.load(str(root / "evalops.yaml"))
        ledger = _ledger(cfg)
        manifest_text = ledger.get_artifact(run_id, "evalops/manifest.json")
        manifest = json.loads(manifest_text)
        assert set(manifest["artifacts"]) == set(STAGED_ARTIFACTS), sorted(manifest["artifacts"])

        stored = {name: ledger.get_artifact(run_id, name) for name in STAGED_ARTIFACTS}
        for name, digest in manifest["artifacts"].items():
            assert hashlib.sha256(stored[name].encode()).hexdigest() == digest, name
            _assert_no_secrets(stored[name], f"stored artifact {name}")

        # the payload rules really ran: fields/hash/patterns on the rows, the hex pattern on a lock
        rows = stored["evalops/rows.jsonl"]
        assert "[REDACTED:inputs.password]" in rows and "[REDACTED:email]" in rows
        assert "sha256:" in rows, "the hash rule must have replaced inputs.customer_id"
        assert "[REDACTED:hex]" in stored["evalops/datasets.lock.json"], "the hex rule must match a digest"

        # the manifest itself is written verbatim: exactly what the stored bytes hash to, no marker
        assert "[REDACTED" not in manifest_text
        assert manifest_text == json.dumps(formats.manifest(stored))
        # ...even though these very rules match it; routing it through the boundary would rewrite
        # every digest and silently break artifact integrity (the defect this test reproduces)
        redactor = redact.from_config(cfg)
        assert redactor.mode == "strict" and redactor.enabled
        assert redactor.redact_json_text(manifest_text)[1], "the hex rule must match the manifest"
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (f2) off mode is bug-compatible: every staged payload keeps the writer's exact bytes
# ---------------------------------------------------------------------------
def _payload_shapes() -> dict[str, str]:
    """One representative text per staged artifact shape (the four `_put_artifact` payloads)."""
    estimate = json.dumps(formats.stamp({"rows": 2, "repeats": 1, "llm_calls": 4, "total_usd": 0.0012}), sort_keys=True)
    prompts_lock = json.dumps(
        locks.document({"support_system": {"name": "support_system", "version": 3, "sha256": DIGEST}}),
        sort_keys=True,
    )
    datasets_lock = json.dumps(locks.document({"smoke": {"rows": 2, "fingerprint": DIGEST}}), sort_keys=True)
    rows = "".join(
        json.dumps(
            formats.stamp(
                {"row_id": f"{i:05d}", "inputs": {"password": "sk-live-PLANTED-9f3a", "customer_id": "acct-98765"}}
            ),
            default=str,
        )
        + "\n"
        for i in range(2)
    )
    return {
        "evalops/cost_estimate.json": estimate,
        "evalops/prompts.lock.json": prompts_lock,
        "evalops/datasets.lock.json": datasets_lock,
        "evalops/rows.jsonl": rows,
    }


def test_off_mode_staging_is_byte_identical_for_all_four_payload_shapes():
    """`off` (and no `redact:` section) stages every payload verbatim, whichever switch turned it off."""
    root = _tmpdir()
    shapes = _payload_shapes()
    assert set(shapes) == set(STAGED_ARTIFACTS)
    try:
        # the rules would rewrite three of the four shapes, so byte-identity is not vacuous
        active = Redactor(DIGEST_RULES)
        for name in ("evalops/prompts.lock.json", "evalops/datasets.lock.json", "evalops/rows.jsonl"):
            assert active.enforce_text(shapes[name], name) != shapes[name], name

        variants: list[tuple[str, config_mod.Config]] = [
            ("no redact section", _cfg(root)),
            ("explicit mode: off", _cfg(root, {**DIGEST_RULES, "mode": "off"})),
        ]
        os.environ["EVALOPS_REDACT"] = "off"
        try:
            variants.append(("strict rules + EVALOPS_REDACT=off", _cfg(root, DIGEST_RULES)))
            for label, cfg in variants:
                artifacts: dict[str, str] = {}
                for name, text in shapes.items():
                    stored = run._put_artifact(cfg, artifacts, name, text)
                    assert stored == text, f"{label}: {name} was not byte-identical"
                    assert artifacts[name] == text, f"{label}: {name} stored text changed"
                assert artifacts == shapes, label
        finally:
            os.environ.pop("EVALOPS_REDACT", None)
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


def test_off_mode_real_eval_stores_exactly_the_writer_texts():
    """A real eval with `EVALOPS_REDACT=off` stores the writer's bytes for all four payloads.

    The writer text is captured at the redaction boundary (the unredacted-writer fixture for the
    same inputs), so the comparison needs no second run and no timestamp-sensitive artifact: each
    stored body must equal the text `_put_artifact` received. The manifest never reaches that
    boundary at all; its digests are verified against the stored bodies.
    """
    root = _project(DIGEST_RULE_CONFIG)
    recorded: dict[str, str] = {}
    real = redact.enforce_artifact
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        cfg = config_mod.load(str(root / "evalops.yaml"))
        os.environ["EVALOPS_REDACT"] = "off"
        assert redact.from_config(cfg).mode == "off"

        def recording(cfg_, source, text):
            recorded[source] = text
            return real(cfg_, source, text)

        redact.enforce_artifact = recording
        out = run.evaluate(cfg, "smoke", "dev")
        run_id = out["run_ids"][0]
        ledger = _ledger(cfg)

        assert set(recorded) == set(STAGED_ARTIFACTS), sorted(recorded)
        for name in STAGED_ARTIFACTS:
            stored = ledger.get_artifact(run_id, name)
            assert stored == recorded[name], f"{name}: off mode did not store the writer's bytes"
        assert "[REDACTED" not in "".join(recorded.values())

        # off is verbatim: secrets and 64-hex digests survive untouched
        rows = ledger.get_artifact(run_id, "evalops/rows.jsonl")
        assert "sk-live-PLANTED-9f3a" in rows
        assert re.search(r"[0-9a-f]{64}", ledger.get_artifact(run_id, "evalops/datasets.lock.json"))

        # the manifest hashes the stored bytes and never passed through the redaction boundary
        manifest = json.loads(ledger.get_artifact(run_id, "evalops/manifest.json"))
        assert "evalops/manifest.json" not in recorded
        for name, digest in manifest["artifacts"].items():
            assert hashlib.sha256(ledger.get_artifact(run_id, name).encode()).hexdigest() == digest, name
    finally:
        redact.enforce_artifact = real
        os.environ.pop("EVALOPS_REDACT", None)
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (b4) the active redactor is config-scoped, not a process global
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
def test_the_active_redactor_is_bound_to_the_config_not_the_process():
    """One process, two projects with different rules, cwd = repo (!= either root).

    `evaluate` project A, then the sinks that take no `Config` (`Cassette.save`,
    `AgentTrace.to_dict`) under project B must use B's rules; A's must never leak -- and the cwd
    fallback (no binding at all) still resolves the project's own `evalops.yaml`.
    """
    root_a = _project(EMAIL_RULE_CONFIG)
    root_b = _project(APIKEY_RULE_CONFIG)
    original = pathlib.Path.cwd()
    try:
        for root in (root_a, root_b):
            assert _cli(root, "sync", "all")[0] == 0
            _commit(root, "sync")
        cfg_a = config_mod.load(str(root_a / "evalops.yaml"))
        cfg_b = config_mod.load(str(root_b / "evalops.yaml"))

        redact.reset()
        os.chdir(REPO)  # neither project root: the cwd fallback is the repo's own empty rules
        assert run.evaluate(cfg_a, "smoke", "dev")["run_ids"]
        # the run's binding was scoped: project A's rules are not left active afterwards
        assert redact.current().fingerprint() != redact.from_config(cfg_a).fingerprint()

        def two_secrets() -> dict:
            return {"email": "alice@example.com", "api_key": "sk-live-PLANTED-9f3a"}

        def save_entry(path: pathlib.Path) -> str:
            cas = Cassette(path, "record")
            cas.entries["k1"] = {"key": "k1", "tool": "t", "args": two_secrets(), "response": None}
            cas.dirty = True
            cas.save()
            return path.read_text()

        # project B's rules apply to B's sinks; A's email rule must not reach them
        redact.activate(cfg_b)
        text_b = save_entry(root_b / "cassettes" / "two.jsonl")
        assert "sk-live-PLANTED-9f3a" not in text_b and "[REDACTED:apikey]" in text_b, text_b
        assert "alice@example.com" in text_b, "project A's email rule leaked into project B"
        trace_b = AgentTrace(spans=[Span(span_id="s", name="llm", kind="llm", inputs=two_secrets())])
        assert trace_b.to_dict()["spans"][0]["inputs"] == {
            "email": "alice@example.com",
            "api_key": "[REDACTED:apikey]",
        }

        # and the reverse: A's rules redact the email and never the api key
        redact.activate(cfg_a)
        text_a = save_entry(root_a / "cassettes" / "two.jsonl")
        assert "alice@example.com" not in text_a and "[REDACTED:email]" in text_a, text_a
        assert "sk-live-PLANTED-9f3a" in text_a, "project B's apikey rule leaked into project A"

        # no binding at all -> the cwd config supplies the rules (the documented fallback)
        redact.reset()
        os.chdir(root_a)
        try:
            assert redact.current().fingerprint() == redact.from_config(cfg_a).fingerprint()
        finally:
            os.chdir(REPO)
    finally:
        os.chdir(original)
        redact.reset()
        shutil.rmtree(root_a, ignore_errors=True)
        shutil.rmtree(root_b, ignore_errors=True)


def test_text_helpers_never_raise_on_payload_shape():
    redactor = Redactor({"mode": "strict", "patterns": [{"name": "apikey", "regex": "sk-live-[A-Za-z0-9-]+"}]})
    for text in ("", "not json at all", "[1, 2, 3]", "null", '{"a": "sk-live-X"}', '{"a":' * 3):
        for redacted, changes in (
            redactor.redact_json_text(text),
            redactor.redact_jsonl_text(text),
        ):
            assert isinstance(redacted, str) and isinstance(changes, list)
    text = '{"a": "sk-live-X"}\n\nnot json\n{"b": 1}\n'
    redacted, changes = redactor.redact_jsonl_text(text)
    assert redacted == '{"a": "[REDACTED:apikey]"}\n\nnot json\n{"b": 1}\n'
    assert [change.path for change in changes] == ["a"]


# ---------------------------------------------------------------------------
# (e) `mode: strict` refuses to persist a payload a rule still matches
# ---------------------------------------------------------------------------
def test_strict_aborts_before_writing_anything_when_the_first_sink_cannot_be_redacted():
    """A hook that keeps the original value for a matched `cost_estimate` field: nothing is written."""
    leaky = CONFIG.replace(
        '  fields: ["inputs.password", "spans[].inputs.authorization"]',
        '  fields: ["llm_calls"]\n  hook: leaky_hook:leaky_estimate',
    )
    assert "leaky_hook:leaky_estimate" in leaky
    root = _project(config=leaky, files={"leaky_hook.py": LEAKY_ESTIMATE_HOOK})
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        code, out = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == EXIT_RUNTIME, out
        assert out["code"] == EXIT_RUNTIME and "strict redaction" in out["errors"][0]["message"], out
        assert out["errors"][0]["details"]["paths"] == ["llm_calls"], out
        assert not (root / "cassettes" / "smoke.jsonl").exists(), "an aborted run must not write a cassette"
        runs = _run_ids(root)
        assert runs, "the run row exists (start_run) but nothing was persisted for it"
        assert _artifacts(root).get(runs[0], {}) == {}, "an aborted run must have no artifacts"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_strict_never_persists_any_artifact_when_the_rows_refusal_is_late():
    """A hook that defeats `inputs.password`: the rows refusal must leave **all four** artifacts unwritten.

    The estimate and both locks are produced before the evaluation, so writing them eagerly (the
    old `_put_artifact`) would persist three artifacts before `rows.jsonl` is even built.
    """
    leaky = CONFIG.replace("  mode: strict", "  mode: strict\n  hook: leaky_hook:leaky")
    root = _project(config=leaky, files={"leaky_hook.py": LEAKY_HOOK})
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        code, out = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == EXIT_RUNTIME, out
        assert out["errors"][0]["details"]["source"] == "evalops/rows.jsonl", out
        runs = _run_ids(root)
        assert runs, "the run row exists (start_run) but nothing was persisted for it"
        for name in (
            "evalops/cost_estimate.json",
            "evalops/prompts.lock.json",
            "evalops/datasets.lock.json",
            "evalops/rows.jsonl",
            "evalops/manifest.json",
        ):
            assert _artifact(root, runs[0], name) is None, f"a strict refusal must not write {name}"
        cassette = root / "cassettes" / "smoke.jsonl"
        assert not cassette.exists(), "no cassette write before every artifact has been verified"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_strict_cassette_save_refuses_and_does_not_rewrite_the_file():
    root = _tmpdir()
    try:
        (root / "leaky_hook.py").write_text(LEAKY_ENTRY_HOOK)
        path = root / "cassettes" / "smoke.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text('{"_meta": {"schema_version": 1}}\n')
        before = _sha(path)
        redact.activate(
            # paths are relative to the payload root, and a cassette payload is keyed by entry key
            _cfg(root, {"mode": "strict", "fields": ["*.args.email"], "hook": "leaky_hook:leaky_entry"})
        )
        cas = Cassette(path, "record")
        cas.entries["k1"] = {"key": "k1", "tool": "t", "args": {"email": "alice@example.com"}, "response": None}
        cas.dirty = True
        exc = _raises(cas.save)
        assert exc.code == EXIT_RUNTIME and "strict redaction" in str(exc)
        assert exc.details["paths"] == ["k1.args.email"], exc.details
        assert _sha(path) == before, "a refused save must not rewrite the file"
        assert cas.dirty is True
        redact.reset()
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (g) `evalops redact --check` never writes and exits 0 even with differences
# ---------------------------------------------------------------------------
def test_redact_check_is_read_only_and_reports_would_be_changes():
    secrets_json = json.dumps(
        {"inputs": {"password": "sk-live-PLANTED-9f3a", "note": "mail alice@example.com"}, "keep": 1}, indent=2
    )
    secrets_jsonl = "\n".join(
        [
            json.dumps({"_meta": {"schema_version": 1}}),
            json.dumps({"key": "k", "response": {"ssn": "123-45-6789"}}),
            json.dumps({"key": "k2", "response": "clean"}),
        ]
    )
    root = _project(files={"secrets.json": secrets_json, "rows.jsonl": secrets_jsonl})
    try:
        targets = [root / "secrets.json", root / "rows.jsonl"]
        before = {path.name: _sha(path) for path in targets}

        code, out = _cli(root, "redact", "--check", "--file", "secrets.json", "--file", "rows.jsonl")
        assert code == 0, out  # the exit matrix is frozen: "found secrets" is not a gate outcome
        data = out["data"]
        assert data["mode"] == "strict" and data["changed"] == 3, data
        by_sink = {sink["sink"]: sink for sink in data["sinks"]}
        assert sorted(by_sink) == ["file:rows.jsonl", "file:secrets.json"]
        assert by_sink["file:secrets.json"]["changes"] == 2
        assert sorted(by_sink["file:secrets.json"]["paths"]) == ["inputs.note", "inputs.password"]
        assert by_sink["file:rows.jsonl"]["paths"] == ["response.ssn"]
        assert all(sink["truncated"] is False for sink in data["sinks"])

        # --check never writes: every input is byte-identical afterwards
        assert {path.name: _sha(path) for path in targets} == before
        assert not list(root.glob(".evalops/out/*")), "redact --check must not write last_eval.json"

        # human mode renders a line-oriented unified diff and still exits 0
        code, human = _cli_human(root, "redact", "--check", "--file", "secrets.json")
        assert code == 0
        assert "--- file:secrets.json:inputs.password" in human and "+++ " in human
        assert "-sk-live-PLANTED-9f3a" in human and "+[REDACTED:inputs.password]" in human
        assert {path.name: _sha(path) for path in targets} == before

        # a sinking default: --group without last_eval.json is exit 2, and `--check` is optional
        # (`redact` and `redact --check` are the same read-only report)
        code, out = _cli(root, "redact", "--check")
        assert code == EXIT_USAGE and "last_eval.json" in out["errors"][0]["message"], out
        code, out = _cli(root, "redact")
        assert code == EXIT_USAGE and "last_eval.json" in out["errors"][0]["message"], out
        bare = _cli(root, "redact", "--file", "secrets.json")[1]
        checked = _cli(root, "redact", "--check", "--file", "secrets.json")[1]
        assert bare == checked and bare["data"]["changed"] == 2, (bare, checked)
        assert {path.name: _sha(path) for path in targets} == before, "neither form may write"
        assert _cli(root, "redact", "--check", "--file", "evalops.yaml")[0] == EXIT_USAGE
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (h) the `redact:` config section is closed and a bad regex names the pattern
# ---------------------------------------------------------------------------
def test_redact_config_section_is_closed_and_validates_regexes_and_hooks():
    root = _tmpdir()
    try:
        exc = _raises(lambda: config_mod.Config({"project": "p", "app": {}, "redact": {"fieldz": []}}, root))
        assert exc.code == EXIT_USAGE and "redact.fieldz" in str(exc), str(exc)

        exc = _raises(
            lambda: config_mod.Config(
                {"project": "p", "app": {}, "redact": {"mode": "loud"}},
                root,
            )
        )
        assert exc.code == EXIT_USAGE and "redact.mode" in str(exc), str(exc)

        exc = _raises(
            lambda: config_mod.Config(
                {
                    "project": "p",
                    "app": {},
                    "redact": {"patterns": [{"name": "broken", "regex": "([unclosed"}]},
                },
                root,
            )
        )
        assert exc.code == EXIT_USAGE and "broken" in str(exc) and "redact.patterns" in str(exc), str(exc)

        exc = _raises(lambda: config_mod.Config({"project": "p", "app": {}, "redact": {"hook": "nocolon"}}, root))
        assert exc.code == EXIT_USAGE and "redact.hook" in str(exc), str(exc)

        # defaults: no `redact:` section at all means mode off and zero rules
        cfg = config_mod.Config({"project": "p", "app": {}}, root)
        assert cfg.section("redact") == {}
        default = redact.from_config(cfg)
        assert default.mode == "off" and not default.enabled and default.rules == Rules()

        # a bad regex handed to the Redactor directly is the same usage error, never a crash
        exc = _raises(lambda: Redactor({"mode": "strict", "patterns": [{"name": "x", "regex": "(["}]}))
        assert exc.code == EXIT_USAGE and "x" in str(exc)
        exc = _raises(lambda: Redactor({"mode": "strict", "fields": ["a..b"]}))
        assert exc.code == EXIT_USAGE and "invalid path" in str(exc)
        redact.reset()
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


def test_cli_redact_command_is_registered_and_never_touches_the_exit_matrix():
    parser = cli_mod.build_parser()
    args = parser.parse_args(["redact", "--check", "--group", "g1", "--file", "a.jsonl"])
    assert (args.cmd, args.check, args.group, args.file) == ("redact", True, "g1", ["a.jsonl"])
    bare = parser.parse_args(["redact"])
    assert (bare.cmd, bare.check) == ("redact", False)
    # the command shows up in --help without adding an envelope field or an exit code
    assert "redact" in parser.format_help()
    assert "reports only, never writes" in " ".join(parser.format_help().split())
    assert sorted(cli_mod.envelope.EXIT_MATRIX) == list(range(8))


# ---------------------------------------------------------------------------
# (i) replay-miss diagnostics are redacted at capture time (CONTRACT §5.1)
# ---------------------------------------------------------------------------
#: The planted tool arguments of the miss tests: one email (also in `SECRETS`) and one SKU.
MISS_EMAIL = "alice@example.com"
MISS_SKU = "SKU-004521"

MISS_APP = r"""
import evalops
from evalops.tools import replayable

SKU = "SKU-004521"


@replayable("lookup_account")
def lookup_account(email, sku):
    return {"email": email, "sku": sku}


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools": "lookup_account@2"}


def build_predict_fn(prompts):
    def predict(question, email="alice@example.com", sku=SKU):
        account = lookup_account(email, sku)
        return "ticket for %s (%s)" % (account["email"], account["sku"])

    return predict


@evalops.scorer("echo")
def echo(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [echo]


def judge_meta():
    return {"llm_judge_count": 0}
"""

#: One row, no tool arguments in the dataset: the miss args come from the predict defaults.
MISS_DATASET = json.dumps({"inputs": {"question": "Where is my order?"}}) + "\n"

#: A hook that puts the raw argument label back for the (root) diagnostic string, so `strict` must
#: refuse to report the miss instead of handing the label to the caller.
LEAKY_LABEL_HOOK = """
def leaky_label(path, value):
    if isinstance(value, str) and "lookup_account" in value:
        return 'lookup_account({"email": "alice@example.com", "sku": "SKU-004521"})'
    return None
"""

#: The two `patterns` rules of the miss tests; `fields:`/`hash:` cannot match a flat string.
MISS_PATTERNS = (
    "  patterns:\n"
    "    - {name: email, regex: '[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\\.[A-Za-z]{2,}'}\n"
    "    - {name: sku, regex: 'SKU-[0-9]{6}'}\n"
)
MISS_RULES = {
    "mode": "strict",
    "patterns": [
        {"name": "email", "regex": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"},
        {"name": "sku", "regex": r"SKU-[0-9]{6}"},
    ],
}


@tools_mod.replayable("lookup_account")
def _miss_lookup_account(email: str, sku: str) -> dict:
    return {"email": email, "sku": sku}


def _miss_config(on_miss: str = "live", hook: str | None = None) -> str:
    """`CONFIG_PREFIX` with replay mode, an `on_miss` policy and the two `patterns` rules."""
    prefix = CONFIG_PREFIX.replace(
        "tools: {mode_by_stage: {dev: record}}",
        f"tools: {{mode_by_stage: {{dev: replay}}, on_miss: {on_miss}}}",
    ).replace("secret_app:", "miss_app:")
    assert f"on_miss: {on_miss}" in prefix and "miss_app:" in prefix, prefix
    return prefix + "redact:\n  mode: strict\n" + (f"  hook: {hook}\n" if hook else "") + MISS_PATTERNS


def _miss_project(config: str, files: dict[str, str] | None = None) -> pathlib.Path:
    merged = {"datasets/smoke.jsonl": MISS_DATASET, "miss_app.py": MISS_APP, **(files or {})}
    return _project(config=config, files=merged)


def test_a_replay_miss_label_is_redacted_before_it_reaches_last_eval_or_the_envelope():
    """`tools.on_miss: live` + a planted email/SKU in the tool args: the miss label is redacted.

    Sinks asserted: the `--json` envelope (`data.tool_replay_misses`), `.evalops/out/last_eval.json`
    bytes, and the `ToolReplayMiss` message of the same project under `on_miss: error`.
    """
    root = _miss_project(_miss_config("live"))
    try:
        assert _cli(root, "sync", "all")[0] == 0
        _commit(root, "sync")
        code, out = _cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        misses = out["data"]["tool_replay_misses"]
        assert len(misses) == 1, misses
        assert misses[0].startswith("lookup_account("), misses
        assert "[REDACTED:email]" in misses[0] and "[REDACTED:sku]" in misses[0], misses
        # the whole envelope, not just the miss list, is clean
        _assert_no_secrets(json.dumps(out), "the --json envelope")
        assert MISS_SKU not in json.dumps(out), "the SKU reached the --json envelope"

        last_eval = root / ".evalops" / "out" / "last_eval.json"
        assert last_eval.exists()
        text = last_eval.read_text()
        _assert_no_secrets(text, "last_eval.json")
        assert MISS_SKU not in text, "the SKU reached last_eval.json"
        assert '"tool_replay_misses"' in text and "[REDACTED:email]" in text, text

        # `on_miss: error`: the raised miss message carries the same redacted label
        cas = Cassette(None, "replay", "error")
        with redact.use(_cfg(root, MISS_RULES)), tools_mod.use(cas):
            try:
                _miss_lookup_account(MISS_EMAIL, MISS_SKU)
                raise AssertionError("a replay miss must raise ToolReplayMiss")
            except tools_mod.ToolReplayMiss as exc:
                message = str(exc)
        assert "no recording for lookup_account(" in message, message
        assert "[REDACTED:email]" in message and "[REDACTED:sku]" in message, message
        assert MISS_EMAIL not in message and MISS_SKU not in message, message
        assert cas.misses == [message.removeprefix("no recording for ").split("; re-record with")[0]], cas.misses
        redact.reset()
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


def test_strict_refuses_a_replay_miss_label_that_still_matches():
    """`mode: strict` + a hook that restores the raw label: exit 5 at the capture point, not a miss.

    `fields:`/`hash:` cannot apply to the flat label (only `patterns`/`hook` can), so the hook is the
    only way a label can still match after redaction. The refusal is the capture point's own exit 5;
    nothing is recorded in `Cassette.misses` and no cassette file is written.
    """
    root = _tmpdir()
    try:
        (root / "leaky_label.py").write_text(LEAKY_LABEL_HOOK)
        cfg = _cfg(root, {**MISS_RULES, "hook": "leaky_label:leaky_label"})
        path = root / "cassettes" / "smoke.jsonl"
        cas = Cassette(path, "replay", "error")
        raised = None
        with redact.use(cfg), tools_mod.use(cas):
            try:
                _miss_lookup_account(MISS_EMAIL, MISS_SKU)
            except Exception as exc:  # noqa: BLE001 - the type is the assertion
                raised = exc
        assert raised is not None, "strict must refuse a label that still matches"
        assert not isinstance(raised, tools_mod.ToolReplayMiss), f"strict must not report a miss: {raised}"
        assert isinstance(raised, EvalOpsError), raised
        assert raised.code == EXIT_RUNTIME, raised.code  # exit 5
        assert "strict redaction" in str(raised) and "the replay-miss label" in str(raised), str(raised)
        assert MISS_EMAIL not in str(raised) and MISS_SKU not in str(raised), str(raised)
        assert raised.details["source"] == "tool_replay_misses", raised.details
        assert raised.details["tool"] == "lookup_account", raised.details
        assert cas.misses == [], "a refused label must never be recorded"
        assert not path.exists() and not cas.dirty, "nothing is persisted"

        # same project through the CLI: the leaky hook cannot persist the secret either (exit 5
        # from the artifact boundary once the row error carries the hook-matching label)
        root2 = _miss_project(
            _miss_config("error", hook="leaky_label:leaky_label"), {"leaky_label.py": LEAKY_LABEL_HOOK}
        )
        try:
            assert _cli(root2, "sync", "all")[0] == 0
            _commit(root2, "sync")
            code, out = _cli(root2, "eval", "--dataset", "smoke", "--stage", "dev")
            assert code == EXIT_RUNTIME, out
            assert out["errors"][0]["details"]["source"] == "evalops/rows.jsonl", out
            assert MISS_EMAIL not in json.dumps(out) and MISS_SKU not in json.dumps(out), out
            runs = _run_ids(root2)
            assert runs, "the run row exists (start_run) but nothing was persisted for it"
            assert _artifacts(root2).get(runs[0], {}) == {}, "a strict refusal must write no artifact"
            assert not (root2 / "cassettes" / "smoke.jsonl").exists()
        finally:
            shutil.rmtree(root2, ignore_errors=True)
        redact.reset()
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)
