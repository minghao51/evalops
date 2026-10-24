"""WP3b.1: ``JudgeSpec`` in core -- a judge that cannot be represented unpinned.

Falsifies, in order: (H1) a fully specified ``judges.<name>`` round-trips into a ``JudgeSpec`` and a
CLI payload exposing exactly ``{name, model, prompt_version, scale, sampling, rubric_chars,
rubric_hash}``, where ``rubric_hash`` is the first 12 hex of sha256(rubric) and the rubric text never
appears in ``payload()``/the envelope/human output; (H2) every invalid pin -- absent, empty,
``latest``, ``gpt-latest``, ``auto``, ``default``, ``project-default``, ``default/*``,
``*/*/default``, ``*/-latest`` case-insensitively -- an absent/empty/``latest`` ``prompt_version``
and a blank rubric are exit **2** naming ``judges.<name>.<key>``; (H3) scale/sampling: unknown kind,
``min >= max``, ``binary`` with a non-0..1 range, a ``threshold`` outside the scale,
``temperature`` 2.5/-0.1, ``max_tokens`` 0 and a nested unknown key (``judges.x.scale.banana``) are
all exit 2 with the dotted path, and a 400-digit bound names the *two layers* distinctly -- the
config model reports ``judges.x.scale.max``, a direct ``JudgeSpec`` reports ``judges.x.scale``;
(H4) ``plan``/``eval``/``calibrate`` reject a declared-but-invalid spec **before** any ledger row
or cost estimate (and before the dirty-tree guard), while a *valid*
spec changes nothing about how scorers are built today (no adapter wiring); (H5) a rule with only
calibration thresholds (the shipped ``evalops.yaml`` shape) declares no spec: ``load() == {}``, it
is listed under ``unpinned``, and nothing else about it changes; (H6) ``evalops judges --json`` is
deterministic (a golden pins it) and the four new ``JudgeRule`` keys are the only schema change.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import dataclasses
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import config as config_mod  # noqa: E402
from evalops import judges as judges_mod  # noqa: E402
from evalops import run as run_mod  # noqa: E402
from evalops.config import models as models_mod  # noqa: E402
from evalops.config import schema as schema_mod  # noqa: E402
from evalops.scoring import scorer_name  # noqa: E402
from evalops.util import EXIT_USAGE, EvalOpsError  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The smallest config ``Config`` accepts (``project`` + an ``app`` block).
BASE = {"project": "judge-spec", "app": {"judge_meta": "os:getcwd"}}

RUBRIC = "Score 1.0 when the answer cites the retrieved context; 0.0 otherwise."
#: One fully specified judge: rubric, concrete pin, prompt version, ordinal scale, sampling.
FULL = {
    "model": "gpt-4o-2024-11-20",
    "rubric": RUBRIC,
    "prompt_version": "3",
    "scale": {"kind": "ordinal", "min": 0.0, "max": 5.0},
    "sampling": {"temperature": 0.2, "max_tokens": 256, "seed": 7},
}
#: The shipped ``evalops.yaml`` shape: calibration thresholds only, no spec.
THRESHOLDS_ONLY = {"min_agreement": 0.85, "min_kappa": 0.6}

#: The rubric the CLI test project declares (block-scalar text, so the trailing newline is real).
CLI_RUBRIC = "Score 1.0 when the answer cites the retrieved context; 0.0 otherwise.\n"
#: Appended to ``_projects.BASE_CONFIG`` (a complete local project) for the CLI/ledger tests.
VALID_JUDGES = f"""
judges:
  correctness:
    model: fake-judge-2024-11-20
    rubric: |
      {CLI_RUBRIC.strip()}
    prompt_version: "3"
    scale: {{kind: binary, min: 0.0, max: 1.0}}
    sampling: {{temperature: 0.0, max_tokens: 256, seed: 7}}
  helpful: {{min_agreement: 0.85, min_kappa: 0.6}}
"""
INVALID_JUDGES = """
judges:
  helpful: {rubric: score it, model: latest, prompt_version: "1"}
"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _cfg(rules: dict):
    """A ``Config`` over a throwaway root, with ``judges`` set to ``rules`` (no backend)."""
    return config_mod.Config({**BASE, "judges": rules}, proj.tmpdir("evalops-judge-spec-"))


