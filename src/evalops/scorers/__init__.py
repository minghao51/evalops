"""Ready-made, framework-neutral scorers. Deterministic trajectory checks are cheap: run them first,
and use LLM judges (evalops.scorers.judges) only where a deterministic check cannot decide."""

from .judges import spec_judge, span_judge, trajectory_judge  # noqa: F401
from .trajectory import (  # noqa: F401
    agent_route,
    expected_state,
    forbidden_tools,
    no_loops,
    step_budget,
    tool_args,
    tool_errors,
    tool_precision,
    tool_recall,
    tool_trajectory,
)
