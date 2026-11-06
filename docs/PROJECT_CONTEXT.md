# Project Context

Keep this file concise and evidence-based. Update current state in place, append decision-log entries, and never store secrets.

## Project Identity

- **Project name:** Hermes Rescue (`hermes-rescue`)
- **Repository:** `0xNexuz/hermes-rescue` (public; owner: Eleazar / 0xNexuz, FUTUREUNI)
- **Project description:** Hermes Agent plugin that stops the agent from wrecking Hermes's own data and explains the real cause of failed replies, with `/rescue` and a Desktop pane.
- **Current workflow stage:** VERIFICATION → SUBMISSION_PREPARATION
- **Overall status:** IMPLEMENTED; core VERIFIED in real Hermes after two independent reviews; Desktop live view UNVERIFIED
- **Last updated date:** 2026-11-06

## Git State

- **Branch:** main, pushed to `origin` (github.com/0xNexuz/hermes-rescue, public)
- **HEAD:** d0f7653638ac93ac736b4d858b5cc15745a6919a (author/committer 0xNexuz <elikrypttt@gmail.com>, no AI attribution)
- **Working tree:** this file updated after the push (goes in with the screenshots commit)
- **Relevant remote or PR:** catalog PR https://github.com/NousResearch/hermes-agent/pull/150015 (open, 1 commit 04db5d3 from fork branch `add-hermes-rescue`, pins `cba030b037a758a95520bd58b92f2e814b94af7a`; no checks or reviews yet as of 2026-11-06)
- **Last verified milestone:** pushed commit installs from GitHub (`hermes plugins install 0xNexuz/hermes-rescue/hermes_rescue --enable`) and its catalog entry validates (2026-11-06)

## Hackathon Context

- **Competition:** Hermes Agent Plugin Hackathon
- **Organizer:** Nous Research
- **Tracks:** single; every kind of plugin eligible
- **Sponsors:** none
- **Deadline:** 5 PM EST, Wed 2026-11-11 (11 PM WAT)
- **Eligibility:** catalog PR + demo on X tagging @NousResearch (under 90 s) + official form
- **Judging:** usefulness, reliability, polish, creativity; favours more than a prompt/skill could do
- **Official source links:** https://nousresearch.com/ ; research doc https://claude.ai/artifact/EFsfeGvm5tRfo5pWxTRNJ2

## Strategic Decisions

