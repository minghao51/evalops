"""WP2.4: the four CI adapters are generated from one template, and stay that way.

The committed files under ``ci/`` are build artifacts of ``evalops.ci_adapters`` (the same deal
``make schema``/``make capabilities`` have with their outputs), so this module pins four properties:

(a) **zero-diff drift** (D1): every rendered adapter is byte-identical to the committed file, and a
    hand edit turns the probe red (mutation on a /tmp copy of ``src`` + ``ci``);
(b) **parity with the pipeline** (D2): every ``pipeline.STAGES`` name appears exactly once as
    ``ci/pipeline.sh <stage>``, no other evalops subcommand appears anywhere, and adding a stage to
    ``STAGES`` on a /tmp copy turns the probe red -- the adapters cannot silently forget a stage;
(c) **no stage logic in the output** (A3/D3): walking each rendered file's own shell lines (parsed
    YAML steps/scripts, ``sh '...'`` for the Jenkinsfile) every command is checkout/install/shim or
    one of the documented publish/diagnostic helpers, and never an evalops invocation;
(d) **determinism, validity and the CLI** (A4/A5/B/D4/D5): rendering twice is byte-identical, the
    three YAML files parse with ``yaml.safe_load``, the Jenkinsfile is a declarative pipeline whose
    stages are exactly ``pipeline.STAGES`` in order, every file starts with the generated header,
    ``evalops ci-adapters --json`` reports four sorted adapters and writes nothing, and
    ``--write --dir`` writes exactly the four spec'd paths (a second run is byte-identical).

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

from __future__ import annotations

import dataclasses
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

import yaml

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(TESTS_DIR / "unit"))

import _projects as proj  # noqa: E402

from evalops import ci_adapters, pipeline  # noqa: E402

CI_DIR = REPO / "ci"

#: A1: the committed file each adapter renders to.
SPEC_PATHS = {
    "azure": "ci/azure-pipelines.yml",
    "github": "ci/github-actions.yml",
    "gitlab": "ci/gitlab-ci.yml",
    "jenkins": "ci/Jenkinsfile",
}
YAML_ADAPTERS = ("github", "gitlab", "azure")
#: A4: the platform comment syntax of the generated-header line.
HEADER_MARKERS = {"github": "#", "gitlab": "#", "azure": "#", "jenkins": "//"}

#: A3: the only evalops subcommand a rendered adapter may reach is `pipeline`, through the shim.
#: Anything else in the list appearing as `evalops <name>` in a rendered file is stage logic.
OTHER_SUBCOMMANDS = [
    "sync",
    "eval",
    "gate",
    "promote",
    "rollback",
    "baseline",
    "calibrate",
    "plan",
    "doctor",
    "guard",
    "init",
    "capabilities",
    "ci-adapters",
    "config",
    "ledger",
    "traces",
    "cost",
    "redact",
    "plugins",
    "pipeline",
]

#: D3: the only shell a rendered adapter may run. Everything else is stage logic in the wrong place.
#: The checkout plumbing is pinned to the exact `git` subcommands the template uses: a wider
#: `git ` prefix would let `git push --force origin HEAD:refs/heads/main` through the no-logic check.
SHELL_ALLOWLIST = (
    "git checkout -B ",  # pr checkout of the request's source branch
    "git remote set-url ",  # GitLab: point origin at the base repo with the push token
    "git fetch ",  # full-history fetch when the platform's clone is shallow
    "pip install ",  # the documented install line
    "bash ci/pipeline.sh ",  # the one shim call (A3)
    "cat .evalops/out/",  # gate/drift report -> the runner's step summary
    "gh pr comment ",  # gate report -> PR comment (github only)
    "export EVALOPS_",  # env plumbing
)
_ENV_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _tmpdir(prefix: str = "evalops-wp24-") -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def _run(root: pathlib.Path, *args: str) -> tuple[int, dict]:
    """Run the real CLI as a subprocess -> (process exit code, envelope); stdout is one document."""
    proc = subprocess.run(
        [sys.executable, "-m", "evalops", "--json", *args],
        cwd=str(root),
        env=proj.env(),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proj.one_document(proc.stdout)


def _tree() -> pathlib.Path:
    """A /tmp copy of ``src`` + ``ci``: mutations never touch the tree under test."""
    root = _tmpdir("evalops-wp24-tree-")
    shutil.copytree(REPO / "src", root / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(CI_DIR, root / "ci")
    return root


def _probe(root: pathlib.Path, script: str) -> subprocess.CompletedProcess:
    """Run one probe script against ``root``'s copy of the package (its own ``PYTHONPATH``)."""
    return subprocess.run(
        [sys.executable, "-c", script, str(root)],
        cwd=str(root),
        env=proj.env({"PYTHONPATH": str(root / "src")}),
        capture_output=True,
        text=True,
    )


