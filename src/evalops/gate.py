"""Quality + cost gate: candidate group vs. production baseline group (via the ledger plugin).

Statistics conventions (WP1.3; the normative text is CONTRACT §9):

* Dispersion is the **sample** standard deviation (`statistics.stdev`, n-1) under the unchanged
  `std` key; `n == 1` reports `0.0`. No rule reads `std` implicitly: every threshold is an explicit
  configured key.
* `ci_lower` (the `min_ci_lower` rule) stays the normal-approximation bound
  `mean - z * sample_stdev / sqrt(n)` for `n >= 2`, `None` below.
* `wilson_interval` is the proportion interval; `bootstrap_ci` is a deterministic percentile
  bootstrap of the mean. `min_ci_lower_bootstrap` is the only rule that reads the bootstrap bound.
* `reliability` is the plug-in pass^k estimator over a per-case success vector ordered by
  `evalops.repeat` as an integer.
* `drift`, `paired_drift` and `attribution` are additive diagnostics: they never change a check.
"""

import math
import random
import statistics
from typing import cast

from . import locks
from .contract import formats
from .ports import Ledger
from .registry import Registry
from .util import EXIT_USAGE, EvalOpsError, FormatError

COST_KEYS = ("cost_per_row_usd", "total_cost_usd", "gen_cost_usd", "judge_cost_usd_est")

#: Params that identify *what* produced a run (D7). `behavior_config` is excluded: it is truncated
#: JSON, so a diff of it is noise, and every behavior change already moves `config_hash`.
ATTRIBUTION_PARAMS = frozenset(
    {
        "app_version",
        "config_hash",
        "git_sha",
        "git_dirty",
        "dataset",
        "dataset_fingerprint",
        "dataset_physical",
        "tool_mode",
        "tool_cassette",
        "source_group",
    }
)
ATTRIBUTION_PREFIXES = ("prompt.", "judge.", "plugin.")


def aggregate(metric_dicts: list[dict]) -> dict:
    """Per-metric `{mean, std, n}`; `std` is the **sample** standard deviation (n-1)."""
    keys = set().union(*metric_dicts) if metric_dicts else set()
    out = {}
    for k in keys:
        vals = [m[k] for m in metric_dicts if isinstance(m.get(k), (int, float))]
        if vals:
            out[k] = {
                "mean": statistics.fmean(vals),
                "std": statistics.stdev(vals) if len(vals) > 1 else 0.0,
                "n": len(vals),
            }
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
            add(
                f"{metric} drop <= {rule['max_drop']}",
                (b - c) <= rule["max_drop"] + 1e-12,
                {"candidate": c, "baseline": b, "delta": c - b},
            )
        if "max_increase_pct" in rule and b:  # e.g. trace.tool_calls/mean, trace.latency_s/p95
            pct = (c - b) / abs(b) * 100
            add(
                f"{metric} increase <= {rule['max_increase_pct']}%",
                pct <= rule["max_increase_pct"] + 1e-9,
                {"candidate": c, "baseline": b, "increase_pct": round(pct, 2)},
            )

    cost_rules = rules.get("cost") or {}
    c_cost = candidate.get("cost_per_row_usd", {}).get("mean")
    b_cost = (baseline or {}).get("cost_per_row_usd", {}).get("mean")
    if c_cost is not None and "max_cost_per_row_usd" in cost_rules:
        add(
            f"cost_per_row <= ${cost_rules['max_cost_per_row_usd']}",
            c_cost <= cost_rules["max_cost_per_row_usd"],
            {"candidate": c_cost},
        )
    if c_cost is not None and b_cost and "max_cost_per_row_increase_pct" in cost_rules:
        pct = (c_cost - b_cost) / b_cost * 100
        add(
            f"cost_per_row increase <= {cost_rules['max_cost_per_row_increase_pct']}%",
            pct <= cost_rules["max_cost_per_row_increase_pct"] + 1e-9,
            {"candidate": c_cost, "baseline": b_cost, "increase_pct": round(pct, 2)},
        )
    return all(x["passed"] for x in checks), checks


# ---------------- row-level statistics (reliability, confidence intervals) ----------------
def _row_value(row, metric):
    key = metric.rsplit("/", 1)[0]
    if key.startswith("trace."):
        return (row.trace_metrics or {}).get(key[len("trace.") :])
    return (row.scores or {}).get(key)


