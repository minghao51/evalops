"""WP1.4 (H): every exit code 0-7 through a real command path.

Each case drives the installed CLI as a subprocess and asserts **both** the process exit code and
``envelope["code"]`` (they must be equal, and `ok` iff the code is 0). No case builds an envelope
directly: every code comes from a command.

| code | path |
|---|---|
| 0 | `plugins` on a valid project |
| 1 | `gate` over a real `sql`-ledger run that misses the configured `min` |
| 2 | `rollback` with an unknown prompt, and `config validate` with a dotted-key typo |
| 3 | `eval` refused by `cost.budgets.per_run_usd` |
| 4 | `promote` on dev-stage (unpromotable) evidence -- promote's own guard |
| 5 | `eval --tool-mode replay` reading a corrupt cassette |
| 6 | `ledger verify` on a ledger plugin without the capability (memory ledger) |
| 7 | `traces` reading a rows artifact whose `schema_version` is in the future |

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import json
import pathlib
import shutil
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

BROKEN_CONFIG = proj.BASE_CONFIG + "gates:\n  metrics:\n    facts/mean: {minn: 1}\n"
FAILING_GATE_CONFIG = proj.BASE_CONFIG + "gates: {metrics: {facts/mean: {min: 1.5}}}\n"
TIGHT_RUN_CONFIG = proj.BASE_CONFIG.replace("per_run_usd: 5", "per_run_usd: 0.000001")
TOOL_CONFIG = proj.BASE_CONFIG.replace("fake_app:", "tool_app:")

#: code -> the named test that drives it (the completeness test below checks this stays honest).
EXIT_CASES = {
    0: "test_exit_code_zero_a_successful_command",
    1: "test_exit_code_one_a_failed_gate_on_a_real_ledger_run",
    2: "test_exit_code_two_usage_errors_from_the_cli",
    3: "test_exit_code_three_a_per_run_budget_refusal",
    4: "test_exit_code_four_promote_refuses_unpromotable_evidence",
    5: "test_exit_code_five_a_corrupt_cassette_read",
    6: "test_exit_code_six_a_ledger_plugin_without_the_capability",
    7: "test_exit_code_seven_a_future_rows_schema_version",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _assert_exit(expected: int, code: int, envelope: dict, command: str) -> None:
    assert code == expected, (expected, code, envelope)
    assert envelope["code"] == expected == code, (expected, code, envelope["code"])
    assert envelope["ok"] is (expected == 0), envelope
    assert envelope["command"] == command, (command, envelope["command"])
    assert envelope["errors"] == [] if expected == 0 else envelope["errors"]


def _sqlite(root: pathlib.Path) -> sqlite3.Connection:
    return sqlite3.connect(str(root / ".evalops" / "ledger.sqlite"))


def _stored_rows(root: pathlib.Path, run_id: str) -> str:
    conn = _sqlite(root)
    try:
        row = conn.execute(
            "SELECT body FROM evalops_artifacts WHERE run_id=? AND name=?", (run_id, "evalops/rows.jsonl")
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "the run has no rows artifact"
    return str(row[0])


def _replace_rows(root: pathlib.Path, run_id: str, body: str) -> None:
    conn = _sqlite(root)
    try:
        conn.execute(
            "UPDATE evalops_artifacts SET body=? WHERE run_id=? AND name=?", (body, run_id, "evalops/rows.jsonl")
        )
        conn.commit()
    finally:
        conn.close()


def _run_tag(root: pathlib.Path, run_id: str, key: str) -> str | None:
    conn = _sqlite(root)
    try:
        row = conn.execute("SELECT v FROM evalops_kv WHERE run_id=? AND kind='tag' AND k=?", (run_id, key)).fetchone()
    finally:
        conn.close()
    return None if row is None else str(row[0])


def _evaluated_project(config: str = proj.BASE_CONFIG, files: dict[str, str] | None = None) -> tuple:
    """A synced, committed project with one `dev` eval -> (root, group, run_id)."""
    root = proj.project(config, files=files)
    assert proj.run_cli(root, "sync", "all")[0] == 0
    proj.commit(root, "sync")
    code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
    assert code == 0, out
    return root, out["data"]["group"], out["data"]["run_ids"][0]


# ---------------------------------------------------------------------------
# 0
# ---------------------------------------------------------------------------
def test_exit_code_zero_a_successful_command():
    root = proj.project()
    try:
        code, envelope = proj.run_cli(root, "plugins")
        _assert_exit(0, code, envelope, "plugins")
        assert envelope["data"]["roles"]["ledger"] == {"plugin": "sql", "ok": True}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 1
# ---------------------------------------------------------------------------
def test_exit_code_one_a_failed_gate_on_a_real_ledger_run():
    root, group, _run_id = _evaluated_project(FAILING_GATE_CONFIG)
    try:
        code, envelope = proj.run_cli(root, "gate", "--group", group)
        _assert_exit(1, code, envelope, "gate")
        checks = envelope["data"]["checks"]
        assert checks == [{"check": "facts/mean >= 1.5", "passed": False, "candidate": 1.0}], checks
        assert envelope["data"]["summary"] == f"gate FAILED for group {group}"
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2
# ---------------------------------------------------------------------------
def test_exit_code_two_usage_errors_from_the_cli():
    root = proj.project()
    try:
        assert proj.run_cli(root, "sync", "prompts")[0] == 0
        code, envelope = proj.run_cli(
            root, "rollback", "--alias", "production", "--prompt", "no_such_prompt", "--version", "1"
        )
        _assert_exit(2, code, envelope, "rollback")
        assert envelope["errors"][0]["message"] == "unknown prompt 'no_such_prompt'"

        # a config-key typo is exit 2 with the full dotted key path
        (root / "broken.yaml").write_text(BROKEN_CONFIG)
        code, envelope = proj.run_cli(root, "--config", "broken.yaml", "config", "validate")
        _assert_exit(2, code, envelope, "config")
        paths = [issue["path"] for issue in envelope["errors"][0]["details"]["errors"]]
        assert paths == ["gates.metrics.facts/mean.minn"], envelope
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 3
# ---------------------------------------------------------------------------
def test_exit_code_three_a_per_run_budget_refusal():
    root = proj.project(TIGHT_RUN_CONFIG)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        _assert_exit(3, code, envelope, "eval")
        error = envelope["errors"][0]
        assert error["message"].startswith("budget exceeded")
        assert error["details"]["per_run_usd"] == 0.000001 and error["details"]["estimate_usd"] > 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 4
# ---------------------------------------------------------------------------
def test_exit_code_four_promote_refuses_unpromotable_evidence():
    """The guard is `prompts.promote`'s own check, driven by a real dev-stage run (tag: false)."""
    root, group, run_id = _evaluated_project()
    try:
        assert _run_tag(root, run_id, "evalops.promotable") == "false"
        code, envelope = proj.run_cli(root, "promote", "--alias", "production", "--evidence-group", group)
        _assert_exit(4, code, envelope, "promote")
        assert envelope["errors"][0]["message"] == "evidence is not promotable (dirty git or non-main stage)"
        # nothing moved: the alias file the git prompt store would have written does not exist
        assert not (root / ".evalops" / "store" / "prompts" / "support_system" / "aliases.json").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 5
