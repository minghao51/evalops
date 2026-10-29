"""The evalops rubric judge over LiteLLM: the declared fallback, never the default (WP3b.3).

Phase 3b is native-first: a declared ``judges.<name>`` spec runs on the evaluator backend's own
judge wherever one exists (:mod:`evalops.scorers.native`). This module is the **fallback** a spec
selects explicitly with ``judge: litellm`` -- evalops owns the prompt, the parsing and the cost
attribution, and LiteLLM is only the transport. It is evaluator-independent, so it works for a
backend that has no native judge and for one whose SDK is not installed.

Three rules this module keeps
-----------------------------
* **Vendor-free core (D4).** ``litellm`` is imported *inside* the factory body, so importing this
  module never needs the package; ``tests/unit/test_no_vendor_imports.py`` pins exactly that lazy
  shape. A missing ``litellm`` is ``CapabilityError`` -> **exit 6** whose hint is
  ``pip install litellm``, chained ``from`` the ``ImportError`` (CONTRACT §3.0/§7.4) -- the same
  posture as the native adapters, so an agent gets the install line instead of a traceback.
* **Strict parsing, never a silent zero (packet B3).** The rubric asks for one JSON object; every
  way that contract can be broken raises ``EvalOpsError(..., EXIT_RUNTIME)`` naming the judge with
  at most :data:`MAX_EVIDENCE_CHARS` of raw output as evidence. The row loop's existing handler
  records it in ``row.error`` and the score is *absent* -- a judge that could not answer is never
  counted as a failure, and never as ``0.0``/``None``. The parse is strict about JSON itself too:
  the non-standard ``NaN``/``Infinity``/``-Infinity`` constants -- which ``json.loads`` accepts by
  default -- are rejected, as is a number evalops cannot represent as a finite float, because every
  comparison against ``NaN`` is ``False`` and would slip through the clamp untouched. The only
  tolerance is the scale: a score outside ``[scale.min, scale.max]`` is clamped to the nearest bound
  (a model that says ``1.7`` on a 0..1 scale meant "maximally good"), and binarization stays
  ``calibrate``'s ``threshold``.
* **Cost is attributed per call (packet B4).** After a completion, the response's token usage is
  reported through :func:`evalops.usage.report_usage`, so the core evaluator's per-row ``collect()``
  puts it in ``row.usage`` and ``cost.measure`` counts it. A response with no usage reports nothing:
  evalops never invents tokens.

The rubric is never emitted. It reaches the vendor (that is its only purpose) and never appears in
a message, hint, ``repr`` or evidence snippet; a spec's public identity is ``judges.rubric_hash``.
That holds on the failure path as well: an evidence snippet -- the model's output *or* a vendor
exception's text, which can embed the request body -- is redacted when it reproduces the rubric
(:data:`RUBRIC_ECHO_MARKER`), so ``row.error`` cannot become a copy of it.

Unverified litellm details carry a registered beta id (see :mod:`evalops.beta`), the same posture as
:mod:`evalops.scorers.native` and ``evalops/plugins/``: the shapes below encode the LiteLLM
documentation as read for this WP, the unit tests pin *evalops'* behaviour against a fake module,
and phase 3 resolves each marker against a pinned SDK version. No network call happens here to
find out.

Reachability: only ``spec_judge`` (:mod:`evalops.scorers.judges`) builds this scorer, and only from
a validated :class:`evalops.judges.JudgeSpec` whose ``judge`` is ``"litellm"``. Gate-time pin
enforcement and resolved-model recording are WP3b.5; pairwise/panel wrappers are WP3b.4.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from typing import Any

from ..judges import JudgeSpec
from ..scoring import scorer
from ..usage import report_usage
from ..util import EXIT_RUNTIME, CapabilityError, EvalOpsError

#: The prompt template's version, recorded next to the template below. ``prompt_version`` on the
#: spec stays the *user-declared* identity (recorded, never resolved: resolution/enforcement is
#: WP3b.5); this constant only versions the evalops-owned wrapper text.
PROMPT_TEMPLATE_VERSION = "litellm-rubric/1"

#: How much of a row is rendered into the user message. A judge that cannot see the whole row would
#: silently mis-score; a judge that sees an unbounded row would silently blow the context window,
#: so the cut is a named constant and the truncation is marked in the text.
MAX_ROW_CHARS = 4000

#: How much raw model output an error may quote. Enough to debug, never a whole completion, and
#: never a reason to re-emit the rubric.
MAX_EVIDENCE_CHARS = 200

#: What replaces evidence that reproduces the rubric. A snippet is a debugging aid; the rubric is the
#: one thing that must never leave the request (packet E3), and a model that echoes it -- or a
#: provider error that embeds the request body -- would otherwise carry it into ``row.error``.
RUBRIC_ECHO_MARKER = "[redacted: rubric echo]"

#: How many consecutive rubric words count as an echo. The whole (whitespace-collapsed) rubric is
#: always checked first; this catches the partial echo -- one line of a long rubric -- that a plain
#: substring test would miss. Redaction is deliberately over-eager: losing a snippet costs debugging
#: context, leaking the rubric breaks the contract.
RUBRIC_ECHO_WORDS = 6

#: The non-standard JSON constants ``json.loads`` accepts by default. They are not JSON and not a
#: score: every comparison against ``NaN`` is ``False``, so a NaN would pass the scale clamp as-is.
_NON_FINITE_CONSTANTS = frozenset({"NaN", "Infinity", "-Infinity"})

#: The *standard* constants. Python 3.11+ only calls ``parse_constant`` for the non-standard three,
#: but 3.10 routes ``true``/``false``/``null`` through it too, so both shapes stay correct.
_STANDARD_CONSTANTS: dict[str, Any] = {"true": True, "false": False, "null": None}

# litellm-rubric/1 -- change this template only together with PROMPT_TEMPLATE_VERSION above (and
# the spec's own ``prompt_version``), because the two together are the judge's identity. The
# placeholder braces are doubled where the JSON contract below needs literal ``{``/``}``.
_PROMPT_TEMPLATE = """\
You are an evaluation judge. Apply the rubric exactly as written and nothing else.