_DRIFT_PROBE = (
    "import pathlib, sys\n"
    "from evalops.ci_adapters import ADAPTERS\n"
    "root = pathlib.Path(sys.argv[1])\n"
    # bytes, not text: read_text() normalizes CRLF to LF and would hide a line-ending drift.
    "drifted = [s.path for s in ADAPTERS.values() if (root / s.path).read_bytes() != s.render().encode('utf-8')]\n"
    "print('drift:', drifted)\n"
    "sys.exit(1 if drifted else 0)\n"
)

_PARITY_PROBE = (
    "import re, sys\n"
    "from evalops.ci_adapters import ADAPTERS\n"
    "from evalops.pipeline import STAGES\n"
    "unwired = [\n"
    "    (spec.name, stage)\n"
    "    for spec in ADAPTERS.values()\n"
    "    for stage in STAGES\n"
    "    if len(re.findall(r'ci/pipeline\\.sh ' + stage + r'\\b', spec.render())) != 1\n"
    "]\n"
    "print('unwired:', unwired)\n"
    "sys.exit(1 if unwired else 0)\n"
)


def _shell_lines(name: str, text: str) -> list[str]:
    """Every shell line a rendered adapter runs, read out of the file's own platform structure."""
    if name == "jenkins":
        return re.findall(r"\bsh '([^']*)'", text)
    if name == "github":
        document = yaml.safe_load(text)
        return [step["run"] for job in document["jobs"].values() for step in job.get("steps", []) if "run" in step]
    if name == "gitlab":
        document = yaml.safe_load(text)
        lines = list(document["before_script"])
        for job in document.values():
            if isinstance(job, dict) and "script" in job:
                lines += list(job["script"])
        return lines
    assert name == "azure", name
    document = yaml.safe_load(text)
    return [line for step in document["steps"] if "script" in step for line in str(step["script"]).splitlines()]


# ---------------------------------------------------------------------------
# A1/A4/A5: the spec table, the header, determinism and validity
# ---------------------------------------------------------------------------
def test_adapter_spec_table_matches_the_documented_paths():
    assert set(ci_adapters.ADAPTERS) == set(SPEC_PATHS), "exactly four adapters (A1)"
    for name, spec in ci_adapters.ADAPTERS.items():
        assert spec.name == name
        assert spec.path == SPEC_PATHS[name], f"{name} must render to its committed path"
        assert spec.description, name
        assert callable(spec.render)
        assert spec.render().strip(), name


def test_every_rendered_file_starts_with_the_generator_header():
    for name, spec in ci_adapters.ADAPTERS.items():
        first = spec.render().splitlines()[0]
        expected = f"{HEADER_MARKERS[name]} {ci_adapters.GENERATED_BY}"
        assert first == expected, f"{name}: first line is {first!r}"


def test_rendering_is_deterministic_and_host_independent():
    for name, spec in ci_adapters.ADAPTERS.items():
        first = spec.render()
        assert first == spec.render(), f"{name} rendered twice must be identical"
        assert str(REPO) not in first, f"{name} leaked the render host's repo path"
        assert str(pathlib.Path.home()) not in first, f"{name} leaked the render host's home path"


def test_rendered_yaml_files_parse():
    for name in YAML_ADAPTERS:
        document = yaml.safe_load(ci_adapters.ADAPTERS[name].render())
        assert isinstance(document, dict), f"{name} is not a YAML mapping"


def test_jenkinsfile_is_a_declarative_pipeline_over_the_pipeline_stages():
    text = ci_adapters.ADAPTERS["jenkins"].render()
    assert "pipeline {" in text and "agent any" in text
    assert re.findall(r"stage\('([^']+)'\)", text) == list(pipeline.STAGES), "stages in STAGES order, no extras"
    for stage in pipeline.STAGES:
        assert text.count(f"sh 'bash ci/pipeline.sh {stage}'") == 1, stage


