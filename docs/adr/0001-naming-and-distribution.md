# ADR 0001: Package and distribution naming

- Status: Accepted (final distribution name deferred to WP6.1)
- Date: 2026-10-23
- Decision: D1

## Context

The import package is `evalops`. Publishing under that name is not available and would collide with
an unrelated product.

**Observations, not timeless facts.** The claims below were checked once, on 2026-10-23, and are
recorded with their method so they can be re-verified rather than trusted:

| Claim | Observation (2026-10-23) | Method |
|---|---|---|
| PyPI `evalops` is an unrelated project | yes (version 0.0.6) | PyPI JSON API: `https://pypi.org/pypi/evalops/json` |
| PyPI `evalops-sdk` is taken | yes | PyPI JSON API: `https://pypi.org/pypi/evalops-sdk/json` |
| GitHub org `evalops` is owned by Deixic, Inc. | yes; ships a live EvalOps product | GitHub REST API: `https://api.github.com/orgs/evalops` |
| `evalplane`, `agentgates`, `evalops-core` are free on PyPI | yes at that time | PyPI JSON API: a 404 means the name is unclaimed |
| `evalplane`, `agentgates` are free as GitHub orgs | yes at that time | GitHub REST API: `https://api.github.com/orgs/<name>` |

Both namespaces change without notice: a name can be registered the day after an observation, so
every row above is re-checked in WP6.1 before anything is published. The name is therefore both
occupied and a trademark risk (R8).

Renaming the import package would break every user, plugin, and recorded import path for no
functional gain: Python import names and distribution names are independent.

## Decision

1. Keep the import package `evalops` for the v1 series.
2. Decouple the **distribution** name from the import name. Do not publish under `evalops` or
   `evalops-sdk`.
3. Pick the final distribution name in WP6.1, before the first public release. Candidates, in
   preference order: `evalplane`, `agentgates`, `evalops-core`. Re-verify availability immediately
   before publishing; this ADR is amended with the final choice.
4. The repository stays private until gate G4 and is published only on explicit instruction.

## Consequences

- Install commands in README/docs must use the distribution name; code always imports `evalops`.
- The packaging metadata (name, URLs, classifiers) changes without touching `src/evalops`.
- Plugin authors depend on the distribution name but import `evalops`; document both explicitly.
- A future rename of the import package is possible but would be a major version with a shim.
- Name availability is time-sensitive; the observations above carry the date they were made
  (2026-10-23) and WP6.1 must re-check PyPI and GitHub before publishing.
