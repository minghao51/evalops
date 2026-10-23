"""WP0.4: versioned persisted formats + semantic-only fingerprints.

Falsifies, in order: (a) v0 files still load, (b) v1 round-trips for all five formats, (c) `_meta`
is never read as a prompt/dataset/cassette entry, (d) fingerprint invariance (a pinned v0 constant),
(e) a future schema_version is exit 7 naming the file, (f) manifest hashes match written artifacts.

Every test is a zero-argument function: `python tests/run_without_pytest.py` runs this module too.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

TESTS_DIR = pathlib.Path(__file__).resolve().parents[1]
REPO = TESTS_DIR.parent
sys.path.insert(0, str(REPO / "src"))

import evalops  # noqa: E402
from evalops import cli as cli_mod  # noqa: E402
from evalops import config as config_mod  # noqa: E402
from evalops import cost as cost_mod  # noqa: E402
from evalops import datasets, locks, prompts, run  # noqa: E402
from evalops.contract import formats  # noqa: E402
from evalops.contract.formats import FORMAT_VERSION, META_KEY  # noqa: E402
from evalops.ports import RowResult, RunRecord  # noqa: E402
from evalops.tools import Cassette, replayable, use  # noqa: E402
from evalops.util import EXIT_CONTRACT, FormatError, canonical, sha256  # noqa: E402

FIXTURES = TESTS_DIR / "fixtures" / "formats"
V0 = FIXTURES / "v0"

# Fingerprint of tests/fixtures/formats/v0/cassette.jsonl computed with the *pre-WP0.4* algorithm
# (sha256 over the sorted canonical whole entries, truncated to 12) before `_meta` existed.
# Pinned deliberately: a v1 cassette with identical entries must still produce exactly this value.
V0_CASSETTE_FP = "73357e6c4b54"

CONFIG = """
project: formats
default_env: local
plugins: {prompts: git, datasets: git, ledger: "memory_ledger:MemoryLedger", tracer: none, evaluator: core}
app:
  behavior_config: fake_app:behavior_config
  predict_fn: fake_app:build_predict_fn
  scorers: fake_app:build_scorers
  judge_meta: fake_app:judge_meta
datasets:
  smoke: {frozen: true}
eval: {max_workers: 2, max_scorer_workers: 1, retries: 1, backoff_seconds: 0.01}
gates:
  metrics: {facts/mean: {min: 0.5}}
cost:
  prices_per_1m_tokens: {generation: {input: 1.0, output: 4.0}, judge: {input: 1.0, output: 1.0}}
  budgets: {per_run_usd: 5, per_month_usd: 50}
guard: {exclude: [".git", "src"]}
"""

# A fake app that calls a record/replayable tool, so a cassette fingerprint enters `app_version`.
TOOL_APP = """
import evalops
from evalops.tools import replayable


@replayable("lookup_order")
def lookup_order(order_id):
    return {"status": "shipped", "order_id": order_id}


def behavior_config():
    return {"llm_endpoint": "fake-llm@1", "tools": "lookup_order@1"}


def build_predict_fn(prompts):
    def predict(question):
        return "%s -> %s" % (question, lookup_order(question)["status"])
    return predict


@evalops.scorer("noop")
def noop(*, outputs, expectations):
    return 1.0


def build_scorers():
    return [noop]


def judge_meta():
    return {}
"""


# ---------------- helpers ----------------
def _tmpdir(prefix="evalops-formats-") -> pathlib.Path:
    return pathlib.Path(tempfile.mkdtemp(prefix=prefix))


def _raises(fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 - the assertion below inspects the type/code
        return e
    raise AssertionError("expected the call to raise, it returned normally")


def _cfg(root: pathlib.Path, data: dict | None = None) -> config_mod.Config:
    """A Config rooted in a temp dir, pointed at the repo's prompt/dataset sources."""
    base = {
        "project": "formats",
        "app": {},
        "paths": {"prompts": str(REPO / "prompts"), "datasets": str(REPO / "datasets")},
    }
    return config_mod.Config(data or base, root)


def _lock_cfg(root: pathlib.Path, kind: str, document: dict) -> config_mod.Config:
    """Write `document` as the `<kind>.lock.json` of `root` and return a Config on `root`."""
    p = locks.path(_cfg(root), kind)  # creates .evalops/locks/local
    p.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return _cfg(root)


