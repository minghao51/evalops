"""Quality + cost gate: candidate group vs. production baseline group (via the ledger plugin)."""
import json
import math
import statistics

from . import locks
from .registry import Registry
from .util import EXIT_USAGE, EvalOpsError

COST_KEYS = ("cost_per_row_usd", "total_cost_usd", "gen_cost_usd", "judge_cost_usd_est")


def aggregate(metric_dicts: list[dict]) -> dict:
    keys = set().union(*metric_dicts) if metric_dicts else set()
    out = {}
    for k in keys:
        vals = [m[k] for m in metric_dicts if isinstance(m.get(k), (int, float))]
        if vals:
            out[k] = {"mean": statistics.fmean(vals), "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
                      "n": len(vals)}
    return out


def check(candidate: dict, baseline: dict | None, rules: dict):
    """Pure function over aggregate() outputs -> (passed, checks)."""
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "passed": bool(ok), **detail})

    for metric, rule in (rules.get("metrics") or {}).items():
        c = candidate.get(metric, {}).get("mean")
        b = (baseline or {}).get(metric, {}).get("mean")
        if c is None:
            add(metric, False, {"reason": "metric missing from candidate"})
            continue
        if "min" in rule:
            add(f"{metric} >= {rule['min']}", c >= rule["min"], {"candidate": c})
        if "max" in rule:
            add(f"{metric} <= {rule['max']}", c <= rule["max"], {"candidate": c})
        if "max_drop" in rule and b is not None:
            add(f"{metric} drop <= {rule['max_drop']}", (b - c) <= rule["max_drop"] + 1e-12,
                {"candidate": c, "baseline": b, "delta": c - b})
        if "max_increase_pct" in rule and b:  # e.g. trace.tool_calls/mean, trace.latency_s/p95
            pct = (c - b) / abs(b) * 100
            add(f"{metric} increase <= {rule['max_increase_pct']}%", pct <= rule["max_increase_pct"] + 1e-9,
                {"candidate": c, "baseline": b, "increase_pct": round(pct, 2)})

    cost_rules = rules.get("cost") or {}
    c_cost = candidate.get("cost_per_row_usd", {}).get("mean")
    b_cost = (baseline or {}).get("cost_per_row_usd", {}).get("mean")
    if c_cost is not None and "max_cost_per_row_usd" in cost_rules:
        add(f"cost_per_row <= ${cost_rules['max_cost_per_row_usd']}", c_cost <= cost_rules["max_cost_per_row_usd"],
            {"candidate": c_cost})
    if c_cost is not None and b_cost and "max_cost_per_row_increase_pct" in cost_rules:
        pct = (c_cost - b_cost) / b_cost * 100
        add(f"cost_per_row increase <= {cost_rules['max_cost_per_row_increase_pct']}%",
            pct <= cost_rules["max_cost_per_row_increase_pct"] + 1e-9,
            {"candidate": c_cost, "baseline": b_cost, "increase_pct": round(pct, 2)})
    return all(x["passed"] for x in checks), checks


# ---------------- row-level statistics (reliability, confidence intervals) ----------------
def _row_value(row, metric):
    key = metric.rsplit("/", 1)[0]
    if key.startswith("trace."):
        return (row.trace_metrics or {}).get(key[len("trace."):])
    return (row.scores or {}).get(key)


def load_group_rows(ledger, group) -> list[list]:
    from .run import rows_from_artifact
    out = []
    for r in sorted(ledger.find_runs(tags={"evalops.group": group}), key=lambda r: r.tags.get("evalops.repeat", "")):
        try:
            out.append(rows_from_artifact(ledger.get_artifact(r.run_id, "evalops/rows.jsonl")))
        except Exception:  # noqa: BLE001
            continue
    return out


def reliability(runs_rows: list[list], success: list[dict]) -> dict:
    """pass^k: share of cases that succeed on ALL k repeats; pass@k: on AT LEAST ONE."""
    by_row: dict[str, list[bool]] = {}
    for rows in runs_rows:
        for row in rows:
            ok = not row.error and all(
                (v := _row_value(row, c["metric"])) is None or v >= c.get("min", 1.0) for c in success)
            by_row.setdefault(row.row_id, []).append(ok)
    if not by_row:
        return {}
    k = min(len(v) for v in by_row.values())
    n = len(by_row)
    return {"k": k, "cases": n,
            "pass_hat_k": sum(all(v[:k]) for v in by_row.values()) / n,
            "pass_at_k": sum(any(v[:k]) for v in by_row.values()) / n,
            "success_rate": sum(sum(v) for v in by_row.values()) / sum(len(v) for v in by_row.values())}


def ci_lower(runs_rows: list[list], metric: str, z: float = 1.96) -> float | None:
    vals = [v for rows in runs_rows for r in rows if (v := _row_value(r, metric)) is not None]
    if len(vals) < 2:
        return None
    return statistics.fmean(vals) - z * statistics.stdev(vals) / math.sqrt(len(vals))