def _error(fn) -> EvalOpsError:
    """Run ``fn``, expect an ``EvalOpsError``, and return it."""
    try:
        fn()
    except EvalOpsError as exc:
        return exc
    raise AssertionError("expected an EvalOpsError, the call returned normally")


def _dotted(exc: EvalOpsError) -> list[str]:
    """The dotted paths an error's details carry (the form a user edits)."""
    return [issue["path"] for issue in exc.details.get("errors", [])]


def _spec_error(rule: dict, name: str = "helpful") -> EvalOpsError:
    return _error(lambda: judges_mod.JudgeSpec.from_rule(name, rule))


def _load_error(rules: dict) -> EvalOpsError:
    """The load-time rejection: exactly what ``plan``/``eval``/``calibrate``/``judges`` run."""
    return _error(lambda: judges_mod.load(_cfg(rules)))


# ---------------------------------------------------------------------------
# (H1) a full spec round-trips; identity, never the rubric text
# ---------------------------------------------------------------------------
def test_a_full_spec_round_trips_its_identity():
    cfg = _cfg({"helpful": dict(FULL)})
    loaded = judges_mod.load(cfg)
    assert list(loaded) == ["helpful"]
    spec = loaded["helpful"]
    assert (spec.name, spec.model, spec.prompt_version) == ("helpful", "gpt-4o-2024-11-20", "3")
    assert spec.rubric == RUBRIC
    assert spec.scale == judges_mod.Scale(max=5.0, min=0.0, kind="ordinal")
    assert spec.sampling == judges_mod.Sampling(temperature=0.2, max_tokens=256, seed=7)


def test_payload_exposes_exactly_the_identity_fields_and_no_rubric_text():
    payload = judges_mod.payload(_cfg({"helpful": dict(FULL), "thresh": dict(THRESHOLDS_ONLY)}))
    assert set(payload) == {"judges", "unpinned"}
    assert payload["unpinned"] == ["thresh"]
    entry = payload["judges"][0]
    assert set(entry) == {"name", "model", "prompt_version", "scale", "sampling", "rubric_chars", "rubric_hash"}
    assert entry["name"] == "helpful" and entry["model"] == "gpt-4o-2024-11-20"
    assert entry["prompt_version"] == "3"
    assert entry["scale"] == {"max": 5.0, "min": 0.0, "kind": "ordinal"}
    assert entry["sampling"] == {"temperature": 0.2, "max_tokens": 256, "seed": 7}
    assert entry["rubric_chars"] == len(RUBRIC)
    assert entry["rubric_hash"] == hashlib.sha256(RUBRIC.encode()).hexdigest()[:12]
    assert RUBRIC not in json.dumps(payload)  # identity, never prose
    assert json.loads(json.dumps(payload)) == payload  # JSON-safe and stable


def test_a_spec_without_scale_or_sampling_gets_the_binary_reproducible_defaults():
    spec = judges_mod.JudgeSpec.from_rule(
        "helpful", {"model": "gpt-4o-2024-11-20", "rubric": RUBRIC, "prompt_version": "1"}
    )
    assert spec.scale == judges_mod.Scale(max=1.0, min=0.0, kind="binary")
    assert spec.sampling == judges_mod.Sampling(temperature=0.0, max_tokens=512, seed=None)


# ---------------------------------------------------------------------------
# (H2) the pin matrix: every alias/default/empty model is exit 2 with its dotted path
# ---------------------------------------------------------------------------
def test_every_unpinned_model_is_exit_two_naming_the_dotted_path():
    for model in (
        None,  # key absent (mypy: from_rule takes the raw rule)
        "",
        "   ",
        "latest",
        "LATEST",
        "  Latest  ",
        "gpt-latest",
        "openai/gpt-4o-latest",
        "auto",
        "default",
        "project-default",
        "default/gpt-4o",
        "openai/support/default",
    ):
        rule = {**FULL, "model": model}
        if model is None:
            rule.pop("model")
        exc = _spec_error(rule)
        assert exc.code == EXIT_USAGE == 2, (model, exc.code)
        assert "judges.helpful.model" in str(exc), (model, str(exc))
        assert _dotted(exc) == ["judges.helpful.model"], (model, _dotted(exc))

    # ... while a concrete pin (any other non-empty string) is accepted, case and vendor agnostic
    for model in ("gpt-4o-2024-11-20", "anthropic/claude-3-5-sonnet-20241022", "my-finetune:v7", "OpenAI/GPT-4o"):
        assert judges_mod.JudgeSpec.from_rule("helpful", {**FULL, "model": model}).model == model


