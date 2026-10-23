"""Minimal dependency-free runner: ``python tests/run_without_pytest.py``.

Discovers ``test_*.py`` under ``tests/unit`` and ``tests/integration`` recursively, imports each
file by path (so it works from any cwd and needs no pytest), calls every module-level ``test_*``
function, and prints one PASS/FAIL line per test. Exits nonzero if any test fails.

Tests must be callable with no arguments. A test that needs pytest fixtures is reported as a FAIL
rather than silently skipped: this runner is the offline fallback, not a second test framework.
"""

from __future__ import annotations

import importlib.util
import inspect
import pathlib
import sys
import traceback
from collections.abc import Callable
from typing import Any

sys.dont_write_bytecode = True

TESTS_DIR = pathlib.Path(__file__).resolve().parent
DISCOVERY_ROOTS = (TESTS_DIR / "unit", TESTS_DIR / "integration")


def discover() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in DISCOVERY_ROOTS:
        if root.is_dir():
            files.extend(p for p in sorted(root.rglob("test_*.py")) if "__pycache__" not in p.parts)
    return files


def import_module(path: pathlib.Path):
    rel = path.relative_to(TESTS_DIR).with_suffix("")
    name = "evalops_tests_" + "_".join(rel.parts)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_functions(module: Any) -> list[tuple[str, Callable[[], Any]]]:
    found: list[tuple[str, Callable[[], Any]]] = []
    for name, obj in vars(module).items():
        if name.startswith("test_") and inspect.isfunction(obj) and obj.__module__ == module.__name__:
            found.append((name, obj))
    return sorted(found)


def main() -> int:
    files = discover()
    if not files:
        print(f"FAIL no test_*.py found under {', '.join(str(p) for p in DISCOVERY_ROOTS)}")
        return 1

    failed = total = 0
    for path in files:
        rel = path.relative_to(TESTS_DIR)
        try:
            module = import_module(path)
        except Exception:  # noqa: BLE001
            failed += 1
            total += 1
            print(f"FAIL {rel} (import error)")
            traceback.print_exc()
            continue
        for name, fn in test_functions(module):
            total += 1
            try:
                if inspect.signature(fn).parameters:
                    required = [
                        p
                        for p in inspect.signature(fn).parameters.values()
                        if p.default is inspect.Parameter.empty
                        and p.kind in (p.POSITIONAL_OR_KEYWORD, p.POSITIONAL_ONLY)
                    ]
                    if required:
                        raise AssertionError(
                            f"{name} requires arguments {[p.name for p in required]}; "
                            "the no-pytest runner only supports zero-argument tests"
                        )
                fn()
                print(f"PASS {rel}::{name}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {rel}::{name}")
                traceback.print_exc()

    print(f"\n{total - failed}/{total} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
