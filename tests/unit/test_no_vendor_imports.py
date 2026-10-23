"""Guard for decision D4: the evalops core never imports a vendor SDK at module level.

The offline suite must run with only `pyyaml` (+ dev tools) installed. Vendor plugins live in
``evalops/plugins`` and trace adapters in ``evalops/traces``, and both are allowed to import their
SDKs. Everywhere else, a vendor import is allowed only as real graceful degradation: inside a
function body, or inside a ``try`` that catches ``ImportError``/``ModuleNotFoundError`` and whose
handlers contain no ``raise`` at all and do nothing that can raise again (only reporting calls
rooted at ``warnings``/``logging``, plain assignments, and ``pass``). A bare ``except:`` and
``except Exception`` do not count.

The scanner is exercised on planted violations below, so a broken scanner cannot pass silently.

Residual limits (documented, not bugs to rediscover):

- Dynamic imports are **not** detected: ``importlib.import_module("mlflow")``, ``__import__``,
  ``exec``/``eval`` and anything assembled at runtime are outside the scanner's scope. It guards
  static, module-level vendor imports, which is the shape that silently breaks the offline suite.
- Function-local imports stay exempt by design: they are the intended lazy-import pattern (the
  caller decides which extras to install), so the scanner cannot distinguish a lazy import from a
  mandatory one.
- Call screening is **root-name only**: ``warnings.warn``/``logging.*`` are allowed by their
  leftmost name. A local alias (``logger = logging.getLogger(...)`` then ``logger.warning(...)``)
  is therefore a violation, and a safe-looking alias cannot smuggle an unsafe call.
- Subtrees ``evalops/plugins`` and ``evalops/traces`` are exempt because they *are* the adapters;
  the guard is only meaningful for the rest of the package.
"""

from __future__ import annotations

import ast
import pathlib
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
CORE_ROOT = REPO_ROOT / "src" / "evalops"

# Package subtrees allowed to import their vendor SDK (they ARE the adapter).
EXEMPT_TOP_LEVEL = {"plugins", "traces"}

# Decision D4: pydantic is allowed in core; these are not. Prefix matching covers
# langchain_core/langchain_openai/openai_agents/opentelemetry_sdk/sqlalchemy_* etc.
VENDOR_ROOTS = (
    "mlflow",
    "langfuse",
    "langsmith",
    "logfire",
    "litellm",
    "langchain",
    "openai",
    "pydantic_ai",
    "opentelemetry",
    "databricks",
    "sqlalchemy",
)


@dataclass(frozen=True)
class Violation:
    path: str
    line: int
    module: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: imports vendor module '{self.module}'"


def is_vendor_module(module: str) -> bool:
    """True for a top-level import of a vendor SDK (or one of its submodules)."""
    top = module.split(".", 1)[0]
    return any(top == root or top.startswith(root) for root in VENDOR_ROOTS)


def _caught_names(expr: ast.expr) -> Iterator[str]:
    if isinstance(expr, ast.Tuple):
        for element in expr.elts:
            yield from _caught_names(element)
    elif isinstance(expr, ast.Name):
        yield expr.id
    elif isinstance(expr, ast.Attribute):
        yield expr.attr


IMPORT_ERROR_NAMES = frozenset({"ImportError", "ModuleNotFoundError"})

# WP0.4d: a degradation handler must not run code that can raise, so the only calls it may make
# are the ones whose whole purpose is to report. Membership is tested against the *leftmost* name
# of the call target, so `warnings.warn(...)`, `logging.warning(...)` and
# `logging.getLogger(...).warning(...)` are safe while `contextlib.suppress(...)`, `helper()` and a
# local alias like `logger.warning(...)` are not: a local name is not provably `logging`.
SAFE_CALL_ROOTS = frozenset({"warnings", "logging"})


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    """True when the handler's ``except`` clause names ``ImportError``/``ModuleNotFoundError``."""
    if handler.type is None:  # bare ``except:`` also catches KeyboardInterrupt/SystemExit
        return False
    return bool(set(_caught_names(handler.type)) & IMPORT_ERROR_NAMES)


def _handler_raises(handler: ast.ExceptHandler) -> bool:
    """True when the handler body contains any ``raise``: bare, ``raise X`` or ``raise X from Y``.

    Deliberately syntactic and conservative: the search also descends into nested function and
    class bodies, so a ``def cleanup(): raise`` inside the handler still disqualifies the fallback.
    """
    return any(isinstance(node, ast.Raise) for stmt in handler.body for node in ast.walk(stmt))


