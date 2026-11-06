# Project Development Instructions

## Repository rules

- Commits are authored by the repository owner (0xNexuz) only. Never author commits as an AI assistant and never add `Co-Authored-By` or similar AI attribution lines to commits or PRs.
- The plugin lives in `hermes_rescue/` (catalog `subdir`). Keep it free of binaries, bytecode and symlinks, and keep `hermes plugins validate hermes_rescue --install-deps` passing (its security scan reads every file there, README included).
- Rescue stays local: no network calls, no shell commands, no model calls, never read `.env` or `auth.json`, never auto-approve. A change to any of these needs the README disclosures and `docs/catalog-entry.yaml` updated in the same commit.
- Code must run on Python 3.11–3.14 (Hermes's supported range).
- `hermes_rescue/README.md` is the root `README.md` minus its Development section (`tests/test_packaging.py` enforces it).

## Persistent project context

- Read `docs/PROJECT_CONTEXT.md` before relevant research, planning, architecture, implementation, verification, or submission work.
- Reuse established decisions and completed research. Do not repeat them without new evidence, staleness, contradiction, or explicit user instruction.
- Respect approved architecture decisions. Do not reconsider or replace them without new evidence or explicit user approval.
- On continuation or recovery requests, inspect the current branch, `HEAD`, and working-tree status; never assume local changes are committed.
- Recover the latest verified milestone, implemented-but-unverified work, blockers, approved scope, and next authorized action before making changes.
- Preserve the existing approved UI unless the user requests a redesign or correctness, accessibility, security, or usability requires a change.
- Match inspection and verification to the requested scope. Prefer targeted checks for ordinary development and comprehensive checks for security-critical or submission-readiness work.
- Maintain evidence honesty. Distinguish `PLANNED`, `PROPOSED`, `APPROVED`, `IMPLEMENTED`, `VERIFIED`, `SIMULATED`, `UNVERIFIED`, and `BLOCKED`. Never claim a test, deployment, integration, or milestone succeeded unless it was actually verified.
- Non-mutating research, evaluation, recommendation, and planning may continue within the requested goal. Do not begin implementation without approval, and do not treat a recommendation as implementation authorization.
- After an explicit build request, continue through coherent milestones inside the approved scope without repeated approval requests.
- Update `docs/PROJECT_CONTEXT.md` after material decisions, verified milestones, changed blockers, or changed evidence status.
- Keep project context concise. Update current state in place and append decision-log entries without deleting historical decisions.
- Do not deploy, publish, submit, spend funds, or modify external systems without explicit authorization.
