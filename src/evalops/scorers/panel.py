"""Comparison wrappers over whichever judge is in use: pairwise (position swap) and panel (WP3b.4).

Phase 3b is native-first (map decision D-judges): each evaluator backend runs its own judge, and
:mod:`evalops.scorers.judges` is the neutral wrapper layer over it. WP3b.3 made a declared spec
resolve to a scorer; this module adds the two *comparison* wrappers that compose those scorers --
or any plain judge callable -- without knowing which backend produced them:

* :func:`pairwise_judge` asks a judge which of two candidates is better, **twice per row** with the
  candidates swapped, then reports whether the decision survived the swap. A judge that prefers
  whichever candidate it sees first yields a visible ``<name>/swap_agreement`` of ``0.0`` instead of
  a hidden position bias.
* :func:`panel_judge` asks several judges about the same row and aggregates their votes by
  ``mean`` / ``majority`` / ``agreement``, always reporting ``<name>/panel_disagreement``, so a panel
  that does not actually agree cannot look unanimous downstream.

Three rules this module keeps
-----------------------------
* **Vendor-free (D4).** No SDK import, no I/O and no config read: the wrappers compose whatever
  callables the caller passes (a native adapter scorer, the LiteLLM rubric fallback, a plain
  function), exactly like :func:`evalops.scorers.judges.span_judge`.
* **Strict, never a silent tie (same posture as the rubric parser, WP3b.3).** An unknown pairwise
  verdict is an ``EvalOpsError`` (exit 5) naming the wrapper and the value -- never a fabricated
  ``'tie'``. A panel member that does not emit exactly one floatable vote (a list, a dict, an
  unparseable value) is an ``EvalOpsError`` naming the member index -- never a silently dropped
  member. A missing baseline and a judge exception propagate to the row's error channel.
* **Disagreement is a metric, not a footnote.** Both wrappers emit their companion metric through
  the multi-metric list-of-``{"key", "score"}`` shape :func:`evalops.scoring.call_scorer` consumes,
  so ``aggregate_rows`` turns it into ``<name>/swap_agreement/mean`` and
  ``<name>/panel_disagreement/mean`` and a gate can rule on it.

Reachability: like ``span_judge``/``trajectory_judge``, these are app-hook composables returned from
an app's ``build_scorers``. There is deliberately **no** ``evalops.yaml`` surface for them (no new
``JudgeRule`` key), no ``plan``/``gate`` wiring, and no resolved-model recording or gate-time pin
enforcement here -- that is WP3b.5.
"""

from __future__ import annotations

import inspect
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ..scoring import scorer, to_float
from ..util import EXIT_RUNTIME, EXIT_USAGE, EvalOpsError

#: The closed set of normalized pairwise verdicts, in the first call's frame.
PAIRWISE_VERDICTS = ("a", "b", "tie")

#: The closed set of panel aggregation methods.
PANEL_METHODS = frozenset({"mean", "majority", "agreement"})

#: The score of a pairwise tie: a visible middle value, never a silent pick.
PAIRWISE_TIE = 0.5

#: The metric suffix both wrappers use for their disagreement companion.
SWAP_AGREEMENT_SUFFIX = "swap_agreement"
PANEL_DISAGREEMENT_SUFFIX = "panel_disagreement"

#: The numbers a judge may return instead of a verdict string (``1`` -> A, ``-1`` -> B, ``0`` -> tie).
_NUMERIC_VERDICTS: tuple[tuple[int, str], ...] = ((1, "a"), (-1, "b"), (0, "tie"))


def _rejected(value: Any) -> EvalOpsError:
    """The one rejection shape of the normalizer: wrapper-named, value-named, exit 5."""
    return EvalOpsError(
        f"pairwise: cannot normalize {value!r} to one of {', '.join(PAIRWISE_VERDICTS)} "
        "(accepted: 'a'/'b'/'tie' case-insensitively, the numbers 1/-1/0, or {'winner': ...})",
        EXIT_RUNTIME,
    )