- **Selected idea:** Rescue — self-repair guard (#144562) + real-cause notes (#147620) + `/rescue` + Desktop pane
- **Why it was selected:** both are open pain clusters filed by Hermes maintainers with no fix in flight; plugin-only mechanisms (hooks); helps developers and non-technical users; open ground in a 1,037-entry catalog
- **Rejected alternatives:** Scout (plugin recommender), Owner's Diary, Snap-a-Bill, local-language voice; spend/safety/undo/crypto plugins (crowded)
- **Competitive advantages:** proven in real Hermes runs; passes the catalog validator; local-only; works on every surface
- **Intended target users:** anyone running Hermes — developers on CLI, everyday users on Desktop/Telegram/WhatsApp

## Architecture Decisions

- **Technology stack:** Python plugin (stdlib only) + Desktop half (plain JS, `@hermes/plugin-sdk`) + FastAPI backend file
- **Core architecture:** `guard.py` (pure verdicts) · `causes.py` (pure rule book) · `store.py` (SQLite via `plugin_db`) · `snapshots.py` · `runtime.py` (hooks) · `commands.py` (`/rescue`) · `dashboard/plugin_api.py` · `desktop/plugin.js`
- **Required integrations:** Hermes hooks `pre_tool_call`, `post_tool_call`, `api_request_error`, `post_api_request`, `transform_llm_output`, `on_session_end`; `ctx.register_command`, `ctx.ask_choice`, `ctx.post_message`; `broadcast_plugin_event`
- **Security assumptions:** never auto-approve; never read/copy/move `.env`, `auth.json` or Hermes's other credential stores; keys (any depth), whole home, whole profiles and Rescue's own records are never unlockable; a bug in the *check* fails open, a failure in *recording* still enforces
- **Important technical constraints:** core skips `transform_llm_output` and `on_session_end` for turns that exhaust retries → failed-turn note goes via `post_message` (immediate on local surfaces, 1.5 s delay in chat apps, edited on late recovery)

## Development Progress

- **Approved implementation scope:** full Rescue plugin as above (approved 2026-11-05: "lets go for rescue, lets begin")
- **Completed features:** guard; snapshots + undo; real-cause notes (transform + post paths); `/rescue` (status, log, snapshots, undo, allow, lock); Desktop backend routes; Desktop UI code; README, catalog entry, demo kit, submission docs
- **Implemented but unverified:** nothing known; Desktop half verified on the owner's Windows laptop (chip, pane, toast, undo, backend)
- **Pending features:** screenshots; optional Telegram demo recording
- **P0 priorities:** demo video (under 90 s) and X post tagging @NousResearch; then the official form with the PR and X links before 5 PM EST Nov 11 (11 PM WAT)
- **P1 priorities:** answer review comments on #150015 (a new pin means a new SHA and re-review)
- **P2 priorities:** issue to Nous about the skipped hooks on exhausted turns
- **P3 priorities:** Scout (plugin recommender) as a follow-up
- **Current milestone:** catalog PR opened; demo + X post + form remain
- **Known blockers:** Desktop screenshots require the owner's machine; attaching the fork for a PR branch was refused by this session's permission check (owner can open the PR from the browser)

## Verification

### Tests Actually Executed

| Date | Command or check | Environment | Result | Evidence or notes |
|---|---|---|---|---|
| 2026-11-05 | `pytest` (82 tests) | Python 3.14.9 and 3.13.18 | VERIFIED | all pass (superseded below) |
| 2026-11-05 | `hermes plugins validate hermes_rescue --install-deps` | Hermes main @ 355a417 | VERIFIED | all checks pass incl. security scan, desktop surface |
| 2026-11-05 | `scripts/validate_plugin_catalog.py` on the catalog entry (dummy SHA) | Hermes main | VERIFIED | OK |
| 2026-11-05 | Real Hermes turns via gateway with scripted model server | Hermes main, Py 3.14 | VERIFIED | memories delete blocked under `--yolo`; missing local model + dead key explained via `post_message` (ok=True); config.yaml edit → approval + snapshot; guard lifted → delete → `/rescue undo` restored content; `/rescue` and `/rescue log` work |
| 2026-11-05 | Desktop plugin render smoke test | Node 22 + React 19, SDK stubbed | SIMULATED | chip + pane render in good/bad/error states; Undo POSTs; events toast + OS notify |
| 2026-11-05 | Independent review (separate agent, read-only) | Py 3.14 / 3.11 | DONE | 3 blockers, 9 should-fix, nits — all fixed 2026-11-06 |
| 2026-11-06 | `pytest` (151 tests, incl. a regression test per review finding) | Python 3.14.9, 3.13.18, 3.11.18 | VERIFIED | 151 passed on each |
| 2026-11-06 | Reviewer's probe scripts (103 everyday commands, cwd=~, source checkout, quoted text, heredocs, slip-throughs) | Py 3.14 | VERIFIED | 102/103 allowed (the 1 is Rescue's own snapshots folder, intended); all listed slip-throughs now caught |
| 2026-11-06 | `hermes plugins validate hermes_rescue --install-deps` | Hermes main @ 355a417 | VERIFIED | passed; security scan safe; no private core imports |
| 2026-11-06 | Catalog entry validator (dummy SHA) | Hermes main | VERIFIED | OK |
| 2026-11-06 | `demo/run_demo.sh` in a fresh home | Hermes main, Py 3.14 | VERIFIED | all three scenarios as expected |
| 2026-11-06 | Live `state.db` check (scratch script: gateway running, raw DELETE blocked; guard off → delete → `/rescue undo`) | Hermes main, Py 3.14, sqlite3 CLI shim | VERIFIED | 10 messages brought back, reply delivered, gateway kept working; `integrity_check` ok, both FTS indexes pass integrity-check |
| 2026-11-06 | Desktop backend routes (`/health`, `/undo`, `/guard`) called in-process | Hermes main, temp home | VERIFIED | correct data; undo ok/false flags; bad ids handled |
| 2026-11-06 | Second independent review (separate agent, read-only) | Py 3.14, real SessionDB | DONE | 5 blockers, 8 should-fix, 11 nits; all fixed except documented limits |
| 2026-11-06 | `pytest` (195 tests) after second-review fixes | Python 3.14.9, 3.13.18, 3.11.18 | VERIFIED | 195 passed on each |
| 2026-11-06 | Both reviewers' probe scripts after fixes | Py 3.14 | VERIFIED | 312/312 everyday runs allowed (review 2), 102/103 (review 1, the 1 intended); 0 fuzz crashes; 2 MB one-liner well under the 30 s hook timeout; remaining allows are documented limits or out of scope (reading keys; git in the checkout, which core refuses) |
| 2026-11-06 | `hermes plugins validate hermes_rescue --install-deps` after fixes | Hermes main @ 355a417 | VERIFIED | passed; security scan safe |
| 2026-11-06 | Status bar on the owner's laptop (Ctrl+Shift+S) | Windows, Hermes Desktop v0.23.0 | VERIFIED | Rescue chip shows at the right with a red dot after a block |
| 2026-11-06 | Owner's Windows laptop after `hermes plugins update` | Windows, real model | VERIFIED | `echo … >> SOUL.md` → approval → Run → pane Undo → toast "Put back snapshot #2 (SOUL.md)". (The agent refused to edit config.yaml by itself, following Hermes's own guidance — so the demo uses SOUL.md.) |
| 2026-11-06 | Owner's Windows laptop, Hermes Desktop (local install), Rescue installed from GitHub with `hermes plugins install … --enable`, both halves on | Windows, real model | VERIFIED | `rm -rf "$LOCALAPPDATA/hermes/memories"` blocked with toast, pane "1 stopped", reply footer; `echo … >> config.yaml` went to Desktop's approval box, pane "asked you first" + snapshot #1 with Undo. Found: Undo clicked before approving marked the snapshot used ("already as saved") → fixed; Desktop's approval box shows the command, not Rescue's reason → docs corrected; status-bar chip not seen yet |
| 2026-11-06 | Windows path rules (Git Bash `/c/…`, `C:\` paths, `%LOCALAPPDATA%`, `$env:`, PowerShell/cmd deletes) | Linux, `use_windows_paths(True)` + PureWindowsPath | SIMULATED | 22 tests pass; first real Windows run is the owner's (native Windows home = `%LOCALAPPDATA%\hermes`) |
| 2026-11-06 | `demo/run_demo.sh` + live `state.db` check rerun after second-review fixes | Hermes main, Py 3.14 | VERIFIED | same results: blocks under `--yolo`, real cause note, undo of memories and of deleted messages (10 back, gateway kept working), project build cleanup ran |

### Integration and Evidence Status

- **Real integrations:** Hermes hooks, slash command, approvals gate, `post_message`, plugin storage — all VERIFIED in real runs
- **Simulated functionality:** Desktop UI rendering (stubbed SDK)
- **Verified evidence links:** none public yet
- **Unverified claims:** none known

## Decision Log

### 2026-11-05 — Build Rescue
- **Decision:** Build Rescue for the Plugin Hackathon
- **Reason:** strongest evidence, plugin-only mechanism, serves devs and everyday users
- **Alternatives considered:** Scout, Owner's Diary, Snap-a-Bill, local-language voice
- **Evidence:** research doc (catalog saturation map of 1,037 entries, past winners, GitHub pain points)
- **Impact:** scope as above
- **Status:** APPROVED

### 2026-11-05 — Re-aim "explain errors" at what core misses
- **Decision:** focus on the self-repair guard (#144562) and the real-cause chain (#147620), not generic API-error copy
- **Reason:** Hermes core already writes plain-language copy for most single failures; duplicating it would read as redundant
- **Alternatives considered:** a full error-explainer for every failure
- **Evidence:** `agent/turn_failure_copy.py` on main; both issues open with no PR
- **Impact:** pitch now answers two maintainer-filed issues
- **Status:** APPROVED (user informed)

### 2026-11-05 — Deliver failed-turn notes with post_message
- **Decision:** post the note at once on local surfaces, after 1.5 s in chat apps; edit on recovery
- **Reason:** exhausted turns skip `transform_llm_output` and `on_session_end` (traced in a real run)
- **Alternatives considered:** `hold_stream=True` (disables streaming profile-wide — rejected)
- **Evidence:** hook trace; `agent/conversation_loop.py` early return before `finalize_turn`
- **Impact:** a short-lived timer thread, disclosed
- **Status:** APPROVED

### 2026-11-05 — Keys, home and install are never unlockable
- **Decision:** `/rescue allow` never unlocks `.env`, `auth.json`, the whole home or the install; snapshots never copy credentials
- **Reason:** catalog rule 11 (no credential reads) and irreversible loss
- **Status:** APPROVED

### 2026-11-06 — Fix the independent review's findings
- **Decision:** rewrite the shell check (quote/heredoc aware, cd/workdir/git -C, sqlite stdin, bash -c, xargs, globs); ignore relative paths when the folder is unknown; leave a Hermes checkout unguarded when working inside it and make program files unlockable; keys protected at any depth; profiles matched as homes; Rescue's own records never unlockable; default home guarded while a profile is active
- **Reason:** review reproduced non-liftable blocks on normal work (cwd = ~, contributors' checkouts) and slip-throughs of the #144562 shapes
- **Status:** IMPLEMENTED, VERIFIED (tests + probes + real runs)

### 2026-11-06 — Undo of state.db adds back missing rows instead of rolling back
- **Decision:** `/rescue undo` merges missing `sessions`/`messages` rows from the snapshot into the live `state.db` (INSERT OR IGNORE; Hermes's triggers keep FTS and display order); never a wholesale rollback; full copy kept in the snapshot
- **Reason:** a backup-API rollback restored the rows but erased the running gateway's own bookkeeping (session_admissions), so the undo reply was never delivered (seen in a real run)
- **Alternatives considered:** file replace (stale WAL replays the damage), full backup-API restore (breaks the live gateway)
- **Status:** IMPLEMENTED, VERIFIED (real run + integrity checks)

### 2026-11-06 — Windows support
- **Decision:** path rules switch to Windows style on Windows (case-insensitive, `\` and `/`), Git Bash drive paths converted, `%LOCALAPPDATA%`/`$env:` expanded, PowerShell and cmd delete/move/copy/write commands read; SQLite opened through portable URIs
- **Reason:** the owner tests on native Windows, where the home is `%LOCALAPPDATA%\hermes` and the agent's terminal is Git Bash; without this the guard missed Windows paths
- **Status:** IMPLEMENTED, SIMULATED (tests on Linux); real Windows run pending

### 2026-11-06 — Credential list mirrored, not imported
- **Decision:** bundle a copy of Hermes's credential-store list instead of importing `hermes_cli.profiles`
- **Reason:** the validator flags private core imports (catalog rule 9)
- **Status:** APPROVED

### 2026-11-06 — Own shell lexer instead of shlex
- **Decision:** a one-pass lexer for quotes, comments, here-documents, `$(…)`/backticks, `${…}`, operators; brace expansion; substitutions checked as commands
- **Reason:** second review: comments with apostrophes hid commands, quadratic shlex on long words (Hermes fails a `pre_tool_call` hook CLOSED after 30 s), substitutions and braces slipped through
- **Status:** IMPLEMENTED, VERIFIED

### 2026-11-06 — Folder undo never moves a live database; whole homes/profiles never snapshotted
- **Decision:** copies into a folder target the file inside it; databases added since are left in place; Hermes state DBs inside folders are merged; merge skips messages without their conversation
- **Reason:** second review showed a profile's live state.db moved away or rolled back by folder undo
- **Status:** IMPLEMENTED, VERIFIED (tests)

## Next Action

- **Next recommended action:** owner runs Rescue in Hermes Desktop, captures screenshots, provides git identity; push to `0xNexuz/hermes-rescue`; open the catalog PR by Nov 9
- **Next authorized action:** local edits and verification only
- **Next approved milestone:** submission package
- **Approval boundary:** pushing to GitHub, opening the catalog PR, posting on X and submitting the form need the owner's explicit go-ahead