def test_prompt_version_must_be_a_concrete_version():
    for version in (None, "", "   ", "latest", "LATEST"):
        rule = {**FULL, "prompt_version": version}
        if version is None:
            rule.pop("prompt_version")
        exc = _spec_error(rule)
        assert exc.code == EXIT_USAGE == 2, (version, exc.code)
        assert _dotted(exc) == ["judges.helpful.prompt_version"], (version, _dotted(exc))


def test_a_blank_rubric_is_exit_two():
    for rubric in (None, "", "   ", "\n\t "):
        rule = {**FULL, "rubric": rubric}
        if rubric is None:
            rule.pop("rubric")
        exc = _spec_error(rule)
        assert exc.code == EXIT_USAGE == 2, (rubric, exc.code)
        assert _dotted(exc) == ["judges.helpful.rubric"], (rubric, _dotted(exc))


def test_the_direct_constructor_still_validates_through_the_public_validate():
    """A dataclass cannot intercept ``__init__``, so ``validate()`` is the public rule set."""
    unpinned = judges_mod.JudgeSpec(name="helpful", rubric=RUBRIC, model="latest", prompt_version="1")
    exc = _error(unpinned.validate)
    assert exc.code == EXIT_USAGE == 2 and _dotted(exc) == ["judges.helpful.model"], _dotted(exc)
    # a valid direct construction validates and passes
    judges_mod.JudgeSpec(name="helpful", rubric=RUBRIC, model="gpt-4o", prompt_version="1").validate()


# ---------------------------------------------------------------------------
# (H3) scale and sampling
# ---------------------------------------------------------------------------
def test_scale_rules_are_exit_two_with_the_dotted_path():
    cases = (
        ({"kind": "banana"}, "judges.helpful.scale.kind"),
        ({"kind": "ordinal", "min": 1.0, "max": 1.0}, "judges.helpful.scale"),
        ({"kind": "ordinal", "min": 5.0, "max": 1.0}, "judges.helpful.scale"),
        ({"kind": "binary", "min": 0.0, "max": 5.0}, "judges.helpful.scale"),
        ({"kind": "binary", "min": 0.5, "max": 1.0}, "judges.helpful.scale"),
    )
    for scale, path in cases:
        exc = _spec_error({**FULL, "scale": scale})
        assert exc.code == EXIT_USAGE == 2, (scale, exc.code)
        assert _dotted(exc) == [path], (scale, _dotted(exc))

    # a threshold must lie inside the declared scale
    out = _spec_error({**FULL, "threshold": 7.5})
    assert _dotted(out) == ["judges.helpful.threshold"], _dotted(out)
    inside = judges_mod.JudgeSpec.from_rule("helpful", {**FULL, "threshold": 4.5})
    assert inside.scale.kind == "ordinal"
    # an ordinal scale with a matching threshold round-trips; a binary one accepts 0..1 only
    judges_mod.JudgeSpec.from_rule("helpful", {**FULL, "scale": {"kind": "binary"}, "threshold": 0.5})


def test_sampling_rules_are_exit_two_with_the_dotted_path():
    cases = (
        ({"temperature": 2.5}, "judges.helpful.sampling.temperature"),
        ({"temperature": -0.1}, "judges.helpful.sampling.temperature"),
        ({"max_tokens": 0}, "judges.helpful.sampling.max_tokens"),
        ({"max_tokens": -5}, "judges.helpful.sampling.max_tokens"),
    )
    for sampling, path in cases:
        exc = _spec_error({**FULL, "sampling": sampling})
        assert exc.code == EXIT_USAGE == 2, (sampling, exc.code)
        assert _dotted(exc) == [path], (sampling, _dotted(exc))

    # the bounds are inclusive and seed is an int or None (the direct path can carry a non-int)
    for temperature in (0.0, 2.0):
        spec = judges_mod.JudgeSpec.from_rule("helpful", {**FULL, "sampling": {"temperature": temperature}})
        assert spec.sampling.temperature == temperature
    for seed in (0, -3, 12345, None):
        assert judges_mod.JudgeSpec.from_rule("helpful", {**FULL, "sampling": {"seed": seed}}).sampling.seed == seed
    for bad in ("7", 1.5):
        spec = judges_mod.JudgeSpec(
            name="helpful",
            rubric=RUBRIC,
            model="gpt-4o",
            prompt_version="1",
            sampling=judges_mod.Sampling(seed=bad),  # type: ignore[arg-type]
        )
        assert _dotted(_error(spec.validate)) == ["judges.helpful.sampling.seed"], bad