def _entry(name: str) -> dict:
    return {"plugin": "git", "physical": name, "version": "1", "sha256": sha256(name)}


def _git(root: pathlib.Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)


def _git_status(root: pathlib.Path) -> list[str]:
    """`git status --porcelain` as a list of stripped lines (empty = clean tree)."""
    out = subprocess.run(["git", "status", "--porcelain"], cwd=root, check=True, capture_output=True, text=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def _run_cli(root: pathlib.Path, *args: str) -> tuple[int, dict]:
    """Run the real CLI in-process with `root` as the project root -> (exit code, JSON envelope)."""
    cwd, buf = os.getcwd(), io.StringIO()
    os.chdir(root)
    try:
        with contextlib.redirect_stdout(buf):
            code = cli_mod.main(["--json", *args])
    finally:
        os.chdir(cwd)
    return code, json.loads(buf.getvalue())


def _write_project(root: pathlib.Path) -> pathlib.Path:
    """A minimal, offline, git-init'd evalops project whose ledger is the in-memory fixture plugin."""
    (root / "evalops.yaml").write_text(CONFIG)
    shutil.copy(TESTS_DIR / "fixtures" / "fake_app.py", root / "fake_app.py")
    shutil.copy(FIXTURES / "memory_ledger.py", root / "memory_ledger.py")
    shutil.copytree(REPO / "prompts", root / "prompts")
    (root / "datasets").mkdir()
    shutil.copy(REPO / "datasets" / "smoke.jsonl", root / "datasets" / "smoke.jsonl")
    (root / ".gitignore").write_text(".evalops/\n__pycache__/\n")  # generated: editing locks must not dirty git
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "formats@example.test")
    _git(root, "config", "user.name", "formats")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    return root