RUBRIC
------
{rubric}

SCALE
-----
Emit one number on the scale {low}..{high} (inclusive). A score outside that range is invalid.

OUTPUT CONTRACT
---------------
Answer with a single JSON object and no other text:
{{"score": <number>, "reason": "<one short sentence>"}}
"score" is required and must be a JSON number (not a string, not a boolean). "reason" is optional.
"""


# The system message every call sends: the rubric + the scale + the JSON output contract (packet
# B2). Built per spec, because the scale bounds are the spec's, from the versioned template above.
def _system_prompt(spec: JudgeSpec) -> str:
    """The stable system message: rubric, declared scale, JSON output contract."""
    return _PROMPT_TEMPLATE.format(
        rubric=spec.rubric,
        low=_show_bound(spec.scale.min),
        high=_show_bound(spec.scale.max),
    )


def _show_bound(value: Any) -> str:
    """``0.0``/``5.0`` rather than ``0``/``5``: the scale is a numeric range, not a count."""
    return str(float(value))


def litellm_judge(spec: JudgeSpec) -> Callable[..., float]:
    """Build the evalops rubric fallback for ``spec`` as a neutral, ``call_scorer``-compatible scorer.

    Returns ``scorer(spec.name, llm=True)`` over ``(*, inputs, outputs, expectations, trace=None)``
    -> ``float``, exactly the return contract of the native adapters (``scoring.call_scorer`` and
    the core evaluator consume it unchanged). The float is always inside ``spec.scale``: in-range
    values pass through, others clamp to the nearest bound.

    Raises ``CapabilityError`` -> exit 6 when ``litellm`` is not installed (hint:
    ``pip install litellm``), chained from the ``ImportError``. That is deliberately a *build*-time
    failure: it surfaces at scorer construction, after the cost estimate and the dry run, but before
    any budget reservation or ledger row.
    """
    try:
        # VERIFY-BETA(litellm-completion-shape): the public entry point is `litellm.completion`, a
        # sync call returning a ModelResponse (OpenAI-compatible). Imported inside the factory so
        # this module stays vendor-free (D4) and the missing package is exit 6, not exit 5.
        # A static analyzer pointed at a checkout of this repo reports this import as unresolvable
        # (`reportMissingImports`): that is the intended state, not a defect. No vendor SDK is
        # installed in the offline environment, and the guard exists to keep this a *function-local*
        # import rather than a module-level one (same shape as `scorers/native.py`).
        import litellm  # pyright: ignore[reportMissingImports]
    except ImportError as exc:
        raise CapabilityError("litellm", f"rubric judge {spec.name!r}", hint="pip install litellm") from exc

    system_prompt = _system_prompt(spec)
    # VERIFY-BETA(litellm-completion-shape): the call kwargs are the OpenAI-compatible ones LiteLLM
    # forwards -- `model`, `messages`, `temperature`, `max_tokens`, `seed`, `response_format`.
    # `response_format` JSON mode is not supported by every provider LiteLLM fronts; a provider that
    # rejects it will surface as an exit-5 runtime error on the first call (phase 3 resolves the
    # fallback matrix).
    settings: dict[str, Any] = {
        # VERIFY-BETA(litellm-completion-shape): a provider-prefixed pin (`openai/gpt-...`) is passed through
        "model": spec.model,
        "temperature": spec.sampling.temperature,
        "max_tokens": spec.sampling.max_tokens,
        "response_format": {"type": "json_object"},
    }
    if spec.sampling.seed is not None:
        # A literal `seed=None` means different things per provider, so it is omitted, not sent.
        settings["seed"] = spec.sampling.seed

    @scorer(spec.name, llm=True)
    def _judge(*, inputs, outputs, expectations, trace=None) -> float:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _render_row(inputs=inputs, outputs=outputs, expectations=expectations)},
        ]
        # VERIFY-BETA(litellm-completion-shape): `completion(messages=..., **settings)` returns a
        # ModelResponse whose `choices[0].message.content` is the assistant text and whose `usage`
        # carries tokens.
        try:
            response = litellm.completion(messages=messages, **settings)
        except EvalOpsError:
            raise  # an evalops refusal (budget, capability) keeps its own code and hint
        except Exception as exc:  # noqa: BLE001 - vendor text is untrusted: it can echo the request
            raise _failed_call(spec, exc) from exc
        _report_tokens(response)
        return _score(_content(response, spec), spec)

    return _judge


# ---------------------------------------------------------------------------
# the row payload (deterministic, bounded)
# ---------------------------------------------------------------------------
def _render_row(*, inputs: Any, outputs: Any, expectations: Any) -> str:
    """The user message: inputs/outputs/expectations, rendered deterministically and truncated.

    ``sort_keys`` makes the same row render to the same bytes on every call (a judge that changes
    its answer because dict order changed would be untraceable), and ``default=str`` keeps an
    exotic row value a string instead of a crash. Truncation is marked in the text itself, so a
    judge that is reasoning about a cut row can see that it is looking at one.
    """
    payload = {
        "inputs": inputs,
        "outputs": outputs,
        "expectations": expectations or {},
    }
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    if len(text) <= MAX_ROW_CHARS:
        return text
    return text[:MAX_ROW_CHARS] + f"\n[truncated at {MAX_ROW_CHARS} characters]"


# ---------------------------------------------------------------------------
# strict parsing (packet B3): every failure raises, naming the judge
# ---------------------------------------------------------------------------
class _NonFiniteConstant(ValueError):
    """The output carried a non-standard JSON constant (``NaN``/``Infinity``/``-Infinity``)."""

    def __init__(self, token: str) -> None:
        super().__init__(token)
        self.token = token


def _reject_constant(token: str) -> Any:
    """``json.loads(parse_constant=...)``: reject the non-standard constants, keep the standard ones.

    ``json.loads`` accepts ``NaN``/``Infinity``/``-Infinity`` by default, which means ``{"score":
    NaN}`` would parse to ``float('nan')`` -- a value every comparison answers ``False`` for, so it
    would pass through the clamp and reach the metrics. The three standard constants are mapped back
    to their values because Python 3.10 routes them through this hook as well.
    """
    if token in _STANDARD_CONSTANTS:
        return _STANDARD_CONSTANTS[token]
    raise _NonFiniteConstant(token)


def _fail(spec: JudgeSpec, detail: str, evidence: Any = None, *, evidence_label: str = "output") -> EvalOpsError:
    """The one rejection: exit 5, the judge named, a bounded snippet of sanitized raw output.

    ``evidence`` is the model's own output (or the vendor's error text), never the rubric: the judge
    is what the user has to reconfigure, and the raw answer is what makes the failure debuggable.
    It always goes through :func:`_snippet`, which drops anything that reproduces the rubric.
    """
    message = f"judge '{spec.name}': {detail}"
    if evidence is not None:
        message = f"{message} ({evidence_label}: {_snippet(evidence, spec)})"
    return EvalOpsError(message, EXIT_RUNTIME)


def _failed_call(spec: JudgeSpec, exc: BaseException) -> EvalOpsError:
    """A vendor failure as an exit-5 error: the judge named, the exception *type*, sanitized text.

    Provider exceptions are treated exactly like completion content -- a provider that echoes the
    request body would otherwise put the rubric into ``row.error`` -- so the text is collapsed, cut
    at :data:`MAX_EVIDENCE_CHARS` and redacted when it repeats the rubric.
    """
    kind = type(exc).__name__
    text = _collapse(exc)
    if not text:
        return _fail(spec, f"the litellm call failed with {kind}")
    return _fail(spec, f"the litellm call failed with {kind}", evidence=text, evidence_label="error")


def _collapse(text: Any) -> str:
    """``text`` on one line. Never raises: a vendor exception's ``__str__`` is not trusted."""
    try:
        rendered = str(text)
    except Exception:  # noqa: BLE001 - an unprintable error is still just an error
        return ""
    return " ".join(rendered.split())