def test_a_nested_unknown_key_is_exit_two_with_its_dotted_path():
    """CONTRACT §1.1: ``scale``/``sampling`` are closed key sets, so a typo names the exact path."""
    for key, path in (
        ({"scale": {"kind": "binary", "banana": 1}}, "judges.helpful.scale.banana"),
        ({"sampling": {"temperature": 0.0, "bananna": 1}}, "judges.helpful.sampling.bananna"),
        ({"rubrik": "typo"}, "judges.helpful.rubrik"),
    ):
        exc = _error(lambda key=key: _cfg({"helpful": {**FULL, **key}}))
        assert exc.code == EXIT_USAGE == 2, (key, exc.code)
        assert _dotted(exc) == [path], (key, _dotted(exc))


def test_specs_fail_fast_on_the_first_invalid_name_in_sorted_order():
    exc = _load_error({"zeta": {**FULL, "model": "latest"}, "alpha": {**FULL, "model": "auto"}})
    assert _dotted(exc) == ["judges.alpha.model"], _dotted(exc)


# ---------------------------------------------------------------------------
# (B) the config surface: the model, the discriminator and the schema cannot drift
# ---------------------------------------------------------------------------
def test_the_rule_key_sets_cannot_drift_from_the_config_model():
    assert set(judges_mod.RULE_KEYS) == set(models_mod.JudgeRule.model_fields)
    assert set(judges_mod.SPEC_KEYS) <= set(judges_mod.RULE_KEYS)
    assert not set(judges_mod.SPEC_KEYS) & set(judges_mod.THRESHOLD_KEYS), "a threshold never declares a spec"
    # the nested models are exactly the value objects' keys (an unknown key is exit 2, §1.1)
    assert set(models_mod.ScaleRule.model_fields) == {field.name for field in dataclasses.fields(judges_mod.Scale)}
    assert set(models_mod.SamplingRule.model_fields) == {
        field.name for field in dataclasses.fields(judges_mod.Sampling)
    }


def test_the_generated_schema_exposes_the_four_new_judge_keys():
    document = schema_mod.config_schema()
    judge_rule = document["$defs"]["JudgeRule"]
    assert set(judges_mod.SPEC_KEYS) <= set(judge_rule["properties"])
    assert "model" in judge_rule["properties"]
    for nested in ("ScaleRule", "SamplingRule"):
        definition = document["$defs"][nested]
        assert definition["additionalProperties"] is False, nested  # a nested typo is exit 2


# ---------------------------------------------------------------------------
# (H5) the discriminator: thresholds-only declares no spec; `model` alone does not either
# ---------------------------------------------------------------------------
def test_threshold_only_rules_declare_no_spec():
    rules = {
        "correctness": dict(THRESHOLDS_ONLY),
        "trajectory_quality": {"label": "traj_ok", "max_mae": 0.15, "threshold": 0.5},
        "pinned_but_unspecified": {"model": "gpt-4o-2024-11-20"},  # model alone is NOT a spec (A2)
        "bare": {},
    }
    cfg = _cfg(rules)
    assert judges_mod.specs(cfg) == {name: None for name in sorted(rules)}
    assert judges_mod.load(cfg) == {}
    payload = judges_mod.payload(cfg)
    assert payload == {"judges": [], "unpinned": ["correctness", "trajectory_quality"]}


def test_the_shipped_evalops_yaml_declares_no_spec():
    """The repo's own config is the backward-compatibility fixture: thresholds only."""
    cfg = config_mod.load(str(REPO / "evalops.yaml"))
    assert judges_mod.load(cfg) == {}
    payload = judges_mod.payload(cfg)
    assert payload["judges"] == [] and payload["unpinned"] == ["correctness"], payload


