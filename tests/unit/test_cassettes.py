"""WP1.4 (C): every cassette path in ``src/evalops/tools.py``.

Falsifies, in order: (C1) record -> replay round-trip with a fingerprint that survives a reload and
a metadata rewrite, (C2) a replay miss is ``ToolReplayMiss`` with ``on_miss: error`` and calls the
real tool with ``on_miss: live`` while the redacted label lands in ``misses``, (C3) ``save()`` is a
no-op without a path or without changes, (C4) a corrupt line is exit 5, a non-object line and a
future ``_meta.schema_version`` are exit 7 naming the file -- through the constructor *and* through
``evalops eval --tool-mode replay``, (C5) without an active cassette the wrapped function runs
directly and records nothing, (C6) the same args in a different dict order produce the same key.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import config as config_mod  # noqa: E402
from evalops import tools  # noqa: E402
from evalops.contract import formats, redact  # noqa: E402
from evalops.util import EXIT_CONTRACT, EXIT_RUNTIME, EvalOpsError  # noqa: E402

EMAIL = "alice@example.com"
EMAIL_RULE = {
    "mode": "strict",
    "patterns": [{"name": "email", "regex": r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"}],
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmpdir() -> pathlib.Path:
    return proj.tmpdir("evalops-cassettes-")


def _raises(fn) -> EvalOpsError:
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError, the call returned normally")


def _redacting_cfg(root: pathlib.Path, section: dict | None = None) -> config_mod.Config:
    return config_mod.Config({"project": "cassettes", "app": {}, "redact": section or EMAIL_RULE}, root)


def _tool():
    calls = {"n": 0}

    @tools.replayable("lookup_order")
    def lookup_order(order_id: str, note: str = "") -> dict:
        calls["n"] += 1
        return {"order_id": order_id, "note": note}

    return lookup_order, calls


# ---------------------------------------------------------------------------
# (C1) record -> replay round-trip and a stable fingerprint
# ---------------------------------------------------------------------------
def test_record_replay_round_trip_keeps_the_fingerprint_across_a_reload():
    root = _tmpdir()
    try:
        path, (tool, calls) = root / "cassettes" / "smoke.jsonl", _tool()
        recording = tools.Cassette(path, "record")
        with tools.use(recording):
            assert tool("A-1") == {"order_id": "A-1", "note": ""}
        recording.save()
        fingerprint = recording.fingerprint()
        assert fingerprint != "none" and len(fingerprint) == 12
        assert path.exists()

        replayed = tools.Cassette(path, "replay")
        assert replayed.fingerprint() == fingerprint
        with tools.use(replayed):
            assert tool(order_id="A-1") == {"order_id": "A-1", "note": ""}
            assert calls["n"] == 1  # the live tool was not called again

        # metadata is not identity: extra `_meta` fields (redaction markers, a later timestamp)
        # never move the fingerprint
        lines = [line for line in path.read_text().splitlines() if line.strip()]
        meta = json.loads(lines[0])
        assert formats.META_KEY in meta and "key" not in meta
        meta[formats.META_KEY].update(
            {"redacted": True, "redaction_rules": "deadbeef", "created_at": "2000-01-01T00:00:00Z"}
        )
        path.write_text(json.dumps(meta) + "\n" + "\n".join(lines[1:]) + "\n")
        after_metadata = tools.Cassette(path, "replay")
        assert after_metadata.fingerprint() == fingerprint
        assert after_metadata.entries == replayed.entries
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C2) replay miss: error vs live, and the redacted label
# ---------------------------------------------------------------------------
def test_a_replay_miss_errors_or_runs_live_and_its_label_is_redacted():
    root = _tmpdir()
    try:
        path, (tool, calls) = root / "cassettes" / "empty.jsonl", _tool()
        cfg = _redacting_cfg(root)

        missing = tools.Cassette(path, "replay", "error")
        with redact.use(cfg), tools.use(missing):
            try:
                tool(EMAIL)
                raise AssertionError("a replay miss must raise ToolReplayMiss")
            except tools.ToolReplayMiss as exc:
                message = str(exc)
        assert calls["n"] == 0  # `error` never falls through to the live tool
        assert "no recording for lookup_order(" in message
        assert len(missing.misses) == 1 and missing.misses[0] != ""
        assert "[REDACTED:email]" in missing.misses[0]
        assert EMAIL not in missing.misses[0] and EMAIL not in message

        live = tools.Cassette(path, "replay", "live")
        with redact.use(cfg), tools.use(live):
            assert tool(EMAIL) == {"order_id": EMAIL, "note": ""}
        assert calls["n"] == 1  # the real tool ran
        assert len(live.misses) == 1 and live.misses[0].startswith("lookup_order(")
        assert "[REDACTED:email]" in live.misses[0] and EMAIL not in live.misses[0]
    finally:
        redact.reset()
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C3) save() no-ops
# ---------------------------------------------------------------------------
def test_save_is_a_no_op_without_a_path_or_without_changes():
    root = _tmpdir()
    try:
        tool, _calls = _tool()
        no_path = tools.Cassette(None, "record")
        with tools.use(no_path):
            tool("A-1")
        assert no_path.dirty is True  # it recorded ...
        assert no_path.save() is None  # ... but there is nowhere to write, and that is not an error

        path = root / "cassettes" / "smoke.jsonl"
        clean = tools.Cassette(path, "record")
        assert clean.dirty is False
        clean.save()
        assert not path.exists()  # nothing dirty -> no file is created

        dirty = tools.Cassette(path, "record")
        with tools.use(dirty):
            tool("A-2")
        assert dirty.dirty is True
        dirty.save()
        assert dirty.dirty is False and path.exists()
        assert len(json.loads(path.read_text().splitlines()[0])[formats.META_KEY]) > 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C4) load errors: exit 5 and exit 7
# ---------------------------------------------------------------------------
def test_cassette_load_errors_are_exit_five_and_exit_seven_naming_the_file():
    root = _tmpdir()
    try:
        corrupt = root / "corrupt.jsonl"
        corrupt.write_text("this is not json\n")
        err = _raises(lambda: tools.Cassette(corrupt, "replay"))
        assert err.code == EXIT_RUNTIME == 5
        assert str(corrupt) in str(err) and ":1:" in str(err) and "not valid JSON" in str(err)

        not_object = root / "not_object.jsonl"
        not_object.write_text('["not", "an", "entry"]\n')
        err = _raises(lambda: tools.Cassette(not_object, "replay"))
        assert err.code == EXIT_CONTRACT == 7 and str(not_object) in str(err)

        future = root / "future.jsonl"
        future.write_text(json.dumps({formats.META_KEY: {**formats.meta(), "schema_version": 2}}) + "\n")
        err = _raises(lambda: tools.Cassette(future, "replay"))
        assert err.code == EXIT_CONTRACT == 7
        assert str(future) in str(err) and "schema_version 2" in str(err)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_corrupt_cassette_read_is_exit_five_through_eval_replay():
    """Command path: `eval --tool-mode replay` reads the cassette and reports its own code."""
    root = proj.project(
        proj.BASE_CONFIG.replace("fake_app:", "tool_app:"),
        files={"tool_app.py": proj.fixture_text("tool_app.py")},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        cassette = root / "cassettes" / "smoke.jsonl"
        cassette.parent.mkdir(parents=True, exist_ok=True)

        body = "this is not json\n"
        cassette.write_text(body)
        proj.commit(root, "corrupt cassette")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev", "--tool-mode", "replay")
        assert code == 5 and envelope["code"] == 5, envelope
        assert envelope["command"] == "eval" and envelope["ok"] is False
        assert str(cassette) in envelope["errors"][0]["message"]

        cassette.write_text('["not", "an", "entry"]\n')
        proj.commit(root, "non-object cassette")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev", "--tool-mode", "replay")
        assert code == 7 and envelope["code"] == 7, envelope
        assert str(cassette) in envelope["errors"][0]["message"]

        cassette.write_text(json.dumps({formats.META_KEY: {**formats.meta(), "schema_version": 2}}) + "\n")
        proj.commit(root, "future cassette")
        code, envelope = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev", "--tool-mode", "replay")
        assert code == 7 and envelope["code"] == 7, envelope
        message = envelope["errors"][0]["message"]
        assert str(cassette) in message and "schema_version 2" in message
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C5) no active cassette
# ---------------------------------------------------------------------------
def test_without_an_active_cassette_the_tool_runs_directly_and_records_nothing():
    root = _tmpdir()
    try:
        previous, tools._active = tools._active, None  # noqa: SLF001 - the module fallback is a test seam
        try:
            tool, calls = _tool()
            assert tools._current() is None
            assert tool("A-1") == {"order_id": "A-1", "note": ""}
            assert calls["n"] == 1
            with tools.use(None):
                assert tools._current() is None
                assert tool("A-2") == {"order_id": "A-2", "note": ""}
            assert calls["n"] == 2
            assert tools._current() is None
        finally:
            tools._active = previous  # noqa: SLF001
        # nothing was recorded anywhere: there is no cassette object to save
        assert not (root / "cassettes").exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (C6) key stability
# ---------------------------------------------------------------------------
def test_the_same_args_in_a_different_dict_order_produce_the_same_key():
    assert tools._key("t", {"a": 1, "b": 2}) == tools._key("t", {"b": 2, "a": 1})
    assert tools._key("t", {"a": 1, "b": 2}) != tools._key("t", {"a": 2, "b": 1})
    assert tools._key("t", {"a": 1}) != tools._key("u", {"a": 1})

    root = _tmpdir()
    try:
        path, (tool, calls) = root / "cassettes" / "smoke.jsonl", _tool()
        recording = tools.Cassette(path, "record")
        with tools.use(recording):
            assert tool(order_id="A-1", note="fast") == {"order_id": "A-1", "note": "fast"}
        recording.save()
        replayed = tools.Cassette(path, "replay")
        with tools.use(replayed):
            assert tool(note="fast", order_id="A-1") == {"order_id": "A-1", "note": "fast"}
        assert calls["n"] == 1 and replayed.misses == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
