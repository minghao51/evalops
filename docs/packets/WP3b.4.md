# WP3b.4 packet — pairwise (position swap) + panel wrappers (mean/majority/agreement)

Dispatch this file to `evalops-worker` (model `deepseek/deepseek-flash`, `cwd` = the repo root).
Repo: `/home/howt/work/evalops_universal/evalops` · `main` · **433 tests** (+1 contract, deselected).
Read `docs/IMPLEMENTATION_MAP.md`'s phase-3b row (this WP's deliverable and done-when),
`docs/CONTRACT.md` §6 (judge pinning context), `src/evalops/scoring.py` (`scorer`, `to_float`,
`call_scorer` — including the multi-metric list-of-`{"key", "score"}` return shape), and
`src/evalops/scorers/judges.py` + `src/evalops/scorers/native.py` (the wrapper-layer style and
docstring conventions this WP follows; `spec_judge` outputs are valid panel members).
Packet template: `docs/packets/WP3b.3.md` (newest).

## Environment (no system pip)

```bash
cd /home/howt/work/evalops_universal/evalops
.venv/bin/python -m pytest -q              # 433 tests
.venv/bin/python tests/run_without_pytest.py   # use the venv interpreter
make check
```

This WP is **pure Python, vendor-free**: the wrappers compose judge callables and need no SDK, no
fake modules, no new dependencies. No network, no docker.

## Goal

Phase 3b's remaining wrapper layer (map: "Pairwise and panel judges are wrappers around whichever
native judge is in use"). WP3b.3 made a declared spec resolvable to a scorer; WP3b.4 adds the two
comparison wrappers on top of that surface:

- **pairwise** — a position-swap judge: the decision (A vs B) must survive swapping the candidates,
  and a swap disagreement must be reported, never hidden;
- **panel** — multiple judges on one row, aggregated by mean / majority / agreement, with the
  panel's disagreement always reported.

Deliverable shape: two wrapper factories plus the normalizer in `scorers/`, returning
`call_scorer`-compatible multi-metric scorers an app hook can return from `build_scorers`. The
done-when from the map: "swap-invariance test: A vs B and B vs A agree after normalization; panel
disagreement reported".

Deliberately **out of scope**: any config/`evalops.yaml` surface for pairwise/panel (no new
`JudgeRule` keys, no schema change — these are app-hook composables exactly like
`span_judge`/`trajectory_judge`), any `run.py`/gate wiring, resolved-model recording and gate-time
pin enforcement (**3b.5**). There is **no** golden change and **no** `docs/CAPABILITIES.md` change
(these are not per-preset native judges).

The design decisions below are **fixed**. Do not re-derive or widen them; if one is impossible,
stop and report a blocker with the failing evidence.

### A. `src/evalops/scorers/panel.py` (new core module; vendor-free, no I/O)

- **A1** `normalize_pairwise(value, *, swapped=False) -> str` → `'a' | 'b' | 'tie'`:
  - accepted (closed set, strict): `'a'`/`'b'`/`'tie'` strings case-insensitively (stripped);
    numbers `1` → `'a'`, `-1` → `'b'`, `0` → `'tie'` (ints/floats equal to those values; a bool is
    **rejected** — ambiguous); a one-level mapping with a `'winner'` key whose value normalizes
    recursively. Anything else (unknown strings, other numbers, lists, None) →
    `EvalOpsError(..., EXIT_RUNTIME)` naming the wrapper and the offending value — a strict
    rejection, never a silent `'tie'`.
  - `swapped=True` flips the result (`'a'`↔`'b'`, `'tie'` stays) — this is how the second call's
    verdict is brought back to the first call's frame.
- **A2** `pairwise_judge(judge, *, name, baseline_key="baseline")` → a neutral scorer
  (`scorer(name, llm=True)`, signature `(*, inputs, outputs, expectations, trace=None)`):
  - candidate A is `outputs`; candidate B is `expectations[baseline_key]` — missing expectations or
    a missing baseline key is an `EvalOpsError` naming the key (the row-error channel; strict,
    never a silent skip).
  - the judge is called **twice**: `judge(a=A, b=B, inputs=inputs)` then
    `judge(a=B, b=A, inputs=inputs)`; verdicts normalize via A1 (the second with `swapped=True`).
  - returns two metrics (the multi-metric list-of-`{"key", "score"}` form):
    `{key: name, score: 1.0 if 'a' else 0.0 if 'b' else 0.5}` (swap disagreement therefore lands
    on the tie value, visible) and `{key: f"{name}/swap_agreement", score: 1.0 if the normalized
    verdicts match else 0.0}`.
  - judge exceptions propagate (row-error channel); the docstring states the cost: **2 judge calls
    per row** — account for it in `judge_meta().llm_judge_count`.