def test_adding_one_spec_key_declares_a_spec_and_then_validates():
    """Any one of the four SPEC_KEYS flips the rule from "thresholds" to "a judge to validate"."""
    for key, value in (
        ("rubric", RUBRIC),
        ("prompt_version", "1"),
        ("scale", {"kind": "continuous", "min": -1.0, "max": 1.0}),
        ("sampling", {"temperature": 0.0}),
    ):
        exc = _load_error({"helpful": {**THRESHOLDS_ONLY, key: value}})
        # the rule is now a *declared* spec, so it is validated (rubric/model/... first failure wins)
        paths = _dotted(exc)
        assert len(paths) == 1 and paths[0].startswith("judges.helpful."), (key, paths)


# ---------------------------------------------------------------------------
# (H4) load-time rejection is real, free, and changes nothing when the spec is valid
# ---------------------------------------------------------------------------
def _ledger_project(config: str):
    """A temp project whose ledger is a fresh in-memory fixture module -> (root, module)."""
    name = proj.ledger_name()
    root = proj.project(config)
    module = proj.install_ledger(root, name)
    proj.ledger_clear(module)
    return root, module


def test_plan_eval_and_calibrate_reject_an_invalid_spec_before_any_spend():
    root, ledger = _ledger_project(proj.ledger_config(proj.ledger_name()) + INVALID_JUDGES)
    # make the tree dirty: `eval`'s exit-4 guard must never be reached before the spec rejection
    (root / "scratch.txt").write_text("uncommitted\n")

    for args in (
        ("plan", "--dataset", "smoke", "--stage", "dev"),
        ("eval", "--dataset", "smoke", "--stage", "dev"),
        ("calibrate", "--dataset", "judge_calibration"),
    ):
        code, envelope = proj.run_cli_in_process(root, *args)
        assert code == 2, (args, code, envelope)
        assert envelope["errors"][0]["code"] == 2 and envelope["ok"] is False, (args, envelope)
        assert "judges.helpful.model" in envelope["errors"][0]["message"], (args, envelope)
        # nothing was spent and nothing was recorded
        assert envelope["data"] == {}, (args, envelope["data"])
        assert ledger.MemoryLedger.runs == {}, (args, ledger.MemoryLedger.runs)
        assert ledger.MemoryLedger.artifacts == {}, (args, ledger.MemoryLedger.artifacts)


def test_a_valid_spec_changes_nothing_about_how_scorers_are_built():
    """The specs are not threaded into scorers yet (that is WP3b.2): same scorers, same order."""
    root = proj.project()
    try:
        without = config_mod.load(str(root / "evalops.yaml"))
        (root / "evalops.yaml").write_text(proj.BASE_CONFIG + VALID_JUDGES)
        with_spec = config_mod.load(str(root / "evalops.yaml"))
        assert list(judges_mod.load(with_spec)) == ["correctness"]
        names = lambda cfg: [scorer_name(s) for s in run_mod._build_scorers(cfg)]  # noqa: E731
        assert names(with_spec) == names(without), (names(with_spec), names(without))
        # ... and the judge_meta hook (today's only judge surface) is untouched too
        assert with_spec.hook("judge_meta")() == without.hook("judge_meta")()
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (D1/H6) the `evalops judges` command
# ---------------------------------------------------------------------------
def test_the_judges_command_is_deterministic_and_lists_both_branches():
    root = proj.project(proj.BASE_CONFIG + VALID_JUDGES)
    try:
        code, first = proj.run_cli(root, "judges")
        assert code == 0, first
        assert first["command"] == "judges" and first["ok"] is True, first
        data = first["data"]
        assert [entry["name"] for entry in data["judges"]] == ["correctness"], data
        assert data["unpinned"] == ["helpful"], data
        entry = data["judges"][0]
        assert set(entry) == {"name", "model", "prompt_version", "scale", "sampling", "rubric_chars", "rubric_hash"}
        assert entry["model"] == "fake-judge-2024-11-20" and entry["prompt_version"] == "3"
        assert entry["rubric_hash"] == hashlib.sha256(CLI_RUBRIC.encode()).hexdigest()[:12], entry
        assert "Score 1.0" not in json.dumps(data)  # the rubric text never reaches the payload

        repeated = proj.run_cli(root, "judges")
        assert repeated == (code, first), "the judges payload is not deterministic"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_judges_command_warns_per_unpinned_rule_in_human_mode():
    root = proj.project(proj.BASE_CONFIG + VALID_JUDGES)
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "judges"],
            cwd=str(root),
            env=proj.env(),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        out = proc.stdout
        # one line per judge: name  model  prompt_version  scale  rubric_hash
        assert "correctness  fake-judge-2024-11-20  3  binary(0.0..1.0)  " in out, out
        assert "warning: judges.helpful" in out, out
        assert "Score 1.0" not in out, out  # identity again, never prose
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_judges_command_rejects_an_invalid_spec_like_the_loaders_do():
    root = proj.project(proj.BASE_CONFIG + INVALID_JUDGES)
    try:
        code, envelope = proj.run_cli(root, "judges")
        assert code == 2 and envelope["code"] == 2, envelope
        assert "judges.helpful.model" in envelope["errors"][0]["message"], envelope
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# (H3 regression) non-finite numbers: a judge envelope is always valid JSON
# ---------------------------------------------------------------------------
#: ``Infinity``/``NaN`` are not JSON tokens, so the CLI envelope can never contain them: the spec
#: validation rejects the value (exit 2) before it is rendered. ``.inf``/``.nan`` are YAML floats.
NON_FINITE_CLI_CASES = (
    ({"scale": "{kind: continuous, min: 0.0, max: .inf}"}, "judges.x.scale"),
    ({"scale": "{kind: continuous, min: -.inf, max: 1.0}"}, "judges.x.scale"),
    ({"scale": "{kind: continuous, min: 0.0, max: .nan}"}, "judges.x.scale"),
    (
        {"scale": "{kind: continuous, min: 0.0, max: 1.0}", "threshold": ".nan"},
        "judges.x.threshold",
    ),
    ({"sampling": "{temperature: .inf}"}, "judges.x.sampling.temperature"),
)


