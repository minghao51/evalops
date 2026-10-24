"""WP1.4 (E+F+G): the ``traces``, ``promote``/``rollback`` and ``plan`` command paths.

Falsifies, in order: (E1) `traces` takes its group from `.evalops/out/last_eval.json` and needs one
(exit 2 when neither exists), (E2) `--row` filters a multi-row group, an unknown row is an empty
success and an unknown group is exit 2, (E3) the rendered tree is present for a traced row and the
output carries no unredacted planted secret; (F1) `promote` without evidence runs is exit 2,
(F2) unpromotable evidence is **exit 4**, (F3) evidence that never passed the gate is exit 4,
(F4) evidence built from different prompt versions is exit 4 naming the prompt, (F5) `candidate`/
`staging` move aliases without touching the baseline and `production` pins it, (F6) `rollback` refuses
an unknown prompt and moves the alias for a known one (`--version` stays the subcommand flag);
(G1) `plan` reports budget problems without failing and `eval --dry-run` says "dry run", (G2) an
unpriced config warns, (G3) an unsynced dataset is exit 2 naming `evalops sync datasets`.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import json
import pathlib
import shutil
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import prompts as prompts_mod  # noqa: E402

PLANTED = "sk-live-PLANTED-TRACE"

TRACE_CONFIG = (
    proj.BASE_CONFIG.replace("fake_app:", "trace_app:")
    + """
redact:
  mode: strict
  patterns:
    - {name: apikey, regex: 'sk-live-[A-Za-z0-9-]+'}
