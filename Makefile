# Thin wrappers so humans, CI and agents use identical commands (any backend: set EVALOPS_ENV).
# This is a Makefile: the shellcheck pass below misreads Make conditionals as shell; ignore it.
# shellcheck disable=SC1050,SC1064,SC1065,SC1072,SC1073,SC1089
DATASET ?= smoke
STAGE   ?= dev
# Prefer the local venv, fall back to the interpreter on PATH (CI installs into it).
PYTHON  ?= $(shell command -v .venv/bin/python || echo python3)

.PHONY: check test test-unit test-integration test-standalone lint fmt typecheck contract clean \
        plugins doctor guard sync plan eval gate cost

# ---- dev loop: offline, no docker, no network ---------------------------------
check: lint typecheck test

test:             ; $(PYTHON) -m pytest -q
test-unit:        ; $(PYTHON) -m pytest -q tests/unit
test-integration: ; $(PYTHON) -m pytest -q tests/integration
# Dependency-free fallback (also how the v0.3 baseline was measured).
test-standalone:  ; $(PYTHON) tests/run_without_pytest.py

lint:
	$(PYTHON) -m ruff check .
	# src/ is being refactored concurrently in this phase; enforce formatting on the tree this
	# WP owns. Run `ruff format src tests` once src/ is quiet, then drop this scope.
	$(PYTHON) -m ruff format --check tests

fmt:
	$(PYTHON) -m ruff check --fix tests
	$(PYTHON) -m ruff format tests

typecheck: ; $(PYTHON) -m mypy

# Vendor contract tests need docker-local services; skip cleanly when unavailable.
contract:
ifeq ($(wildcard tests/contract),)
	@echo "SKIP contract: tests/contract not present yet"
else ifeq ($(shell command -v docker 2>/dev/null),)
	@echo "SKIP contract: docker not available"
else
	$(PYTHON) -m pytest -q -m contract tests/contract
endif

clean:
	find src tests -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info

# ---- evalops CLI wrappers -----------------------------------------------------
plugins: ; evalops plugins
doctor:  ; evalops doctor
guard:   ; evalops guard
sync:    ; evalops sync all
plan:    ; evalops plan --dataset $(DATASET) --stage $(STAGE)
eval:    ; evalops eval --dataset $(DATASET) --stage $(STAGE)
gate:    ; evalops gate
cost:    ; evalops cost