def test_jenkinsfile_declares_the_documented_frozen_change_parameter():
    text = ci_adapters.ADAPTERS["jenkins"].render()
    parameters = re.search(r"parameters \{(.*?)\n  \}", text, re.DOTALL)
    assert parameters, "the Jenkinsfile declares no parameters block"
    assert "booleanParam(name: 'EVALOPS_ALLOW_FROZEN_CHANGE'" in parameters.group(1), parameters.group(1)
    environment = re.search(r"environment \{(.*?)\n  \}", text, re.DOTALL)
    assert environment, "the Jenkinsfile declares no environment block"
    assert "EVALOPS_ALLOW_FROZEN_CHANGE = " in environment.group(1), (
        "the documented build parameter must reach the stage environment"
    )


# ---------------------------------------------------------------------------
# A2: the Jenkins `when` blocks must be mutually exclusive per build cause
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class _Cause:
    """One build cause, as the declarative `when` predicates observe it."""

    name: str
    branch: str | None
    evidence: str
    triggered_by: str
    change_request: bool


#: The causes the four stages must partition. A PR build's BRANCH_NAME is the request ref, not the
#: target branch, so ``branch 'main'`` is false there; a nightly cron build *is* on main.
CAUSES = (
    _Cause("pull request", None, "", "UserIdCause", change_request=True),
    _Cause("scheduled nightly", "main", "", "TimerTrigger", change_request=False),
    _Cause("manual release", "main", "group-42", "UserIdCause", change_request=False),
    _Cause("push to main", "main", "", "SCMTrigger", change_request=False),
    _Cause("push to a feature branch", "feature", "", "SCMTrigger", change_request=False),
    _Cause("manual build on main without a group", "main", "", "UserIdCause", change_request=False),
)
#: The stage each canonical cause must select; the remaining causes select nothing.
EXPECTED_STAGE = {
    "pull request": "pr",
    "scheduled nightly": "nightly",
    "manual release": "release",
    "push to main": "main",
    "manual build on main without a group": "main",
}

_EXPRESSION_BLOCK = re.compile(r"expression\s*\{([^{}]*)\}")
_EVIDENCE_EXPRESSION = re.compile(r"^return\s+params\.([A-Za-z_][A-Za-z0-9_]*)\?\.trim\(\)\s*!=\s*''$")
_WHEN_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|'[^']*'|\"[^\"]*\"|[{}();]")


def _unquote(token: str) -> str:
    return token[1:-1] if token[:1] in "'\"" else token


class _Predicate:
    """A tiny evaluator for the declarative `when` grammar this Jenkinsfile uses.

    It models ``changeRequest()``, ``triggeredBy '...'``, ``branch '...'``, ``allOf``/``anyOf``/
    ``not`` and ``expression { return params.X?.trim() != '' }``; any other token raises, so a
    condition the fixture cannot model turns the test red instead of silently passing.
    """

    def __init__(self, text: str) -> None:
        self._expressions: list[str] = []
        rewritten = _EXPRESSION_BLOCK.sub(self._stash, text)
        self._tokens = _WHEN_TOKEN.findall(rewritten)
        self._index = 0

    def _stash(self, match: re.Match[str]) -> str:
        self._expressions.append(match.group(1).strip())
        return f"EXPR{len(self._expressions) - 1}"

    def evaluate(self, cause: _Cause) -> bool:
        self._index = 0
        value = self._predicate(cause)
        assert self._index == len(self._tokens), f"unparsed when-condition: {self._tokens[self._index :]!r}"
        return value

    def _next(self) -> str:
        token = self._tokens[self._index]
        self._index += 1
        return token

    def _expect(self, token: str) -> None:
        assert self._next() == token, f"expected {token!r}"

    def _block(self, cause: _Cause) -> list[bool]:
        self._expect("{")
        values = []
        while self._tokens[self._index] != "}":
            values.append(self._predicate(cause))
            if self._tokens[self._index] == ";":
                self._index += 1
        self._expect("}")
        return values

    def _predicate(self, cause: _Cause) -> bool:
        token = self._next()
        if token in ("allOf", "anyOf", "not"):
            values = self._block(cause)
            if token == "allOf":
                return all(values)
            if token == "anyOf":
                return any(values)
            assert len(values) == 1, "`not` wraps exactly one predicate"
            return not values[0]
        if token == "changeRequest":
            self._expect("(")
            self._expect(")")
            return cause.change_request
        if token == "triggeredBy":
            return cause.triggered_by == _unquote(self._next())
        if token == "branch":
            pattern = _unquote(self._next())
            return cause.branch is not None and cause.branch == pattern
        match = re.fullmatch(r"EXPR(\d+)", token)
        if match:
            inner = self._expressions[int(match.group(1))]
            parsed = _EVIDENCE_EXPRESSION.match(inner)
            assert parsed, f"the fixture does not model the expression {inner!r}"
            assert parsed.group(1) == ci_adapters.EVIDENCE_GROUP, parsed.group(1)
            return cause.evidence.strip() != ""
        raise AssertionError(f"the fixture does not model the when-condition token {token!r}")