def _judge_project(*, scale: str | None = None, threshold: str | None = None, sampling: str | None = None):
    """A temp project with one fully specified judge ``x`` whose numeric block is a YAML snippet."""
    body = [
        "judges:",
        "  x:",
        "    model: fake-judge-2024-11-20",
        "    rubric: |",
        "      Score it",
        '    prompt_version: "1"',
    ]
    if scale is not None:
        body.append(f"    scale: {scale}")
    if threshold is not None:
        body.append(f"    threshold: {threshold}")
    if sampling is not None:
        body.append(f"    sampling: {sampling}")
    return proj.project(proj.BASE_CONFIG + "\n".join(body) + "\n")


def _strict_json(text: str) -> dict:
    """Parse ``text`` like a strict JSON parser: ``Infinity``/``NaN`` are a failure, not a float."""

    def reject(token: str):
        raise AssertionError(f"non-finite JSON token {token!r} in the envelope: {text}")

    return json.loads(text, parse_constant=reject)


def test_non_finite_scale_bounds_threshold_and_temperature_are_exit_two():
    infinite = float("inf")
    cases = (
        ({"scale": {"kind": "continuous", "min": 0.0, "max": infinite}}, "judges.helpful.scale"),
        ({"scale": {"kind": "continuous", "min": -infinite, "max": 1.0}}, "judges.helpful.scale"),
        ({"scale": {"kind": "continuous", "min": 0.0, "max": float("nan")}}, "judges.helpful.scale"),
        ({"threshold": float("nan")}, "judges.helpful.threshold"),
        ({"sampling": {"temperature": infinite}}, "judges.helpful.sampling.temperature"),
    )
    for extra, path in cases:
        exc = _spec_error({**FULL, **extra})
        assert exc.code == EXIT_USAGE == 2, (extra, exc.code)
        assert _dotted(exc) == [path], (extra, _dotted(exc))
        assert "OverflowError" not in str(exc), exc

    # ... and through the public validate() path (a dataclass cannot intercept its constructor)
    for scale in (
        judges_mod.Scale(max=infinite),
        judges_mod.Scale(min=-infinite, max=1.0, kind="continuous"),
        judges_mod.Scale(max=float("nan"), kind="continuous"),
    ):
        spec = judges_mod.JudgeSpec(name="helpful", rubric=RUBRIC, model="gpt-4o", prompt_version="1", scale=scale)
        exc = _error(spec.validate)
        assert exc.code == EXIT_USAGE == 2 and _dotted(exc) == ["judges.helpful.scale"], _dotted(exc)
    hot = judges_mod.JudgeSpec(
        name="helpful",
        rubric=RUBRIC,
        model="gpt-4o",
        prompt_version="1",
        sampling=judges_mod.Sampling(temperature=infinite),
    )
    exc = _error(hot.validate)
    assert exc.code == EXIT_USAGE == 2, exc
    assert _dotted(exc) == ["judges.helpful.sampling.temperature"], _dotted(exc)