def _ordered_rows(runs_rows: list[list], repeats: list | None) -> tuple[list[list], list[str], bool]:
    """Order per-run row lists by `evalops.repeat` **as an integer** (D3).

    Returns `(rows, repeat_tags, unordered)`. `unordered` is true when the numeric order cannot be
    established (no tags, a non-numeric or duplicate tag, a length mismatch): the caller's order is
    then positional and the result must say so instead of pretending to be ordered. String sorting
    is exactly the bug this avoids -- it puts repeat "10" before "2".
    """
    if repeats is None or len(repeats) != len(runs_rows):
        return list(runs_rows), [str(i + 1) for i in range(len(runs_rows))], True
    tags = [str(t) for t in repeats]
    try:
        ints = [int(t) for t in tags]
    except (TypeError, ValueError):
        return list(runs_rows), tags, True
    if len(set(ints)) != len(ints):
        return list(runs_rows), tags, True
    order = sorted(range(len(runs_rows)), key=lambda i: ints[i])
    return [runs_rows[i] for i in order], [tags[i] for i in order], False


def aligned_by(runs_rows: list[list]) -> str:
    """`"row_key"` when every stored row carries a stable key, else `"row_id"` (D4 fallback)."""
    rows = [r for rows in runs_rows for r in rows]
    if rows and all(getattr(r, "row_key", None) for r in rows):
        return "row_key"
    return "row_id"


def _row_identity(row, field: str = "row_id") -> str:
    if field == "row_key":
        return row.row_key or row.row_id
    return row.row_id


def load_group(ledger, group) -> tuple[list[list], list[str]]:
    """Stored rows per run of `group`, plus the matching `evalops.repeat` tags.

    Runs come back ordered by `evalops.repeat` as an integer when that is possible. A format
    mismatch is never swallowed: a rows artifact that declares an unknown schema_version (or is not
    an object at all) is Exit 7 and is re-raised, so a future writer cannot silently downgrade a
    gate to "no row data".
    """
    from .run import rows_from_artifact

    rows: list[list] = []
    repeats: list[str] = []
    for r in ledger.find_runs(tags={"evalops.group": group}):
        try:
            stored = rows_from_artifact(ledger.get_artifact(r.run_id, "evalops/rows.jsonl"))
        except FormatError:
            raise  # a future-version rows artifact is exit 7, never a silent skip
        except Exception:  # noqa: BLE001 - a run whose artifact is genuinely missing is skipped
            continue
        rows.append(stored)
        repeats.append(str(r.tags.get("evalops.repeat", "")))
    ordered, tags, _ = _ordered_rows(rows, repeats)
    return ordered, tags


def load_group_rows(ledger, group) -> list[list]:
    """`load_group` without the repeat tags (kept for callers that only need the rows)."""
    return load_group(ledger, group)[0]


def reliability(runs_rows: list[list], success: list[dict], repeats: list | None = None) -> dict:
    """pass^k / pass@k / success_rate over a per-case success vector ordered by repeat (D3).

    For each case (the stable row key, else `row_id`) the success vector is ordered by the run's
    `evalops.repeat` as an integer and `k = min(len(vector))`:

    * `pass_hat_k = (1/cases) * sum(all(v[:k]))` -- succeeds on **every one** of the first k repeats
      (the plug-in estimator, not the combinatorial "at least k of n"),
    * `pass_at_k = (1/cases) * sum(any(v[:k]))`,
    * `success_rate = total successes / total repeats`.

    `unordered_repeats` is true when the repeat order could not be established.
    `cases_with_fewer_repeats` counts cases missing from at least one run; `comparable` is false
    when it is non-zero, because a ragged matrix compares different denominators.
    """
    if not runs_rows:
        return {}
    ordered, _tags, unordered = _ordered_rows(runs_rows, repeats)
    field = aligned_by(ordered)
    by_row: dict[str, list[bool]] = {}
    for rows in ordered:
        for row in rows:
            ok = not row.error and all(
                (v := _row_value(row, c["metric"])) is None or v >= c.get("min", 1.0) for c in success
            )
            by_row.setdefault(_row_identity(row, field), []).append(ok)
    if not by_row:
        return {}
    k = min(len(v) for v in by_row.values())
    cases = len(by_row)
    total = sum(len(v) for v in by_row.values())
    short = sum(1 for v in by_row.values() if len(v) < len(ordered))
    hits = sum(1 for v in by_row.values() if all(v[:k]))
    at_least_one = sum(1 for v in by_row.values() if any(v[:k]))
    successes = sum(sum(v) for v in by_row.values())
    return {
        "k": k,
        "cases": cases,
        "pass_hat_k": hits / cases,
        "pass_at_k": at_least_one / cases,
        "success_rate": successes / total,
        # Proportions get Wilson intervals (D2); the counts above are the numerators, so nothing
        # is re-derived from a rounded proportion.
        "pass_hat_k_ci": wilson_interval(hits, cases),
        "pass_at_k_ci": wilson_interval(at_least_one, cases),
        "success_rate_ci": wilson_interval(successes, total),
        "aligned_by": field,
        "unordered_repeats": unordered,
        "cases_with_fewer_repeats": short,
        "comparable": short == 0,
    }


