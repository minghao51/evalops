# ADR 0003: Pydantic in core, dataclasses at the ports

- Status: Accepted
- Date: 2026-10-23
- Decision: D4

## Context

`evalops.yaml` is a user-facing contract. It needs validation, precise error paths for unknown keys,
merged environment semantics, and a generated JSON Schema that editors and CI can consume. Hand
written validation already produced ambiguous errors. The port layer and scorer API, by contrast,
are the plugin boundary: plugin authors should not be forced to depend on pydantic or on its
version.

Pydantic is a general-purpose validation library, not a vendor SDK, so it does not violate the
"core is vendor-free" rule (it becomes a regular dependency).

## Decision

1. Use **pydantic v2** for configuration models and for generating `docs/evalops.schema.json`.
2. Keep every **port type** in `src/evalops/ports.py` as a dataclass (`RowResult`, `Usage`,
   `LoadedPrompt`, `RunRecord`, `EvalResult`) and keep scorers/plugins pydantic-free.
3. Generate the schema from the models in CI and fail on drift; `evalops config schema` prints it.
4. Validation failures are exit 2 and name the offending key path.

## Consequences

- Core gains a runtime dependency (pydantic v2), raising the minimum install size; this is accepted
  because it is pure-Python and ubiquitous.
- Plugin and scorer authors keep a dependency-free API surface.
- The generated schema is a committed artifact and must be regenerated whenever config models change.
- The legacy `Config` shim must preserve `section()`, env expansion, and `EVALOPS_ENV` merge semantics
  so existing `evalops.yaml` files keep working (tested in WP0.3).
- Vendor imports remain forbidden in core; pydantic does not become a precedent for SDK imports.