def test_a_huge_integer_bound_is_exit_two_not_an_overflow_error():
    """``float(10**400)`` raises ``OverflowError`` -- the helper turns it into exit 2, not a crash."""
    for huge in (10**400, 10**10000):
        exc = _spec_error({**FULL, "scale": {"kind": "continuous", "min": 0.0, "max": huge}})
        assert exc.code == EXIT_USAGE == 2, (huge, exc.code)
        assert _dotted(exc) == ["judges.helpful.scale"], _dotted(exc)
        assert "OverflowError" not in str(exc), exc  # the message is a rejection, not a traceback
    direct = judges_mod.JudgeSpec(
        name="helpful", rubric=RUBRIC, model="gpt-4o", prompt_version="1", scale=judges_mod.Scale(max=10**400)
    )
    exc = _error(direct.validate)
    assert exc.code == EXIT_USAGE == 2 and _dotted(exc) == ["judges.helpful.scale"], _dotted(exc)


def test_a_boolean_is_not_a_scale_bound_threshold_or_temperature():
    """JSON ``true`` is not a number (a Python ``bool`` is an ``int``, so guard it explicitly)."""
    cases = (
        ({"scale": {"kind": "continuous", "min": 0.0, "max": True}}, "judges.helpful.scale"),
        ({"scale": {"kind": "continuous", "min": False, "max": 1.0}}, "judges.helpful.scale"),
        ({"threshold": True}, "judges.helpful.threshold"),
        ({"sampling": {"temperature": True}}, "judges.helpful.sampling.temperature"),
    )
    for extra, path in cases:
        exc = _spec_error({**FULL, **extra})
        assert exc.code == EXIT_USAGE == 2, (extra, exc.code)
        assert _dotted(exc) == [path], (extra, _dotted(exc))