def ci_lower(runs_rows: list[list], metric: str, z: float = 1.96) -> float | None:
    """Normal-approximation lower bound: `mean - z * sample_stdev / sqrt(n)`, `None` for `n < 2`."""
    vals = [v for rows in runs_rows for r in rows if (v := _row_value(r, metric)) is not None]
    if len(vals) < 2:
        return None
    return statistics.fmean(vals) - z * statistics.stdev(vals) / math.sqrt(len(vals))


def wilson_interval(successes: int, n: int, z: float = 1.96) -> dict | None:
    """Wilson score interval for a proportion `successes / n` (D2); `None` when `n == 0`.

    Unlike the normal approximation it stays inside [0, 1] and is non-degenerate at 0/n and n/n.
    """
    if n <= 0:
        return None
    k = min(max(int(successes), 0), int(n))
    p = k / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {
        # the exact endpoints are 0 and 1, so pin them instead of returning -1e-17 / 1+1e-17
        "lower": 0.0 if k == 0 else max(0.0, centre - half),
        "upper": 1.0 if k == n else min(1.0, centre + half),
        "method": "wilson",
        "n": int(n),
        "k": k,
    }


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolation percentile of an already sorted list (the numpy default)."""
    pos = q * (len(sorted_vals) - 1)
    low, high = math.floor(pos), math.ceil(pos)
    if low == high:
        return sorted_vals[low]
    return sorted_vals[low] + (sorted_vals[high] - sorted_vals[low]) * (pos - low)


def bootstrap_ci(vals: list[float], resamples: int = 2000, seed: int = 0, alpha: float = 0.05) -> dict | None:
    """Percentile bootstrap CI of the mean (D2): deterministic for the same input and seed.

    `None` below two values. The resampling RNG is local to the call, so a gate report is
    reproducible; callers must not rely on the global `random` state.
    """
    values = [float(v) for v in vals]
    n = len(values)
    if n < 2:
        return None
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(resamples))
    return {
        "lower": _percentile(means, alpha / 2),
        "upper": _percentile(means, 1 - alpha / 2),
        "method": "bootstrap-percentile",
        "resamples": resamples,
        "n": n,
    }


def _grouped_values(runs_rows: list[list], metric: str, field: str) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    for rows in runs_rows:
        for row in rows:
            v = _row_value(row, metric)
            if v is None:
                continue
            out.setdefault(_row_identity(row, field), []).append(float(v))
    return out


def paired_drift(cand_rows: list[list], base_rows: list[list], metric: str) -> dict | None:
    """Paired drift over stable row identity (D6): only keys present in both groups contribute.

    `cand_rows` / `base_rows` are per-run row lists (as returned by `load_group`); a side's value for
    a key is the mean over the runs that contain it. `unaligned` counts the keys that exist on only
    one side -- a key that is missing from a side is never silently paired with something else.
    Returns None when either side has no value for `metric`.
    """
    if not cand_rows or not base_rows:
        return None
    field = aligned_by(cand_rows + base_rows)
    cand = _grouped_values(cand_rows, metric, field)
    base = _grouped_values(base_rows, metric, field)
    if not cand or not base:
        return None
    keys = sorted(set(cand) & set(base))
    deltas = [statistics.fmean(cand[k]) - statistics.fmean(base[k]) for k in keys]
    ci = bootstrap_ci(deltas)
    return {
        "metric": metric,
        "aligned": len(keys),
        "unaligned": {"candidate": len(set(cand) - set(base)), "baseline": len(set(base) - set(cand))},
        "mean_delta": statistics.fmean(deltas) if deltas else None,
        "ci": ci,
        "significant": bool(ci and (ci["lower"] > 0.0 or ci["upper"] < 0.0)),
        "aligned_by": field,
    }


def _drift_block(cand: dict, base: dict) -> dict:
    """Effect size + two-sample z-test for one metric, from `aggregate()` outputs (D6).

    `z = delta / sqrt(se_c^2 + se_b^2)` with `se = sample_stdev / sqrt(n)`; the two-sided p-value is
    `erfc(|z| / sqrt(2))` (no scipy). Either is `None` when a side has fewer than two runs.
    """
    delta = cand["mean"] - base["mean"]
    nc, nb = cand["n"], base["n"]
    pooled = None
    if nc + nb - 2 > 0:
        pooled = math.sqrt(((nc - 1) * cand["std"] ** 2 + (nb - 1) * base["std"] ** 2) / (nc + nb - 2))
    z = p = None
    if nc >= 2 and nb >= 2:
        se2 = cand["std"] ** 2 / nc + base["std"] ** 2 / nb
        if se2 > 0:
            z = delta / math.sqrt(se2)
            p = math.erfc(abs(z) / math.sqrt(2))
    return {
        "candidate": cand,
        "baseline": base,
        "delta": delta,
        "pct_change": (delta / abs(base["mean"]) * 100) if base["mean"] else None,
        "cohen_d": (delta / pooled) if pooled else None,
        "z": z,
        "p": p,
        "significant": bool(p is not None and p < 0.05),
    }


def drift(cand_runs: list, base_runs: list | None, rules: dict, *, cand_rows=None, base_rows=None) -> dict:
    """Two-group drift for every rule metric and every metric present in both aggregates (D6).

    `cand_runs`/`base_runs` are ledger runs (anything with `.metrics`). Per metric the block reports
    `delta`, `pct_change`, `cohen_d` (pooled sample sd), `z`, `p` and `significant`. A paired block
    is added for metrics with row values on both sides, but only when alignment is by `row_key`: a
    `row_id` alignment is positional and pairing it would invent a correspondence.
    """
    rules = rules or {}
    base_runs = base_runs or []
    cand = aggregate([r.metrics for r in cand_runs])
    base = aggregate([r.metrics for r in base_runs])
    wanted = list((rules.get("metrics") or {}).keys())
    wanted += sorted(k for k in set(cand) & set(base) if k not in wanted)
    metrics: dict[str, dict] = {}
    for metric in wanted:
        c, b = cand.get(metric), base.get(metric)
        if c is None or b is None:
            continue
        metrics[metric] = _drift_block(c, b)
    paired: dict[str, dict] = {}
    field = aligned_by((cand_rows or []) + (base_rows or [])) if cand_rows and base_rows else None
    if field == "row_key":
        for metric in wanted:
            block = paired_drift(cand_rows or [], base_rows or [], metric)
            if block is not None:
                paired[metric] = block
    return {"metrics": metrics, "paired": paired, "aligned_by": field}


def _is_attribution_param(name: str) -> bool:
    return name in ATTRIBUTION_PARAMS or name.startswith(ATTRIBUTION_PREFIXES)


def attribution(cand_runs: list, base_runs: list | None) -> dict:
    """Name the identity/provenance params that changed between candidate and baseline (D7).

    Compares the newest run of each group, restricted to `ATTRIBUTION_PARAMS`, `prompt.*`,
    `judge.*` and `plugin.*`. Values are compared as strings; `behavior_config` never appears (it is
    truncated JSON). Only identifiers and their values are reported: no row payloads, no secrets.
    """
    base_runs = base_runs or []
    if not cand_runs or not base_runs:
        return {"changed": {}, "summary": "no baseline run to attribute against" if cand_runs else "no candidate run"}
    c, b = cand_runs[0].params or {}, base_runs[0].params or {}
    changed = {}
    for name in sorted(set(c) | set(b)):
        if not _is_attribution_param(name):
            continue
        cv, bv = str(c.get(name, "")), str(b.get(name, ""))
        if cv != bv:
            changed[name] = {"candidate": cv, "baseline": bv}
    if not changed:
        return {"changed": {}, "summary": "no identity/provenance params changed"}
    names = ", ".join(changed)
    return {"changed": changed, "summary": f"{len(changed)} identity/provenance param(s) changed: {names}"}


def check_rows(cand_rows, base_rows, rules, *, cand_repeats=None, base_repeats=None) -> list[dict]:
    """Row-level checks: configured CI bounds and the reliability rules (additive per WP1.3)."""
    checks = []
    for metric, rule in (rules.get("metrics") or {}).items():
        if "min_ci_lower" in rule:
            lo = ci_lower(cand_rows, metric)
            checks.append(
                {
                    "check": f"{metric} 95% CI lower >= {rule['min_ci_lower']}",
                    "passed": lo is not None and lo >= rule["min_ci_lower"],
                    "candidate": lo,
                }
            )
        if "min_ci_lower_bootstrap" in rule:
            vals = [v for rows in cand_rows for r in rows if (v := _row_value(r, metric)) is not None]
            ci = bootstrap_ci(vals)
            lo = ci["lower"] if ci else None
            checks.append(
                {
                    "check": f"{metric} bootstrap CI lower >= {rule['min_ci_lower_bootstrap']}",
                    "passed": lo is not None and lo >= rule["min_ci_lower_bootstrap"],
                    "candidate": lo,
                    "method": "bootstrap-percentile",
                }
            )
    rel = rules.get("reliability") or {}
    if rel.get("success"):
        c = reliability(cand_rows, rel["success"], cand_repeats)
        b = reliability(base_rows, rel["success"], base_repeats) if base_rows else {}
        for key in ("pass_hat_k", "pass_at_k"):
            if f"min_{key}" in rel:
                checks.append(
                    {
                        "check": f"{key} (k={c.get('k')}) >= {rel[f'min_{key}']}",
                        "passed": c.get(key, 0) >= rel[f"min_{key}"],
                        "candidate": c.get(key),
                    }
                )
            if f"max_drop_{key}" in rel and b.get(key) is not None:
                checks.append(
                    {
                        "check": f"{key} drop <= {rel[f'max_drop_{key}']}",
                        "passed": b[key] - c.get(key, 0) <= rel[f"max_drop_{key}"] + 1e-12,
                        "candidate": c.get(key),
                        "baseline": b[key],
                    }
                )
        if rel.get("min_pass_hat_k_wilson") is not None:
            ci = c.get("pass_hat_k_ci")
            lo = ci["lower"] if isinstance(ci, dict) else None
            checks.append(
                {
                    "check": f"pass_hat_k_ci.lower >= {rel['min_pass_hat_k_wilson']}",
                    # a missing interval fails: an absent CI is not evidence of reliability
                    "passed": lo is not None and lo >= rel["min_pass_hat_k_wilson"],
                    "candidate": lo,
                    "method": "wilson",
                }
            )
        checks.append({"check": "reliability (info)", "passed": True, **{f"cand_{k}": v for k, v in c.items()}})
    return checks


def _needs_rows(rules) -> bool:
    """True when any configured rule reads stored rows (CI bounds, or any reliability rule)."""
    rel = rules.get("reliability") or {}
    return (
        bool(rel.get("success"))
        # explicit: this rule is one more reason to load rows, not a side effect of `success`
        or rel.get("min_pass_hat_k_wilson") is not None
        or any("min_ci_lower" in r or "min_ci_lower_bootstrap" in r for r in (rules.get("metrics") or {}).values())
    )


# ---------------- baseline pinning (D5) ----------------
def baseline_info(run) -> dict | None:
    """`{group, app_version, dataset_fingerprint, pinned}` for the baseline run, or None."""
    if run is None:
        return None
    tag_ver = run.tags.get("evalops.baseline.app_version")
    tag_fp = run.tags.get("evalops.baseline.dataset_fingerprint")
    return {
        "group": run.tags.get("evalops.group"),
        "app_version": tag_ver or run.params.get("app_version") or run.tags.get("evalops.app_version"),
        "dataset_fingerprint": tag_fp or run.params.get("dataset_fingerprint"),
        "pinned": bool(tag_ver and tag_fp),
    }


def baseline_checks(run, cand_runs) -> list[dict]:
    """Failing-only checks from D5: pin integrity, then candidate-vs-baseline dataset identity.

    No baseline -> no check (exactly as before). Comparing different data is not a drift
    measurement, and a pinned baseline that disagrees with the run it is pinned to was moved or
    tampered with. Neither check exists when there is nothing to compare.
    """
    if run is None:
        return []
    checks = []
    tag_ver = run.tags.get("evalops.baseline.app_version") or ""
    tag_fp = run.tags.get("evalops.baseline.dataset_fingerprint") or ""
    if tag_ver or tag_fp:
        own_ver = str(run.params.get("app_version") or run.tags.get("evalops.app_version") or "")
        own_fp = str(run.params.get("dataset_fingerprint") or "")
        if tag_ver != own_ver or tag_fp != own_fp:
            checks.append(
                {
                    "check": "baseline pin matches run",
                    "passed": False,
                    "pin": {"app_version": tag_ver, "dataset_fingerprint": tag_fp},
                    "run": {"app_version": own_ver, "dataset_fingerprint": own_fp},
                }
            )
    base_fp = tag_fp or str(run.params.get("dataset_fingerprint") or "")
    cand_fps = sorted(
        {str(r.params.get("dataset_fingerprint")) for r in cand_runs if r.params.get("dataset_fingerprint")}
    )
    if base_fp and cand_fps and cand_fps != [base_fp]:
        checks.append(
            {
                "check": "baseline dataset fingerprint matches",
                "passed": False,
                "candidate": cand_fps,
                "baseline": base_fp,
            }
        )
    return checks


def _fmt_info(name, value) -> str:
    if isinstance(value, float):
        return f"{name}={value:.3f}"
    if isinstance(value, dict) and "lower" in value:
        return f"{name}=[{value['lower']:.3f},{value['upper']:.3f}]"
    return f"{name}={value}"


def report_md(
    label, passed, checks, cand, base, cost_info, plugins, *, baseline=None, drift_info=None, attribution_info=None
) -> str:
    def fmt(v):
        return "n/a" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))

    lines = [
        f"## evalops {label}: {'✅ PASSED' if passed else '❌ FAILED'}",
        "",
        f"_Backends: {', '.join(f'{k}={v}' for k, v in (plugins or {}).items())}_",
        "",
        "| Metric | Baseline | Candidate (mean ± std, n) | Δ |",
        "|---|---|---|---|",
    ]
    for k in sorted(cand):
        if "/" not in k and k not in COST_KEYS and k not in ("error_rate", "budget_exceeded_rate"):
            continue
        c, b = cand[k], (base or {}).get(k)
        delta = c["mean"] - b["mean"] if b else None
        lines.append(
            f"| {k} | {fmt(b['mean'] if b else None)} | {fmt(c['mean'])} ± {fmt(c['std'])}, n={c['n']} | {fmt(delta)} |"
        )
    lines += ["", "| Check | Result |", "|---|---|"]
    for x in checks:
        if x["check"] == "reliability (info)":
            info = ", ".join(_fmt_info(k[5:], v) for k, v in x.items() if k.startswith("cand_"))
            lines.append(f"| reliability: {info} | info |")
        else:
            lines.append(f"| {x['check']} | {'pass' if x['passed'] else '**FAIL**'} |")
    if baseline:
        lines += [
            "",
            f"**Baseline:** group `{baseline.get('group')}` · app_version `{baseline.get('app_version')}` · "
            f"dataset `{baseline.get('dataset_fingerprint')}` · pinned: {'yes' if baseline.get('pinned') else 'no'}",
        ]
    if drift_info and drift_info.get("metrics"):
        lines += [
            "",
            "## Drift",
            "",
            "| Metric | Δ | Δ% | Cohen's d | z | p | significant |",
            "|---|---|---|---|---|---|---|",
        ]
        for k, d in drift_info["metrics"].items():
            lines.append(
                f"| {k} | {fmt(d['delta'])} | {fmt(d['pct_change'])} | {fmt(d['cohen_d'])} | "
                f"{fmt(d['z'])} | {fmt(d['p'])} | {'yes' if d['significant'] else 'no'} |"
            )
        for k, d in (drift_info.get("paired") or {}).items():
            ci = d.get("ci") or {}
            lines.append(
                f"| {k} (paired, n={d['aligned']}) | {fmt(d['mean_delta'])} | "
                f"CI [{fmt(ci.get('lower'))}, {fmt(ci.get('upper'))}] | | | | {'yes' if d['significant'] else 'no'} |"
            )
    if attribution_info:
        lines += ["", "## Change attribution", ""]
        changed = attribution_info.get("changed") or {}
        if changed:
            lines += ["| Param | Baseline | Candidate |", "|---|---|---|"]
            for k in sorted(changed):
                lines.append(f"| {k} | `{changed[k]['baseline']}` | `{changed[k]['candidate']}` |")
        else:
            lines.append(f"_{attribution_info.get('summary')}_")
    if cost_info:
        b = cost_info.get("budget", {})
        lines += [
            "",
            f"**Cost:** estimated ${cost_info.get('estimate_usd')}, actual ${cost_info.get('actual_usd')} "
            f"(month to date ${b.get('month_spent_usd')} of ${b.get('per_month_usd')}). "
            "Judge cost is an estimate.",
        ]
    if base is None:
        lines += ["", "_No production baseline yet: only absolute thresholds were applied._"]
    return "\n".join(lines) + "\n"


def run_gate(cfg, group=None, label="gate") -> dict:
    last_path = locks.scratch(cfg, "last_eval.json")
    last = formats.read_versioned(last_path) if last_path.exists() else {}
    group = group or last.get("group")
    if not group:
        raise EvalOpsError("no --group given and no last_eval.json", EXIT_USAGE)
    reg = Registry(cfg)
    ledger = cast(Ledger, reg.get("ledger"))
    cand_runs = ledger.find_runs(tags={"evalops.group": group})
    if not cand_runs:
        raise EvalOpsError(f"no runs for group {group}", EXIT_USAGE)
    base_runs = ledger.find_runs(tags={"evalops.baseline": "true"}, limit=1)
    base_run = base_runs[0] if base_runs else None
    base_group = base_run.tags.get("evalops.group") if base_run else None
    if base_group == group:
        base_group = None
    cand = aggregate([r.metrics for r in cand_runs])
    base_group_runs = ledger.find_runs(tags={"evalops.group": base_group}) if base_group else []
    base = aggregate([r.metrics for r in base_group_runs]) if base_group else None
    rules = cfg.section("gates")
    passed, checks = check(cand, base, rules)
    baseline = baseline_info(base_run)
    # Rows are read for the row rules and, with a baseline present, for paired drift. Without either
    # the gate keeps its previous no-artifact behaviour (a missing rows artifact is still skipped).
    if _needs_rows(rules) or base_group:
        cand_rows, cand_repeats = load_group(ledger, group)
        base_rows, base_repeats = load_group(ledger, base_group) if base_group else ([], [])
    else:
        cand_rows, cand_repeats, base_rows, base_repeats = [], [], [], []
    extra = baseline_checks(base_run, cand_runs)
    if _needs_rows(rules):
        extra += check_rows(cand_rows, base_rows, rules, cand_repeats=cand_repeats, base_repeats=base_repeats)
    if extra:
        checks += extra
        passed = passed and all(x["passed"] for x in extra)
    rel_cfg = rules.get("reliability") or {}
    reliability_info = (
        {
            "candidate": reliability(cand_rows, rel_cfg["success"], cand_repeats),
            "baseline": reliability(base_rows, rel_cfg["success"], base_repeats) if base_rows else None,
        }
        if rel_cfg.get("success")
        else None
    )
    drift_info = (
        drift(cand_runs, base_group_runs, rules, cand_rows=cand_rows, base_rows=base_rows) if base_group else None
    )
    attribution_info = attribution(cand_runs, [base_run] if base_run else [])
    tag = "evalops.gate" if label == "gate" else f"evalops.{label}"
    for r in cand_runs:
        ledger.log(r.run_id, tags={tag: "passed" if passed else "failed"})
    report = locks.scratch(cfg, f"{label}_report.md")
    report.write_text(
        report_md(
            label,
            passed,
            checks,
            cand,
            base,
            last.get("cost") if last.get("group") == group else None,
            reg.describe(),
            baseline=baseline,
            drift_info=drift_info,
            attribution_info=attribution_info,
        )
    )
    return {
        "ok": passed,
        "summary": f"{label} {'passed' if passed else 'FAILED'} for group {group}",
        "group": group,
        "baseline_group": base_group,
        "baseline": baseline,
        "aligned_by": aligned_by(cand_rows + base_rows) if (cand_rows or base_rows) else None,
        "reliability": reliability_info,
        "drift": drift_info,
        "attribution": attribution_info,
        "checks": checks,
        "report": str(report),
    }