def _call_root(func: ast.expr) -> str | None:
    """Leftmost name of a call target: ``logging`` for ``logging.getLogger(x).warning(...)``.

    Returns ``None`` only when the target is not rooted at a name at all (``(lambda: f)()``),
    which the allowlist rejects too. No interprocedural reasoning: the *root* is all we look at.
    """
    while True:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, (ast.Attribute, ast.Subscript)):
            func = func.value
        elif isinstance(func, ast.Call):
            func = func.func
        else:
            return None


def _handler_calls_unsafe_code(handler: ast.ExceptHandler) -> bool:
    """True when the handler does something that may raise, so it is not a plain fallback.

    A call to an external helper proves nothing about that helper: it may itself raise, which turns
    an "optional" import back into a mandatory one (``except ImportError: fallback()``). The rule is
    therefore an allowlist of calls, not a blocklist: every ``Call`` in the handler must be rooted at
    ``warnings``/``logging``. ``exec``/``eval``, ``contextlib.suppress(...)`` and a call hidden in a
    ``lambda``/``def`` all fall out of that, and ``import``/``with`` statements are rejected outright
    because both can fail on their own. Like ``_handler_raises`` the walk descends into nested
    bodies, so a rewritten handler cannot hide a call.
    """
    for stmt in handler.body:
        for node in ast.walk(stmt):
            if isinstance(node, (ast.Import, ast.ImportFrom, ast.With, ast.AsyncWith)):
                return True
            if isinstance(node, ast.Call) and _call_root(node.func) not in SAFE_CALL_ROOTS:
                return True
    return False


def _handler_is_acceptable(handler: ast.ExceptHandler) -> bool:
    """A handler is a real fallback only if it neither re-raises nor runs fallible code."""
    return not _handler_raises(handler) and not _handler_calls_unsafe_code(handler)


def _try_is_graceful_degradation(node: ast.Try) -> bool:
    """Graceful degradation = the vendor import is genuinely optional for this ``try``.

    The rule is intentionally simple and static: the ``try`` must have at least one handler
    catching ``ImportError``/``ModuleNotFoundError`` and **every** handler must be acceptable.
    A handler that raises cannot be a fallback -- the import stays mandatory -- and re-raise
    detection by data flow is not worth the complexity: catching an alias
    (``err = e; raise err``), a chain (``raise RuntimeError(...) from e``), a rebind
    (``raise RuntimeError(...) from None``) or a nested ``def`` that raises all mean the same
    thing, so any ``raise`` in any handler of the same ``try`` forfeits the exemption.

    The same holds one step weaker (WP0.4d): a handler that *calls* arbitrary code is not a
    fallback either, because the callee may raise (``except ImportError: fallback()`` makes the
    import mandatory again, just indirectly). Only reporting calls rooted at ``warnings`` or
    ``logging`` -- plus assignments, ``pass`` and other call-free statements -- are accepted; see
    ``_handler_calls_unsafe_code``. Both checks are syntactic and deliberately over-approximate.
    """
    if not any(_handler_catches_import_error(handler) for handler in node.handlers):
        return False
    return all(_handler_is_acceptable(handler) for handler in node.handlers)


class _ImportScanner(ast.NodeVisitor):
    def __init__(self, filename: str) -> None:
        self.filename = filename
        self.violations: list[Violation] = []
        self._function_depth = 0
        self._guarded_depth = 0

    def _check(self, node: ast.Import | ast.ImportFrom, modules: list[str]) -> None:
        if self._function_depth or self._guarded_depth:
            return
        for module in modules:
            if is_vendor_module(module):
                self.violations.append(Violation(self.filename, node.lineno, module))

    def visit_Import(self, node: ast.Import) -> None:
        self._check(node, [alias.name for alias in node.names])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:  # relative import: stays inside evalops
            return
        if node.module:
            self._check(node, [node.module])

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self._function_depth -= 1

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def _visit_try(self, node: ast.Try) -> None:
        guarded = _try_is_graceful_degradation(node)
        if guarded:
            self._guarded_depth += 1
            try:
                for child in node.body:
                    self.visit(child)
            finally:
                self._guarded_depth -= 1
        else:
            for child in node.body:
                self.visit(child)
        # handlers, else and finally are outside the guard
        for handler in node.handlers:
            self.visit(handler)
        for child in node.orelse:
            self.visit(child)
        for child in node.finalbody:
            self.visit(child)

    def visit_Try(self, node: ast.Try) -> None:
        self._visit_try(node)

    # `except*` (PEP 654) has the same body/handler structure.
    def visit_TryStar(self, node: ast.AST) -> None:
        self._visit_try(cast(ast.Try, node))


