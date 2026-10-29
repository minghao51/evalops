"""WP3.2a H3: the VERIFY ledger as an executable exit criterion (phase 3).

``src/evalops/beta.py`` is the single registry of vendor surfaces evalops cannot assert offline.
This module makes "zero unresolved VERIFY" mechanical: it scans ``src/evalops/plugins/*.py`` and
``src/evalops/scorers/*.py`` and fails when

(a) a bare ``VERIFY`` token remains (a marker must be ``VERIFY-BETA(<id>)``, or the behavior must be
    asserted by a test and the marker deleted);
(b) a ``VERIFY-BETA(<id>)`` names an id that is not in :data:`evalops.beta.BETA_SURFACES`;
(c) a registered id has no marker at all (a stale registry entry);
(d) a registry entry's ``hint`` is empty (a beta with no user-visible action is not a beta);
(e) the registry is not sorted by id with unique ids (``doctor``'s payload is order-stable).

Every failure message names the offending ``file:line`` and ``id`` -- the point is that a reviewer
can see *what* is unresolved and *where*, not just that the guard failed. For a registry entry the
``file:line`` is the entry's own declaration in ``src/evalops/beta.py``, found by scanning the file
for its ``id`` (never hard-coded), so a stale entry and a hint-less entry are as actionable as a
bare marker.

The hints are checked too: they are the user-visible half of a beta, so a hint may only name a check
that exists in this repo today (a path that resolves, a ``module.py::test_name`` that holds that
test, ``make contract`` only with a ``tests/contract/`` module), and the surfaces nothing exercises
must say so plainly.

The trace-dialect markers in ``src/evalops/traces/**`` are deliberately **out of scope**: they are
phase-5 work (dialect adapters), not vendor surfaces, so this scan covers exactly ``plugins/`` and
``scorers/``. That is asserted below rather than assumed.

The registry is also surfaced through the CLI: ``doctor --json`` carries ``data.beta`` (the sorted
payload) and the human rendering prints every hint, so an unverified surface is always visible to a
user and never only to a test.

Every test is a zero-argument function: ``python tests/run_without_pytest.py`` runs this module too.
"""

import pathlib
import re
import shutil
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2] / "src"))

import _projects as proj  # noqa: E402

from evalops import beta  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "evalops"

#: The guarded roots. `traces/**` is intentionally absent (phase 5, out of this guard's scope).
SCANNED: tuple[pathlib.Path, ...] = tuple(
    sorted((SRC / "plugins").glob("*.py")) + sorted((SRC / "scorers").glob("*.py"))
)

#: `VERIFY-BETA(<id>)`; the id may be empty, which then fails the registry check (never a bare pass).
MARKER = re.compile(r"VERIFY-BETA\(([^)\n]*)\)")
#: Every `VERIFY` token; each one must be the start of a registered marker (else it is bare).
TOKEN = re.compile(r"VERIFY")
#: A repo-relative path a hint may name, optionally pinned to a test function (`module.py::test`).
HINT_PATH = re.compile(r"(?:tests|src)/[A-Za-z0-9_./-]+\.py(?:::[A-Za-z0-9_]+)?")
#: The registry module; a failure that names an entry names this file and the entry's line.
BETA_MODULE = SRC / "beta.py"
#: Surfaces with no automated check of any kind: their hint has to say that, not name a command.
UNCHECKED = ("langsmith-pull-prompt-commit", "langsmith-run-results", "mlflow-result-df")
#: The phrasings the first registry used for checks that did not exist (kept out by a test).
DISHONEST = (
    "langsmith extra to verify",
    "rubric-judge e2e",
    "managed-evaluator contract slice",
    "openevals adapter contract slice",
    "pydantic-evals adapter contract slice",
)


