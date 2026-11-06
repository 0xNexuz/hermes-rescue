# Submission checklist — Hermes Agent Plugin Hackathon

Deadline: **5 PM EST, Wed Nov 11, 2026 (11 PM WAT)**. Entries are reviewed every few days and catalog
checks only run once a maintainer starts them, so open the PR early (target: Mon Nov 9).

## 1. Repository

- [ ] Public repo `0xNexuz/hermes-rescue`, commits authored by 0xNexuz only.
- [ ] `pytest` passes and `hermes plugins validate hermes_rescue --install-deps` passes on the pushed commit.
- [ ] Desktop half checked on a real machine: status-bar chip, pane, Undo, toast. Screenshots saved to
      `docs/screenshots/` and committed.
- [ ] Code frozen before the PR (every SHA change is re-reviewed).

## 2. Catalog PR to `NousResearch/hermes-agent`

- [ ] Fork, branch, add `plugin-catalog/hermes-rescue.yaml` from `docs/catalog-entry.yaml`.
- [ ] Replace both `<SHA>` placeholders with the full 40-character SHA (`git rev-parse HEAD`).
- [ ] Add `screenshots:` URLs (raw.githubusercontent.com, pinned to the same SHA).
- [ ] `python3 scripts/validate_plugin_catalog.py plugin-catalog/hermes-rescue.yaml` → OK.
- [ ] PR title: `plugin-catalog: add hermes-rescue (Plugin Hackathon entry)`.
- [ ] PR body: the text below, plus screenshots.

### PR body (draft)

> **Hermes Agent Plugin Hackathon entry.**
>
> Rescue keeps the agent from breaking Hermes and says what really went wrong. It targets two open
> pain clusters: #144562 (agents destructively "repairing" their own install) and #147620 (failure
> notices that hide the cause the product already knows).
>
> **Surfaces:** `pre_tool_call` (block / approve), `post_tool_call`, `api_request_error`,
> `post_api_request`, `transform_llm_output`, `on_session_end`; a `/rescue` slash command
> (uses `ctx.ask_choice` and `ctx.post_message`); a Desktop half (`desktop/plugin.js`: status bar +
> pane) with a `dashboard/plugin_api.py` backend.
>
> **Disclosures:** reads tool-call arguments; writes `plugin-data/hermes-rescue/rescue.db` and
> snapshots under `plugin-data/hermes-rescue/snapshots/` (never `.env`, `auth.json` or the other
> credential stores Hermes lists); undo of `state.db` only adds back missing conversations and
> messages; a short-lived background thread posts the failed-turn note with `ctx.post_message`;
> never auto-approves; no network, shell, model calls, stored credentials or telemetry.
>
> **Validation:** `hermes plugins validate --install-deps` passes (security scan safe, desktop
> surface OK, no private core imports). 195 tests on Python 3.11, 3.13 and 3.14. Reproduce end to
> end with `demo/run_demo.sh` (scripted model, throwaway home).
>
> **Found while building:** a turn that runs out of retries returns before `finalize_turn`, so
> neither `transform_llm_output` nor `on_session_end` fires for it, although the hooks doc says
> `on_session_end` always fires. Rescue works around it with `post_message`; happy to open an issue.

## 3. Demo and form

- [ ] Record the demo (`docs/DEMO.md`), under 90 seconds, plugin visibly working.
- [ ] Post it on X tagging @NousResearch (a YouTube link alone doesn't count).
- [ ] Submit the PR link and the X post link through the official form.
- [ ] Optional: share in the Nous Discord `#plugins-skills-and-skins` channel.