def check_rows(cand_rows, base_rows, rules) -> list[dict]:
    checks = []
    for metric, rule in (rules.get("metrics") or {}).items():
        if "min_ci_lower" in rule:
            lo = ci_lower(cand_rows, metric)
            checks.append({"check": f"{metric} 95% CI lower >= {rule['min_ci_lower']}",
                           "passed": lo is not None and lo >= rule["min_ci_lower"], "candidate": lo})
    rel = rules.get("reliability") or {}
    if rel.get("success"):
        c = reliability(cand_rows, rel["success"])
        b = reliability(base_rows, rel["success"]) if base_rows else {}
        for key in ("pass_hat_k", "pass_at_k"):
            if f"min_{key}" in rel:
                checks.append({"check": f"{key} (k={c.get('k')}) >= {rel[f'min_{key}']}",
                               "passed": c.get(key, 0) >= rel[f"min_{key}"], "candidate": c.get(key)})
            if f"max_drop_{key}" in rel and b.get(key) is not None:
                checks.append({"check": f"{key} drop <= {rel[f'max_drop_{key}']}",
                               "passed": b[key] - c.get(key, 0) <= rel[f"max_drop_{key}"] + 1e-12,
                               "candidate": c.get(key), "baseline": b[key]})
        checks.append({"check": "reliability (info)", "passed": True, **{f"cand_{k}": v for k, v in c.items()}})
    return checks


def _needs_rows(rules) -> bool:
    return bool((rules.get("reliability") or {}).get("success")) or any(
        "min_ci_lower" in r for r in (rules.get("metrics") or {}).values())


def report_md(label, passed, checks, cand, base, cost_info, plugins) -> str:
    def fmt(v):
        return "n/a" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))

    lines = [f"## evalops {label}: {'✅ PASSED' if passed else '❌ FAILED'}", "",
             f"_Backends: {', '.join(f'{k}={v}' for k, v in (plugins or {}).items())}_", "",
             "| Metric | Baseline | Candidate (mean ± std, n) | Δ |", "|---|---|---|---|"]
    for k in sorted(cand):
        if "/" not in k and k not in COST_KEYS and k not in ("error_rate", "budget_exceeded_rate"):
            continue
        c, b = cand[k], (base or {}).get(k)
        delta = c["mean"] - b["mean"] if b else None
        lines.append(f"| {k} | {fmt(b['mean'] if b else None)} | {fmt(c['mean'])} ± {fmt(c['std'])}, n={c['n']} "
                     f"| {fmt(delta)} |")
    lines += ["", "| Check | Result |", "|---|---|"]
    for x in checks:
        if x["check"] == "reliability (info)":
            info = ", ".join(f"{k[5:]}={v:.3f}" if isinstance(v, float) else f"{k[5:]}={v}"
                             for k, v in x.items() if k.startswith("cand_"))
            lines.append(f"| reliability: {info} | info |")
        else:
            lines.append(f"| {x['check']} | {'pass' if x['passed'] else '**FAIL**'} |")
    if cost_info:
        b = cost_info.get("budget", {})
        lines += ["", f"**Cost:** estimated ${cost_info.get('estimate_usd')}, actual ${cost_info.get('actual_usd')} "
                      f"(month to date ${b.get('month_spent_usd')} of ${b.get('per_month_usd')}). "
                      "Judge cost is an estimate."]
    if base is None:
        lines += ["", "_No production baseline yet: only absolute thresholds were applied._"]
    return "\n".join(lines) + "\n"


def run_gate(cfg, group=None, label="gate") -> dict:
    last_path = locks.scratch(cfg, "last_eval.json")
    last = json.loads(last_path.read_text()) if last_path.exists() else {}
    group = group or last.get("group")
    if not group:
        raise EvalOpsError("no --group given and no last_eval.json", EXIT_USAGE)
    reg = Registry(cfg)
    ledger = reg.get("ledger")
    cand_runs = ledger.find_runs(tags={"evalops.group": group})
    if not cand_runs:
        raise EvalOpsError(f"no runs for group {group}", EXIT_USAGE)
    base_runs = ledger.find_runs(tags={"evalops.baseline": "true"}, limit=1)
    base_group = base_runs[0].tags.get("evalops.group") if base_runs else None
    if base_group == group:
        base_group = None
    cand = aggregate([r.metrics for r in cand_runs])
    base = aggregate([r.metrics for r in ledger.find_runs(tags={"evalops.group": base_group})]) if base_group else None
    rules = cfg.section("gates")
    passed, checks = check(cand, base, rules)
    if _needs_rows(rules):
        extra = check_rows(load_group_rows(ledger, group), load_group_rows(ledger, base_group) if base_group else [],
                           rules)
        checks += extra
        passed = passed and all(x["passed"] for x in extra)
    tag = "evalops.gate" if label == "gate" else f"evalops.{label}"
    for r in cand_runs:
        ledger.log(r.run_id, tags={tag: "passed" if passed else "failed"})
    report = locks.scratch(cfg, f"{label}_report.md")
    report.write_text(report_md(label, passed, checks, cand, base,
                                last.get("cost") if last.get("group") == group else None, reg.describe()))
    return {"ok": passed, "summary": f"{label} {'passed' if passed else 'FAILED'} for group {group}",
            "group": group, "baseline_group": base_group, "checks": checks, "report": str(report)}
