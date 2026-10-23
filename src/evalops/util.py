import hashlib
import json
import re
import subprocess

# Stable exit codes: CI systems and agents branch on these.
EXIT_OK = 0
EXIT_GATE_FAILED = 1
EXIT_USAGE = 2
EXIT_BUDGET = 3
EXIT_GUARD = 4
EXIT_RUNTIME = 5
EXIT_CAPABILITY = 6  # the configured plugin does not support the requested operation
EXIT_CONTRACT = 7  # a persisted artifact declares a format this evalops does not understand


class EvalOpsError(Exception):
    """A recoverable failure: `code` is the process exit code, `hint` is actionable advice.

    `details` and `hint` are surfaced in the `--json` envelope as `errors[0].details` and
    `errors[0].hint` (docs/CONTRACT.md §2); they never add top-level envelope fields.
    """

    def __init__(self, message: str, code: int = EXIT_RUNTIME, details: dict | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}
        self.hint = hint


class CapabilityError(EvalOpsError):
    """Exit 6: the configured plugin cannot serve the role. The message stays self-contained (it is
    what humans read) and the hint is also exposed structurally for `--json` consumers."""

    def __init__(self, plugin: str, operation: str, hint: str = ""):
        super().__init__(
            f"plugin '{plugin}' does not support {operation}. {hint}".strip(), EXIT_CAPABILITY, hint=hint or None
        )


class FormatError(EvalOpsError):
    """A persisted artifact is newer than this evalops: name the file, the found and the supported version."""

    def __init__(self, source: str, found, supported: int):
        super().__init__(
            f"{source}: unsupported schema_version {found!r}; this evalops supports up to "
            f"schema_version {supported} (upgrade evalops or regenerate the file)",
            EXIT_CONTRACT,
        )
        self.source, self.found, self.supported = source, found, supported


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render(template, variables: dict):
    """Render {{var}} placeholders in a text template or a list of chat messages."""

    def sub(text: str) -> str:
        return _VAR.sub(lambda m: str(variables.get(m.group(1), m.group(0))), text)

    if isinstance(template, str):
        return sub(template)
    return [{**m, "content": sub(m.get("content", ""))} for m in template]


def _git(*args: str, cwd=None) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL, cwd=cwd).strip()


def git_sha(short: bool = True, cwd=None) -> str:
    try:
        sha = _git("rev-parse", "HEAD", cwd=cwd)
    except Exception:  # noqa: BLE001
        return "nogit"
    return sha[:8] if short else sha


def git_dirty(cwd=None) -> bool:
    try:
        return bool(_git("status", "--porcelain", cwd=cwd))
    except Exception:  # noqa: BLE001
        return True
