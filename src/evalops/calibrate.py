"""Judge calibration: evaluate the evaluator before it gates anything.

A calibration dataset holds PRE-RECORDED outputs (and optionally traces) with human labels:
  {"inputs": {...}, "outputs": "...", "trace": {...optional AgentTrace dict...},
   "expectations": {"human_labels": {"helpful": 1, "trajectory_quality": 0}}}
`evalops calibrate --dataset judge_calibration` scores them with your scorers (no generation) and
compares to the labels. evalops.yaml:
  judges:
    helpful: {min_agreement: 0.85, min_kappa: 0.6}                 # binary: threshold 0.5 by default
    trajectory_quality: {label: traj_ok, max_mae: 0.15, threshold: 0.5}
An omitted threshold (or an explicit `null`, e.g. `max_mae: null`) means "no such gate": the check
is not emitted at all, rather than compared against a missing value.
"""

import json
from typing import Any

from . import judges, locks
from .registry import Registry
from .run import evaluate
from .util import EXIT_USAGE, EvalOpsError

DEFAULT_THRESHOLD = 0.5  # binarization cut-off when a judge rule does not set one


def _threshold(value: Any) -> float:
    """The binarization threshold: omitted OR explicit null means the 0.5 default.

    Same defect class as `max_mae: null`: `float(None)` used to raise TypeError from inside
    calibration, and only when a config set the key to null. A non-numeric value is a usage error
    (exit 2), never an unhandled ValueError (exit 5).
    """
    if value is None:
        return DEFAULT_THRESHOLD
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise EvalOpsError(f"judges threshold must be a number, got {value!r}", EXIT_USAGE) from exc


def _stats(pairs, threshold):
    n = len(pairs)
    pb = [(p >= threshold, h >= threshold) for p, h in pairs]
    agree = sum(a == b for a, b in pb) / n
    p1, h1 = sum(a for a, _ in pb) / n, sum(b for _, b in pb) / n
    pe = p1 * h1 + (1 - p1) * (1 - h1)
    kappa = 1.0 if pe >= 1 else (agree - pe) / (1 - pe)
    mae = sum(abs(p - h) for p, h in pairs) / n
    return {"n": n, "agreement": round(agree, 4), "kappa": round(kappa, 4), "mae": round(mae, 4)}


def calibrate(cfg, dataset, scorers=None, allow_dirty=False) -> dict:
    # Same load-time rejection as plan/evaluate (WP3b.1): a bad declared spec is exit 2 before the
    # calibration evaluate() spends anything or writes a run row.
    judges.load(cfg)
    rules = cfg.section("judges")
    names = scorers or list(rules)
    if not names:
        raise EvalOpsError("no judges to calibrate: add a `judges:` section or pass --scorer", EXIT_USAGE)
    # WP3b.5 B3: the third load point (`plan`, `evaluate`, `calibrate`). A `judge_meta()` identity
    # that disagrees with a declared spec is exit 2 before the calibration run is recorded (the
    # no-judges error above is unchanged, so the hook is not called on that path).
    meta = cfg.hook("judge_meta")()
    judges.conflicts_with_meta(cfg, meta)
    out = evaluate(
        cfg,
        dataset,
        "dev",
        repeats=1,
        judge_only=True,
        allow_dirty=allow_dirty,
        kind="calibration",
        scorer_filter=set(names),
    )
    rows = out.pop("_rows")[0]
    results, checks = {}, []
    for name in names:
        rule = rules.get(name) or {}
        label = rule.get("label", name)
        pairs = []
        for r in rows:
            human = ((r.expectations or {}).get("human_labels") or {}).get(label)
            pred = r.scores.get(name)
            if human is not None and pred is not None:
                pairs.append((float(pred), float(human)))
        if not pairs:
            checks.append({"check": f"{name}: labeled+scored pairs", "passed": False, "candidate": 0})
            continue
        st = results[name] = _stats(pairs, _threshold(rule.get("threshold")))
        for key, op in (("min_agreement", ">="), ("min_kappa", ">="), ("max_mae", "<=")):
            limit = rule.get(key)
            if limit is None:  # not set, or explicitly null: there is no threshold to compare to
                continue
            val = st[key.split("_", 1)[1]]
            ok = val >= limit if op == ">=" else val <= limit
            checks.append({"check": f"{name}: {key.split('_', 1)[1]} {op} {limit}", "passed": ok, "candidate": val})
    passed = all(c["passed"] for c in checks)
    ledger: Any = Registry(cfg).get("ledger")  # Registry.get is unannotated; the role check guarantees a Ledger
    for run_id in out["run_ids"]:
        ledger.log(
            run_id,
            metrics={f"judge.{n}.{k}": v for n, s in results.items() for k, v in s.items()},
            tags={"evalops.calibration": "passed" if passed else "failed"},
        )
    md = [
        f"## evalops judge calibration: {'✅ PASSED' if passed else '❌ FAILED'}",
        "",
        "| Judge | n | agreement | kappa | MAE |",
        "|---|---|---|---|---|",
    ]
    md += [f"| {n} | {s['n']} | {s['agreement']} | {s['kappa']} | {s['mae']} |" for n, s in results.items()]
    md += ["", "| Check | Result |", "|---|---|"] + [
        f"| {c['check']} | {'pass' if c['passed'] else '**FAIL**'} |" for c in checks
    ]
    report = locks.scratch(cfg, "calibration_report.md")
    report.write_text("\n".join(md) + "\n")
    return {
        "ok": passed,
        "summary": f"judge calibration {'passed' if passed else 'FAILED'} on {dataset}",
        "judges": results,
        "checks": checks,
        "group": out["group"],
        "report": str(report),
        "judge_meta": json.dumps(meta),
    }