def _echoes_rubric(text: str, spec: JudgeSpec) -> bool:
    """True iff the collapsed ``text`` reproduces the rubric, whole or in a :data:`RUBRIC_ECHO_WORDS` run."""
    rubric = _collapse(spec.rubric)
    if not rubric or not text:
        return False
    if rubric in text:
        return True
    words = rubric.split()
    if len(words) < RUBRIC_ECHO_WORDS:
        return False  # a short rubric is only recognisable as a whole, and that was checked above
    span = len(words) - RUBRIC_ECHO_WORDS + 1
    return any(" ".join(words[i : i + RUBRIC_ECHO_WORDS]) in text for i in range(span))


def _snippet(text: Any, spec: JudgeSpec) -> str:
    """At most :data:`MAX_EVIDENCE_CHARS` of ``text``, on one line, never a rubric echo."""
    collapsed = _collapse(text)
    if _echoes_rubric(collapsed, spec):
        return RUBRIC_ECHO_MARKER
    if len(collapsed) <= MAX_EVIDENCE_CHARS:
        return repr(collapsed)
    return repr(collapsed[:MAX_EVIDENCE_CHARS]) + "..."


def _content(response: Any, spec: JudgeSpec) -> str:
    """``choices[0].message.content`` as a non-empty string, or an exit-5 rejection."""
    try:
        # VERIFY-BETA(litellm-completion-shape): OpenAI-shaped ModelResponse
        content = response.choices[0].message.content
    except (AttributeError, IndexError, KeyError, TypeError) as exc:
        raise _fail(spec, "the completion has no choices[0].message.content") from exc
    if not isinstance(content, str):
        raise _fail(spec, f"the completion content is {type(content).__name__}, not a string", evidence=content)
    if not content.strip():
        raise _fail(spec, "the completion content is empty", evidence=content)
    return content