def _jenkins_when_blocks(text: str) -> dict[str, str]:
    """``stage -> its single-line when-condition``, read out of the rendered declarative pipeline."""
    blocks: dict[str, str] = {}
    stage: str | None = None
    for line in text.splitlines():
        opened = re.match(r"\s*stage\('([^']+)'\) \{", line)
        if opened:
            stage = opened.group(1)
            continue
        condition = re.match(r"\s*when \{ (.*) \}$", line)
        if condition and stage:
            blocks[stage] = condition.group(1)
    return blocks


def test_jenkins_when_conditions_are_pairwise_disjoint_per_cause():
    conditions = _jenkins_when_blocks(ci_adapters.ADAPTERS["jenkins"].render())
    assert set(conditions) == set(pipeline.STAGES), conditions

    # structural minimum: no non-pr stage can fire for a change request, and main excludes the
    # two other causes that are also true on the default branch (timer + release input).
    assert conditions["pr"] == "changeRequest()", conditions["pr"]
    assert conditions["nightly"] == "triggeredBy 'TimerTrigger'", conditions["nightly"]
    for stage in ("main", "release"):
        assert "changeRequest()" not in conditions[stage], f"{stage} would also fire on a pull request"
    assert "branch 'main'" in conditions["main"], conditions["main"]
    assert "not { triggeredBy 'TimerTrigger' }" in conditions["main"], (
        f"a nightly build of main would also select main: {conditions['main']}"
    )
    assert "not { triggeredBy 'TimerTrigger' }" in conditions["release"], conditions["release"]
    assert "EVIDENCE_GROUP" in conditions["main"], "a manual release on main would also select main"
    assert "branch" not in conditions["nightly"], "the nightly stage must not also claim the branch"

    evaluators = {stage: _Predicate(condition) for stage, condition in conditions.items()}
    for cause in CAUSES:
        selected = [stage for stage in pipeline.STAGES if evaluators[stage].evaluate(cause)]
        assert len(selected) <= 1, f"cause {cause.name!r} selects {selected}: the stages overlap"
        expected = [EXPECTED_STAGE[cause.name]] if cause.name in EXPECTED_STAGE else []
        assert selected == expected, f"cause {cause.name!r} selects {selected}, expected {expected}"


# ---------------------------------------------------------------------------
# A2: the binding table
# ---------------------------------------------------------------------------
def test_bindings_cover_exactly_the_pipeline_stages():
    assert set(ci_adapters.BINDINGS) == set(ci_adapters.ADAPTERS)
    for adapter, rows in ci_adapters.BINDINGS.items():
        assert set(rows) == set(pipeline.STAGES), f"{adapter} must bind every stage and no other"
        for stage, item in rows.items():
            assert item.stage == stage
            assert item.trigger and item.when, (adapter, stage)


def test_bindings_plumb_the_documented_env_per_stage():
    for adapter, rows in ci_adapters.BINDINGS.items():
        pr_env = dict(rows["pr"].env)
        # WP2.2: EVALOPS_PR routes the state commit to the bot branch, so the pr job must set both.
        assert {"EVALOPS_PR", "EVALOPS_BRANCH"} <= set(pr_env), f"{adapter}: pr env is {sorted(pr_env)}"
        assert "EVALOPS_BRANCH" in dict(rows["main"].env), adapter
        release = rows["release"]
        # A2: a release is manual and needs EVIDENCE_GROUP -- plumbed, or set by the operator.
        assert "EVIDENCE_GROUP" in dict(release.env) or release.manual, adapter


