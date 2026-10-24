"""WP1.5 (A, B): the nested trace document is versioned, and unmodeled span attributes are reported.

A -- the trace document version matrix (absent = v1, 1 and 2 read, anything else exit 7 naming the
artifact), the writer always emitting 2, unknown keys ignored, and the version never reaching an
identity hash.
B -- `Span.unknown_attributes` carries *names* only (sorted, deduplicated), never a consumed
attribute, aggregated by `AgentTrace.unknown_attributes()`, surfaced only through
`evalops traces --unknown-attrs` so the default payload is byte-identical.

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
from evalops import datasets, run  # noqa: E402
from evalops.contract import redact  # noqa: E402
from evalops.traces import SOURCE, TRACE_SCHEMA_VERSION, AgentTrace, Span  # noqa: E402
from evalops.traces.normalize import normalize  # noqa: E402
from evalops.util import EXIT_CONTRACT, FormatError  # noqa: E402

V1 = "traces/rows_v1.jsonl"
V2 = "traces/rows_v2.jsonl"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _row(name: str) -> dict:
    return json.loads(proj.fixture_text(name).strip())


def _raises(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - the assertions below inspect type/code/message
        return exc
    raise AssertionError("expected the call to raise, it returned normally")


def _raw(sid: str, name: str, parent: str | None = None, t: int = 0, attrs: dict | None = None) -> dict:
    return {
        "span_id": sid,
        "parent_id": parent,
        "name": name,
        "start_ns": t,
        "end_ns": t + 10,
        "attributes": dict(attrs or {}),
    }


def _traces_cli(root, *args: str) -> tuple[int, dict]:
    """The real `evalops traces` command, in-process (the fixture ledger lives in this process)."""
    return proj.run_cli_in_process(root, *args)


# ---------------------------------------------------------------------------
# (A1, A2) the constant and the writer
# ---------------------------------------------------------------------------
def test_a1_a2_the_writer_always_emits_trace_schema_version_two():
    assert TRACE_SCHEMA_VERSION == 2
    trace = AgentTrace(spans=[Span(span_id="1", name="llm", kind="llm")], trace_id="t", source="otel")
    document = trace.to_dict()
    assert document["schema_version"] == 2
    assert set(document) == {"schema_version", "trace_id", "source", "spans"}

    # the upgrade path: a document read as v1 is written as v2
    v1 = _row(V1)["trace"]
    assert "schema_version" not in v1
    read = AgentTrace.from_dict(v1)
    assert read is not None and read.schema_version == 1
    assert read.to_dict()["schema_version"] == 2


def test_a2_redaction_runs_last_and_never_rewrites_the_trace_version():
    root = proj.tmpdir()
    try:
        cfg = config_mod.Config(
            {
                "project": "wp15",
                "app": {},
                "redact": {"mode": "strict", "patterns": [{"name": "all", "regex": "."}]},
            },
            root,
        )
        trace = AgentTrace(spans=[Span(span_id="1", name="ChatOpenAI", kind="llm")], trace_id="t", source="otel")
        with redact.use(cfg):
            document = trace.to_dict()
        assert document["schema_version"] == 2  # an int, not a string leaf: redaction cannot match it
        assert document["spans"][0]["name"] != "ChatOpenAI"  # the redactor really was active
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (A3) the read matrix
# ---------------------------------------------------------------------------
def test_a3_absent_version_reads_as_v1_and_one_and_two_are_accepted():
    for declared, expected in ((None, 1), (1, 1), (2, 2)):
        document: dict = {"spans": []}
        if declared is not None:
            document["schema_version"] = declared
        trace = AgentTrace.from_dict(document)
        assert trace is not None and trace.schema_version == expected, (declared, expected)
    assert AgentTrace.from_dict(None) is None and AgentTrace.from_dict({}) is None


def test_a3_unknown_top_level_and_span_keys_are_ignored_never_a_type_error():
    trace = AgentTrace.from_dict(
        {
            "spans": [{"span_id": "1", "name": "x", "brand_new_field": 1, "unknown_attributes": ["a.b"]}],
            "future_top_level": {"nested": True},
        }
    )
    assert trace is not None
    assert trace.spans[0].name == "x" and trace.spans[0].unknown_attributes == ["a.b"]


def test_a3_future_and_non_integer_versions_are_exit_seven_naming_the_source():
    for bad in (3, 99, 0, -1, "2", None, 2.0, True):
        document = {"schema_version": bad, "spans": []}
        err = _raises(lambda doc=document: AgentTrace.from_dict(doc, source="evalops/rows.jsonl"))
        assert isinstance(err, FormatError), (bad, err)
        assert err.code == EXIT_CONTRACT == 7, (bad, err.code)
        assert err.source == "evalops/rows.jsonl" and err.supported == 2, (bad, err.source, err.supported)
        assert "evalops/rows.jsonl: unsupported schema_version" in str(err), str(err)
    # the default source when the caller names none
    err = _raises(lambda: AgentTrace.from_dict({"schema_version": 3, "spans": []}))
    assert isinstance(err, FormatError) and err.source == "trace"
    assert "trace: unsupported schema_version 3" in str(err)


def test_a4_evalops_traces_names_the_rows_artifact_for_a_future_nested_trace_version():
    root, ledger = proj.ledger_project()
    try:
        row = _row(V2)
        row["trace"]["schema_version"] = 3
        proj.add_run(
            ledger,
            "run-v3",
            tags={"evalops.group": "g1"},
            artifacts={"evalops/rows.jsonl": json.dumps(row) + "\n"},
        )
        code, envelope = _traces_cli(root, "traces", "--group", "g1")
        assert code == EXIT_CONTRACT == 7, (code, envelope)
        message = envelope["errors"][0]["message"]
        assert "evalops/rows.jsonl" in message and "schema_version 3" in message, message
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a4_calibration_names_the_dataset_for_a_future_nested_trace_version():
    """`run.evaluate(kind="calibration")` reads recorded traces: the source is `dataset:<name>`."""
    secret = _row(V2)["trace"]
    record = {
        "inputs": {"question": "q1"},
        "outputs": "ok",
        "trace": {**secret, "schema_version": 3},
        "expectations": {"human_labels": {"helpful": 1}},
    }
    root = proj.project(
        config=proj.BASE_CONFIG + "judges:\n  helpful: {min_agreement: 0.5}\n",
        dataset_text=json.dumps(record) + "\n",
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli(root, "calibrate", "--dataset", "smoke")
        assert code == EXIT_CONTRACT == 7, (code, envelope)
        message = envelope["errors"][0]["message"]
        assert "dataset:smoke" in message and "schema_version 3" in message, message
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (A5) the SOURCE set
# ---------------------------------------------------------------------------
def test_a5_every_source_value_round_trips_and_the_default_is_stored():
    assert SOURCE == ("otel", "langchain", "openai_agents", "mixed", "stored", "unknown")
    for source in SOURCE:
        trace = AgentTrace.from_dict({"source": source, "spans": []})
        assert trace is not None and trace.source == source
        assert trace.to_dict()["source"] == source
    assert AgentTrace.from_dict({"spans": []}).source == "stored"  # type: ignore[union-attr]
    assert AgentTrace(spans=[]).source == "unknown"  # the in-process default is unchanged


# ---------------------------------------------------------------------------
# (A6) the version is metadata: no identity hash moves
# ---------------------------------------------------------------------------
ROW_KEY = "bc857b1f6c72e243"
DATASET_FINGERPRINT = "bc857b1f6c72e243f62342a8ac1696ae0abe6feedc1784d54502e9049315bbfe"
DATASET_RECORD = {"inputs": {"question": "Where is order A1?"}, "expectations": {"expected_tools": ["lookup_order"]}}


def test_a6_the_v1_and_v2_fixtures_share_the_pinned_row_key_and_dataset_fingerprint():
    v1, v2 = _row(V1), _row(V2)
    assert datasets.row_key(DATASET_RECORD) == ROW_KEY
    assert datasets.fingerprint([DATASET_RECORD]) == DATASET_FINGERPRINT
    assert v1["row_key"] == v2["row_key"] == ROW_KEY
    # the trace version is not part of the dataset fingerprint: the fixture's dataset source has
    # no `trace` field at all, and the pin above is what the WP must not move.
    assert "trace" not in DATASET_RECORD


def test_a6_an_embedded_trace_dataset_is_fingerprinted_verbatim_by_design():
    # The honest scope of §5.6: row identity ignores the trace document, but a dataset source
    # record that embeds one is a different snapshot when that document changes.
    v1_rec = {**DATASET_RECORD, "trace": _row(V1)["trace"]}
    v2_rec = {**DATASET_RECORD, "trace": _row(V2)["trace"]}
    assert datasets.row_key(v1_rec) == datasets.row_key(v2_rec) == ROW_KEY
    assert datasets.fingerprint([v1_rec]) != datasets.fingerprint([v2_rec])


def test_a6_v1_and_v2_produce_identical_trace_metrics_and_gate_checks():
    v1_trace = AgentTrace.from_dict(_row(V1)["trace"])
    v2_trace = AgentTrace.from_dict(_row(V2)["trace"])
    assert v1_trace is not None and v2_trace is not None
    assert v1_trace.metrics() == v2_trace.metrics() == _row(V1)["trace_metrics"]
    assert v1_trace.unknown_attributes() == {}  # the legacy document carried no names
    assert v2_trace.unknown_attributes() == {"weird.vendor.attr": 1}

    # gating both artifacts gives the same checks list and the same verdict (D2 proves the command)
    v1_rows = run.rows_from_artifact(proj.fixture_text(V1))
    v2_rows = run.rows_from_artifact(proj.fixture_text(V2))
    assert [r.trace_metrics for r in v1_rows] == [r.trace_metrics for r in v2_rows]
    assert [r.row_key for r in v1_rows] == [r.row_key for r in v2_rows] == [ROW_KEY]


def test_a6_a_trace_document_is_not_an_app_version_input():
    """The same behavior config + git sha produced the same `app_version`, with and without spans."""
    root = proj.project(
        config=proj.BASE_CONFIG.replace("fake_app:", "trace_otel_app:"),
        files={"trace_otel_app.py": OTEL_APP},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        with_spans, out_a = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        without, out_b = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev", env_extra={"NO_TRACE": "1"})
        assert with_spans == 0 and without == 0, (out_a, out_b)
        assert out_a["data"]["app_version"] == out_b["data"]["app_version"]
        # the captures really differ: one wrote a trace document, the other wrote none
        assert proj.run_cli(root, "traces", "--group", out_a["data"]["group"])[1]["data"]["rows"][0]["tree"]
        assert proj.run_cli(root, "traces", "--group", out_b["data"]["group"])[1]["data"]["rows"][0]["tree"] is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (B1, B2) names only, consumed attributes never reported
# ---------------------------------------------------------------------------
def test_b1_b2_unknown_attributes_are_sorted_names_and_consumed_attributes_are_not_reported():
    span = normalize(
        _raw(
            "1",
            "chat gpt-4o",
            t=1,
            attrs={
                # consumed attributes (models, tokens, payloads, the kind marker, a kept attribute)
                "gen_ai.operation.name": "chat",
                "gen_ai.request.model": "gpt-4o",
                "gen_ai.usage.input_tokens": 10,
                "gen_ai.usage.output_tokens": 5,
                "gen_ai.input.messages": '{"role": "user"}',
                "input.value": "hello",
                "gen_ai.system": "openai",
                # unmodeled, supplied out of order on purpose
                "zeta.vendor.attr": "z",
                "weird.vendor.attr": "w",
            },
        )
    )
    assert span.unknown_attributes == ["weird.vendor.attr", "zeta.vendor.attr"]  # sorted, deduped, names only
    assert span.attributes["gen_ai.system"] == "openai"  # `Span.attributes` keeps its meaning
    assert span.model == "gpt-4o" and span.input_tokens == 10 and span.output_tokens == 5


def test_b1_the_list_is_deduplicated_and_never_holds_values():
    span = normalize(_raw("1", "chat", attrs={"gen_ai.operation.name": "chat", "vendor.payload": {"secret": "s3cr3t"}}))
    assert span.unknown_attributes == ["vendor.payload"]
    assert "s3cr3t" not in json.dumps(span.unknown_attributes)


# ---------------------------------------------------------------------------
# (B3) aggregate method
# ---------------------------------------------------------------------------
def test_b3_agent_trace_unknown_attributes_counts_spans_per_name_in_order():
    trace = AgentTrace(
        spans=[
            Span(span_id="1", name="a", kind="llm", unknown_attributes=["b.attr", "a.attr"]),
            Span(span_id="2", name="b", kind="tool", unknown_attributes=["a.attr"]),
            Span(span_id="3", name="c", kind="chain"),
        ],
        source="otel",
    )
    assert trace.unknown_attributes() == {"a.attr": 2, "b.attr": 1}
    assert list(trace.unknown_attributes()) == ["a.attr", "b.attr"]  # sorted, deterministic


# ---------------------------------------------------------------------------
# (B4) rows round-trip
# ---------------------------------------------------------------------------
def test_b4_rows_artifact_round_trip_retains_the_names_and_metrics_are_unchanged():
    rows = run.rows_from_artifact(proj.fixture_text(V2))
    assert len(rows) == 1
    trace = rows[0].trace
    assert trace is not None
    assert trace.unknown_attributes() == {"weird.vendor.attr": 1}
    assert trace.to_dict() == _row(V2)["trace"]  # the artifact is what a writer emits
    assert trace.metrics() == _row(V1)["trace_metrics"]
    assert rows[0].trace_metrics == _row(V1)["trace_metrics"]


# ---------------------------------------------------------------------------
# (B5) the CLI flag is the only surface
# ---------------------------------------------------------------------------
def _fixture_project() -> tuple:
    root, ledger = proj.ledger_project()
    proj.add_run(
        ledger,
        "run-v1",
        tags={"evalops.group": "g1"},
        artifacts={"evalops/rows.jsonl": proj.fixture_text(V1)},
    )
    proj.add_run(
        ledger,
        "run-v2",
        tags={"evalops.group": "g2"},
        artifacts={"evalops/rows.jsonl": proj.fixture_text(V2)},
    )
    return root, ledger


def test_b5_the_default_traces_payload_has_no_unknown_attribute_keys():
    root, _ = _fixture_project()
    try:
        code, envelope = _traces_cli(root, "traces", "--group", "g2")
        assert code == 0, envelope
        data = envelope["data"]
        assert set(data) == {"summary", "rows"}
        assert set(data["rows"][0]) == {"row_id", "inputs", "scores", "error", "trace_metrics", "tree"}
        assert "unknown_attributes" not in json.dumps(data)
        assert data["rows"][0]["tree"].startswith("[agent] supervisor")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_b5_the_flag_adds_the_per_row_list_and_the_aggregate():
    root, _ = _fixture_project()
    try:
        code, envelope = _traces_cli(root, "traces", "--group", "g2", "--unknown-attrs")
        assert code == 0, envelope
        data = envelope["data"]
        assert data["rows"][0]["unknown_attributes"] == ["weird.vendor.attr"]
        assert data["unknown_attributes"] == {"weird.vendor.attr": 1}
        # v1: no names, an empty list and an empty aggregate (never an error)
        code, legacy = _traces_cli(root, "traces", "--group", "g1", "--unknown-attrs")
        assert code == 0, legacy
        assert legacy["data"]["rows"][0]["unknown_attributes"] == []
        assert legacy["data"]["unknown_attributes"] == {}
        # `--row` filtering still applies with the flag
        code, filtered = _traces_cli(root, "traces", "--group", "g2", "--unknown-attrs", "--row", "99999")
        assert code == 0 and filtered["data"]["rows"] == [] and filtered["data"]["unknown_attributes"] == {}
        code, kept = _traces_cli(root, "traces", "--group", "g2", "--unknown-attrs", "--row", "00000")
        assert code == 0 and len(kept["data"]["rows"]) == 1
        assert kept["data"]["rows"][0]["unknown_attributes"] == ["weird.vendor.attr"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (B6) a planted value never reaches the envelope
# ---------------------------------------------------------------------------
def test_b6_a_planted_value_under_an_unmodeled_attribute_never_reaches_the_envelope():
    secret = "sk-live-PLANTED-UNMODELLED-7f3a"
    span = normalize(
        _raw(
            "1",
            "chat gpt-4o",
            attrs={
                "gen_ai.operation.name": "chat",
                "vendor.secret.attr": secret,
                "vendor.other.attr": secret,
            },
        )
    )
    assert span.unknown_attributes == ["vendor.other.attr", "vendor.secret.attr"]
    assert secret not in json.dumps(AgentTrace(spans=[span], trace_id="t", source="otel").to_dict())

    # end to end: the same raw capture through a real `evalops eval`, then `traces --unknown-attrs`
    root = proj.project(
        config=proj.BASE_CONFIG.replace("fake_app:", "trace_otel_app:"),
        files={"trace_otel_app.py": OTEL_APP.replace("PLANTED-OTEL-SECRET-7f3a", secret)},
    )
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, out
        code, envelope = proj.run_cli(root, "traces", "--group", out["data"]["group"], "--unknown-attrs")
        assert code == 0, envelope
        assert "vendor.secret.attr" in envelope["data"]["unknown_attributes"]
        assert secret not in json.dumps(envelope)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# a fake app that emits synthetic OTel spans through the REAL CaptureProcessor
# ---------------------------------------------------------------------------
OTEL_APP = '''
"""Emits synthetic OTel spans through the real CaptureProcessor (no SDK is installed)."""

import os
import types

import evalops
from evalops.traces.capture import CaptureProcessor

PROCESSOR = CaptureProcessor()
SECRET = "PLANTED-OTEL-SECRET-7f3a"


def _span(i: int):
    ctx = types.SimpleNamespace(trace_id=99, span_id=100 + i)
    span = types.SimpleNamespace(
        name="chat gpt-4o",
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": "gpt-4o",
            "gen_ai.usage.input_tokens": 12,
            "gen_ai.usage.output_tokens": 8,
            "weird.vendor.attr": SECRET,
            "vendor.secret.attr": SECRET,
        },
        start_time=1_000 + i,
        end_time=1_010 + i,
        events=[],
        parent=None,
        status=None,
    )
    span.get_span_context = lambda: ctx
    return span


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools_manifest_sha": "none"}


def build_predict_fn(prompts):
    def predict(question: str) -> str:
        if not os.environ.get("NO_TRACE"):
            for i in range(4):
                span = _span(i)
                PROCESSOR.on_start(span)
                PROCESSOR.on_end(span)
        return "ok"

    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers(evaluator="core"):
    return [noop]


def judge_meta():
    return {"llm_judge_count": 0}
'''