def scan_source(source: str, filename: str = "<string>") -> list[Violation]:
    scanner = _ImportScanner(filename)
    scanner.visit(ast.parse(source, filename=filename))
    return scanner.violations


def iter_core_files(root: pathlib.Path = CORE_ROOT) -> Iterator[pathlib.Path]:
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root)
        if "__pycache__" in rel.parts:
            continue
        if rel.parts and rel.parts[0] in EXEMPT_TOP_LEVEL:
            continue
        yield path


def scan_tree(root: pathlib.Path = CORE_ROOT) -> list[Violation]:
    violations: list[Violation] = []
    for path in iter_core_files(root):
        violations.extend(scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(root))))
    return violations


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
def test_core_is_vendor_free():
    violations = scan_tree(CORE_ROOT)
    assert not violations, "vendor imports in core:\n" + "\n".join(str(v) for v in violations)


def test_scanner_detects_planted_imports():
    cases = {
        "import mlflow\n": "mlflow",
        "from mlflow.genai import register_prompt\n": "mlflow.genai",
        "import langfuse\n": "langfuse",
        "import langsmith\n": "langsmith",
        "import logfire\n": "logfire",
        "import litellm\n": "litellm",
        "import langchain_core.prompts\n": "langchain_core.prompts",
        "import openai_agents\n": "openai_agents",
        "import pydantic_ai\n": "pydantic_ai",
        "from opentelemetry import trace\n": "opentelemetry",
        "import databricks.sdk\n": "databricks.sdk",
        "import sqlalchemy\n": "sqlalchemy",
    }
    for source, expected in cases.items():
        found = scan_source(source)
        assert found, f"scanner missed planted violation {source!r}"
        assert found[0].module == expected, (source, found[0].module)


def test_scanner_respects_graceful_degradation():
    assert scan_source("def f():\n    import mlflow\n") == []
    assert scan_source("async def f():\n    from langfuse import Langfuse\n") == []
    assert scan_source("try:\n    import mlflow\nexcept ImportError:\n    mlflow = None\n") == []
    assert (
        scan_source(
            "try:\n    from langfuse import Langfuse\nexcept (ImportError, AttributeError):\n    Langfuse = None\n"
        )
        == []
    )
    assert scan_source("try:\n    import logfire\nexcept ModuleNotFoundError:\n    pass\n") == []
    # a conditional import is optional only when the module-level symbol is replaced, not re-raised
    assert scan_source("try:\n    import mlflow\nexcept ImportError:\n    mlflow = None\nmlflow.X = 1\n") == []
    # the inner handler only sets a flag, the outer handler is the genuine ImportError fallback
    assert (
        scan_source(
            "try:\n"
            "    try:\n"
            "        import mlflow\n"
            "    except ValueError:\n"
            "        ok = False\n"
            "except ImportError:\n"
            "    mlflow = None\n"
        )
        == []
    )
    # the inner handler only sets a flag; the outer handler catches a tuple naming ImportError
    assert (
        scan_source(
            "try:\n"
            "    try:\n"
            "        import logfire\n"
            "    except AttributeError:\n"
            "        pass\n"
            "except (ImportError, ModuleNotFoundError):\n"
            "    logfire = None\n"
        )
        == []
    )


def test_scanner_rejects_chained_or_aliased_fallbacks():
    """WP0.4c regression: aliasing or chaining a re-raise must not buy the exemption."""
    probes = {
        "alias": "try:\n    import mlflow\nexcept ImportError as e:\n    err = e\n    raise err\n",
        "chained": 'try:\n    import mlflow\nexcept ImportError as e:\n    raise RuntimeError("missing") from e\n',
        "from_none": 'try:\n    import mlflow\nexcept ImportError as e:\n    raise RuntimeError("missing") from None\n',
        "nested_def": (
            "try:\n    import mlflow\nexcept ImportError:\n    def cleanup():\n        raise\n    cleanup()\n"
        ),
        "inner_raise_outer_broad": (
            "try:\n"
            "    try:\n"
            "        import mlflow\n"
            "    except ImportError:\n"
            "        raise\n"
            "except Exception:\n"
            "    pass\n"
        ),
    }
    for label, source in probes.items():
        found = scan_source(source)
        assert found, f"scanner missed the {label} probe: {source!r}"
        assert found[0].module == "mlflow", (label, found[0].module)