# ---------------------------------------------------------------------------
# A3/D3: no stage logic in the rendered output
# ---------------------------------------------------------------------------
def _logic_problems(name: str, text: str) -> list[str]:
    """A3/D3 violations in one rendered adapter: a non-shim evalops call or an unknown shell line."""
    problems = [
        f"evalops {subcommand}"
        for subcommand in OTHER_SUBCOMMANDS
        if re.search(rf"\bevalops\s+{re.escape(subcommand)}\b", text)
    ]
    if "head.repo" in text:
        problems.append("fork gating belongs to EVALOPS_PR, not to the adapter")
    for line in _shell_lines(name, text):
        words = shlex.split(line.strip())
        while words and _ENV_WORD.match(words[0]):
            words.pop(0)  # leading env assignments are plumbing, not a command
        if words and not " ".join(words).startswith(SHELL_ALLOWLIST):
            problems.append(f"unexpected shell line {line!r}")
    return problems


def test_rendered_adapters_invoke_only_the_shim_and_shell_helpers():
    for name, spec in ci_adapters.ADAPTERS.items():
        problems = _logic_problems(name, spec.render())
        assert not problems, f"{name}: {problems}"


def test_no_logic_check_is_mutation_sensitive():
    spec = ci_adapters.ADAPTERS["github"]
    control = spec.render()
    assert not _logic_problems("github", control)
    # the two shapes a hand edit would take: a real evalops subcommand, and a stray shell command.
    smuggled = control + "      - run: evalops gate --min facts/mean 0.9\n"
    assert _logic_problems("github", smuggled), "a smuggled evalops gate call must be caught"
    stray = control + "      - run: curl https://example.invalid/x\n"
    assert _logic_problems("github", stray), "a shell command outside the allowlist must be caught"


def test_no_logic_check_catches_a_git_push_injected_into_a_script():
    assert "git " not in SHELL_ALLOWLIST, "a bare `git ` prefix accepts `git push --force ...`"
    control = ci_adapters.ADAPTERS["gitlab"].render()
    assert not _logic_problems("gitlab", control)
    injected = control.replace(
        "    - bash ci/pipeline.sh pr",
        "    - git push --force origin HEAD:refs/heads/main\n    - bash ci/pipeline.sh pr",
        1,
    )
    assert injected != control, "the gitlab pr script moved: update this mutation"
    problems = _logic_problems("gitlab", injected)
    assert any("git push --force" in problem for problem in problems), problems


# ---------------------------------------------------------------------------
# D1: the committed files are the generated output (zero-diff drift)
# ---------------------------------------------------------------------------
def test_committed_adapters_are_the_generated_output():
    # bytes, not text: read_text() normalizes CRLF to LF, so a line-ending drift would pass.
    drifted = [
        spec.path for spec in ci_adapters.ADAPTERS.values() if (REPO / spec.path).read_bytes() != spec.render().encode()
    ]
    assert not drifted, f"{drifted} drifted from src/evalops/ci_adapters.py; run `make ci-adapters`"


def test_drift_probe_is_mutation_sensitive():
    tree = _tree()
    control = _probe(tree, _DRIFT_PROBE)
    assert control.returncode == 0, control.stdout + control.stderr
    edited = tree / SPEC_PATHS["github"]
    edited.write_text(edited.read_text() + "\n# hand edit\n")
    mutated = _probe(tree, _DRIFT_PROBE)
    assert mutated.returncode == 1, "an edited adapter must turn the drift test red"
    assert SPEC_PATHS["github"] in mutated.stdout, mutated.stdout


def test_drift_probe_catches_a_line_ending_only_drift():
    tree = _tree()
    assert _probe(tree, _DRIFT_PROBE).returncode == 0
    crlf = tree / SPEC_PATHS["gitlab"]
    crlf.write_bytes(crlf.read_bytes().replace(b"\n", b"\r\n"))
    mutated = _probe(tree, _DRIFT_PROBE)
    assert mutated.returncode == 1, "CRLF-vs-LF drift must turn the drift test red"
    assert SPEC_PATHS["gitlab"] in mutated.stdout, mutated.stdout


# ---------------------------------------------------------------------------
# D2: parity with pipeline.STAGES
# ---------------------------------------------------------------------------
def test_every_stage_runs_through_the_shim_exactly_once():
    for name, spec in ci_adapters.ADAPTERS.items():
        text = spec.render()
        assert text.count("ci/pipeline.sh") == len(pipeline.STAGES), f"{name}: shim call count"
        for stage in pipeline.STAGES:
            found = re.findall(rf"ci/pipeline\.sh {stage}\b", text)
            assert len(found) == 1, f"{name}: stage {stage!r} appears {len(found)} times"


