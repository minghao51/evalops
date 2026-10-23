"""Eval cost: pre-run estimates, budget enforcement, post-run measurement. Backend-agnostic.

Per-row generation cost, in order of preference:
  1. usage reported by the app via evalops.report_usage(cost_usd=...)
  2. the captured agent trace: tokens per LLM span x that span's model price
     (prices_per_1m_tokens.models.<model>, falling back to .generation), so multi-model agents are exact
  3. cost reported by the tracer plugin (e.g. MLflow >= 3.10 trace cost, Langfuse/LangSmith run cost)
  4. tokens x prices_per_1m_tokens.generation
Judge cost is estimated from configured per-call token sizes; reconcile against provider billing.
"""
import datetime as dt

from .util import EXIT_BUDGET, EvalOpsError


def _prices(cfg, role):
    p = cfg.section("cost").get("prices_per_1m_tokens", {}).get(role, {})
    return float(p.get("input", 0) or 0), float(p.get("output", 0) or 0)


def usd(cfg, role, tokens_in, tokens_out) -> float:
    pi, po = _prices(cfg, role)
    return (tokens_in * pi + tokens_out * po) / 1_000_000


def usd_model(cfg, model, tokens_in, tokens_out) -> float:
    models = cfg.section("cost").get("prices_per_1m_tokens", {}).get("models") or {}
    p = models.get(model) if model else None
    if not p:
        return usd(cfg, "generation", tokens_in, tokens_out)
    return (tokens_in * float(p.get("input", 0)) + tokens_out * float(p.get("output", 0))) / 1_000_000


def _trace_cost(cfg, trace) -> tuple[int, int, float]:
    llm = [s for s in trace.llm_calls() if s.input_tokens or s.output_tokens]
    if llm:
        return (sum(s.input_tokens for s in llm), sum(s.output_tokens for s in llm),
                sum(usd_model(cfg, s.model, s.input_tokens, s.output_tokens) for s in llm))
    tin, tout = trace.tokens()
    return tin, tout, usd(cfg, "generation", tin, tout)


def _defaults(cfg):
    return cfg.section("cost").get("defaults", {})


def judge_cost_per_call(cfg) -> float:
    d = _defaults(cfg)
    return usd(cfg, "judge", d.get("judge_input_tokens_per_call", 2000), d.get("judge_output_tokens_per_call", 200))


def measure(cfg, rows) -> dict:
    tin = tout = missing = 0
    cost, sources = 0.0, set()
    for r in rows:
        u = r.usage
        trace = getattr(r, "trace", None)
        if u is None and trace is not None and any(trace.tokens()):
            ti, to, c = _trace_cost(cfg, trace)
            tin, tout, cost = tin + ti, tout + to, cost + c
            sources.add("trace")
            continue
        if u is None:
            missing += 1 if r.outputs is not None else 0
            continue
        tin, tout = tin + u.input_tokens, tout + u.output_tokens
        if u.cost_usd is not None:
            cost += u.cost_usd
            sources.add("reported")
        else:
            cost += usd(cfg, "generation", u.input_tokens, u.output_tokens)
            sources.add("price_table")
    return {"gen_input_tokens": tin, "gen_output_tokens": tout, "gen_cost_usd": round(cost, 6),
            "gen_cost_source": "+".join(sorted(sources)) or "none", "rows_missing_usage": missing}


def history(ledger) -> dict:
    runs = ledger.find_runs(tags={"evalops.baseline": "true"}, limit=1)
    if not runs:
        return {}
    m = runs[0].metrics
    return {k: m[k] for k in ("gen_input_tokens_per_row", "gen_output_tokens_per_row") if m.get(k)}


def estimate(cfg, rows, repeats, llm_judges, judge_only, hist) -> dict:
    d = _defaults(cfg)
    gi = hist.get("gen_input_tokens_per_row") or d.get("gen_input_tokens_per_row", 1500)
    go = hist.get("gen_output_tokens_per_row") or d.get("gen_output_tokens_per_row", 400)
    gen = 0.0 if judge_only else rows * usd(cfg, "generation", gi, go)
    judge = rows * llm_judges * judge_cost_per_call(cfg)
    calls = 0 if judge_only else int(d.get("gen_calls_per_row", 1))
    priced = any(_prices(cfg, r) != (0.0, 0.0) for r in ("generation", "judge"))
    return {"rows": rows, "repeats": repeats, "llm_judges": llm_judges, "judge_only": judge_only,
            "llm_calls": rows * repeats * (calls + llm_judges),
            "gen_usd": round(gen * repeats, 4), "judge_usd": round(judge * repeats, 4),
            "total_usd": round((gen + judge) * repeats, 4),
            "basis": "measured_baseline" if hist else "config_defaults",
            "warning": None if priced else "prices_per_1m_tokens are all zero; estimates are meaningless"}


def month_spend(ledger) -> float:
    start = dt.datetime.now(dt.timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    runs = ledger.find_runs(since_ms=int(start.timestamp() * 1000))
    return round(sum(r.metrics.get("total_cost_usd", 0.0) for r in runs), 4)


def check_budget(cfg, ledger, est, override) -> dict:
    budgets = cfg.section("cost").get("budgets", {})
    spent = month_spend(ledger)
    info = {"estimate_usd": est["total_usd"], "per_run_usd": budgets.get("per_run_usd"),
            "month_spent_usd": spent, "per_month_usd": budgets.get("per_month_usd"), "override": override}
    problems = []
    if budgets.get("per_run_usd") is not None and est["total_usd"] > float(budgets["per_run_usd"]):
        problems.append("estimate exceeds per_run_usd")
    if budgets.get("per_month_usd") is not None and spent + est["total_usd"] > float(budgets["per_month_usd"]):
        problems.append("estimate would exceed per_month_usd")
    info["problems"] = problems
    if problems and not override:
        raise EvalOpsError("budget exceeded: " + "; ".join(problems), EXIT_BUDGET, info)
    return info
