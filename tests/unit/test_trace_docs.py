"""WP1.5 (E): the documentation describes the shipped code, not the plan.

E1 -- `docs/CONTRACT.md` §5 carries the additive nested-trace row: `TRACE_SCHEMA_VERSION = 2`,
absent = v1, readers accept 1-2, future/non-int = exit 7 naming the artifact, and the version never
reaches `app_version`, the dataset fingerprint or `row_key`.
E2 -- `docs/AGENT_TRACES.md` documents the version matrix, the SOURCE set, `--unknown-attrs` (names
only, never values) and the breaker-parity table, which is **tied to `BREAKER_PARITY` here** so the
docs cannot drift from the code.
E3 -- no config/schema or envelope change is proven by the existing drift and golden tests
(`test_config_models.py::test_committed_schema_is_byte_identical_to_the_generated_one`,
`test_envelope.py::test_envelope_goldens_cover_the_deterministic_commands`); this module adds the
narrow claim that the trace document version is not a config key at all.

Every test is a zero-argument function, so the dependency-free runner runs this module too.
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from evalops.traces import SOURCE, TRACE_SCHEMA_VERSION  # noqa: E402
from evalops.traces.capture import BREAKER_PARITY  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
CONTRACT = (REPO / "docs" / "CONTRACT.md").read_text()
AGENT_TRACES = (REPO / "docs" / "AGENT_TRACES.md").read_text()


def _contract_trace_section() -> str:
    assert "### 5.6" in CONTRACT, "CONTRACT §5.6 must exist"
    return CONTRACT.split("### 5.6", 1)[1].split("\n## ", 1)[0]


# ---------------------------------------------------------------------------
# (E1) CONTRACT §5, additive
# ---------------------------------------------------------------------------
def test_e1_contract_documents_the_nested_trace_version_additively():
    section = _contract_trace_section()
    assert "TRACE_SCHEMA_VERSION = 2" in section
    assert "`evalops/rows.jsonl`" in section
    assert "absent" in section and "v1" in section  # absent = v1
    assert "`1` or `2`" in section  # readers accept 1-2
    assert "exit 7" in section  # future/non-int -> exit 7
    assert "never enters `app_version` or\n  `row_key`" in section
    assert "trace-less" in section and "embeds a trace document" in section
    assert "dataset fingerprint" in section
    # the versioned-artifact table names the nested document, not just the rows line
    assert "nested trace document" in CONTRACT


def test_e1_the_contract_trace_version_matches_the_code():
    assert f"TRACE_SCHEMA_VERSION = {TRACE_SCHEMA_VERSION}" in _contract_trace_section()
    # §5.1 (redaction) still claims authorship of the trace boundary; §5.6 is additive, not a rename
    assert "`AgentTrace.to_dict`" in CONTRACT


# ---------------------------------------------------------------------------
# (E2) AGENT_TRACES.md
# ---------------------------------------------------------------------------
def test_e2_agent_traces_documents_the_version_matrix_and_source_set():
    assert "TRACE_SCHEMA_VERSION = 2" in AGENT_TRACES
    for marker in ("absent", "`1` or `2`", "exit 7", "`evalops/rows.jsonl`", "dataset:<name>"):
        assert marker in AGENT_TRACES, marker
    assert "`otel`, `langchain`, `openai_agents`, `mixed`, `stored`, `unknown`" in AGENT_TRACES
    for source in SOURCE:
        assert f"`{source}`" in AGENT_TRACES, source
    assert "never enters `app_version` or `row_key`" in AGENT_TRACES
    assert "embeds a trace document" in AGENT_TRACES


def test_e2_agent_traces_documents_unknown_attributes_names_only():
    assert "--unknown-attrs" in AGENT_TRACES
    assert "name only" in AGENT_TRACES and "never by value" in AGENT_TRACES
    assert "`Span.unknown_attributes`" in AGENT_TRACES
    assert "`{name: span_count}`" in AGENT_TRACES
    assert "byte-identical" in AGENT_TRACES  # the default payload is frozen


def test_e2_agent_traces_breaker_table_matches_breaker_parity_one_row_per_path():
    assert set(BREAKER_PARITY) == {"langchain", "otel", "openai_agents", "custom"}
    for path, state in BREAKER_PARITY.items():
        assert f"| `{path}` | `{state}` |" in AGENT_TRACES, (path, state)
    # the async-only max_seconds caveat and the sync limitation are stated, not implied
    assert "max_seconds" in AGENT_TRACES
    assert "async" in AGENT_TRACES and "sync" in AGENT_TRACES
    assert "cannot be interrupted" in AGENT_TRACES
    assert "never relabelled as `budget_exceeded`" in AGENT_TRACES


# ---------------------------------------------------------------------------
# (E3) the trace document version is not a config key (schema drift is covered by the drift test)
# ---------------------------------------------------------------------------
def test_e3_no_config_key_or_schema_entry_was_added_for_the_trace_version():
    schema = json.loads((REPO / "docs" / "evalops.schema.json").read_text())
    dumped = json.dumps(schema)
    assert "trace_schema_version" not in dumped
    assert "unknown_attrs" not in dumped
    assert "unknown_attributes" not in dumped
    # the frozen default `traces` golden has no new keys
    golden = json.loads((REPO / "tests" / "fixtures" / "envelope" / "traces.json").read_text())
    assert set(golden["data"]) == {"summary", "rows"}
    assert set(golden["data"]["rows"][0]) == {"row_id", "inputs", "scores", "error", "trace_metrics", "tree"}