def test_normal_finite_continuous_scales_still_pass():
    for low, high in ((0.0, 1.0), (0.0, 10.0)):
        spec = judges_mod.JudgeSpec.from_rule(
            "helpful",
            {**FULL, "scale": {"kind": "continuous", "min": low, "max": high}, "threshold": None},
        )
        assert (spec.scale.min, spec.scale.max, spec.scale.kind) == (low, high, "continuous")

    root = _judge_project(scale="{kind: continuous, min: 0.0, max: 10.0}", threshold="5.0")
    try:
        code, envelope = proj.run_cli_in_process(root, "judges")
        assert code == 0 and envelope["ok"] is True, envelope
        assert envelope["data"]["judges"][0]["scale"] == {"min": 0.0, "max": 10.0, "kind": "continuous"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_cli_rejects_non_finite_judge_numbers_with_exit_two():
    """``evalops judges`` and ``config validate`` both reject ``inf``/``NaN`` with the dotted path."""
    for kwargs, path in NON_FINITE_CLI_CASES:
        root = _judge_project(**kwargs)
        try:
            for args in (("judges",), ("config", "validate")):
                code, envelope = proj.run_cli_in_process(root, *args)
                assert code == 2 and envelope["code"] == 2, (kwargs, args, envelope)
                message = envelope["errors"][0]["message"]
                assert path in message, (kwargs, args, message)
                assert "OverflowError" not in message and "Traceback" not in message, message
        finally:
            shutil.rmtree(root, ignore_errors=True)

    # a 400-digit integer bound is a usage error, never an ``OverflowError`` traceback. The config
    # model owns it first, so the exact path is the full model path (``.scale.max``) -- asserted
    # exactly, not as a substring, because ``JudgeSpec``'s owning path would be ``.scale``.
    root = _judge_project(scale=f"{{kind: continuous, min: 0.0, max: {10**400}}}")
    try:
        for args in (("judges",), ("config", "validate")):
            proc = subprocess.run(
                [sys.executable, "-m", "evalops", "--json", *args],
                cwd=str(root),
                env=proj.env(),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 2, (args, proc.returncode, proc.stderr)
            combined = proc.stdout + proc.stderr
            assert "Traceback" not in combined and "OverflowError" not in combined, combined
            envelope = _strict_json(proc.stdout)  # exactly one strict-JSON document
            assert envelope["ok"] is False and envelope["code"] == 2, (args, envelope)
            error = envelope["errors"][0]
            assert error["message"].startswith("invalid evalops.yaml: "), (args, error)
            assert "judges.x.scale.max" in error["message"], (args, error)
            assert [issue["path"] for issue in error["details"]["errors"]] == ["judges.x.scale.max"], (
                args,
                error,
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # the other layer: a direct API caller never reaches the config model, so ``JudgeSpec``
    # (``from_rule`` -> ``validate()``) names the owning field, ``.scale``, without ``.max``.
    exc = _spec_error(
        {**FULL, "scale": {"kind": "continuous", "min": 0, "max": 10**400}},
        name="x",
    )
    assert exc.code == EXIT_USAGE == 2, exc.code
    assert _dotted(exc) == ["judges.x.scale"], _dotted(exc)
    assert "OverflowError" not in str(exc) and "Traceback" not in str(exc), exc


def test_the_judges_envelope_is_strict_json_for_a_finite_spec():
    """The invariant: the whole ``evalops judges --json`` envelope is always valid JSON."""
    root = _judge_project(scale="{kind: continuous, min: 0.0, max: 10.0}", threshold="5.0")
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "--json", "judges"],
            cwd=str(root),
            env=proj.env(),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert "Infinity" not in proc.stdout and "NaN" not in proc.stdout, proc.stdout
        envelope = _strict_json(proc.stdout)
        assert envelope["ok"] is True and envelope["code"] == 0, envelope
        assert envelope["data"]["judges"][0]["scale"] == {"min": 0.0, "max": 10.0, "kind": "continuous"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_yaml_integer_bounds_are_normalized_to_floats():
    """``min: 0``/``max: 10`` are raw dict ints; the value objects must expose ``float`` bounds."""
    root = _judge_project(scale="{kind: continuous, min: 0, max: 10}", sampling="{temperature: 0}")
    try:
        cfg = config_mod.load(str(root / "evalops.yaml"))
        spec = judges_mod.load(cfg)["x"]
        assert spec.scale.min == 0.0 and isinstance(spec.scale.min, float), repr(spec.scale.min)
        assert spec.scale.max == 10.0 and isinstance(spec.scale.max, float), repr(spec.scale.max)
        assert spec.sampling.temperature == 0.0 and isinstance(spec.sampling.temperature, float)

        data = judges_mod.payload(cfg)
        entry = data["judges"][0]
        assert entry["scale"] == {"min": 0.0, "max": 10.0, "kind": "continuous"}, entry
        assert entry["sampling"]["temperature"] == 0.0, entry
        text = json.dumps(data)
        assert '"min": 0.0' in text and '"max": 10.0' in text and '"temperature": 0.0' in text, text
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _huge_integer_project() -> pathlib.Path:
    """A complete project whose judge scale max is a 10001-digit YAML integer literal."""
    huge = "1" + "0" * 10000
    body = [
        "judges:",
        "  x:",
        "    model: fake-judge-2024-11-20",
        "    rubric: |",
        "      Score it",
        '    prompt_version: "1"',
        f"    scale: {{kind: continuous, min: 0, max: {huge}}}",
    ]
    return proj.project(proj.BASE_CONFIG + "\n".join(body) + "\n")


def test_a_10001_digit_integer_makes_the_loader_name_the_config_file():
    """The parser refuses the scalar before any judge key exists: exit 2 about the file, never 5."""
    root = _huge_integer_project()
    try:
        for args in (("judges",), ("config", "validate")):
            proc = subprocess.run(
                [sys.executable, "-m", "evalops", "--json", *args],
                cwd=str(root),
                env=proj.env(),
                capture_output=True,
                text=True,
            )
            assert proc.returncode == 2, (args, proc.returncode, proc.stderr)
            combined = proc.stdout + proc.stderr
            assert "Traceback" not in combined and "OverflowError" not in combined, combined
            envelope = _strict_json(proc.stdout)  # exactly one strict-JSON document
            assert envelope["ok"] is False and envelope["code"] == 2, (args, envelope)
            message = envelope["errors"][0]["message"]
            assert "evalops.yaml" in message, (args, message)
            assert "integer string conversion" in message, (args, message)  # the parser's own text
    finally:
        shutil.rmtree(root, ignore_errors=True)
