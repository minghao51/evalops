"""WP3.2a: the logfire read path, tested offline against a fake SDK (no network, no vendor install).

What this module falsifies, group by group:

(a) ``usage()`` calls the Query API with the **keyword-only** ``min_timestamp`` -- the argument the
    current SDK requires and the old code omitted (a ``TypeError`` live) -- and with the exact SQL
    statement, character for character (the statement is now asserted by a test instead of carried
    as an unverified marker), against the configured input/output token attributes;
(b) the window is derived from ``usage_lookback_hours`` (default 24), is a timezone-aware
    ``datetime``, and is recomputed on every call (never cached on the plugin, where a long retry
    loop would reuse a stale window);
(c) the retry loop survives: a first read-back that finds nothing is retried until the ingestion
    catches up, and a read token that is absent short-circuits to ``{}`` without building a client;
(d) the span name is the constant ``"evalops.row"`` -- never an f-string interpolated from the
    caller's source frame -- with ``row_id``/``evalops_run_id`` as attributes, and the trace id is
    still returned as 32 hex characters;
(e) ``doctor()`` keeps reporting ``usage_lookup`` from ``read_token``.

The fakes are installed with :func:`fake_modules` inside each test and removed afterwards (the
``test_native_judges.py`` posture): ``tests/unit/test_presets.py`` pins the real offline vendor-import
outcome in the same process, so a leaked ``sys.modules["logfire"]`` would corrupt those pins.
Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import contextlib
import inspect
import pathlib
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

from evalops.plugins import logfire as logfire_module  # noqa: E402
from evalops.plugins.logfire import LogfirePlugin  # noqa: E402
from evalops.ports import Usage  # noqa: E402


# ---------------------------------------------------------------------------
# a fake logfire + opentelemetry SDK, recording every call
# ---------------------------------------------------------------------------
class FakeSpan:
    """The span object ``logfire.span`` yields: records attributes set on it."""

    def __init__(self) -> None:
        self.attributes: dict[str, Any] = {}

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = value


class FakeLogfire:
    """The fake ``logfire`` module state: one ``span()`` per traced row, plus ``configure``."""

    def __init__(self) -> None:
        self.spans: list[dict[str, Any]] = []
        self.configured: list[dict[str, Any]] = []
        self.flushed = 0

    def configure(self, **kwargs: Any) -> None:
        self.configured.append(kwargs)

    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any):
        recorded = {"name": name, "attributes": attributes, "span": FakeSpan()}
        self.spans.append(recorded)
        yield recorded["span"]


class FakeQueryClient:
    """The fake ``LogfireQueryClient``: records each call and answers from a scripted queue."""

    #: Class-level so a test can inspect the calls after the ``with`` block closes the client.
    calls: list[dict[str, Any]] = []
    responses: list[Any] = []
    read_tokens: list[str] = []

    def __init__(self, *, read_token: str) -> None:
        self.read_token = read_token
        FakeQueryClient.read_tokens.append(read_token)

    def __enter__(self) -> "FakeQueryClient":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        return None

    def query_json_rows(self, *, sql: str, min_timestamp: datetime) -> Any:
        FakeQueryClient.calls.append({"sql": sql, "min_timestamp": min_timestamp})
        return FakeQueryClient.responses.pop(0) if FakeQueryClient.responses else {"rows": []}


def reset_fakes(**knobs: Any) -> FakeLogfire:
    """Fresh fake state; ``responses`` scripts the query client's answers in order."""
    FakeQueryClient.calls = []
    FakeQueryClient.responses = list(knobs.get("responses", []))
    FakeQueryClient.read_tokens = []
    return FakeLogfire()


def fake_module(name: str, **attributes: Any) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


@contextlib.contextmanager
def fake_modules(modules: dict[str, types.ModuleType]):
    """Install fake SDK modules for exactly one test, restoring ``sys.modules`` afterwards."""
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