# ---------------------------------------------------------------------------
def test_exit_code_five_a_corrupt_cassette_read():
    root = proj.project(TOOL_CONFIG, files={"tool_app.py": proj.fixture_text("tool_app.py")})
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        cassette = root / "cassettes" / "smoke.jsonl"
        cassette.parent.mkdir(parents=True, exist_ok=True)
        cassette.write_text("{ this is not json\n")
        proj.commit(root, "corrupt cassette")

        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev", "--tool-mode", "replay")
        _assert_exit(5, code, envelope, "eval")
        message = envelope["errors"][0]["message"]
        assert str(cassette) in message and "not valid JSON" in message
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 6
# ---------------------------------------------------------------------------
def test_exit_code_six_a_ledger_plugin_without_the_capability():
    name = proj.ledger_name()
    root = proj.project(proj.ledger_config(name))
    try:
        proj.install_ledger(root, name)
        code, envelope = proj.run_cli(root, "ledger", "verify")
        _assert_exit(6, code, envelope, "ledger")
        error = envelope["errors"][0]
        assert "plugin 'memory' does not support ledger verify" in error["message"]
        assert error["hint"] == "Only SQL-backed ledgers can verify their tables."
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# 7
# ---------------------------------------------------------------------------
def test_exit_code_seven_a_future_rows_schema_version():
    root, group, run_id = _evaluated_project()
    try:
        original = _stored_rows(root, run_id)
        assert original.strip()  # a real eval wrote real rows
        future = [
            json.dumps({**json.loads(line), "schema_version": 2}) for line in original.splitlines() if line.strip()
        ]
        _replace_rows(root, run_id, "\n".join(future) + "\n")

        code, envelope = proj.run_cli(root, "traces", "--group", group)
        _assert_exit(7, code, envelope, "traces")
        message = envelope["errors"][0]["message"]
        assert "evalops/rows.jsonl" in message and "schema_version 2" in message
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# completeness
# ---------------------------------------------------------------------------
def test_every_exit_code_has_a_real_command_path():
    assert sorted(EXIT_CASES) == list(range(8)), EXIT_CASES
    for expected, name in EXIT_CASES.items():
        fn = globals().get(name)
        assert callable(fn), (expected, name)
        assert name.startswith("test_exit_code_"), (expected, name)