def test_scanner_rejects_fake_degradation_handlers():
    """A handler that re-raises, a bare `except:`, and `except Exception` are not degradations."""
    cases = {
        # re-raises the caught error -> the import is mandatory
        "try:\n    import mlflow\nexcept ImportError:\n    raise\n": "mlflow",
        "try:\n    import mlflow\nexcept ImportError as e:\n    raise e\n": "mlflow",
        "try:\n    import mlflow\nexcept (ImportError, ModuleNotFoundError) as exc:\n    raise exc\n": "mlflow",
        # bare except: hides everything, not degradation
        "try:\n    import logfire\nexcept:\n    pass\n": "logfire",
        # not an import error at all
        "try:\n    import mlflow\nexcept Exception:\n    pass\n": "mlflow",
    }
    for source, expected in cases.items():
        found = scan_source(source)
        assert found, f"scanner granted a bogus exemption to {source!r}"
        assert found[0].module == expected, (source, found[0].module)
    # WP0.4c: the rule is syntactic, so *any* raise in *any* handler forfeits the exemption --
    # a handler that raises cannot be a fallback, whatever it raises and however it is spelled.
    for source in (
        # alias of the caught error
        "try:\n    import mlflow\nexcept ImportError as e:\n    err = e\n    raise err\n",
        # chained raise
        'try:\n    import mlflow\nexcept ImportError as e:\n    raise RuntimeError("missing") from e\n',
        # "from None" is still a raise
        'try:\n    import mlflow\nexcept ImportError as e:\n    raise RuntimeError("missing") from None\n',
        # raising a different exception type
        "try:\n    import mlflow\nexcept ImportError:\n    raise ValueError('x')\n",
        # raise hidden in a nested function/method body inside the handler
        "try:\n    import mlflow\nexcept ImportError:\n    def cleanup():\n        raise\n    cleanup()\n",
        # a nested try whose handler raises, under an outer ImportError handler
        "try:\n    import mlflow\nexcept ImportError:\n    try:\n        pass\n    except Exception:\n        raise\n",
        # the ImportError handler itself is clean, but a sibling handler in the same try raises
        "try:\n    import mlflow\nexcept ImportError:\n    pass\nexcept Exception:\n    raise\n",
    ):
        found = scan_source(source)
        assert found, f"scanner granted a bogus exemption to {source!r}"
        assert found[0].module == "mlflow", (source, found[0].module)


def test_scanner_rejects_handler_that_calls_an_external_helper():
    """WP0.4d: `except ImportError: fallback()` makes the import mandatory again.

    The handler looks like a fallback but hands control to code the scanner cannot see, and that
    code may raise -- so the vendor import is not optional. Anything that is not a reporting call
    disqualifies the handler: a plain call, a method call, a call hidden in a lambda, ``exec``,
    ``eval``, ``contextlib.suppress(...)``, a ``with`` block, an ``import``.
    """
    probes = {
        # (a) the headline defect: merely calling an external helper was treated as safe
        "helper_call": "try:\n    import mlflow\nexcept ImportError:\n    fallback()\n",
        "method_call": "try:\n    import mlflow\nexcept ImportError:\n    state.mark_unavailable()\n",
        "call_in_lambda": "try:\n    import mlflow\nexcept ImportError:\n    f = lambda: fallback()\n",
        "call_in_nested_def": (
            "try:\n    import mlflow\nexcept ImportError:\n    def note():\n        fallback()\n    note()\n"
        ),
        # (e) exec/eval are calls like any other, and the worst of them
        "eval_call": 'try:\n    import mlflow\nexcept ImportError:\n    eval("1")\n',
        "exec_call": 'try:\n    import mlflow\nexcept ImportError:\n    exec("x = 1")\n',
        # not on the allowlist either: suppression, with-blocks and imports can all fail
        "contextlib_suppress": "try:\n    import mlflow\nexcept ImportError:\n    contextlib.suppress(Exception)\n",
        "with_block": 'try:\n    import mlflow\nexcept ImportError:\n    with open("x"):\n        pass\n',
        "import_in_handler": "try:\n    import mlflow\nexcept ImportError:\n    import warnings\n",
    }
    for label, source in probes.items():
        found = scan_source(source)
        assert found, f"scanner treated the {label} handler as a safe fallback: {source!r}"
        assert found[0].module == "mlflow", (label, found[0].module)