def _score(content: str, spec: JudgeSpec) -> float:
    """The declared-scale score out of one completion, or an exit-5 rejection.

    Strict on the shape the prompt asked for and tolerant only on the range: an in-range number
    passes through unchanged, an out-of-range number clamps to the nearest bound. A bool or a
    numeric *string* is not a JSON number and is a rejection, not a coercion: ``{"score": "0.9"}``
    usually means the model ignored the output contract, and guessing would hide that. The parse is
    strict about JSON too: the non-standard ``NaN``/``Infinity``/``-Infinity`` constants are
    rejected rather than accepted, and a score that cannot become a finite float is a rejection
    naming the judge -- never a NaN that would slip through the clamp, never a raw traceback.
    """
    try:
        parsed = json.loads(content, parse_constant=_reject_constant)
    except _NonFiniteConstant as exc:
        raise _fail(
            spec,
            f'output carries the non-standard JSON constant {exc.token} ("score" must be a number)',
            evidence=content,
        ) from exc
    except json.JSONDecodeError as exc:
        raise _fail(spec, f"output is not valid JSON ({exc.msg})", evidence=content) from exc
    except ValueError as exc:
        # the decoder itself refused a numeric literal (an integer past CPython's digit limit);
        # uncaught it escapes as a raw ValueError traceback rather than the judge-named error
        raise _fail(spec, "output is not valid JSON (number literal out of range)", evidence=content) from exc
    if not isinstance(parsed, Mapping):
        raise _fail(spec, f"output is a JSON {type(parsed).__name__}, not an object", evidence=content)
    if "score" not in parsed:
        raise _fail(spec, 'output has no "score" key', evidence=content)
    raw = parsed["score"]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise _fail(spec, f'"score" is a JSON {type(raw).__name__}, not a number', evidence=content)
    try:
        value = float(raw)
    except (ValueError, OverflowError) as exc:
        # an integer past the float range is exit 5 naming the judge, never a traceback -- and never
        # a clamped bound, which would turn a broken judge into a confident perfect score
        raise _fail(spec, '"score" is a number evalops cannot represent as a float', evidence=content) from exc
    return _clamp(value, spec)