- **A3** `panel_judge(judges, *, name, method="mean")` → a neutral scorer (same signature):
  - `judges` is a sequence of judge **callables** (a `spec_judge(...)` output or any callable
    accepting the row kwargs and returning a floatable value). Fewer than 2 members, an empty
    sequence, or a `method` outside `{"mean", "majority", "agreement"}` is an `EvalOpsError` at
    **construction** (exit 2 — a panel of one is not a panel; an unknown method is a usage error).
  - per row: each member is called with the row kwargs it declares (filter by
    `inspect.signature`, the same approach `scoring.call_scorer` uses; `trace` only if requested);
    its return must be a **single floatable vote** via `to_float` — a member returning a list or a
    dict (multi-metric) raises `EvalOpsError` naming the member index (a panel member must emit
    one vote; never silently dropped). `None` votes are excluded. No votes at all → the scorer
    returns `None` (honest absence; no companion metric — there is nothing to disagree about).
  - the score by method: `mean` → the vote mean; `majority` → strict majority of exact vote values
    (more than half), no strict majority → `0.5` (a visible tie, never a silent pick);
    `agreement` → the common value when every vote is exactly equal, else `None`.
  - **always** (whenever there is at least one vote) emits the companion
    `{key: f"{name}/panel_disagreement", score: 1 - (largest exact-value group share)}` — `0.0` is
    unanimous. This is the map's "panel disagreement reported"; it is emitted for every method,
    including when the score itself is `None` (agreement method, no consensus).
  - the docstring states the cost: **one judge call per member per row**.
- **A4** Both wrappers are pure: no vendor import, no I/O, no config read; they compose whatever
  the caller passes (native adapter scorers, the LiteLLM fallback scorer, plain callables).

### B. Exports (`src/evalops/scorers/__init__.py`)

Additive only: `normalize_pairwise`, `pairwise_judge`, `panel_judge` re-exported beside
`span_judge`/`trajectory_judge`/`spec_judge`. No other module changes.

### C. Tests (`tests/unit/test_pairwise_panel.py`, new)

- **C1** Normalizer matrix: `'A'`, `'a '`, `'TIE'`, `1`, `-1`, `0`, `0.0`, `{'winner': 'B'}`;
  swapped flips a/b and keeps tie; bools, `'x'`, `2`, `0.5`, `None`, `[]` rejected with the
  wrapper named.
- **C2** Pairwise: both orders are called (assert the call log has exactly two entries with
  swapped arguments); a consistent judge → decision metric + `swap_agreement` 1.0; a
  position-biased judge (always `'a'`) → decision `1.0` (first candidate wins) with
  `swap_agreement` 0.0; a consistent tie; missing baseline → error naming `baseline_key`; a
  raising judge propagates; the pair lands in `row.scores` through a real `call_scorer`.
- **C3** Panel: mean `mean([2, 4]) == 3.0`; majority `[1, 1, 0] → 1.0` and `[1, 0] → 0.5`;
  agreement `[0.9, 0.9, 0.9] → 0.9` and `[0.9, 0.8] → None`; `panel_disagreement`: unanimous
  `0.0`, `[1, 1, 0] → 1/3`; `None` votes excluded; all-`None` → score `None` with no companion;
  a multi-metric member → error naming the index; construction errors (< 2 members, empty, unknown
  method) are exit-2 `EvalOpsError`s; kwarg filtering (a member declaring only `inputs`/`outputs`
  is called without `expectations`/`trace`).
- **C4** End-to-end: a temp project (`tests/unit/_projects.py`) whose app hook returns a
  `pairwise_judge` (fake deterministic judge callable) → both metrics appear in the persisted
  rows/metrics of a real `evaluate`; likewise one `panel_judge` over two fake members. No SDK, no
  fake modules needed.
- **C5** Mutations (on /tmp copies, report each red test): (i) skip the swapped call (single
  order) → the C2 two-call/swap test red; (ii) drop the `swap_agreement`/`panel_disagreement`
  companion metric → the reporting tests red; (iii) `majority` without a strict majority returns
  `0.0` instead of `0.5` → red; (iv) `normalize_pairwise` maps unknown values to `'tie'` → the C1
  matrix red.

## Falsifiable done-when

- **H1** `normalize_pairwise` implements A1 exactly (closed set, swapped flip, strict rejections);
  the C1 matrix passes.
- **H2** `pairwise_judge` calls the judge in both orders per row, normalizes the second verdict
  back into the first frame, and emits exactly two metrics (`name`, `name/swap_agreement`) in the
  multi-metric shape `call_scorer` consumes; missing baseline is a named error; judge exceptions
  propagate.
- **H3** `panel_judge` implements the three methods exactly per A3 (strict majority → visible
  `0.5` tie; agreement without consensus → `None`), always reports `panel_disagreement` when votes
  exist, excludes `None` votes, rejects multi-metric members by index, and validates members/method
  at construction with exit 2.
- **H4** End-to-end: both wrappers' metrics appear in a real `evaluate` on a temp project through
  the app hook (C4), and no existing metric/golden changes.
- **H5** Exports are additive; `span_judge`/`trajectory_judge`/`spec_judge` and every existing
  scorer test are unmodified and green; no config/schema/capability/golden change of any kind;
  `make schema`, `make capabilities`, `make ci-adapters` zero drift.