def sdk(logfire: FakeLogfire, *, trace_id: int = 0x1234) -> dict[str, types.ModuleType]:
    """The four fake modules a plugin call needs: logfire, its query client, opentelemetry + trace."""
    query_client = fake_module("logfire.query_client", LogfireQueryClient=FakeQueryClient)
    otel_trace = fake_module(
        "opentelemetry.trace",
        get_current_span=lambda: types.SimpleNamespace(
            get_span_context=lambda: types.SimpleNamespace(trace_id=trace_id)
        ),
    )
    otel = fake_module("opentelemetry", trace=otel_trace)
    logfire_module = fake_module(
        "logfire",
        configure=logfire.configure,
        span=logfire.span,
        force_flush=lambda: logfire.flushed + 1,
        query_client=query_client,
    )
    return {"logfire": logfire_module, "logfire.query_client": query_client, "opentelemetry": otel}


def plugin(options: dict[str, Any] | None = None) -> LogfirePlugin:
    """A plugin over fake modules: ``configure`` off unless a test asks for it (that path has its own)."""
    return LogfirePlugin(None, {"configure": False, **(options or {})})


#: The statement the plugin must send, built from the default attribute names.
EXPECTED_SQL = (
    "SELECT trace_id, SUM(CAST(attributes->>'gen_ai.usage.input_tokens' AS BIGINT)) AS i, "
    "SUM(CAST(attributes->>'gen_ai.usage.output_tokens' AS BIGINT)) AS o FROM records "
    "WHERE trace_id IN ('abc123', 'def456') GROUP BY trace_id"
)


# ---------------------------------------------------------------------------
# (a)+(b) the query: keyword-only min_timestamp, exact SQL, the configured lookback
# ---------------------------------------------------------------------------
def test_usage_passes_the_required_min_timestamp_and_the_exact_sql():
    fake = reset_fakes(
        responses=[
            {
                "rows": [
                    {"trace_id": "abc123", "i": 12, "o": 3},
                    {"trace_id": "def456", "i": 0, "o": 9},
                ]
            }
        ]
    )
    with fake_modules(sdk(fake)):
        before = datetime.now(timezone.utc)
        found = plugin({"read_token": "tok", "usage_retries": 1}).usage(["abc123", "def456"])
        after = datetime.now(timezone.utc)
    assert found == {"abc123": Usage(12, 3), "def456": Usage(0, 9)}, found
    assert len(FakeQueryClient.calls) == 1, FakeQueryClient.calls
    call = FakeQueryClient.calls[0]
    assert sorted(call) == ["min_timestamp", "sql"], sorted(call)  # keyword-only, exactly two kwargs
    assert call["sql"] == EXPECTED_SQL, call["sql"]
    stamp = call["min_timestamp"]
    assert isinstance(stamp, datetime) and stamp.tzinfo == timezone.utc, stamp
    # the default window is 24 hours, measured from *now* (a per-call computation)
    assert before - timedelta(hours=24, seconds=1) <= stamp <= after - timedelta(hours=24) + timedelta(seconds=1), stamp


def test_usage_lookback_hours_sets_the_window_and_is_recomputed_per_call():
    """Same plugin instance, two calls, two windows: the value is not cached at construction."""
    fake = reset_fakes(responses=[{"rows": []}, {"rows": []}])
    with fake_modules(sdk(fake)):
        instance = plugin({"read_token": "tok", "usage_retries": 1, "usage_lookback_hours": 2})
        first = datetime.now(timezone.utc)
        instance.usage(["abc123"])
        second = datetime.now(timezone.utc)
        instance.usage(["abc123"])
        last = datetime.now(timezone.utc)
    assert len(FakeQueryClient.calls) == 2, FakeQueryClient.calls
    first_stamp, second_stamp = (call["min_timestamp"] for call in FakeQueryClient.calls)
    assert first - timedelta(hours=2, seconds=1) <= first_stamp <= second - timedelta(hours=2)
    assert second - timedelta(hours=2) <= second_stamp <= last - timedelta(hours=2) + timedelta(seconds=1)
    source = inspect.getsource(LogfirePlugin.usage)
    assert "datetime.now(timezone.utc)" in source, source  # computed in the method, not __init__