def _normalized(value: Any) -> str:
    """Normalize one verdict, without the swap flip (see :func:`normalize_pairwise`)."""
    if isinstance(value, bool):  # before int: a bool is an int and its 0/1 reading is ambiguous
        raise _rejected(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in PAIRWISE_VERDICTS:
            return text
        raise _rejected(value)
    if isinstance(value, (int, float)):
        for number, verdict in _NUMERIC_VERDICTS:
            if value == number:
                return verdict
        raise _rejected(value)
    if isinstance(value, Mapping) and "winner" in value:
        return _normalized(value["winner"])
    raise _rejected(value)


def normalize_pairwise(value: Any, *, swapped: bool = False) -> str:
    """Normalize a judge's pairwise verdict to ``'a' | 'b' | 'tie'`` (strict; never a silent tie).

    Accepted: ``'a'``/``'b'``/``'tie'`` (case-insensitive, stripped), the numbers ``1``/``-1``/``0``
    (``1.0``/``-1.0``/``0.0`` too; a ``bool`` is rejected because ``True``/``False`` would be
    ambiguous), and a mapping with a ``'winner'`` key whose value normalizes recursively. Anything
    else -- an unknown string, another number, ``None``, a list, a mapping without ``'winner'`` --
    raises ``EvalOpsError`` (exit 5) naming the wrapper and the value.

    ``swapped=True`` flips ``'a'`` and ``'b'`` (``'tie'`` stays): that is how the second, reversed
    judge call's verdict is brought back into the first call's frame before the two are compared.
    """
    verdict = _normalized(value)
    if not swapped or verdict == "tie":
        return verdict
    return "b" if verdict == "a" else "a"


def pairwise_judge(judge: Callable[..., Any], *, name: str, baseline_key: str = "baseline") -> Callable[..., Any]:
    """Position-swap pairwise judge: ``outputs`` (A) against ``expectations[baseline_key]`` (B).

    The judge is called **twice per row** -- ``judge(a=A, b=B, inputs=inputs)`` and then
    ``judge(a=B, b=A, inputs=inputs)`` -- and both verdicts are normalized (the second with
    ``swapped=True``) so they speak the first call's frame. The scorer returns two metrics:

    * ``name`` -- ``1.0`` when A wins, ``0.0`` when B wins, ``0.5`` on a tie;
    * ``name/swap_agreement`` -- ``1.0`` when both orders agree after normalization, else ``0.0``.
      A position-biased judge therefore shows up as ``0.0`` here rather than being smoothed into
      the decision metric.

    Candidate A is the row's ``outputs``; candidate B is ``expectations[baseline_key]``. Missing
    expectations or a missing baseline key raises ``EvalOpsError`` (exit 5) naming the key; a judge
    exception propagates to the row's error channel (a failed judge is never a tie).

    Cost: **2 judge calls per row** -- account for it in ``judge_meta().llm_judge_count``.
    """

    @scorer(name, llm=True)
    def _pairwise(*, inputs, outputs, expectations, trace=None):
        expectations = expectations or {}
        if baseline_key not in expectations:
            raise EvalOpsError(
                f"pairwise_judge('{name}'): row has no expectations['{baseline_key}'] to compare against",
                EXIT_RUNTIME,
            )
        baseline = expectations[baseline_key]
        forward = normalize_pairwise(judge(a=outputs, b=baseline, inputs=inputs))
        backward = normalize_pairwise(judge(a=baseline, b=outputs, inputs=inputs), swapped=True)
        decision = 1.0 if forward == "a" else 0.0 if forward == "b" else PAIRWISE_TIE
        return [
            {"key": name, "score": decision},
            {"key": f"{name}/{SWAP_AGREEMENT_SUFFIX}", "score": 1.0 if forward == backward else 0.0},
        ]

    return _pairwise


def _member_params(member: Callable[..., Any], *, name: str, index: int) -> Mapping[str, Any]:
    """The member's declared parameters (same kwarg-filtering rule as ``scoring.call_scorer``)."""
    try:
        return inspect.signature(member).parameters
    except (TypeError, ValueError) as exc:  # pragma: no cover - exotic callables
        raise EvalOpsError(
            f"panel_judge('{name}'): member {index} has no inspectable signature: {exc}",
            EXIT_RUNTIME,
        ) from exc


def _found_members(members: Any, *, name: str) -> tuple[Any, ...]:
    """The panel's members, from a real sequence only (exit 2): no iterators, no strings."""
    if isinstance(members, (str, bytes)):
        raise EvalOpsError(
            f"panel_judge('{name}'): judges must be a sequence of judge callables, not a "
            f"{type(members).__name__} -- iterating it would yield characters, not judges",
            EXIT_USAGE,
        )
    if not isinstance(members, Sequence):
        raise EvalOpsError(
            f"panel_judge('{name}'): judges must be a sequence of judge callables, got "
            f"{type(members).__name__} (a generator expression is consumed once and is not a panel)",
            EXIT_USAGE,
        )
    return tuple(members)


def _selection(members: Any, *, name: str, method: Any) -> tuple[tuple[Any, ...], str]:
    """Validate the panel at construction (exit 2) and return its members + method."""
    found = _found_members(members, name=name)
    if len(found) < 2:
        raise EvalOpsError(
            f"panel_judge('{name}'): a panel needs at least 2 members, got {len(found)}",
            EXIT_USAGE,
        )
    for index, member in enumerate(found):
        if not callable(member):
            raise EvalOpsError(
                f"panel_judge('{name}'): member {index} is not callable ({type(member).__name__})",
                EXIT_USAGE,
            )
    try:  # an unhashable method (a list, a dict) is a usage error, not a raw TypeError
        known = method in PANEL_METHODS
    except TypeError:
        known = False
    if not known:
        raise EvalOpsError(
            f"panel_judge('{name}'): unknown method {method!r} (expected one of {', '.join(sorted(PANEL_METHODS))})",
            EXIT_USAGE,
        )
    return found, method


def _vote(value: Any, *, name: str, index: int) -> float | None:
    """One member's vote: ``None`` (excluded) or exactly one floatable value; never silently dropped."""
    if value is None:
        return None
    if isinstance(value, (list, Mapping)):
        raise EvalOpsError(
            f"panel_judge('{name}'): member {index} returned a multi-metric {type(value).__name__}; "
            "a panel member must emit exactly one vote",
            EXIT_RUNTIME,
        )
    vote = to_float(value)
    if vote is None:
        raise EvalOpsError(
            f"panel_judge('{name}'): member {index} returned {value!r}, which is not a single floatable vote",
            EXIT_RUNTIME,
        )
    return vote


def panel_judge(judges: Sequence[Callable[..., Any]], *, name: str, method: str = "mean") -> Callable[..., Any]:
    """Aggregate several judges on one row: ``mean`` / ``majority`` / ``agreement``.

    ``judges`` is a sequence of **callables** that each return one floatable vote (a
    ``spec_judge(...)`` output is one; so is any function accepting the row kwargs). Every member is
    called with the row kwargs **it declares** -- inspected per member, ``trace`` only if it asks
    for it -- so a plain ``(*, inputs, outputs)`` member is not handed ``expectations``. A member
    returning ``None`` is excluded from the vote set; a member returning a list or a mapping (a
    multi-metric score) is an ``EvalOpsError`` naming its index, because a panel must never silently
    drop a member. Fewer than 2 members, a non-callable member, or an unknown ``method`` is an
    ``EvalOpsError`` at **construction** (exit 2: a panel of one is not a panel), and so are a
    non-sequence ``judges`` (a generator expression, a lone string -- iterating it would yield
    characters rather than judges) and an unhashable ``method`` (``[]``), which is a usage error
    naming the accepted methods, never a ``TypeError`` leaking out of construction.

    ``judges`` must be an actual sequence: it is inspected once at construction and its members are
    captured, so a one-shot iterator can never silently produce a panel whose votes depend on how
    often the scorer runs.

    Per row, with at least one vote, the scorer returns two metrics:

    * ``name`` -- the method's aggregate: ``mean`` the vote mean; ``majority`` the value held by a
      strict majority (more than half the votes), else the visible tie ``0.5``; ``agreement`` the
      common value when every vote is exactly equal, else ``None``;
    * ``name/panel_disagreement`` -- ``1 - (largest exact-value group's share)``, so ``0.0`` is
      unanimous and the value is reported for every method, including when ``agreement`` has no
      consensus and the score is ``None``.

    With **no** votes at all the scorer returns ``None`` (honest absence) and no companion metric:
    there is nothing to disagree about.

    Cost: **one judge call per member per row** -- account for it in ``judge_meta().llm_judge_count``.
    """
    members, method = _selection(judges, name=name, method=method)

    @scorer(name, llm=True)
    def _panel(*, inputs, outputs, expectations, trace=None):
        row_kwargs: dict[str, Any] = {"inputs": inputs, "outputs": outputs, "expectations": expectations or {}}
        votes: list[float] = []
        for index, member in enumerate(members):
            params = _member_params(member, name=name, index=index)
            call_kwargs = dict(row_kwargs)
            if "trace" in params:
                call_kwargs["trace"] = trace
            if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                call_kwargs = {k: v for k, v in call_kwargs.items() if k in params}
            vote = _vote(member(**call_kwargs), name=name, index=index)
            if vote is not None:
                votes.append(vote)
        if not votes:
            return None
        counts = Counter(votes)
        disagreement = 1.0 - max(counts.values()) / len(votes)
        if method == "mean":
            score: float | None = sum(votes) / len(votes)
        elif method == "majority":
            majority = next((value for value, count in counts.items() if count * 2 > len(votes)), None)
            score = majority if majority is not None else PAIRWISE_TIE
        else:  # agreement: the common value only when every vote is exactly equal
            score = votes[0] if len(counts) == 1 else None
        return [
            {"key": name, "score": score},
            {"key": f"{name}/{PANEL_DISAGREEMENT_SUFFIX}", "score": disagreement},
        ]

    return _panel
