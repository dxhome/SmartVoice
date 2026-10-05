# Documentation guide

This directory contains maintained product and engineering documentation. It is not a scratchpad for temporary investigation notes.

## Top-level documents

Keep only the current, maintained descriptions of the product and its development process at this level:

| Document | Purpose |
| --- | --- |
| `api-spec.md` | HTTP endpoints, request/response contracts, errors, and limits. |
| `architecture-guidelines.md` | Architecture principles plus the current implementation/platform snapshot. |
| `implementation-plan.md` | Prioritized product roadmap and open implementation/acceptance work. |
| `industry-research.md` | Durable external research and model/technology comparisons. |
| `requirements-spec.md` | Product requirements and scope. |
| `releasing.md` | Release process and packaging/release checklist. |
| `testing.md` | Standard test suites and supported validation workflows. |

The repository-level `README.md` remains the user-facing introduction and quick start.

## Subdirectories

- `archive/` stores completed, superseded, or historical investigation reports. Preserve their dates, scope, limitations, and links to reproducible raw evidence. The STT reports are indexed in [`archive/stt/README.md`](archive/stt/README.md).
- `release/` is for version-specific release notes, acceptance evidence, and release artifacts that should be reviewed as one release package. Keep the reusable release process in the top-level `releasing.md`.

## Adding documentation

- Do not place temporary notes, draft plans, per-run logs, generated transcripts, or scratch reports in `doc/`. Keep transient work in the conversation or ignored `sandbox/` area.
- Add a top-level document only when it becomes a maintained, canonical description of product requirements, API, architecture, roadmap, research, testing, or the release process. Update this index when doing so.
- Put completed or superseded investigation reports under `archive/<topic>/`; avoid adding another top-level report for each experiment. Add or update the topic index and cross-links.
- Put version-specific release notes and acceptance packages under `release/<version>/`. Do not duplicate the general release procedure there.
- Prefer updating the existing canonical document when the information is current policy or product behavior. Keep detailed experimental evidence separate from normative requirements and clearly label unverified results.
- Every retained validation report should state date, scope, environment, commands, evidence location, limitations, and whether its result is current or historical. Keep generated data out of Git unless it is approved as a durable benchmark fixture or release artifact.
