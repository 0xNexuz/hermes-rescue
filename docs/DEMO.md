# 90-second demo

The rule: under 90 seconds, posted on X tagging @NousResearch, showing the plugin working. Lead
with the break-and-fix moment; no intro slides.

## Setup before recording

- Hermes Desktop open, Rescue enabled for the profile (`hermes plugins enable hermes-rescue`) and
  its Desktop half switched on in **Capabilities → Plugins**. Rescue pane docked on the right.
- A terminal beside it for the scripted parts.
- For the "real cause" scene, any local OpenAI-compatible server works: Ollama with a model name
  that isn't pulled is the clearest. If recording with a real agent is unreliable, run
  `demo/run_demo.sh` instead and say on screen that the model is scripted so the run is
  reproducible.

## Script

| Time | Screen | Voice-over / caption |
| --- | --- | --- |
| 0–8 s | Chat: "Hermes feels slow, clean it up." The agent starts a delete on `~/.hermes/memories`. | "Ask an agent to fix Hermes and it can wreck it. One user lost 237 sessions this way." |
| 8–22 s | The reply ends with the Rescue note; toast in the corner; memories folder still there. | "Rescue blocks it, even in YOLO mode, and tells the agent the safe command instead." |
| 22–40 s | Ask it to append a line to SOUL.md (the agent itself refuses hand-edits of config.yaml, following Hermes's guidance). Hermes asks for approval; the Rescue pane shows "asked you first" and a new snapshot. Click **Run**, then **Undo** in the pane. | "Edits you might want go to your approval, with a snapshot first. One click puts it back." |
| 40–60 s | Switch to a model the local server hasn't loaded. Hermes says "temporarily unavailable, wait and retry". Rescue's note appears: model not loaded → `ollama pull …`. | "When a reply fails, Hermes guesses. Rescue tells you the real cause and the fix." |
| 60–75 s | `/rescue` in Telegram (or the CLI): health, last problem, buttons. | "Same answers in every app: CLI, Desktop, Telegram, WhatsApp." |
| 75–88 s | Terminal: `pytest` green, `hermes plugins validate` passing, "no network calls". | "190+ tests, passes Hermes's own validator. Local only: no network, no model calls, never reads your keys." |
| 88–90 s | Title card: Hermes Rescue · `hermes plugins install hermes-rescue`. | — |

## Post text (draft — edit in your own words)

> Asked my agent to "fix Hermes" and it tried to delete its own memories. So I built 🛟 Hermes
> Rescue for the @NousResearch plugin hackathon:
> • blocks the agent from wrecking Hermes's own data (even in YOLO mode)
> • snapshot + one-click undo for risky edits
> • when a reply fails, the real cause and the fix
> Local only. Works in CLI, Desktop, Telegram.
> github.com/0xNexuz/hermes-rescue
