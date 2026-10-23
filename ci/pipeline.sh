#!/usr/bin/env bash
# CI- and backend-agnostic pipeline. Any CI (GitHub, GitLab, Azure DevOps, Jenkins, Databricks job,
# cron...) calls ONE of:  ci/pipeline.sh pr | main | release | nightly
# The backend is chosen by EVALOPS_ENV (see evalops.yaml environments): mlflow, databricks,
# langfuse, langsmith, logfire, local, or your own.
#
# Optional env: EVALOPS_PR, EVALOPS_BRANCH (pr: branch to push lock files to),
#   EVALOPS_ALLOW_FROZEN_CHANGE (from an approval label), EVIDENCE_GROUP (release),
#   SMOKE_DATASET / GOLDEN_DATASET / CALIBRATION_DATASET (defaults: smoke / golden_regression /
#   judge_calibration). Tools replay from cassettes in pr/main (tools.mode_by_stage); nightly runs live.
set -euo pipefail
STAGE="${1:?usage: ci/pipeline.sh pr|main|release|nightly}"
export EVALOPS_ACTOR="${EVALOPS_ACTOR:-ci}"
SMOKE="${SMOKE_DATASET:-smoke}"
GOLDEN="${GOLDEN_DATASET:-golden_regression}"
CALIB="${CALIBRATION_DATASET:-judge_calibration}"
mkdir -p .evalops/out

ev() { evalops --json "$@" | tee -a .evalops/out/ci_log.jsonl; }
json_get() { python3 -c "import json,sys; d=json.load(open(sys.argv[1])); print(eval(sys.argv[2], {}, {'d': d}))" "$@"; }

commit_state() {  # lock files (+ git-plugin store/aliases) are versioned state
  git add -A .evalops/locks .evalops/store 2>/dev/null || true
  if git diff --cached --quiet; then return 0; fi
  if [[ -z "${EVALOPS_BRANCH:-}" ]]; then
    echo "evalops state changed but EVALOPS_BRANCH is unset; commit .evalops/locks and .evalops/store" >&2
    return 4
  fi
  git config user.name "${GIT_BOT_NAME:-evalops-bot}"
  git config user.email "${GIT_BOT_EMAIL:-evalops-bot@users.noreply.local}"
  git commit -m "chore(evalops): update versioned state [skip ci]"
  git push origin "HEAD:${EVALOPS_BRANCH}"
}

case "$STAGE" in
  pr)
    ev guard
    ev sync all                         # immutable PR versions, tagged with EVALOPS_PR
    commit_state
    if [[ -f "datasets/${CALIB}.jsonl" ]]; then ev calibrate --dataset "$CALIB"; fi   # judges first
    ev plan --dataset "$SMOKE" --stage pr
    ev eval --dataset "$SMOKE" --stage pr
    ev gate                             # writes .evalops/out/gate_report.md for the PR comment
    ;;
  main)
    ev guard
    ev sync all --check                 # state must already be committed by the PR stage
    if [[ -f "datasets/${CALIB}.jsonl" ]]; then ev calibrate --dataset "$CALIB"; fi
    ev plan --dataset "$GOLDEN" --stage main
    ev eval --dataset "$GOLDEN" --stage main
    ev gate
    GROUP=$(json_get .evalops/out/last_eval.json "d['group']")
    ev promote --alias candidate --evidence-group "$GROUP"
    EVALOPS_BRANCH="${EVALOPS_BRANCH:-main}" commit_state   # git plugin: alias moves are commits
    echo "evidence group for release: $GROUP"
    ;;
  release)
    : "${EVIDENCE_GROUP:?set EVIDENCE_GROUP to the main-stage group id}"
    ev promote --alias production --evidence-group "$EVIDENCE_GROUP"
    EVALOPS_BRANCH="${EVALOPS_BRANCH:-main}" commit_state
    ;;
  nightly)
    # Control run: re-evaluate the UNCHANGED production version to detect platform/judge drift.
    evalops --json baseline > .evalops/out/baseline.json
    SHA=$(json_get .evalops/out/baseline.json "(d.get('baseline') or {}).get('git_sha') or ''")
    if [[ -z "$SHA" ]]; then echo "no baseline yet; skipping control run"; exit 0; fi
    ORIG=$(git symbolic-ref -q --short HEAD || git rev-parse HEAD)
    git -c advice.detachedHead=false checkout -q "$SHA"
    trap 'git checkout -q "$ORIG"' EXIT
    ev eval --dataset "$GOLDEN" --stage control
    ev gate --label drift               # exit 1 if scores or cost moved beyond tolerance
    ;;
  *) echo "unknown stage $STAGE" >&2; exit 2 ;;
esac
