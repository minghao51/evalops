"""Backend-independent usage reporting: call evalops.report_usage(...) inside your predict_fn.

Use it when your tracer cannot report tokens/cost (e.g. tracer 'none', or a custom model client).
Reported usage takes precedence over tracer-derived usage for that row.
"""
import contextlib
import contextvars

_current = contextvars.ContextVar("evalops_usage", default=None)


class _Collector:
    def __init__(self):
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = None
        self.reported = False


def report_usage(input_tokens: int = 0, output_tokens: int = 0, cost_usd: float | None = None) -> None:
    c = _current.get()
    if c is None:  # called outside an evalops row (e.g. in production): no-op
        return
    c.reported = True
    c.input_tokens += int(input_tokens or 0)
    c.output_tokens += int(output_tokens or 0)
    if cost_usd is not None:
        c.cost_usd = (c.cost_usd or 0.0) + float(cost_usd)


@contextlib.contextmanager
def collect():
    c = _Collector()
    token = _current.set(c)
    try:
        yield c
    finally:
        _current.reset(token)