# ---------------------------------------------------------------------------
# (c) the retry loop and the no-token short circuit
# ---------------------------------------------------------------------------
def test_usage_retries_until_asynchronous_ingestion_returns_the_rows():
    fake = reset_fakes(
        responses=[{"rows": []}, {"rows": [{"trace_id": "abc123", "i": 7, "o": 1}]}],
    )
    with fake_modules(sdk(fake)):
        found = plugin({"read_token": "tok", "usage_retries": 3, "usage_retry_seconds": 0}).usage(["abc123"])
    assert found == {"abc123": Usage(7, 1)}, found
    assert len(FakeQueryClient.calls) == 2, FakeQueryClient.calls  # first miss, then the hit


def test_usage_without_a_read_token_never_builds_a_client():
    fake = reset_fakes()
    with fake_modules(sdk(fake)):
        assert plugin({}).usage(["abc123"]) == {}
        assert plugin({"read_token": "tok"}).usage([]) == {}
    assert FakeQueryClient.read_tokens == [], FakeQueryClient.read_tokens


def test_usage_skips_trace_ids_that_are_not_hex():
    """The id list is interpolated into the SQL, so a non-hex id is dropped rather than quoted."""
    fake = reset_fakes()
    with fake_modules(sdk(fake)):
        plugin({"read_token": "tok", "usage_retries": 1}).usage(["abc123", "not-hex; DROP"])
    assert "'abc123'" in FakeQueryClient.calls[0]["sql"], FakeQueryClient.calls[0]["sql"]
    assert "DROP" not in FakeQueryClient.calls[0]["sql"], FakeQueryClient.calls[0]["sql"]


# ---------------------------------------------------------------------------
# (d) the span: a constant name, row identity as attributes
# ---------------------------------------------------------------------------
def test_the_span_name_is_the_constant_and_the_row_identity_travels_as_attributes():
    fake = reset_fakes()
    with fake_modules(sdk(fake, trace_id=0x1234)):
        instance = plugin()
        traced = instance.wrap(lambda **inputs: f"answer: {inputs['question']}", run_id="run-1", row_id="00001")
        out, trace_id = traced(question="hello")
    assert out == "answer: hello"
    assert trace_id == format(0x1234, "032x") and len(trace_id) == 32, trace_id
    assert len(fake.spans) == 1, fake.spans
    span = fake.spans[0]
    assert span["name"] == "evalops.row", span["name"]
    assert span["attributes"]["row_id"] == "00001", span["attributes"]
    assert span["attributes"]["evalops_run_id"] == "run-1", span["attributes"]
    assert span["span"].attributes["outputs"] == "answer: hello", span["span"].attributes
    source = inspect.getsource(LogfirePlugin.wrap)
    assert 'logfire.span("evalops.row"' in source, source  # a literal, not an f-string
    assert 'f"evalops.row' not in source, source


# ---------------------------------------------------------------------------
# (e) the plugin surface that stays: configure, flush, doctor
# ---------------------------------------------------------------------------
def test_configure_is_called_by_default_and_doctor_reports_the_read_token():
    fake = reset_fakes()
    with fake_modules(sdk(fake)):
        configured = LogfirePlugin(None, {"service_name": "wp32"})
        assert fake.configured == [{"service_name": "wp32", "send_to_logfire": "if-token-present"}], fake.configured
        assert configured.doctor() == {"plugin": "logfire", "ok": True, "usage_lookup": False}
        assert plugin({"read_token": "tok"}).doctor()["usage_lookup"] is True


def test_the_docstring_names_the_httpx_requirement():
    """D3: `read_token` needs httpx, which the logfire extra does not pull."""
    docstring = logfire_module.__doc__
    assert docstring is not None and "httpx" in docstring, docstring
