"""WP1.5 (D): the v1/v2 rows fixtures, and compatibility across the trace version.

D1 -- `rows_v1.jsonl` is the legacy shape (rows line `schema_version: 1`, nested trace with no
version and no `unknown_attributes`); `rows_v2.jsonl` is the same trace document with
`"schema_version": 2` and per-span names. The pin below makes the pair impossible to drift.
D2 -- a v1 artifact still gates, with the same checks list and verdict as v2.
D3 -- `evalops traces --unknown-attrs` reads both, names the planted attribute on v2 and invents
nothing on v1.
D4 -- the v1 -> v2 upgrade writes exactly the v2 document; metrics are equal; v0 rows still read.

Every test is a zero-argument function, so the dependency-free runner runs this module too.
"""

import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import run  # noqa: E402
from evalops.traces import AgentTrace  # noqa: E402

V1 = "traces/rows_v1.jsonl"
V2 = "traces/rows_v2.jsonl"
V0 = proj.FIXTURES / "formats" / "v0" / "rows.jsonl"
PLANTED = "weird.vendor.attr"

#: Fabricating both runs' metrics identically isolates the trace document as the only variable.
RUN_METRICS = {"tool_trajectory/mean": 1.0, "error_rate": 0.0}
GATES = """
gates:
  reliability:
    success: [{metric: tool_trajectory/mean, min: 1.0}]
    min_pass_hat_k: 0.5
"""


def _row(name: str) -> dict:
    return json.loads(proj.fixture_text(name).strip())


def _gate_project() -> tuple:
    """A temp project whose ledger is an in-memory fixture with a reliability rule configured."""
    name = proj.ledger_name()
    root = proj.project(proj.ledger_config(name) + GATES)
    module = proj.install_ledger(root, name)
    proj.ledger_clear(module)
    return root, module


def _add_fixture_run(module, run_id: str, group: str, fixture: str) -> None:
    proj.add_run(
        module,
        run_id,
        tags={"evalops.group": group, "evalops.repeat": "1"},
        metrics=dict(RUN_METRICS),
        artifacts={"evalops/rows.jsonl": proj.fixture_text(fixture)},
    )


# ---------------------------------------------------------------------------
# (D1) the pair is exact
# ---------------------------------------------------------------------------
def test_d1_rows_v1_is_the_legacy_shape_and_rows_v2_is_the_same_trace_versioned():
    v1, v2 = _row(V1), _row(V2)
    # the rows *line* stays FORMAT_VERSION (1): only the nested trace document is versioned
    assert v1["schema_version"] == v2["schema_version"] == 1
    assert "schema_version" not in v1["trace"]
    assert v2["trace"]["schema_version"] == 2

    assert all("unknown_attributes" not in span for span in v1["trace"]["spans"])
    assert all("unknown_attributes" in span for span in v2["trace"]["spans"])
    stripped = [{k: v for k, v in span.items() if k != "unknown_attributes"} for span in v2["trace"]["spans"]]
    assert stripped == v1["trace"]["spans"]  # the same spans, not two different traces
    assert [span["unknown_attributes"] for span in v2["trace"]["spans"]] == [[], [PLANTED], []]

    for field in ("inputs", "expectations", "outputs", "scores", "usage", "trace_metrics", "row_key"):
        assert v1[field] == v2[field], field


# ---------------------------------------------------------------------------
# (D2) a v1 artifact still gates, identically
# ---------------------------------------------------------------------------
def test_d2_a_v1_rows_artifact_gates_with_the_same_checks_as_v2():
    root, module = _gate_project()
    try:
        _add_fixture_run(module, "run-v1", "g1", V1)
        _add_fixture_run(module, "run-v2", "g2", V2)
        code_v1, gate_v1 = proj.run_cli_in_process(root, "gate", "--group", "g1")
        code_v2, gate_v2 = proj.run_cli_in_process(root, "gate", "--group", "g2")
        assert code_v1 == code_v2 == 0, (gate_v1, gate_v2)
        assert gate_v1["ok"] is True and gate_v2["ok"] is True
        assert gate_v1["data"]["checks"] == gate_v2["data"]["checks"]
        assert any(c["check"].startswith("pass_hat_k") for c in gate_v1["data"]["checks"])
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (D3) the CLI reads both fixtures
# ---------------------------------------------------------------------------
def test_d3_traces_reads_both_fixtures_and_only_v2_names_the_planted_attribute():
    root, module = _gate_project()
    try:
        _add_fixture_run(module, "run-v1", "g1", V1)
        _add_fixture_run(module, "run-v2", "g2", V2)
        code, legacy = proj.run_cli_in_process(root, "traces", "--group", "g1", "--unknown-attrs")
        assert code == 0, legacy
        assert legacy["data"]["unknown_attributes"] == {}
        assert legacy["data"]["rows"][0]["unknown_attributes"] == []
        assert legacy["data"]["rows"][0]["tree"].startswith("[agent] supervisor")

        code, versioned = proj.run_cli_in_process(root, "traces", "--group", "g2", "--unknown-attrs")
        assert code == 0, versioned
        assert versioned["data"]["unknown_attributes"] == {PLANTED: 1}
        assert versioned["data"]["rows"][0]["unknown_attributes"] == [PLANTED]
        assert versioned["data"]["rows"][0]["tree"].startswith("[agent] supervisor")

        # the planted value never made it into the fixture, so it cannot be in the payload either
        assert "PLANTED-VENDOR-VALUE" not in json.dumps(versioned)
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (D4) the upgrade path and v0 compatibility
# ---------------------------------------------------------------------------
def test_d4_upgrading_the_v1_trace_writes_exactly_the_v2_document():
    v1, v2 = _row(V1)["trace"], _row(V2)["trace"]
    upgraded = AgentTrace.from_dict(v1)
    assert upgraded is not None and upgraded.schema_version == 1
    # `to_dict()` never invents a name: the legacy serializer had no field to carry it, so the only
    # delta between the upgraded document and the fixture is the name the capture actually saw.
    assert upgraded.to_dict() == {
        **v2,
        "spans": [{**span, "unknown_attributes": []} for span in v2["spans"]],
    }
    for span in upgraded.spans:
        if span.kind == "tool":
            span.unknown_attributes = [PLANTED]
    assert upgraded.to_dict() == v2

    upgraded_metrics = upgraded.metrics()
    re_read = AgentTrace.from_dict(v2)
    assert re_read is not None and re_read.schema_version == 2
    assert upgraded_metrics == re_read.metrics() == _row(V1)["trace_metrics"]


def test_d4_the_v0_rows_fixture_still_reads():
    rows = run.rows_from_artifact(V0.read_text())
    assert len(rows) == 3
    assert all(r.trace is None and r.row_key is None for r in rows)  # v0: no trace, no stable key
    assert rows[0].row_id == "00000" and rows[0].scores["facts"] == 1.0
