"""evalops: backend-agnostic versioning, evaluation, gating and cost control for LLM apps.

Core logic never imports a vendor SDK. Backends (MLflow, Langfuse, LangSmith, Logfire, git, SQL...)
are plugins implementing one or more *roles*: prompts, datasets, ledger, tracer, evaluator.
"""
__version__ = "0.3.0"

from .scoring import scorer  # noqa: E402,F401
from .usage import report_usage  # noqa: E402,F401
from .tools import replayable  # noqa: E402,F401
