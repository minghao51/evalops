"""The unverified-surface ("beta") registry (WP3.2a).

Every vendor API detail evalops cannot assert offline is either covered by a test (and then carries
no marker) or named here and referenced from the code as ``VERIFY-BETA(<id>)``. This module is the
single source of truth: ``tests/unit/test_verify_markers.py`` scans ``plugins/`` and ``scorers/``
and fails on a bare ``VERIFY``, on a ``VERIFY-BETA(<id>)`` whose id is not registered, and on a
registry entry no marker references. ``evalops doctor`` prints the whole registry, so a beta is
never a silent gap: every entry carries a user-visible ``hint`` -- the action that would resolve it.

The trace-dialect markers in ``evalops/traces/**`` are deliberately out of scope (phase 5).

Entries are sorted by ``id`` (the module's contract; :data:`BETA_SURFACES` is a tuple, so the order
is stable and reproducible in ``doctor``).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TextIO


@dataclass(frozen=True)
class BetaSurface:
    """One unverified vendor surface: what is unverified, why, and what the user can do about it."""

    id: str
    """The marker key: the code says ``VERIFY-BETA(<id>)``."""
    area: str
    """The vendor or role the surface belongs to (``langsmith``, ``litellm``, ``native-judges``)."""
    summary: str
    """One line: which vendor behaviour is unverified."""
    reason: str
    """Why this phase cannot assert it (no credentials, server-side execution, unobserved vendor)."""
    hint: str
    """The user-visible action that would resolve the surface -- see the honesty rule below."""


# A hint is only ever as strong as the check it names, so a hint says exactly one of two things:
#
# - a check that exists **today**, named down to the module and test function (and whether that
#   check is a fake-SDK unit test or a docker-gated contract slice); or
# - plainly that no automated check exists yet, plus the prerequisite and the WP that will add one.
#
# ``tests/unit/test_verify_markers.py`` enforces this: every path a hint names must resolve, a
# ``module.py::test_name`` must hold that test, ``make contract`` may only be named together with a
# ``tests/contract/`` module that exists, and the surfaces nothing exercises must say so.


BETA_SURFACES: tuple[BetaSurface, ...] = tuple(
    sorted(
        (
            BetaSurface(
                id="langfuse-run-experiment",
                area="langfuse",
                summary=(
                    "the top-level `lf.run_experiment(data=...)` runner accepts the same keyword set as a dataset run"
                ),
                reason=(
                    "the offline suite cannot reach a Langfuse project; only the docker-gated slice "
                    "in tests/contract/test_langfuse_plugin.py exercises the top-level runner, and it "
                    "is skipped without docker"
                ),
                hint=(
                    "run `make contract` with docker — "
                    "tests/contract/test_langfuse_plugin.py::"
                    "test_evaluator_local_data_run_experiment_is_visible_server_side exercises "
                    "`run_experiment(data=...)` against tests/contract/docker-compose.langfuse.yml; "
                    "it is skipped without docker, so an offline run verifies nothing"
                ),
            ),
            BetaSurface(
                id="langfuse-usage-read",
                area="langfuse",
                summary=(
                    "`usage()` reads trace cost/usage through `api.trace.get(trace_id).observations[].usage_details`"
                ),
                reason=(
                    "Langfuse SDK 5 removed `api.trace.get`; the docker-gated slice asserts the "
                    "degradation (`usage == {}`) instead of the read, and its replacement is "
                    "WP3.2c's"
                ),
                hint=(
                    "no automated check can confirm the read yet: "
                    "tests/contract/test_langfuse_plugin.py::"
                    "test_tracer_evaluate_trace_ids_are_readable_and_usage_is_exercised runs "
                    "`usage()` but asserts `{}` on the current SDK 5 / self-host v4 pairing "
                    "(api.trace.get is gone); needs a live Langfuse project plus the "
                    "`api.observations.get_many` rewrite (WP3.2c, not yet written) — until then "
                    "treat this surface as unverified"
                ),
            ),
            BetaSurface(
                id="langsmith-pull-prompt-commit",
                area="langsmith",
                summary="a pushed prompt's commit hash is read from `pull_prompt_commit(name).commit_hash`",
                reason=(
                    "no LANGSMITH_API_KEY in this phase and no langsmith contract module "
                    "(WP3.2d); the attribute name is unobserved"
                ),
                hint=(
                    "no automated check yet — needs LANGSMITH_API_KEY and the langsmith contract "
                    "slice (WP3.2d, not yet written); until then treat this surface as unverified"
                ),
            ),
            BetaSurface(
                id="langsmith-run-results",
                area="langsmith",
                summary="`client.evaluate` returns rows keyed `run` / `example` / `evaluation_results`",
                reason=(
                    "no LANGSMITH_API_KEY in this phase and no langsmith contract module "
                    "(WP3.2d); the row dict shape is unobserved"
                ),
                hint=(
                    "no automated check yet — needs LANGSMITH_API_KEY and a langsmith contract "
                    "slice asserting the `client.evaluate` row keys (WP3.2d, not yet written); "
                    "until then treat this surface as unverified"
                ),
            ),
            BetaSurface(
                id="litellm-completion-shape",
                area="litellm",
                summary=(
                    "`litellm.completion` returns an OpenAI-shaped response whose "
                    "`choices[0].message.content` and `usage` carry the text and the tokens"
                ),
                reason=(
                    "needs a provider key and a network call, which the offline suite forbids; "
                    "offline only a fake completion is asserted (tests/unit/test_litellm_judge.py)"
                ),
                hint=(
                    "no live check exists — "
                    "tests/unit/test_litellm_judge.py::test_the_full_spec_reaches_the_litellm_call "
                    "pins evalops's side against a fake `litellm.completion`; a real check needs "
                    "the litellm extra, a provider key and a network call, which the offline suite "
                    "forbids and no WP schedules; until then treat this surface as unverified"
                ),
            ),
            BetaSurface(
                id="mlflow-dataset-api",
                area="mlflow",
                summary="`datasets.create_dataset`'s accepted kwargs and `get_dataset(...).to_df()`",
                reason=(
                    "the signature and `to_df` vary by MLflow version/backend; only the "
                    "docker-gated slice in tests/contract/test_mlflow_plugin.py exercises them, and "
                    "it runs `mlflow:latest`, so no MLflow version is pinned"
                ),
                hint=(
                    "run `make contract` with docker — "
                    "tests/contract/test_mlflow_plugin.py::test_datasets_write_exists_and_read_roundtrip "
                    "exercises `create_dataset`/`get_dataset(...).to_df()` against "
                    "tests/contract/docker-compose.yml (`mlflow:latest`); offline runs verify nothing"
                ),
            ),
            BetaSurface(
                id="mlflow-result-df",
                area="mlflow",
                summary="which `result_df` column carries a per-row predict failure, and the score column names",
                reason=(
                    "the failure column is MLflow-version-dependent and no test asserts `result_df` "
                    "yet; WP3.2b pins it against the live stack"
                ),
                hint=(
                    "no automated check yet — tests/contract/test_mlflow_plugin.py does not assert "
                    "`result_df` columns; the failure column (`state`) and the score names need a "
                    "live MLflow run, which WP3.2b adds against "
                    "tests/contract/docker-compose.yml"
                ),
            ),
            BetaSurface(
                id="native-langfuse-managed-evaluator",
                area="native-judges",
                summary=(
                    "`api.evaluators.upsert`/`trigger` and `api.scores.list` configure and read back "
                    "a managed evaluator"
                ),
                reason=(
                    "the evaluator runs server-side and needs an LLM key plus a live Langfuse "
                    "project; offline only a fake client is asserted (tests/unit/test_native_judges.py)"
                ),
                hint=(
                    "no live check exists — "
                    "tests/unit/test_native_judges.py::"
                    "test_langfuse_adapter_configures_a_pinned_evaluator_and_reads_the_score_back "
                    "pins evalops's side against a fake client; a real check needs an LLM key and "
                    "a live project (tests/contract/docker-compose.langfuse.yml) and no WP "
                    "schedules it yet"
                ),
            ),
            BetaSurface(
                id="native-mlflow-make-judge",
                area="native-judges",
                summary="`mlflow.genai.make_judge`'s call shape and the `Feedback` value it returns",
                reason=(
                    "needs mlflow >= 3.2 and a judge model key; `make_judge` executes server-side, "
                    "so offline only a fake is asserted (tests/unit/test_native_judges.py)"
                ),
                hint=(
                    "no live check exists — "
                    "tests/unit/test_native_judges.py::"
                    "test_mlflow_adapter_sends_the_whole_spec_and_normalizes_feedback pins "
                    "evalops's side against a fake `make_judge`; a real check needs mlflow >= 3.2, "
                    "a judge model key and tests/contract/docker-compose.yml, and no WP schedules "
                    "it yet"
                ),
            ),
            BetaSurface(
                id="native-openevals",
                area="native-judges",
                summary=(
                    "`openevals.llm.create_llm_as_judge` accepts `prompt`/`feedback_key` and returns "
                    "a `{key, score}` dict"
                ),
                reason=(
                    "no openevals install or judge model key in this phase; the call executes an "
                    "LLM, so offline only a fake is asserted (tests/unit/test_native_judges.py)"
                ),
                hint=(
                    "no live check exists — "
                    "tests/unit/test_native_judges.py::"
                    "test_openevals_adapter_sends_the_whole_spec_and_normalizes_the_score pins "
                    "evalops's side against a fake openevals; a real check needs an openevals "
                    "install, a judge model key and a network call, and no WP schedules it yet"
                ),
            ),
            BetaSurface(
                id="native-pydantic-evals",
                area="native-judges",
                summary=(
                    "`LLMJudge`'s `rubric`/`model`/`model_settings` kwargs and its sync-or-async `evaluate` result"
                ),
                reason=(
                    "pydantic-evals runs an LLM judge and is not installed in the offline suite "
                    "(an absent package is exit 6); offline only a fake is asserted "
                    "(tests/unit/test_native_judges.py)"
                ),
                hint=(
                    "no live check exists — "
                    "tests/unit/test_native_judges.py::"
                    "test_pydantic_evals_adapter_sends_the_whole_spec_and_normalizes_the_metric "
                    "pins evalops's side against a fake `LLMJudge`; a real check needs "
                    "pydantic-evals installed with a judge model key, and no WP schedules it yet"
                ),
            ),
            BetaSurface(
                id="settings-langfuse",
                area="langfuse",
                summary="`client.api.projects.get().default_evaluator_model` is the project's default judge model",
                reason=(
                    "needs a live Langfuse project and credentials; offline only a fake client is "
                    "asserted (tests/unit/test_judge_settings.py)"
                ),
                hint=(
                    "no automated check against the real client yet — "
                    "tests/unit/test_judge_settings.py::"
                    "test_the_langfuse_project_default_reader_compares_the_same_way pins a fake "
                    "client; a real check needs LANGFUSE_* credentials, `evaluator: langfuse` and "
                    "`evalops --json judges --check-settings` against the project (the command "
                    "exists; no WP schedules a live check yet)"
                ),
            ),
            BetaSurface(
                id="settings-pydantic-evals",
                area="native-judges",
                summary="`LLMJudge.model_fields['model'].default` is the SDK's default judge model",
                reason=(
                    "pydantic-evals is not installed in the offline suite (an absent package is "
                    "exit 6); offline only a fake package is asserted "
                    "(tests/unit/test_judge_settings.py)"
                ),
                hint=(
                    "no automated check against the real SDK yet — "
                    "tests/unit/test_judge_settings.py::"
                    "test_an_agreeing_pydantic_evals_default_is_ok_and_exit_zero pins a fake "
                    "package; a real check needs `pip install pydantic-evals`, `evaluator: core` "
                    "and `evalops --json judges --check-settings` (the command exists; no WP "
                    "schedules a live check yet)"
                ),
            ),
        ),
        key=lambda surface: surface.id,
    )
)


def payload() -> list[dict[str, str]]:
    """The registry as a sorted, JSON-safe list -- what ``evalops doctor`` puts in ``data.beta``."""
    return [
        {
            "id": surface.id,
            "area": surface.area,
            "summary": surface.summary,
            "reason": surface.reason,
            "hint": surface.hint,
        }
        for surface in BETA_SURFACES
    ]


def write_human(stream: TextIO | None = None) -> None:
    """Human-mode rendering: one line per surface, ending with the action that would resolve it."""
    out = stream if stream is not None else sys.stdout
    out.write("beta (unverified surfaces):\n")
    for surface in BETA_SURFACES:
        out.write(f"{surface.id}  [{surface.area}]  {surface.summary} — {surface.hint}\n")
    out.flush()
