# 🛟 Hermes Rescue

**Keeps your agent from breaking Hermes, and tells you what really went wrong.**

Two things go wrong for Hermes users more than they should, whether they are developers or not:

1. **You ask your agent to "fix Hermes", and it breaks it.** Agents delete their own saved conversations, hand-edit `config.yaml`, or run raw SQL on `state.db` while "repairing" the install they run from. One user lost 237 sessions this way. Hermes maintainers track this as a pain cluster in [#144562](https://github.com/NousResearch/hermes-agent/issues/144562).
2. **A reply fails and the message doesn't say why.** The real cause (a fallback key that's dead, a local server that hasn't loaded the model) sits in the logs while the chat says "temporarily unavailable, wait and retry". That's [#147620](https://github.com/NousResearch/hermes-agent/issues/147620).

Rescue fixes both, on every surface Hermes runs on: the CLI, the TUI, Hermes Desktop, Telegram, WhatsApp, Discord and the rest.

## What it does

**Guard.** Before any tool call runs, Rescue checks whether it would delete or rewrite Hermes's own data. Deleting is refused and the agent is pointed to the supported command. Editing something a person may want edited (settings, `SOUL.md`, a memory file) goes to Hermes's normal approval prompt, with Rescue's reason, and a snapshot is saved first. Refusals hold even in YOLO mode, because Rescue runs before approvals. (In YOLO mode, or with approvals off, Hermes approves the "asks you" edits on its own; the snapshot is still saved, so `/rescue undo` still works.)

```text
I tried to tidy up Hermes for you.

---
🛟 Rescue stopped a risky change: the agent tried to delete what Hermes remembers about you
(memories/). Nothing was changed. Safer: the memory tool to add or remove single entries.
If you really meant it, send /rescue allow 10 and ask again.
```

**Real cause.** When a reply fails, Rescue tells you what each attempt actually hit and the one fix. When a reply quietly came from a backup because your main model is broken, it says so.

```text
🛟 Rescue · what actually happened
1. Ollama · missing-model → model not loaded (404) ×3
Fix: Load it on that server (`ollama pull missing-model`), or pick a model it has with /model.
```

(For the same failure, Hermes itself said the server "looks temporarily unavailable" and to wait and retry.)

**`/rescue`.** Health for the last 24 hours, the event log, snapshots and undo, and a switch to turn the guard off for a few minutes when you really do want the agent to do something drastic.

**Desktop.** A lifebuoy in the status bar (green, amber or red), a Rescue pane with the guard switch, recent events and one-click Undo, and a toast when Rescue stops something.

## What the guard protects

| Hermes keeps | Delete | Edit | What the agent is told to use instead |
| --- | --- | --- | --- |
| Saved conversations (`state.db`, `sessions/`) | refused | refused | `hermes sessions prune --dry-run`, `hermes sessions delete <id>` |
| Memories (`memories/`) | refused | asks you | the memory tool |
| Personality (`SOUL.md`) | refused | asks you | edit only when you asked |
| Skills (`skills/`) | refused | asks you | `hermes skills uninstall` / `reset` |
| Settings (`config.yaml`) | refused | asks you | `hermes config set` / `unset` |
| Scheduled tasks, plugins | refused | asks you | `hermes cron`, `hermes plugins` |
| Undo history (`checkpoints/`) | refused | refused | `/rollback` |
| Data other plugins keep (`plugin-data/<plugin>/`) | refused | allowed | the plugin's own command |
| API keys and sign-ins (`.env`, `auth.json` anywhere in the Hermes home; `mcp-tokens/`, `vault/` and the other token files) | always refused | always refused | `hermes auth add`, `hermes setup` |
| A whole profile, or the `profiles/` folder | always refused | asks you | `hermes profile delete <name>` |
| Rescue's own records, or all of `plugin-data/` | always refused | always refused | `/rescue snapshots`, `/rescue undo` |
| The whole Hermes home | always refused | — | `hermes doctor`, `hermes debug share` |
| Hermes's own program files (a source checkout, or `hermes-agent/` in the home) | refused | asks you | `hermes update`, `hermes doctor` |

Inside a profile (`profiles/<name>/`) the same rows apply to that profile's own files. While a profile is active, the default home is guarded too. On native Windows the Hermes home is `%LOCALAPPDATA%\hermes` instead of `~/.hermes`; Rescue guards it the same way, whether the agent writes Git Bash paths (`/c/Users/…`), Windows paths (`C:\Users\…`, `$LOCALAPPDATA`), or calls PowerShell or `cmd` to delete, move or write files.

Logs, caches and build leftovers are left alone, as are your own projects and anything outside the Hermes folders. Rescue looks at where a command runs: `find . -name '*.tmp' -delete` in your home folder isn't treated as wiping Hermes, and when Hermes doesn't say which folder the terminal is in, relative paths are left alone rather than guessed. When the terminal is inside a Hermes checkout (you're working on Hermes itself), its program files aren't guarded; for a `pip`/`pipx` install the package manager owns them and Rescue only refuses `pip uninstall hermes-agent`.

`/rescue allow` unlocks the "refused" rows for a few minutes, with a snapshot first (if Rescue can't save one, it asks you instead). The "always refused" rows never unlock: there is a supported command for each, or you do it yourself.

## Install

```bash
hermes plugins install 0xNexuz/hermes-rescue/hermes_rescue --enable
```

Once the plugin is listed in the catalog: `hermes plugins install hermes-rescue --enable`. In Hermes Desktop, also switch on **Rescue** under **Capabilities → Plugins** for the status-bar chip and the pane.

## Commands

| Command | What it does |
| --- | --- |
| `/rescue` | Health for the last 24 hours, the last problem and its fix |
| `/rescue log [n]` | The last *n* events (default 10) |
| `/rescue snapshots` | Saved snapshots |
| `/rescue undo [id]` | Put a snapshot back; with no id, pick one (buttons in Telegram, Discord, Slack and Desktop) |
| `/rescue allow [minutes]` | Turn the guard off for 1–120 minutes (default 10); snapshots are still taken |
| `/rescue lock` | Turn the guard back on now |

## How it works

| Part | Hermes surface |
| --- | --- |
| Guard | `pre_tool_call` returns `block` (with the supported alternative) or `approve` (Hermes's own human approval gate, with Rescue's reason). Rescue never auto-approves anything. |
| Snapshots and undo | Copied before the change into `plugin-data/hermes-rescue/snapshots/`; SQLite databases through SQLite's backup API, so the copy is consistent while Hermes has them open. Undo of `state.db` adds back the conversations and messages that went missing and leaves every other row alone, because Hermes is using that database at that moment (the full copy stays in the snapshot). Files are put back with the current version kept inside the snapshot, so an undo can itself be undone; folders are merged (files added since are set aside, not deleted). Undo only writes inside the Hermes folders. |
| Real cause | `api_request_error` and `post_api_request` follow each attempt in a turn. A local rule book (no model, no network) classifies each failure. The note is added with `transform_llm_output` when the turn ends normally, or posted with `ctx.post_message` when Hermes ends a failed turn early. If a later attempt succeeds, the note is edited to say so. |
| Desktop | `desktop/plugin.js` (status bar, pane, toasts) reads `dashboard/plugin_api.py`; the agent half pushes `plugin.hermes-rescue.changed` events so the pane updates live. |

## Privacy and disclosures

- **No network calls, no model calls, no shell commands, no telemetry.**
- **Never reads credentials.** `.env`, `auth.json` and the other credential stores Hermes lists (MCP tokens, the vault, browser logins and the like) are never opened, copied, moved or overwritten by snapshots or undo.
- **Reads** the arguments of tool calls (the command, path or code) to decide whether they touch Hermes's own data.
- **Writes** `plugin-data/hermes-rescue/rescue.db` in your Hermes home: the event kind, a short description, at most 200 characters of the command involved, and for failed model calls the provider, model, status code and Rescue's own explanation. About 2,000 events are kept.
- **Copies** the files a risky change would touch (settings, `SOUL.md`, memories, skills, `state.db` up to 300 MB) into `plugin-data/hermes-rescue/snapshots/` before the change, keeping the newest 30. `/rescue undo` writes them back.
- **Background:** after a failed model call that ends a turn, a short-lived background thread posts the note into the same conversation with `ctx.post_message` (in chat apps it first waits 1.5 seconds, so a backup that answers in time cancels it) and edits it if a later attempt answers. The post takes as long as the chat app does.

## Limits

- The shell check reads commands the way the shell does: quotes, comments, here-documents, `$(…)`, globs and `{a,b}` braces, `cd`/`env -C`, `bash -c`, `| xargs rm`, `find -name … -delete`, `sqlite3` writes (including SQL piped in or read from a file), `rsync --delete`, `tar -x`, `pip uninstall`. Quoted text, such as a commit message that mentions a command, is not treated as a command. It does not expand aliases, variables other than `$HOME`/`$HERMES_HOME`, or the scripts a command calls.
- On Windows, PowerShell and `cmd` commands are read for deletes, moves, copies and writes (`Remove-Item`, `rd`, `Move-Item`, `Set-Content` and their short names); longer PowerShell scripts with their own logic are not followed.
- `/rescue undo` for `state.db` only brings back missing conversations and messages; edits to rows that still exist, and other tables, are not rolled back (the full copy is in the snapshot). A database that appeared after the snapshot is left in place.
- Python passed to `execute_code` can build paths at run time, so Rescue only asks you (never blocks), and only when a destructive call and a path into the Hermes home appear in the same code.
- In the classic CLI running without the Hermes gateway, a failed-turn note has nowhere to go; `/rescue log` still has it.

## Tested

- 210+ automated tests (`pytest`) on Python 3.11, 3.13 and 3.14, including Windows path rules (Git Bash, `C:\` paths, `%LOCALAPPDATA%`, PowerShell and `cmd`) and every case from two independent reviews: everyday commands from a project, from your home folder and inside a Hermes checkout; quoted text, comments and here-documents; braces, `$(…)` and `find` patterns; keys inside profiles; Rescue's own records; a live database undo; a 2 MB one-liner (checked in well under the hook timeout).
- `hermes plugins validate --install-deps` passes, including the install security scan and the Desktop SDK surface check.
- Run against real Hermes (`main`, November 2026) with a scripted model server:
  - a delete of `memories/` blocked under `--yolo`;
  - a raw `DELETE` on `state.db` blocked; with the guard off, run for real, then `/rescue undo` brought the deleted messages back while the gateway kept the database open (integrity and search index checked afterwards);
  - a missing local model and a rejected key explained;
  - a `config.yaml` edit sent to approval with a snapshot;
  - the guard lifted, `memories/` deleted for real, then restored with `/rescue undo`;
  - ordinary build cleanup in a project folder left alone.

## Development

```bash
pip install pytest
pytest                                    # from the repository root
hermes plugins validate hermes_rescue --install-deps
```

The plugin lives in `hermes_rescue/` (the catalog entry uses `subdir: hermes_rescue`); tests are in `tests/`.

## License

MIT — see [LICENSE](LICENSE).
