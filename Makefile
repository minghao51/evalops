# Thin wrappers so humans, CI and agents use identical commands (any backend: set EVALOPS_ENV).
DATASET ?= smoke
STAGE   ?= dev
plugins:    ; evalops plugins
doctor:     ; evalops doctor
guard:      ; evalops guard
sync:       ; evalops sync all
plan:       ; evalops plan --dataset $(DATASET) --stage $(STAGE)
eval:       ; evalops eval --dataset $(DATASET) --stage $(STAGE)
gate:       ; evalops gate
cost:       ; evalops cost
test:       ; python -m pytest -q tests
