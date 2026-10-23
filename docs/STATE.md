# evalops state board

Updated: 2026-10-23 · owner: orchestrator · detail: [`IMPLEMENTATION_MAP.md`](IMPLEMENTATION_MAP.md)

## Where we are

- **Phase:** 0 (bedrock) — G0 accepted, WP0.3 in progress.
- **Tests:** 55 green (`make check`), 17 at baseline.
- **Gates passed:** G0 (contract, formats, tooling) — with two tracked compromises below.
- **Next:** WP0.3 (typed config) → WP0.5 (envelope freeze) → WP0.6 (state/changelog/CI).

## Work packages

| WP | State | Commit |
|---|---|---|
| 0.0 repo + agents + map | done | `16ce2e1` |
| 0.1 contract doc + ADRs | done | `6280873` |
| 0.2 dev tooling + test layout + vendor-import guard | done | `6280873`…`09c26fb` |
| 0.4 versioned formats + semantic fingerprints | done | `6280873` |
| 0.3 typed config (pydantic) + JSON schema | in progress | — |
| 0.5 envelope freeze + exit-code goldens | pending | — |
| 0.6 STATE/CHANGELOG + first real CI run | in progress (this file) | — |

## Known compromises (tracked, not forgotten)

1. **CI matrix never ran.** `.github/workflows/ci.yml` targets 3.10–3.12; only 3.12 has been executed
   locally. Addressed in WP0.6 (needs the first push).
2. **`src/` is not format-enforced.** `make check` runs `ruff format --check tests` only; `src/` is
   `ruff check`-clean. Remove the scope restriction in WP1.1 once `src/` is quiet.
3. **Vendor plugins remain unverified.** Every `VERIFY` marker in `plugins/` is a phase 3 contract
   test, not a claim. The `logfire` usage lookup (private SQL) is the highest risk.
4. **No redaction.** Traces, rows, cassettes and artifacts store payloads verbatim (length-clipped
   only). WP1.1 is the fix; do not use evalops on real PII data before then.
5. **Package name.** PyPI `evalops` / `evalops-sdk` and the GitHub org `evalops` belong to another
   product (Deixic, Inc.). Import name stays `evalops`; distribution name is decided in WP6.1
   (`evalplane` and `agentgates` are free). See ADR 0001.

## How to work in this repo

```bash
make check            # lint + typecheck + unit/integration tests (offline)
make test             # pytest only
make test-standalone  # dependency-free runner (no pytest)
make schema           # regenerate docs/evalops.schema.json
```

Rules that matter: persisted formats are additive and versioned; a green suite is not evidence of a
met requirement; no vendor import outside `plugins/` and `traces/`; never weaken a gate to pass.