def occurrences() -> list[tuple[pathlib.Path, int, str | None]]:
    """One entry per ``VERIFY`` token: ``(file, line number, marker id or None)``.

    Splitting the token from its id this way is what catches the evasions a naive grep misses: an
    id split across lines leaves a bare token on the first line, and whitespace/case inside the
    parentheses leaves an unregistered id -- both fail below with the file:line and the id.
    """
    found: list[tuple[pathlib.Path, int, str | None]] = []
    for path in SCANNED:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            for match in TOKEN.finditer(line):
                marker = MARKER.match(line[match.start() :])
                found.append((path, number, marker.group(1) if marker else None))
    return found


def _registered() -> dict[str, beta.BetaSurface]:
    return {surface.id: surface for surface in beta.BETA_SURFACES}


def _where(path: pathlib.Path, line: int) -> str:
    return f"{path.relative_to(REPO)}:{line}"


def _registry_location(surface_id: str) -> str:
    """``src/evalops/beta.py:<line>`` of the entry declaring ``surface_id`` -- computed, not stored.

    Scanning for the entry's ``id`` literal keeps the message honest even after the registry moves:
    a reviewer jumps straight to the line to edit, the way the bare-marker message already allows.
    """
    needle = f'id="{surface_id}"'
    for number, line in enumerate(BETA_MODULE.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in line:
            return _where(BETA_MODULE, number)
    raise AssertionError(f"{BETA_MODULE}: no registry entry declares id {surface_id!r}")


# ---------------------------------------------------------------------------
# (a) no bare VERIFY token survives
# ---------------------------------------------------------------------------
def test_h3_no_bare_verify_marker_remains_in_plugins_or_scorers():
    bare = [f"{_where(path, line)}: bare VERIFY" for path, line, marker_id in occurrences() if marker_id is None]
    assert not bare, "\n".join(bare)


# ---------------------------------------------------------------------------
# (b) every marker id is registered
# ---------------------------------------------------------------------------
def test_h3_every_verify_beta_marker_names_a_registered_id():
    registered = _registered()
    unregistered = [
        f"{_where(path, line)}: VERIFY-BETA({marker_id}) is not in beta.BETA_SURFACES"
        for path, line, marker_id in occurrences()
        if marker_id is not None and marker_id not in registered
    ]
    assert not unregistered, "\n".join(unregistered)


# ---------------------------------------------------------------------------
# (c) no stale registry entry: every registered id is referenced by a marker
# ---------------------------------------------------------------------------
def test_h3_every_registered_surface_is_referenced_by_a_marker():
    referenced = {marker_id for _path, _line, marker_id in occurrences() if marker_id is not None}
    stale = [
        f"{_registry_location(surface.id)}: beta.BETA_SURFACES id {surface.id!r} has no VERIFY-BETA marker"
        for surface in beta.BETA_SURFACES
        if surface.id not in referenced
    ]
    assert not stale, "\n".join(stale)


# ---------------------------------------------------------------------------
# (d) a beta always has a user-visible action
# ---------------------------------------------------------------------------
def test_h3_every_registered_surface_has_a_non_empty_hint():
    hintless = [
        f"{_registry_location(surface.id)}: beta.BETA_SURFACES id {surface.id!r}: empty hint"
        for surface in beta.BETA_SURFACES
        if not surface.hint.strip()
    ]
    assert not hintless, "\n".join(hintless)


# ---------------------------------------------------------------------------
# (e) the registry shape doctor exposes
# ---------------------------------------------------------------------------
def test_h3_the_registry_is_sorted_by_id_and_unique():
    ids = [surface.id for surface in beta.BETA_SURFACES]
    assert ids == sorted(ids), ids
    assert len(ids) == len(set(ids)), sorted({i for i in ids if ids.count(i) > 1})
    assert [entry["id"] for entry in beta.payload()] == ids
    assert all(set(entry) == {"id", "area", "summary", "reason", "hint"} for entry in beta.payload())


def test_h3_every_registry_entry_is_located_by_the_scan():
    """The stale/hint-less messages quote ``beta.py:<line>``: prove the scan finds every entry."""
    lines = BETA_MODULE.read_text(encoding="utf-8").splitlines()
    for surface in beta.BETA_SURFACES:
        location = _registry_location(surface.id)
        path_text, _, line_text = location.partition(":")
        assert path_text == "src/evalops/beta.py", location
        assert f'id="{surface.id}"' in lines[int(line_text) - 1], location


# ---------------------------------------------------------------------------
# (f) a hint names a check that exists today, or says there is none
# ---------------------------------------------------------------------------
def test_h3_every_hint_names_only_paths_and_tests_that_exist():
    for surface in beta.BETA_SURFACES:
        for named in HINT_PATH.findall(surface.hint):
            path_text, _, test_name = named.partition("::")
            path = REPO / path_text
            assert path.is_file(), f"{surface.id}: hint names {path_text!r}, which does not exist"
            if test_name:
                assert f"def {test_name}(" in path.read_text(encoding="utf-8"), (
                    f"{surface.id}: hint names {path_text}::{test_name}, but that test is not there"
                )


def test_h3_make_contract_is_only_named_with_a_contract_module_that_exists():
    """`make contract` runs ``tests/contract``; naming it without such a module is a hollow hint."""
    assert sorted((REPO / "tests" / "contract").glob("test_*.py")), "the contract slice is missing"
    for surface in beta.BETA_SURFACES:
        if "make contract" not in surface.hint:
            continue
        named = [path for path in HINT_PATH.findall(surface.hint) if path.startswith("tests/contract/")]
        assert named, f"{surface.id}: hint names `make contract` without naming a tests/contract module"


def test_h3_surfaces_without_any_check_say_no_automated_check_exists():
    registered = _registered()
    for surface_id in UNCHECKED:
        surface = registered[surface_id]
        assert "no automated check yet" in surface.hint, f"{surface_id}: {surface.hint}"
        assert "::" not in surface.hint, (
            f"{surface_id}: nothing exercises this surface, yet the hint pins a test: {surface.hint}"
        )


def test_h3_no_hint_revives_a_check_that_does_not_exist():
    for surface in beta.BETA_SURFACES:
        for phrase in DISHONEST:
            assert phrase not in surface.hint, f"{surface.id}: hint names {phrase!r}, which is not a check here"


def test_h3_the_guard_scope_is_plugins_and_scorers_only():
    """The trace-dialect markers belong to phase 5: assert the scan cannot see them."""
    assert SCANNED, "the guard scans nothing"
    for path in SCANNED:
        assert path.parent.name in ("plugins", "scorers"), path
        assert "traces" not in path.parts, path
    # and prove such a marker could exist there without tripping this guard
    traces = sorted((SRC / "traces").glob("*.py"))
    assert traces, "no traces modules found"
    assert not any(path in SCANNED for path in traces), "a traces module is in the guard's scope"


# ---------------------------------------------------------------------------
# the CLI surface: doctor carries the registry and prints every hint
# ---------------------------------------------------------------------------
def test_doctor_json_carries_the_sorted_beta_payload():
    root = proj.project()
    try:
        code, envelope = proj.run_cli(root, "doctor")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert code == 0 and envelope["ok"] is True, envelope
    reported = envelope["data"]["beta"]
    assert reported == beta.payload(), reported
    assert [entry["id"] for entry in reported] == sorted(entry["id"] for entry in reported), reported
    assert all(entry["hint"] for entry in reported), reported


def test_doctor_human_prints_the_beta_block_and_every_hint():
    root = proj.project()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "evalops", "doctor"],
            cwd=str(root),
            env=proj.env(),
            capture_output=True,
            text=True,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)
    assert proc.returncode == 0, proc.stderr
    assert "beta (unverified surfaces):" in proc.stdout, proc.stdout
    # the block follows the role report (roles first, then the unverified surfaces)
    assert proc.stdout.index("roles") < proc.stdout.index("beta (unverified surfaces):"), proc.stdout
    # and it is the *only* human rendering: the generic `data.beta` list line is not printed too
    assert proc.stdout.count("beta (unverified surfaces):") == 1, proc.stdout
    assert "\n  beta: [" not in proc.stdout, proc.stdout
    for surface in beta.BETA_SURFACES:
        assert surface.hint in proc.stdout, f"hint for {surface.id!r} missing from human doctor output"