def _memory_ledger(root: pathlib.Path):
    """Import the fixture plugin the same way the registry will (one module object per process)."""
    if "memory_ledger" in sys.modules:
        return sys.modules["memory_ledger"]
    spec = importlib.util.spec_from_file_location("memory_ledger", root / "memory_ledger.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["memory_ledger"] = module
    spec.loader.exec_module(module)
    return module


def _v0_dataset_fingerprint(records: list) -> str:
    """The pre-WP0.4 dataset hash, copied literally: the new implementation must not move it."""
    rows = sorted(
        canonical(
            {
                "inputs": r.get("inputs"),
                "expectations": r.get("expectations") or None,
                **{k: r[k] for k in ("outputs", "trace") if r.get(k) is not None},
            }
        )
        for r in records
    )
    return sha256("\n".join(rows))


# ---------------- (a) v0 files still load ----------------
def test_v0_prompts_lock_loads_and_is_in_sync():
    root = _tmpdir()
    try:
        lock = json.loads((V0 / "prompts.lock.json").read_text())
        cfg = _lock_cfg(root, "prompts", lock)
        assert locks.read(cfg, "prompts") == lock  # read as stored, no _meta added
        assert locks.entries(locks.read(cfg, "prompts")).keys() == lock.keys()
        assert prompts.diff(cfg) == ([], [])  # repo prompts match the v0 lock
        assert prompts.sync(cfg, None, check=True)["changed"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v0_datasets_lock_loads_and_hashes_are_unchanged():
    root = _tmpdir()
    try:
        lock = json.loads((V0 / "datasets.lock.json").read_text())
        cfg = _cfg(
            root,
            {
                "project": "formats",
                "app": {},
                "datasets": {"smoke": {"frozen": True}, "golden_regression": {"frozen": True}},
                "paths": {"prompts": str(REPO / "prompts"), "datasets": str(REPO / "datasets")},
            },
        )
        locks.path(cfg, "datasets").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        assert datasets.status(cfg) == ([], [], [])  # v0 lock still compares clean
        smoke = datasets.read_records(REPO / "datasets" / "smoke.jsonl")
        assert datasets.fingerprint(smoke) == lock["smoke"]["fingerprint"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v0_rows_and_last_eval_load():
    rows = run.rows_from_artifact((V0 / "rows.jsonl").read_text())
    assert [r.row_id for r in rows] == ["00000", "00001", "00002"]
    assert rows[0].scores == {"facts": 1.0, "helpful": 1.0} and rows[0].trace is None

    last = formats.read_versioned(V0 / "last_eval.json")
    assert formats.check_version(last, V0 / "last_eval.json") == 0  # v0 = unversioned
    assert last["group"] and last["app_version"] and last["cost"]["actual_usd"] > 0


def test_v0_cassette_loads_with_the_old_fingerprint():
    cas = Cassette(V0 / "cassette.jsonl", "replay")
    assert len(cas.entries) == 3 and META_KEY not in cas.entries
    assert cas.fingerprint() == V0_CASSETTE_FP


# ---------------- (b) v1 round-trip for all five formats ----------------
def test_v1_locks_roundtrip():
    root = _tmpdir()
    try:
        cfg = _cfg(root)
        data = {"support_system": _entry("support_system")}
        locks.write(cfg, "prompts", data)
        raw = json.loads(locks.path(cfg, "prompts").read_text())
        assert raw[META_KEY]["schema_version"] == FORMAT_VERSION
        assert raw[META_KEY]["evalops_version"] == evalops.__version__
        assert raw[META_KEY]["created_at"]
        assert {k: v for k, v in raw.items() if k != META_KEY} == data  # flat name->entry mapping
        assert locks.read(cfg, "prompts")[META_KEY]["schema_version"] == FORMAT_VERSION
        assert locks.entries(locks.read(cfg, "prompts")) == data

        ds = {
            "smoke": {
                "plugin": "git",
                "physical": "smoke__ba92db99",
                "fingerprint": "ba92db99",
                "rows": 1,
                "git_sha": "deadbeef",
            }
        }
        locks.write(cfg, "datasets", ds)
        raw = json.loads(locks.path(cfg, "datasets").read_text())
        assert raw[META_KEY]["schema_version"] == FORMAT_VERSION
        assert locks.entries(locks.read(cfg, "datasets")) == ds
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v1_cassette_roundtrip():
    root = _tmpdir()
    try:
        path = root / "cassettes" / "smoke.jsonl"

        @replayable("lookup_order")
        def lookup_order(order_id: str) -> dict:
            return {"status": "shipped", "order_id": order_id}

        cas = Cassette(path, "record")
        with use(cas):
            assert lookup_order("A-1") == {"status": "shipped", "order_id": "A-1"}
        cas.save()
        lines = path.read_text().splitlines()
        assert json.loads(lines[0])[META_KEY]["schema_version"] == FORMAT_VERSION
        assert len([ln for ln in lines if ln.strip()]) == 2  # _meta + one entry

        reloaded = Cassette(path, "replay")
        assert reloaded.entries.keys() == cas.entries.keys()
        assert reloaded.fingerprint() == cas.fingerprint()
        with use(reloaded):
            assert lookup_order("A-1")["status"] == "shipped"  # replay still resolves
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_v1_rows_and_last_eval_roundtrip():
    root = _tmpdir()
    try:
        row = RowResult(
            row_id="00000",
            inputs={"question": "q"},
            outputs="a",
            expectations={"expected_facts": ["a"]},
            scores={"facts": 1.0},
            trace_metrics={"tool_calls": 0},
        )
        line = json.dumps(formats.stamp(run.row_to_json(row, 4000)), default=str)
        doc = json.loads(line)
        assert doc["schema_version"] == FORMAT_VERSION
        assert doc["evalops_version"] == evalops.__version__
        back = run.rows_from_artifact(line)
        assert len(back) == 1
        assert (back[0].row_id, back[0].outputs, back[0].scores) == ("00000", "a", {"facts": 1.0})

        written = formats.write_versioned(root / "last_eval.json", {"group": "g1", "app_version": "v"})
        assert written["schema_version"] == FORMAT_VERSION
        assert formats.read_versioned(root / "last_eval.json") == written
        assert json.loads((root / "last_eval.json").read_text())["evalops_version"] == evalops.__version__
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------- (c) `_meta` is ignored by every reader ----------------
def test_meta_never_read_as_a_prompt():
    root = _tmpdir()
    try:
        (root / "prompts").mkdir()
        (root / "prompts" / "support_system.txt").write_text("X {{question}}\n")
        entry = {"plugin": "git", "physical": "support_system", "version": "1", "sha256": sha256("X {{question}}\n")}
        v0_cfg = _cfg(root, {"project": "formats", "app": {}, "paths": {"prompts": str(root / "prompts")}})
        locks.path(v0_cfg, "prompts").write_text(json.dumps({"support_system": entry}) + "\n")
        v1_cfg = _cfg(root, {"project": "formats", "app": {}, "paths": {"prompts": str(root / "prompts")}})
        locks.write(v1_cfg, "prompts", {"support_system": entry})

        assert prompts.diff(v0_cfg) == ([], [])
        assert prompts.diff(v1_cfg) == ([], [])  # no "_meta" in changed/orphaned
        assert set(locks.entries(locks.read(v1_cfg, "prompts"))) == {"support_system"}

        class Store:
            name = "git"

            def load(self, physical, version):
                return type("Loaded", (), {"template": "X {{question}}", "version": version})()

        class Reg:
            def get(self, role):
                return Store()

        loaded, lock = prompts.load_locked(v1_cfg, Reg())
        assert set(loaded) == {"support_system"} and set(lock) == {"support_system"}
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_meta_never_read_as_a_dataset():
    root = _tmpdir()
    try:
        (root / "datasets").mkdir()
        (root / "datasets" / "smoke.jsonl").write_text(json.dumps({"inputs": {"q": "x"}}) + "\n")
        records = datasets.read_records(root / "datasets" / "smoke.jsonl")
        entry = {
            "plugin": "git",
            "physical": "smoke__x",
            "fingerprint": datasets.fingerprint(records),
            "rows": 1,
            "git_sha": "deadbeef",
        }
        data = {
            "project": "formats",
            "app": {},
            "datasets": {"smoke": {}},
            "paths": {"datasets": str(root / "datasets")},
        }
        cfg = _lock_cfg(root, "datasets", {"smoke": entry, META_KEY: formats.meta()})
        assert datasets.status(cfg) == ([], [], [])
        assert set(locks.entries(locks.read(cfg, "datasets"))) == {"smoke"}
        err = _raises(lambda: datasets.load(_cfg(root, data), None, META_KEY, verify=False))
        assert "not synced" in str(err)  # never a dataset entry
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_meta_never_read_as_a_cassette_entry():
    root = _tmpdir()
    try:
        lines = (V0 / "cassette.jsonl").read_text().splitlines()
        path = root / "cassette.jsonl"
        path.write_text(json.dumps({META_KEY: formats.meta()}) + "\n" + "\n".join(lines) + "\n")
        cas = Cassette(path, "replay")
        assert set(cas.entries) == {json.loads(ln)["key"] for ln in lines}
        assert META_KEY not in cas.entries and len(cas.entries) == 3
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------- (d) fingerprint invariance ----------------
def test_cassette_fingerprint_is_semantic_only():
    root = _tmpdir()
    try:
        entries = (V0 / "cassette.jsonl").read_text().splitlines()
        assert Cassette(V0 / "cassette.jsonl", "replay").fingerprint() == V0_CASSETTE_FP

        v1 = root / "v1.jsonl"
        v1.write_text(json.dumps({META_KEY: formats.meta()}) + "\n" + "\n".join(entries) + "\n")
        assert Cassette(v1, "replay").fingerprint() == V0_CASSETTE_FP  # identical entries, new meta

        other_meta = root / "v1-other-meta.jsonl"
        other_meta.write_text(
            json.dumps(
                {
                    META_KEY: {
                        "schema_version": FORMAT_VERSION,
                        "evalops_version": "9.9.9",
                        "created_at": "2000-01-01T00:00:00Z",
                    }
                }
            )
            + "\n"
            + "\n".join(entries)
            + "\n"
        )
        assert Cassette(other_meta, "replay").fingerprint() == V0_CASSETTE_FP

        decorated = [json.loads(ln) for ln in entries]
        decorated[0]["recorded_at"] = "2026-01-01T00:00:00Z"  # per-entry metadata
        decorated[0]["latency_ms"] = 12
        extra = root / "decorated.jsonl"
        extra.write_text("\n".join(json.dumps(e, sort_keys=True) for e in decorated) + "\n")
        assert Cassette(extra, "replay").fingerprint() == V0_CASSETTE_FP

        changed = [dict(e) for e in decorated]
        changed[1]["response"] = {"status": "cancelled"}  # semantic change must move it
        changed_path = root / "changed.jsonl"
        changed_path.write_text("\n".join(json.dumps(e, sort_keys=True) for e in changed) + "\n")
        assert Cassette(changed_path, "replay").fingerprint() != V0_CASSETTE_FP
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_dataset_fingerprint_is_unmoved_and_metadata_free():
    records = datasets.read_records(REPO / "datasets" / "smoke.jsonl")
    pinned = json.loads((V0 / "datasets.lock.json").read_text())["smoke"]["fingerprint"]
    assert datasets.fingerprint(records) == _v0_dataset_fingerprint(records) == pinned
    decorated = [
        {**r, "row_id": "00000", "schema_version": FORMAT_VERSION, "_meta": formats.meta(), "dataset_id": "backend/1"}
        for r in records
    ]
    assert datasets.fingerprint(decorated) == pinned  # metadata never hashed
    assert datasets.fingerprint(records + [{"inputs": {"question": "new"}}]) != pinned


# ---------------- (e) future schema_version -> FormatError / exit 7 ----------------
def test_future_version_in_a_lock_is_exit_7_naming_the_file():
    root = _tmpdir()
    try:
        cfg = _lock_cfg(
            root,
            "prompts",
            {"support_system": _entry("support_system"), META_KEY: {**formats.meta(), "schema_version": 2}},
        )
        path = locks.path(cfg, "prompts")
        err = _raises(lambda: locks.read(cfg, "prompts"))
        assert isinstance(err, FormatError) and err.code == EXIT_CONTRACT == 7
        assert str(path) in str(err) and "2" in str(err)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_future_version_in_a_cassette_is_exit_7_naming_the_file():
    root = _tmpdir()
    try:
        path = root / "cassette.jsonl"
        path.write_text(
            json.dumps({META_KEY: {**formats.meta(), "schema_version": 2}}) + "\n" + (V0 / "cassette.jsonl").read_text()
        )
        err = _raises(lambda: Cassette(path, "replay"))
        assert isinstance(err, FormatError) and err.code == 7
        assert str(path) in str(err) and "2" in str(err)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_future_version_in_rows_is_exit_7_naming_the_artifact():
    line = json.dumps({**json.loads((V0 / "rows.jsonl").read_text().splitlines()[0]), "schema_version": 2})
    err = _raises(lambda: run.rows_from_artifact(line))
    assert isinstance(err, FormatError) and err.code == 7
    assert "evalops/rows.jsonl" in str(err) and "2" in str(err)


def test_future_version_in_last_eval_is_exit_7_naming_the_file():
    root = _tmpdir()
    try:
        path = root / "last_eval.json"
        path.write_text(json.dumps({**json.loads((V0 / "last_eval.json").read_text()), "schema_version": 2}))
        err = _raises(lambda: formats.read_versioned(path))
        assert isinstance(err, FormatError) and err.code == 7
        assert str(path) in str(err) and "2" in str(err)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_non_object_artifact_is_a_contract_mismatch_never_v0():
    """A JSONL/JSON payload that is not an object cannot be a versioned artifact of any version."""
    root = _tmpdir()
    try:
        path = root / "cassette.jsonl"
        path.write_text('["not", "an", "entry"]\n')  # a JSON array is not a cassette entry
        err = _raises(lambda: Cassette(path, "replay"))
        assert isinstance(err, FormatError) and err.code == EXIT_CONTRACT == 7
        assert str(path) in str(err)
        assert isinstance(_raises(lambda: formats.check_version([1, 2], "rows.jsonl")), FormatError)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_future_version_in_cost_estimate_is_exit_7_naming_the_artifact():
    """`evalops/cost_estimate.json` is versioned on write and checked on every read."""
    estimate = {"rows": 1, "total_usd": 0.42}
    assert cost_mod.estimate_from_artifact(json.dumps(formats.stamp(estimate)))["total_usd"] == 0.42
    assert cost_mod.estimate_from_artifact(json.dumps(estimate))["rows"] == 1  # v0 (unversioned) still reads

    err = _raises(lambda: cost_mod.estimate_from_artifact(json.dumps({**estimate, "schema_version": 2})))
    assert isinstance(err, FormatError) and err.code == EXIT_CONTRACT == 7
    assert cost_mod.ESTIMATE_ARTIFACT in str(err) and "2" in str(err)
    # not an object at all -> contract mismatch, never silently read as v0
    assert isinstance(_raises(lambda: cost_mod.estimate_from_artifact("[1, 2]")), FormatError)

    class FakeLedger:
        """The baseline lookup `cost.history` uses: one baseline run, optional estimate artifact."""

        def __init__(self, text: str | None) -> None:
            self.text = text

        def find_runs(self, tags=None, since_ms=None, limit=None) -> list[RunRecord]:
            return [RunRecord("r1", {"evalops.baseline": "true"}, {}, {"gen_input_tokens_per_row": 10.0}, 1)]

        def get_artifact(self, run_id: str, name: str) -> str:
            if self.text is None:
                raise KeyError(name)  # a baseline recorded before the artifact existed
            return self.text

    assert cost_mod.history(FakeLedger(None)) == {"gen_input_tokens_per_row": 10.0}
    assert cost_mod.history(FakeLedger(json.dumps(formats.stamp(estimate))))["gen_input_tokens_per_row"] == 10.0
    hist_err = _raises(lambda: cost_mod.history(FakeLedger(json.dumps({**estimate, "schema_version": 2}))))
    assert isinstance(hist_err, FormatError) and hist_err.code == 7


def test_traces_command_exits_7_on_a_future_version_last_eval():
    """Command path: `evalops traces` reads last_eval.json through the versioned reader."""
    root = _tmpdir()
    try:
        _write_project(root)
        _memory_ledger(root)
        out_dir = root / ".evalops" / "out"
        out_dir.mkdir(parents=True)
        (out_dir / "last_eval.json").write_text(json.dumps({"group": "g1", "schema_version": 2}))

        code, envelope = _run_cli(root, "traces")
        assert code == EXIT_CONTRACT == 7, (code, envelope)
        assert envelope["ok"] is False and "last_eval.json" in envelope["errors"][0]["message"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_gate_command_exits_7_on_a_future_version_rows_artifact():
    """Command path: a row-level format mismatch is exit 7, never a silently skipped repeat."""
    root = _tmpdir()
    try:
        _write_project(root)
        # row-level checks (CI lower bound) are what make the gate read stored rows
        (root / "evalops.yaml").write_text(CONFIG.replace("min: 0.5}", "min: 0.5, min_ci_lower: 0.1}"))
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "row rules")

        ledger_mod = _memory_ledger(root)
        ledger_mod.MemoryLedger.runs.clear()
        ledger_mod.MemoryLedger.artifacts.clear()
        cfg = config_mod.load(str(root / "evalops.yaml"))
        from evalops.registry import Registry

        reg = Registry(cfg)
        prompts.sync(cfg, reg)
        datasets.sync(cfg, reg)
        out = run.evaluate(cfg, "smoke", "pr")
        run_id = out["run_ids"][0]
        assert ledger_mod.MemoryLedger.artifacts[run_id]["evalops/rows.jsonl"].strip()

        future = [
            json.dumps({**json.loads(line), "schema_version": 2})
            for line in ledger_mod.MemoryLedger.artifacts[run_id]["evalops/rows.jsonl"].splitlines()
            if line.strip()
        ]
        ledger_mod.MemoryLedger.artifacts[run_id]["evalops/rows.jsonl"] = "\n".join(future) + "\n"

        code, envelope = _run_cli(root, "gate", "--group", out["group"])
        assert code == EXIT_CONTRACT == 7, (code, envelope)
        assert envelope["ok"] is False and "evalops/rows.jsonl" in envelope["errors"][0]["message"]
        # no gate verdict was recorded for the unreadable group
        assert not ledger_mod.MemoryLedger.runs[run_id]["tags"].get("evalops.gate")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------- (f) manifest covers the artifacts actually written ----------------
def test_manifest_hashes_match_written_artifacts():
    root = _tmpdir()
    try:
        _write_project(root)
        ledger_mod = _memory_ledger(root)
        ledger_mod.MemoryLedger.runs.clear()
        ledger_mod.MemoryLedger.artifacts.clear()

        cfg = config_mod.load(str(root / "evalops.yaml"))
        from evalops.registry import Registry

        reg = Registry(cfg)
        prompts.sync(cfg, reg)
        datasets.sync(cfg, reg)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "locks", "--allow-empty")

        out = run.evaluate(cfg, "smoke", "pr")
        run_id = out["run_ids"][0]
        written = ledger_mod.MemoryLedger.artifacts[run_id]
        manifest = json.loads(written["evalops/manifest.json"])

        expected = {
            "evalops/rows.jsonl",
            "evalops/cost_estimate.json",
            "evalops/prompts.lock.json",
            "evalops/datasets.lock.json",
        }
        assert set(manifest["artifacts"]) == expected, manifest["artifacts"]
        assert manifest["schema_version"] == FORMAT_VERSION
        assert manifest["evalops_version"] == evalops.__version__
        for name in expected:
            assert manifest["artifacts"][name] == hashlib.sha256(written[name].encode()).hexdigest(), name
        assert "evalops/manifest.json" not in manifest["artifacts"]  # never hashes itself

        # every artifact in the manifest is versioned, including the cost estimate
        from evalops.cost import ESTIMATE_ARTIFACT

        estimate = cost_mod.estimate_from_artifact(written[ESTIMATE_ARTIFACT])
        assert estimate["schema_version"] == FORMAT_VERSION
        assert estimate["evalops_version"] == evalops.__version__
        assert estimate["rows"] == 1 and "total_usd" in estimate

        rows = [json.loads(ln) for ln in written["evalops/rows.jsonl"].splitlines() if ln.strip()]
        assert rows and all(r["schema_version"] == FORMAT_VERSION for r in rows)
        assert all(r["evalops_version"] == evalops.__version__ for r in rows)
        assert len(run.rows_from_artifact(written["evalops/rows.jsonl"])) == len(rows)

        for artifact in ("evalops/prompts.lock.json", "evalops/datasets.lock.json"):
            assert json.loads(written[artifact])[META_KEY]["schema_version"] == FORMAT_VERSION

        params = ledger_mod.MemoryLedger.runs[run_id]["params"]
        assert params["evalops_version"] == evalops.__version__
        assert params["format_version"] == FORMAT_VERSION

        last = formats.read_versioned(root / ".evalops" / "out" / "last_eval.json")
        assert last["schema_version"] == FORMAT_VERSION and last["group"] == out["group"]

        # R1: touching only lock metadata must not change config_hash / app_version
        lock_path = locks.path(cfg, "prompts")
        doc = json.loads(lock_path.read_text())
        doc[META_KEY]["created_at"] = "2000-01-01T00:00:00Z"
        doc[META_KEY]["evalops_version"] = "0.0.1-fixture"
        lock_path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
        assert not subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True
        ).stdout.strip()  # .evalops/ is generated

        again = run.evaluate(cfg, "smoke", "pr")
        before = ledger_mod.MemoryLedger.runs[run_id]["params"]
        after = ledger_mod.MemoryLedger.runs[again["run_ids"][0]]["params"]
        assert before["config_hash"] == after["config_hash"]
        assert before["app_version"] == after["app_version"] == out["app_version"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------- (g) replay: the cassette fingerprint is in the hash, its `_meta` is not ----------
def test_replay_cassette_metadata_never_changes_identity():
    """R1 in the path that matters: replay reads a recorded cassette, and only semantic entries hash.

    Three mutations are covered, each touching ONLY `_meta`:

    1. a committed cassette `_meta` edit: fingerprint + `config_hash` unchanged, and `app_version`
       differs only in its git-sha segment (committing is what moves HEAD);
    2. an uncommitted cassette `_meta` edit at the same HEAD: every identity value byte-identical,
       and the run leaves the tree with no delta of its own;
    3. a lock `_meta` edit (`.evalops/` is generated): identical values, clean tree.
    """
    root = _tmpdir()
    try:
        _write_project(root)
        (root / "tool_app.py").write_text(TOOL_APP)
        text = (root / "evalops.yaml").read_text().replace("fake_app:", "tool_app:")  # app hooks only
        (root / "evalops.yaml").write_text(text)
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "tool app")

        ledger_mod = _memory_ledger(root)
        ledger_mod.MemoryLedger.runs.clear()
        ledger_mod.MemoryLedger.artifacts.clear()
        cfg = config_mod.load(str(root / "evalops.yaml"))
        from evalops.registry import Registry

        reg = Registry(cfg)
        prompts.sync(cfg, reg)
        datasets.sync(cfg, reg)
        cassette = cfg.path("cassettes") / "smoke.jsonl"

        run.evaluate(cfg, "smoke", "pr", tool_mode="record")  # records the real tool call
        assert cassette.exists()
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "record cassette")
        fingerprint = Cassette(cassette, "replay").fingerprint()
        assert fingerprint != "none"

        first = run.evaluate(cfg, "smoke", "pr", tool_mode="replay")
        first_params = ledger_mod.MemoryLedger.runs[first["run_ids"][0]]["params"]
        assert first_params["tool_mode"] == "replay"
        assert first_params["tool_cassette"] == fingerprint  # the cassette really is in the hash
        assert first["runs"][0]["errors"] == 0  # replay resolved every row from the recording
        assert _git_status(root) == []

        # 1. committed `_meta` edit -> tree clean, semantic identity unchanged
        lines = cassette.read_text().splitlines()
        meta = json.loads(lines[0])[META_KEY]
        meta.update({"created_at": "2000-01-01T00:00:00Z", "evalops_version": "0.0.1-fixture"})
        cassette.write_text("\n".join([json.dumps({META_KEY: meta}, sort_keys=True), *lines[1:]]) + "\n")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "touch cassette meta")
        assert _git_status(root) == []

        second = run.evaluate(cfg, "smoke", "pr", tool_mode="replay")
        second_params = ledger_mod.MemoryLedger.runs[second["run_ids"][0]]["params"]
        assert Cassette(cassette, "replay").fingerprint() == fingerprint
        assert second_params["tool_cassette"] == fingerprint
        assert second_params["config_hash"] == first_params["config_hash"]
        project_1, sha_1, hash_1 = first["app_version"].rsplit("-", 2)
        project_2, sha_2, hash_2 = second["app_version"].rsplit("-", 2)
        assert (project_1, hash_1) == (project_2, hash_2) == ("formats", first_params["config_hash"])
        assert sha_1 != sha_2  # the commit moved HEAD; that segment is the only difference
        assert _git_status(root) == []  # replay never rewrites the cassette

        # 2. uncommitted `_meta` edit at the same HEAD -> every identity value is byte-identical
        lines = cassette.read_text().splitlines()
        meta = json.loads(lines[0])[META_KEY]
        meta["created_at"] = "1999-12-31T00:00:00Z"
        cassette.write_text("\n".join([json.dumps({META_KEY: meta}, sort_keys=True), *lines[1:]]) + "\n")
        third = run.evaluate(cfg, "smoke", "pr", tool_mode="replay", allow_dirty=True)
        third_params = ledger_mod.MemoryLedger.runs[third["run_ids"][0]]["params"]
        assert Cassette(cassette, "replay").fingerprint() == fingerprint
        assert third_params["config_hash"] == first_params["config_hash"]
        assert third["app_version"] == second["app_version"]  # unchanged by metadata
        assert _git_status(root) == ["M cassettes/smoke.jsonl"]  # the only delta is the deliberate edit
        _git(root, "checkout", "--", "cassettes/smoke.jsonl")
        assert _git_status(root) == []

        # 3. lock `_meta` edit: `.evalops/` is generated, so the tree stays clean
        lock_path = locks.path(cfg, "prompts")
        lock = json.loads(lock_path.read_text())
        lock[META_KEY].update({"created_at": "2000-01-01T00:00:00Z", "evalops_version": "0.0.1-fixture"})
        lock_path.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n")
        fourth = run.evaluate(cfg, "smoke", "pr", tool_mode="replay")
        fourth_params = ledger_mod.MemoryLedger.runs[fourth["run_ids"][0]]["params"]
        assert Cassette(cassette, "replay").fingerprint() == fingerprint
        assert fourth_params["config_hash"] == first_params["config_hash"]
        assert fourth["app_version"] == second["app_version"]
        assert _git_status(root) == []
    finally:
        shutil.rmtree(root, ignore_errors=True)