def _clamp(value: float, spec: JudgeSpec) -> float:
    """The scale clamp: out-of-range to the nearest bound, in-range untouched (packet B3).

    A non-finite value is a rejection, not a clamp: every comparison against ``NaN`` is ``False``,
    so the two range tests below would return the NaN untouched, and ``1e400`` parses to ``inf``
    (finite *syntax*, infinite value) which the clamp would silently turn into the upper bound. This
    is the last line of defence: no non-finite value can leave this function.
    """
    if not math.isfinite(value):
        raise _fail(spec, f'"score" is {value!r}, not a finite number')
    low, high = float(spec.scale.min), float(spec.scale.max)
    if value < low:
        return low
    if value > high:
        return high
    return value


# ---------------------------------------------------------------------------
# per-call cost attribution (packet B4)
# ---------------------------------------------------------------------------
def _token_count(value: Any) -> int | None:
    """A usage field as an ``int``, or ``None`` when the response did not carry a usable number.

    A bool is not a token count (JSON ``true`` is not a number) and a non-numeric value is a shape
    this module does not understand: both are skipped rather than coerced, because evalops never
    invents tokens.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):  # a NaN/inf token count is not something evalops will bill
        return None


def _report_tokens(response: Any) -> None:
    """Report the completion's token usage to the active row, if the response carries any.

    ``report_usage`` is a no-op outside a row (production use), and it *adds* to whatever the app
    already reported, so a judge call inside an instrumented row shows up in that row's ``usage``.
    Missing usage is skipped silently: an invented token count would corrupt both the cost report
    and the next estimate's baseline.
    """
    # VERIFY-BETA(litellm-completion-shape): `usage.prompt_tokens`/`.completion_tokens`
    usage = getattr(response, "usage", None)
    if usage is None:
        return
    prompt_tokens = _token_count(getattr(usage, "prompt_tokens", None))
    completion_tokens = _token_count(getattr(usage, "completion_tokens", None))
    if prompt_tokens is None and completion_tokens is None:
        return
    report_usage(input_tokens=prompt_tokens or 0, output_tokens=completion_tokens or 0)