def test_scanner_rejects_chained_raise_even_next_to_an_allowlisted_call():
    """(b) regression: the reporting-call allowlist must not buy back the raise rule."""
    probes = {
        "chained_raise": (
            'try:\n    import mlflow\nexcept ImportError as e:\n    raise RuntimeError("missing") from e\n'
        ),
        "report_then_chained_raise": (
            "try:\n    import mlflow\nexcept ImportError as e:\n"
            '    logging.warning("mlflow missing")\n    raise RuntimeError("missing") from e\n'
        ),
    }
    for label, source in probes.items():
        found = scan_source(source)
        assert found, f"scanner treated the {label} handler as a safe fallback: {source!r}"
        assert found[0].module == "mlflow", (label, found[0].module)


def test_scanner_allows_reporting_calls_in_an_import_error_handler():
    """(c) `warnings`/`logging` calls stay exempt: they report, they do not resurrect the import."""
    probes = {
        "warnings_warn": 'try:\n    import mlflow\nexcept ImportError:\n    warnings.warn("mlflow missing")\n',
        "logging_warning": 'try:\n    import mlflow\nexcept ImportError:\n    logging.warning("mlflow missing")\n',
        "logging_getlogger_warning": (
            "try:\n    import logfire\nexcept ImportError:\n"
            '    logging.getLogger(__name__).warning("logfire missing")\n'
        ),
        "report_and_record_flag": (
            "try:\n    import langfuse\nexcept (ImportError, ModuleNotFoundError):\n"
            '    warnings.warn("langfuse missing", stacklevel=2)\n    langfuse = None\n'
        ),
    }
    for label, source in probes.items():
        found = scan_source(source)
        assert found == [], f"scanner rejected the legitimate {label} fallback: {found}"


def test_scanner_allows_call_free_fallback_assignments():
    """(d) a handler that only assigns (no call, no raise) is a plain fallback."""
    for source in (
        "try:\n    import mlflow\nexcept ImportError:\n    mlflow = None\n",
        "try:\n    import mlflow\nexcept ImportError:\n    MLFLOW_AVAILABLE = False\n",
        "try:\n    import mlflow\nexcept ImportError:\n    pass\n",
    ):
        assert scan_source(source) == [], source


def test_scanner_rejects_logger_call_on_a_local_alias_because_root_is_not_logging():
    """(f) documented decision: the allowlist matches the *root* name, not the attribute name.

    ``logger.warning(...)`` looks like reporting, but ``logger`` is a local alias assigned inside
    the handler, so nothing proves it is ``logging``; it could be any object whose ``warning``
    raises. Only roots named ``logging``/``warnings`` are accepted -- a false negative (rejecting a
    legitimate alias) is the safe direction for this guard.
    """
    probes = {
        "alias_assigned_in_handler": (
            "try:\n    import mlflow\nexcept ImportError:\n"
            '    logger = logging.getLogger(__name__)\n    logger.warning("mlflow missing")\n'
        ),
        "alias_assigned_before_try": (
            "logger = logging.getLogger(__name__)\ntry:\n    import mlflow\nexcept ImportError:\n"
            '    logger.warning("mlflow missing")\n'
        ),
    }
    for label, source in probes.items():
        found = scan_source(source)
        assert found, f"scanner treated the local-alias {label} call as a safe fallback: {source!r}"
        assert found[0].module == "mlflow", (label, found[0].module)


def test_scanner_is_not_fooled_by_lookalike_guards():
    # catching something other than ImportError is not graceful degradation
    assert scan_source("try:\n    import mlflow\nexcept ValueError:\n    pass\n")
    # a guarded import does not cover a sibling import after the try block
    assert scan_source("try:\n    import mlflow\nexcept ImportError:\n    pass\nimport langfuse\n")
    # imports in a class body are module-level imports
    assert scan_source("class X:\n    import mlflow\n")


def test_scanner_allows_pydantic_and_non_vendors():
    assert scan_source("import pydantic\nimport yaml\nfrom evalops import ports\n") == []


def test_scanner_skips_plugin_and_trace_packages():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / "plugins").mkdir()
        (root / "plugins" / "mlflow_adapter.py").write_text("import mlflow\n", encoding="utf-8")
        (root / "traces").mkdir()
        (root / "traces" / "capture.py").write_text("from opentelemetry import trace\n", encoding="utf-8")
        (root / "core.py").write_text("import logfire\n", encoding="utf-8")

        violations = scan_tree(root)
        assert [v.path for v in violations] == ["core.py"], violations
