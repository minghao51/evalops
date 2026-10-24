#!/usr/bin/env bash
# Thin shim: evalops pipeline <pr|main|release|nightly> [--dry-run]
# The stage table lives in src/evalops/pipeline.py (see `evalops pipeline --help`); this file exists
# only so existing CI configurations that call `ci/pipeline.sh` keep working. Every argument is
# forwarded, so `ci/pipeline.sh pr --dry-run` and the usage message are the CLI's own.
set -euo pipefail
exec evalops pipeline "$@"
