"""Minimal runner for environments without pytest: python tests/run_without_pytest.py"""
import pathlib
import sys
import traceback

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_core  # noqa: E402
import test_e2e_agent  # noqa: E402
import test_e2e_local  # noqa: E402
import test_traces  # noqa: E402

failed = 0
for mod in (test_core, test_traces, test_e2e_local, test_e2e_agent):
    for name in [n for n in dir(mod) if n.startswith("test_")]:
        try:
            getattr(mod, name)()
            print(f"PASS {mod.__name__}.{name}")
        except Exception:  # noqa: BLE001
            failed += 1
            print(f"FAIL {mod.__name__}.{name}")
            traceback.print_exc()
sys.exit(1 if failed else 0)