"""
)
#: Three rows, so `--row` has something to filter (the shipped smoke dataset has one).
TRACE_DATASET = "".join(json.dumps({"inputs": {"question": q}}) + "\n" for q in ("q one", "q two", "q three"))

TIGHT_RUN_CONFIG = proj.BASE_CONFIG.replace(
    "budgets: {per_run_usd: 5, per_month_usd: 50}", "budgets: {per_run_usd: 0.000001, per_month_usd: 50}"
)
TIGHT_MONTH_CONFIG = proj.BASE_CONFIG.replace(
    "budgets: {per_run_usd: 5, per_month_usd: 50}", "budgets: {per_month_usd: 0.000001}"
)
UNPRICED_CONFIG = proj.BASE_CONFIG.replace(
    """cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}""",
    "cost: {budgets: {per_run_usd: 5}}",
)


# ---------------------------------------------------------------------------
# (E) evalops traces
# ---------------------------------------------------------------------------
def _traced_project() -> tuple[pathlib.Path, str]:
    """A project whose one dev eval wrote a 3-row group with a redacted, rendered trace."""
    root = proj.project(
        TRACE_CONFIG, files={"trace_app.py": proj.fixture_text("trace_app.py")}, dataset_text=TRACE_DATASET
    )
    code, synced = proj.run_cli(root, "sync", "all")
    assert code == 0, synced
    proj.commit(root, "sync")
    code, out = proj.run_cli(root, "eval", "--dataset", "smoke", "--stage", "dev")
    assert code == 0, out
    return root, out["data"]["group"]


def test_traces_defaults_to_the_last_eval_group_and_requires_one():
    root, group = _traced_project()
    try:
        last = proj.read_json(proj.last_eval_path(root))
        assert last["group"] == group and len(last["run_ids"]) == 1
        code, envelope = proj.run_cli(root, "traces")
        assert code == 0 and envelope["code"] == 0, envelope
        rows = envelope["data"]["rows"]
        assert len(rows) == 3
        assert envelope["data"]["summary"] == f"{len(rows)} row(s) from run {last['run_ids'][0]}"

        # neither --group nor the file: a usage error (exit 2), not a runtime crash
        proj.last_eval_path(root).unlink()
        code, envelope = proj.run_cli(root, "traces")
        assert code == 2, envelope
        assert envelope["code"] == 2 and envelope["ok"] is False and envelope["command"] == "traces"
        assert "last_eval.json" in envelope["errors"][0]["message"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_traces_row_filter_unknown_row_and_unknown_group():
    root, group = _traced_project()
    try:
        code, filtered = proj.run_cli(root, "traces", "--group", group, "--row", "00000")
        assert code == 0 and [row["row_id"] for row in filtered["data"]["rows"]] == ["00000"], filtered
        assert filtered["data"]["summary"].startswith("1 row(s)")

        code, unknown = proj.run_cli(root, "traces", "--group", group, "--row", "99999")
        assert code == 0, unknown
        assert unknown["data"]["rows"] == [] and unknown["data"]["summary"].startswith("0 row(s)")

        code, missing = proj.run_cli(root, "traces", "--group", "nope")
        assert code == 2 and missing["code"] == 2, missing
        assert missing["errors"][0]["message"] == "no runs for group nope"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_traces_renders_the_tree_without_leaking_the_planted_secret():
    root, group = _traced_project()
    try:
        code, envelope = proj.run_cli(root, "traces", "--group", group)
        assert code == 0, envelope
        trees = [row["tree"] for row in envelope["data"]["rows"] if row["tree"]]
        assert len(trees) == 3, envelope["data"]["rows"]
        assert "[agent] supervisor" in trees[0] and "[tool] lookup_order" in trees[0]
        assert "[llm] fake-gpt" in trees[0]

        output = json.dumps(envelope)
        assert PLANTED not in output, "the planted secret reached the `traces` output"
        assert "[REDACTED:apikey]" in output  # the redaction rule ran before the value was stored
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (F) promote / rollback
# ---------------------------------------------------------------------------
def _promote_project():
    """A temp project with git prompts and a fresh in-memory ledger, already `sync all`-ed."""
    root, module = proj.ledger_project()
    code, envelope = proj.run_cli_in_process(root, "sync", "all")
    assert code == 0, envelope
    lock = proj.read_json(root / ".evalops" / "locks" / "local" / "prompts.lock.json")
    refs = {f"prompt.{name}": prompts_mod.ref(entry) for name, entry in lock.items() if not name.startswith("_")}
    assert refs == {"prompt.support_system": "git:support_system@1"}, lock
    return root, module, refs


def _evidence(refs: dict, **overrides) -> dict:
    """The kwargs of one promotable, gate-passed evidence run (override tags/params/add `start_ms`).

    A `tags` override replaces the whole tag dict (so a test can drop `evalops.gate` on purpose).
    """
    tags = overrides.pop("tags", {"evalops.group": "g1", "evalops.promotable": "true", "evalops.gate": "passed"})
    params = {"app_version": "wp14-aaaaaaaa-12345678", "dataset": "smoke", "dataset_fingerprint": "fp-a", **refs}
    params.update(overrides.pop("params", {}))
    return {"tags": tags, "params": params, "metrics": {"noop/mean": 1.0}, **overrides}


def _alias_path(root: pathlib.Path) -> pathlib.Path:
    return root / ".evalops" / "store" / "prompts" / "support_system" / "aliases.json"


def test_promote_without_evidence_runs_is_exit_two():
    root, _module, _refs = _promote_project()
    try:
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "candidate", "--evidence-group", "g1")
        assert code == 2 and envelope["code"] == 2, envelope
        assert envelope["command"] == "promote" and envelope["ok"] is False
        assert envelope["errors"][0]["message"] == "no runs for evidence group g1"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_promote_refuses_unpromotable_evidence_with_exit_four():
    root, module, refs = _promote_project()
    try:
        proj.add_run(module, "e1", **_evidence(refs, tags={"evalops.group": "g1", "evalops.promotable": "false"}))
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g1")
        assert code == 4 and envelope["code"] == 4, envelope
        assert envelope["command"] == "promote" and envelope["ok"] is False
        assert "not promotable" in envelope["errors"][0]["message"]
        # nothing moved: the alias file was never written
        assert not _alias_path(root).exists()
        assert "evalops.baseline" not in module.MemoryLedger.runs["e1"]["tags"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_promote_requires_evidence_that_passed_the_gate():
    root, module, refs = _promote_project()
    try:
        tags = {"evalops.group": "g1", "evalops.promotable": "true"}  # no evalops.gate tag at all
        proj.add_run(module, "e1", **_evidence(refs, tags=tags))
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g1")
        assert code == 4 and envelope["code"] == 4, envelope
        assert envelope["errors"][0]["message"] == "evidence has not passed `evalops gate`"
        assert not _alias_path(root).exists()

        # a recorded failure is refused the same way
        failed = {"evalops.group": "g1", "evalops.promotable": "true", "evalops.gate": "failed"}
        proj.add_run(module, "e2", **_evidence(refs, tags=failed, start_ms=2))
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g2")
        assert code == 2, envelope  # e2 is not in g2: no evidence runs there
        proj.add_run(module, "e3", **_evidence(refs, tags={**failed, "evalops.group": "g3"}, start_ms=3))
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g3")
        assert code == 4 and envelope["errors"][0]["message"] == "evidence has not passed `evalops gate`", envelope
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_promote_rejects_evidence_from_different_prompt_versions():
    root, module, refs = _promote_project()
    try:
        stale = {"prompt.support_system": "git:support_system@999"}
        proj.add_run(module, "e1", **_evidence(refs, params=stale))
        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g1")
        assert code == 4 and envelope["code"] == 4, envelope
        message = envelope["errors"][0]["message"]
        assert message == "evidence used different prompt versions: ['support_system']"
        assert "support_system" in message and not _alias_path(root).exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_promote_moves_aliases_and_only_production_pins_the_baseline():
    root, module, refs = _promote_project()
    try:
        proj.add_run(
            module,
            "b0",
            tags={"evalops.group": "g0", "evalops.baseline": "true"},
            params={"app_version": "wp14-bbbbbbbb-12345678", "dataset_fingerprint": "fp-old"},
            metrics={"noop/mean": 0.9},
            start_ms=1,
        )
        evidence = _evidence(refs, start_ms=2)
        proj.add_run(module, "e1", **evidence)

        for alias in ("candidate", "staging"):
            code, envelope = proj.run_cli_in_process(root, "promote", "--alias", alias, "--evidence-group", "g1")
            assert code == 0, envelope
            assert envelope["data"]["moves"] == [{"prompt": "support_system", "from": None, "to": "1"}]
            aliases = proj.read_json(_alias_path(root))
            assert aliases[alias] == "1"
            # a non-production alias never moves the baseline, and never pins the evidence run
            assert module.MemoryLedger.runs["b0"]["tags"]["evalops.baseline"] == "true"
            assert "evalops.baseline" not in module.MemoryLedger.runs["e1"]["tags"]
        assert proj.read_json(_alias_path(root)) == {"candidate": "1", "staging": "1"}

        code, envelope = proj.run_cli_in_process(root, "promote", "--alias", "production", "--evidence-group", "g1")
        assert code == 0, envelope
        assert envelope["data"]["moves"] == [{"prompt": "support_system", "from": None, "to": "1"}]
        assert proj.read_json(_alias_path(root)) == {"candidate": "1", "production": "1", "staging": "1"}
        # production moves the baseline and pins it to what produced the evidence
        assert module.MemoryLedger.runs["b0"]["tags"]["evalops.baseline"] == "false"
        pinned = module.MemoryLedger.runs["e1"]["tags"]
        assert pinned["evalops.baseline"] == "true"
        assert pinned["evalops.baseline.app_version"] == evidence["params"]["app_version"]
        assert pinned["evalops.baseline.dataset_fingerprint"] == "fp-a"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_rollback_refuses_an_unknown_prompt_and_moves_a_known_alias():
    root, module, _refs = _promote_project()
    try:
        proj.write_json(_alias_path(root), {"production": "9"})  # the alias is on a version we regret

        code, envelope = proj.run_cli_in_process(
            root, "rollback", "--alias", "production", "--prompt", "nope", "--version", "1"
        )
        assert code == 2 and envelope["code"] == 2, envelope
        assert envelope["errors"][0]["message"] == "unknown prompt 'nope'"
        assert proj.read_json(_alias_path(root)) == {"production": "9"}  # unchanged

        # `--version 1` is the rollback subcommand's own option, not the top-level version flag
        code, envelope = proj.run_cli_in_process(
            root, "rollback", "--alias", "production", "--prompt", "support_system", "--version", "1"
        )
        assert code == 0, envelope
        assert envelope["command"] == "rollback"
        assert envelope["data"]["summary"] == "support_system@production -> 1 (rollback via git)"
        assert proj.read_json(_alias_path(root)) == {"production": "1"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (G) plan
# ---------------------------------------------------------------------------
def test_plan_reports_budget_problems_without_failing():
    root = proj.project(TIGHT_RUN_CONFIG)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")

        code, envelope = proj.run_cli(root, "plan", "--dataset", "smoke", "--stage", "dev")
        assert code == 0 and envelope["code"] == 0, envelope
        plan = envelope["data"]
        assert plan["summary"].startswith("estimated $")
        assert plan["budget"]["override"] is True  # plan never enforces the budget
        assert plan["budget"]["problems"] == ["estimate exceeds per_run_usd"]
        assert plan["budget"]["estimate_usd"] > plan["budget"]["per_run_usd"]

        # `eval --dry-run` prints the "dry run" summary and keeps the same budget report
        code, envelope = proj.run_cli(
            root, "eval", "--dataset", "smoke", "--stage", "dev", "--dry-run", "--override-budget"
        )
        assert code == 0, envelope
        assert envelope["data"]["summary"].startswith("dry run: would spend ~$")
        assert envelope["data"]["budget"]["problems"] == ["estimate exceeds per_run_usd"]

        # the monthly cap is a second, independent problem (plan does not need a clean tree)
        (root / "evalops.yaml").write_text(TIGHT_MONTH_CONFIG)
        code, envelope = proj.run_cli(root, "plan", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        assert envelope["data"]["budget"]["problems"] == ["estimate would exceed per_month_usd"]
        assert envelope["data"]["estimate"]["total_usd"] > 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_plan_warns_when_the_config_is_unpriced():
    root = proj.project(UNPRICED_CONFIG)
    try:
        assert proj.run_cli(root, "sync", "all")[0] == 0
        proj.commit(root, "sync")
        code, envelope = proj.run_cli(root, "plan", "--dataset", "smoke", "--stage", "dev")
        assert code == 0, envelope
        warning = envelope["data"]["estimate"]["warning"]
        assert warning is not None and "prices_per_1m_tokens" in warning
        assert envelope["data"]["estimate"]["total_usd"] == 0.0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_plan_refuses_a_dataset_that_was_never_synced():
    root = proj.project()
    try:
        code, envelope = proj.run_cli(root, "plan", "--dataset", "smoke", "--stage", "dev")
        assert code == 2 and envelope["code"] == 2, envelope
        message = envelope["errors"][0]["message"]
        assert "dataset 'smoke' not synced" in message and "evalops sync datasets" in message
    finally:
        shutil.rmtree(root, ignore_errors=True)