def test_parity_probe_is_mutation_sensitive():
    tree = _tree()
    control = _probe(tree, _PARITY_PROBE)
    assert control.returncode == 0, control.stdout + control.stderr
    module = tree / "src" / "evalops" / "pipeline.py"
    source = module.read_text()
    stages = ", ".join(f'"{stage}"' for stage in (*pipeline.STAGES, "shadow"))
    mutated, count = re.subn(r"^STAGES = \([^)]*\)$", f"STAGES = ({stages})", source, count=1, flags=re.MULTILINE)
    assert count == 1, "the STAGES table moved: update this mutation"
    module.write_text(mutated)
    probe = _probe(tree, _PARITY_PROBE)
    assert probe.returncode != 0, "a stage with no binding must turn the parity test red"
    assert "shadow" in probe.stderr, probe.stderr


# ---------------------------------------------------------------------------
# H3: the bindings the hand-written adapters carried survive
# ---------------------------------------------------------------------------
def test_github_workflow_keeps_the_events_environments_and_release_input():
    text = ci_adapters.ADAPTERS["github"].render()
    document = yaml.safe_load(text)
    # YAML 1.1 reads the bare key `on` as True; both spellings name the trigger block.
    triggers = document.get("on") or document.get(True)
    assert set(triggers) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    assert triggers["push"]["branches"] == [pipeline.DEFAULT_BRANCH]
    assert triggers["schedule"][0]["cron"] == ci_adapters.CRON
    assert triggers["workflow_dispatch"]["inputs"]["evidence_group"]["required"] is True
    jobs = document["jobs"]
    assert list(jobs) == list(pipeline.STAGES)
    assert "pull_request" in jobs["pr"]["if"]
    assert {"EVALOPS_PR", "EVALOPS_BRANCH"} <= set(jobs["pr"]["env"])
    # fork PRs: the base repository only has the PR head as refs/pull/<N>/head, never as a branch.
    pr_checkout = jobs["pr"]["steps"][0]["with"]
    assert pr_checkout["ref"] == "refs/pull/${{ github.event.pull_request.number }}/head", pr_checkout
    assert pr_checkout["fetch-depth"] == 0, "the pipeline diffs a baseline commit: full history is required"
    assert jobs["pr"]["env"]["EVALOPS_BRANCH"] == "${{ github.head_ref }}", "the env still names the head branch"
    release_step = jobs["release"]["steps"][-1]
    assert release_step["run"] == "bash ci/pipeline.sh release"
    assert "inputs.evidence_group" in release_step["env"]["EVIDENCE_GROUP"]
    assert jobs["release"]["environment"] == ci_adapters.PROTECTED_ENV
    # the gate report is surfaced where the hand-written workflow surfaced it:
    assert any(step.get("run", "").startswith("gh pr comment ") for step in jobs["pr"]["steps"])
    assert any("GITHUB_STEP_SUMMARY" in step.get("run", "") for step in jobs["main"]["steps"])


def test_github_fork_note_documents_the_write_token_prerequisite():
    text = ci_adapters.ADAPTERS["github"].render()
    assert yaml.safe_load(text)["permissions"]["contents"] == "write", "the workflow must ask for contents: write"
    # the comment block directly above the pr job is the fork/bot-branch story
    lines = text.splitlines()
    note: list[str] = []
    for line in reversed(lines[: lines.index("  pr:")]):
        if not line.startswith("  #"):
            break
        note.append(line)
    note_text = "\n".join(reversed(note))
    assert "read-only GITHUB_TOKEN" in note_text, note_text
    assert "Send write tokens to workflows from pull requests" in note_text, (
        "the fork note must name the repository setting the bot-branch push depends on"
    )
    assert "secrets." not in note_text and "token: " not in note_text, "no secret handling in the template"