- **H6** Gates: `make check` green (433 → new total), standalone runner green, `make contract`
  skips, core stays vendor-free (the guard test untouched and green — `panel.py` adds no import
  beyond stdlib + `evalops`), and `git status --short` lists only: `src/evalops/scorers/panel.py`
  (new), `src/evalops/scorers/__init__.py` (the exports), `tests/unit/test_pairwise_panel.py`
  (new), plus the untracked `docs/packets/WP3b.4.md` (the orchestrator's, committed in the docs
  commit). Nothing else.
- **H7** The C5 mutations each turn their named test red (reproduced on /tmp copies).

## Allowed paths

`src/evalops/scorers/panel.py` (new), `src/evalops/scorers/__init__.py` (the exports only),
`tests/unit/test_pairwise_panel.py` (new).

## Forbidden

`src/evalops/run.py`, `gate.py`, `calibrate.py`, `pipeline.py`, `cost.py`, `usage.py`,
`plugins/**`, `traces/**`, `scorers/judges.py`, `scorers/native.py`, `scorers/litellm_judge.py`,
`scoring.py` (the protocol already supports everything needed — if you believe a helper must
change, stop and report a blocker instead), core `judges.py`, `config/**` (no schema change),
`pyproject.toml`, `Makefile`, `ci/**`, `evalops.yaml`, `datasets/**`, `docs/**` (no generated
file changes are expected — zero schema/capabilities diff), any envelope golden,
deleting/skipping/loosening tests, `git commit`/`push`/branch, installing anything into `.venv`.

## Commands before reporting (paste tails)

```bash
.venv/bin/python -m ruff format src tests
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check src tests
.venv/bin/python -m mypy
.venv/bin/python -m pytest -q
.venv/bin/python tests/run_without_pytest.py
make check
make contract
make schema && git status --short docs/
make capabilities && git status --short docs/
make ci-adapters && git status --short ci/
git status --short
```

## Report format (exactly this)

```text
## Completed
<file by file, one line each>

## Wrapper surface
<normalizer rules; pairwise call/emit contract; panel methods + disagreement metric; cost notes>

## Verification
<exact commands + tail output; PASS/FAIL each>

## Claims
<8-12 falsifiable statements, incl. the H7 mutation evidence>

## Deviations
<none | list with reason>

## Blockers
<none | what is needed>
```

---

## Review prompt (paste to `codex-luna-reviewer`, model `openai-codex/gpt-6-luna`, thinking xhigh)

WP3b.4 (pairwise + panel wrappers) review, uncommitted tree at
`/home/howt/work/evalops_universal/evalops`. The packet is `docs/packets/WP3b.4.md` — read it,
then the new `src/evalops/scorers/panel.py`, the additive exports, and
`tests/unit/test_pairwise_panel.py`.

- **H1 normalizer.** Drive the matrix yourself: case/whitespace variants, `1/-1/0`, the
  `{'winner': ...}` mapping, the swapped flip, and the rejections (bool, `'x'`, `2`, `0.5`, None,
  list). A silent `'tie'` for unknown input is a defect — try to sneak one past.
- **H2 swap discipline.** Exactly two judge calls per row with genuinely swapped arguments (log
  them); the second verdict is normalized with `swapped=True`; a position-biased judge yields
  decision `1.0` + `swap_agreement` `0.0` (disagreement visible, never smoothed); missing baseline
  names the key; the multi-metric shape is what `call_scorer` consumes (verify through a real
  call, not by re-implementing it).
- **H3 panel semantics.** Verify each method against hand-computed cases (mean, strict majority
  with and without consensus, exact agreement), the `0.5` no-majority tie, the `None` no-consensus
  score with `panel_disagreement` still emitted, `None`-vote exclusion, the all-`None` case (score
  `None`, no companion), the multi-metric-member rejection naming the index, and the construction
  errors (<2 members, empty, unknown method → exit 2). Check the kwarg filtering actually inspects
  signatures (a member without `trace` must not receive it).
- **H4 end-to-end.** Both wrappers through a real `evaluate` on a temp project; metrics land in
  rows/metrics; nothing else in the envelope changes.
- **H5/H6 blast radius.** `git diff --stat` limited to the three allowed paths (plus the
  orchestrator's untracked packet); no config/schema/capabilities/golden change; exports additive
  only; `make check` + standalone runner green; the guard test untouched and green.
- **H7 mutations.** Reproduce all four on /tmp copies; name the red test each.
- **Scope honesty.** No config surface, no run.py/gate wiring, no 3b.5 features (resolved-model
  recording, pin enforcement). The wrappers must be reachable only from app hooks, like
  `span_judge`.

End with the gate decision in exactly this shape:

## Verdict
PASS | BLOCK
## Defects
<numbered, file:line, why it matters, concrete fix; empty if PASS>
## Residual risk
<what remains unproven even on PASS>
