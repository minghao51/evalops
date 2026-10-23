"""Judge calibration: evaluate the evaluator before it gates anything.

A calibration dataset holds PRE-RECORDED outputs (and optionally traces) with human labels:
  {"inputs": {...}, "outputs": "...", "trace": {...optional AgentTrace dict...},
   "expectations": {"human_labels": {"helpful": 1, "trajectory_quality": 0}}}
`evalops calibrate --dataset judge_calibration` scores them with your scorers (no generation) and
compares to the labels. evalops.yaml:
  judges:
    helpful: {min_agreement: 0.85, min_kappa: 0.6}                 # binary: threshold 0.5 by default
    trajectory_quality: {label: traj_ok, max_mae: 0.15, threshold: 0.5}
"""
import json

from . import locks
from .registry import Registry
from .run import evaluate
from .util import EXIT_USAGE, EvalOpsError


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
    rules = cfg.section("judges")
    names = scorers or list(rules)
    if not names:
        raise EvalOpsError("no judges to calibrate: add a `judges:` section or pass --scorer", EXIT_USAGE)
    out = evaluate(cfg, dataset, "dev", repeats=1, judge_only=True, allow_dirty=allow_dirty, kind="calibration",
                   scorer_filter=set(names))
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
        st = results[name] = _stats(pairs, float(rule.get("threshold", 0.5)))
        for key, op in (("min_agreement", ">="), ("min_kappa", ">="), ("max_mae", "<=")):
            if key in rule:
                val = st[key.split("_", 1)[1]]
                ok = val >= rule[key] if op == ">=" else val <= rule[key]
                checks.append({"check": f"{name}: {key.split('_', 1)[1]} {op} {rule[key]}", "passed": ok,
                               "candidate": val})
    passed = all(c["passed"] for c in checks)
    ledger = Registry(cfg).get("ledger")
    for run_id in out["run_ids"]:
        ledger.log(run_id, metrics={f"judge.{n}.{k}": v for n, s in results.items() for k, v in s.items()},
                   tags={"evalops.calibration": "passed" if passed else "failed"})
    md = [f"## evalops judge calibration: {'✅ PASSED' if passed else '❌ FAILED'}", "",
          "| Judge | n | agreement | kappa | MAE |", "|---|---|---|---|---|"]
    md += [f"| {n} | {s['n']} | {s['agreement']} | {s['kappa']} | {s['mae']} |" for n, s in results.items()]
    md += ["", "| Check | Result |", "|---|---|"] + [f"| {c['check']} | {'pass' if c['passed'] else '**FAIL**'} |"
                                                     for c in checks]
    report = locks.scratch(cfg, "calibration_report.md")
    report.write_text("\n".join(md) + "\n")
    return {"ok": passed, "summary": f"judge calibration {'passed' if passed else 'FAILED'} on {dataset}",
            "judges": results, "checks": checks, "group": out["group"], "report": str(report),
            "judge_meta": json.dumps(cfg.hook("judge_meta")())}