def test_gitlab_pipeline_keeps_rules_credentials_and_artifacts():
    document = yaml.safe_load(ci_adapters.ADAPTERS["gitlab"].render())
    assert document["image"] == f"python:{ci_adapters.PYTHON_VERSION}"
    assert document["before_script"] == [ci_adapters.PIP_INSTALL["gitlab"]]
    pr = document["evalops:pr"]
    assert pr["rules"] == [{"if": '$CI_PIPELINE_SOURCE == "merge_request_event"'}]
    assert pr["variables"]["EVALOPS_PR"] == "$CI_MERGE_REQUEST_IID"
    assert pr["variables"]["EVALOPS_BRANCH"] == "$CI_MERGE_REQUEST_SOURCE_BRANCH_NAME"
    assert any("git remote set-url" in line for line in pr["script"]), "fork PRs push with the bot token"
    assert document["evalops:main"]["resource_group"] == "evalops-main"
    assert document["evalops:release"]["rules"][0]["when"] == "manual"
    assert document["evalops:release"]["environment"]["name"] == ci_adapters.PROTECTED_ENV
    for job in ("evalops:pr", "evalops:main", "evalops:nightly"):
        assert document[job]["artifacts"] == {"when": "always", "paths": [f"{ci_adapters.OUTPUT_DIR}/"]}


def test_azure_pipeline_keeps_checkout_reasons_and_evidence_parameter():
    document = yaml.safe_load(ci_adapters.ADAPTERS["azure"].render())
    checkout = document["steps"][0]
    assert checkout["checkout"] == "self" and checkout["persistCredentials"] is True
    assert checkout["fetchDepth"] == 0, "the pipeline diffs a baseline commit: a full clone is required"
    scripts = [step for step in document["steps"] if "script" in step]
    assert any(
        "System.PullRequest.PullRequestId" in step["script"] and "PullRequest" in step["condition"] for step in scripts
    ), "the pr step must pass the PR id and the stripped source branch"
    assert any(
        step["script"]
        == 'EVALOPS_BRANCH=main EVIDENCE_GROUP="${{ parameters.evidenceGroup }}" bash ci/pipeline.sh release'
        for step in scripts
    ), "the release step must plumb the evidenceGroup parameter"
    assert document["parameters"][0]["name"] == "evidenceGroup"
    assert document["steps"][-1]["publish"] == ci_adapters.OUTPUT_DIR


# ---------------------------------------------------------------------------
# D5: the CLI paths
# ---------------------------------------------------------------------------
def test_cli_reports_four_sorted_adapters_and_writes_nothing():
    root = _tmpdir("evalops-wp24-report-")
    code, document = _run(root, "ci-adapters")
    assert code == 0, document
    assert document["command"] == "ci-adapters" and document["ok"] is True
    adapters = document["data"]["adapters"]
    assert [entry["name"] for entry in adapters] == sorted(SPEC_PATHS)
    assert [entry["path"] for entry in adapters] == [SPEC_PATHS[name] for name in sorted(SPEC_PATHS)]
    for entry in adapters:
        assert entry["stages"] == list(pipeline.STAGES)
        assert entry["bytes"] == len(ci_adapters.ADAPTERS[entry["name"]].render().encode("utf-8"))
    assert list(root.iterdir()) == [], "the report path must write nothing"


def test_cli_write_lands_exactly_the_four_spec_paths():
    root = _tmpdir("evalops-wp24-write-")
    target = root / "out"
    target.mkdir()
    code, document = _run(root, "ci-adapters", "--write", "--dir", str(target))
    assert code == 0, document
    assert document["data"]["files_written"] == sorted(str(target / path) for path in SPEC_PATHS.values())
    files = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
    assert files == sorted(SPEC_PATHS.values()), "exactly four files, nothing else"
    first = {path: (target / path).read_bytes() for path in SPEC_PATHS.values()}
    code, second = _run(root, "ci-adapters", "--write", "--dir", str(target))
    assert code == 0 and second["data"]["files_written"] == document["data"]["files_written"]
    assert {path: (target / path).read_bytes() for path in SPEC_PATHS.values()} == first, "a re-run is byte-identical"
    assert sorted(path.name for path in root.iterdir()) == ["out"], "nothing is written beside --dir"


def test_cli_dir_that_is_not_a_directory_is_exit_two():
    root = _tmpdir("evalops-wp24-baddir-")
    (root / "a-file").write_text("not a directory\n")
    code, document = _run(root, "ci-adapters", "--write", "--dir", str(root / "a-file"))
    assert code == 2 and document["code"] == 2, document
    assert "not a directory" in document["errors"][0]["message"]
    code, document = _run(root, "ci-adapters", "--write", "--dir", str(root / "absent"))
    assert code == 2 and document["code"] == 2, "a missing directory is a usage error, not created"
    assert not (root / "absent").exists()
