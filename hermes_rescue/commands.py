"""``/rescue`` — health at a glance, the incident log, undo, and the guard switch.

Works the same in the CLI, the TUI, Desktop and every chat app. Where Hermes can show buttons
(Telegram, Discord, Slack, Desktop), choices use ``ctx.ask_choice``; elsewhere the text names
the command to type.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from . import snapshots

HELP = """🛟 Rescue — keeps your agent from breaking Hermes, and says what really went wrong.

/rescue                 health for the last 24 hours
/rescue log [n]         the last n events (default 10)
/rescue undo [id]       put back files from a snapshot (pick one if no id)
/rescue snapshots       list saved snapshots
/rescue allow [min]     turn the guard off for a few minutes (default 10, max 120)
/rescue lock            turn the guard back on now
/rescue help            this text"""

_KIND_LABEL = {
    "guard_block": "stopped", "guard_ask": "asked you first", "guard_allowed": "let through (guard off)",
    "failed_turn": "reply failed", "degraded_turn": "a backup answered",
    "recovered_turn": "recovered after a retry", "undo": "undo",
    "snapshot_skipped": "snapshot skipped", "guard_switch": "guard",
}


def ago(ts: float, now: Optional[float] = None) -> str:
    secs = max(0, int((now or time.time()) - ts))
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def _clock(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class RescueCommand:
    def __init__(self, rescue: Any, ask: Optional[Any] = None):
        self.rescue = rescue
        self._ask = ask  # ctx.ask_choice, or None in tests / surfaces that cannot ask

    def __call__(self, raw_args: str = "", **_: Any) -> str:
        words = (raw_args or "").split()
        sub, rest = (words[0].lower(), words[1:]) if words else ("", [])
        handler = {
            "": self.status, "status": self.status, "log": self.log, "undo": self.undo,
            "snapshots": self.list_snapshots, "allow": self.allow, "unlock": self.allow,
            "lock": self.lock, "help": lambda _r: HELP,
        }.get(sub)
        if handler is None:
            return f"Unknown option “{sub}”.\n\n{HELP}"
        try:
            return handler(rest)
        except Exception as exc:  # a broken store must still answer the user
            return f"🛟 Rescue couldn't do that: {exc}"

    def ask(self, question: str, choices: list) -> Optional[str]:
        if self._ask is None:
            return None
        try:
            return self._ask(question, choices)
        except Exception:
            return None

    # ---- status -------------------------------------------------------------------------------
    def status(self, _rest: list[str]) -> str:
        store = self.rescue.store()
        now = time.time()
        counts = store.counts(now - 86400)
        lines = ["🛟 Rescue — last 24 hours"]
        if store.guard_lifted(now):
            lines.append(f"Guard: OFF until {_clock(store.allowed_until())} — /rescue lock turns it back on")
        else:
            lines.append("Guard: on")
        summary = [f"{counts.get(k, 0)} {label}" for k, label in
                   (("guard_block", "risky changes stopped"), ("guard_ask", "asked you first"),
                    ("failed_turn", "failed replies explained"), ("degraded_turn", "answered by a backup"))
                   if counts.get(k)]
        lines.append("• " + " · ".join(summary) if summary else "• Nothing went wrong. 🎉")
        problem = next((i for i in store.incidents(20, now - 86400)
                        if i["kind"] in ("failed_turn", "degraded_turn", "guard_block")), None)
        if problem:
            label = _KIND_LABEL.get(problem["kind"], problem["kind"])
            lines.append(f"Last problem ({ago(problem['ts'], now)}) · {label}: {problem['title']}")
            if problem["fix"]:
                lines.append(f"  Fix: {problem['fix']}")
        snaps = store.snapshots(1)
        if snaps:
            lines.append(f"Last snapshot: #{snaps[0]['id']} {snaps[0]['label']}, {ago(snaps[0]['ts'], now)} "
                         "— /rescue undo")
        lines.append("More: /rescue log · /rescue undo · /rescue help")
        text = "\n".join(lines)
        if snaps or problem:
            choice = self.ask("What next?", [
                *([{"value": "undo", "label": "Undo the last change"}] if snaps else []),
                {"value": "log", "label": "Show the log"},
                {"value": "done", "label": "Done"},
            ])
            if choice == "undo":
                return text + "\n\n" + self.undo([str(snaps[0]["id"])])
            if choice == "log":
                return text + "\n\n" + self.log([])
        return text

    def log(self, rest: list[str]) -> str:
        try:
            n = max(1, min(50, int(rest[0]))) if rest else 10
        except ValueError:
            n = 10
        events = self.rescue.store().incidents(n)
        if not events:
            return "🛟 Rescue — nothing logged yet."
        now = time.time()
        lines = ["🛟 Rescue — last event" if len(events) == 1 else f"🛟 Rescue — last {len(events)} events"]
        for e in events:
            lines.append(f"• {ago(e['ts'], now)} · {_KIND_LABEL.get(e['kind'], e['kind'])}: {e['title']}")
            if e["fix"]:
                lines.append(f"    Fix: {e['fix']}")
        return "\n".join(lines)

    # ---- snapshots ----------------------------------------------------------------------------
    def list_snapshots(self, _rest: list[str]) -> str:
        snaps = self.rescue.store().snapshots(10)
        if not snaps:
            return "🛟 Rescue — no snapshots yet. One is saved before every risky change Rescue lets through."
        now = time.time()
        lines = ["🛟 Rescue — snapshots (newest first)"]
        lines += [f"#{s['id']} · {s['label']} · {ago(s['ts'], now)} · {len(s['files'])} item(s)" for s in snaps]
        lines.append("Put one back with /rescue undo <id>.")
        return "\n".join(lines)

    def undo(self, rest: list[str]) -> str:
        store = self.rescue.store()
        snap = None
        if rest:
            try:
                snap = store.snapshot(int(rest[0].lstrip("#")))
            except ValueError:
                return "Use /rescue undo <id> — see /rescue snapshots for ids."
            if snap is None:
                return f"No snapshot #{rest[0]}. See /rescue snapshots."
        else:
            snaps = store.snapshots(5)
            if not snaps:
                return "🛟 Rescue — nothing to undo yet."
            now = time.time()
            picked = self.ask("Which change should Rescue undo?", [
                *({"value": str(s["id"]), "label": f"#{s['id']} {s['label']}", "description": ago(s["ts"], now)}
                  for s in snaps),
                {"value": "cancel", "label": "Cancel"},
            ])
            if picked is None:
                return self.list_snapshots([]).replace("Put one back with", "Pick one and send")
            if picked == "cancel":
                return "Nothing changed."
            snap = store.snapshot(int(picked))
        return self.put_back(snap)[1]

    def put_back(self, snap: dict) -> tuple[bool, str, str]:
        """Restore one snapshot. Returns (anything put back, full message, one-line summary)."""
        name = f"snapshot #{snap['id']} ({snap['label']})"
        if snap["status"] == "restored":
            msg = f"Snapshot #{snap['id']} was already put back. Its newer files were saved alongside it."
            return False, msg, msg
        store = self.rescue.store()
        unchanged: list[str] = []
        done, problems = snapshots.restore(snap["files"], self.rescue.home(), Path(snap["folder"]),
                                           self.rescue.policy(), self.rescue.snap_root(), unchanged)
        if not done and not problems:
            # The change hasn't happened (yet): keep the snapshot usable for when it does.
            what = ", ".join(unchanged) or "nothing"
            msg = (f"🛟 Nothing to undo yet: {what} is still exactly as in {name}. If the change is waiting "
                   f"for your approval, approve it first; Undo will put it back afterwards.")
            return False, msg, f"Nothing to undo yet: {what} hasn't changed since snapshot #{snap['id']}."
        if done:
            store.set_snapshot_status(snap["id"], "restored")
        store.add_incident("undo", f"Put back {name}" if done else f"Could not put back {name}",
                           data={"done": done, "problems": problems})
        lines = [f"🛟 Rescue put back {name}." if done else f"🛟 Rescue couldn't put back {name}."]
        lines += [f"• {d}" for d in done]
        lines += [f"⚠ {p}" for p in problems]
        if done:
            lines.append("The versions it replaced are kept inside the snapshot folder. Keys and sign-ins are "
                         "never part of a snapshot. Start a new session (/new) so Hermes reloads the files.")
            summary = f"Put back {name}. Start a new session so Hermes reloads it."
        else:
            summary = f"Couldn't put back snapshot #{snap['id']}: {problems[0]}"
        if done and problems:
            summary += f" ({len(problems)} item{'s' if len(problems) != 1 else ''} couldn't be restored — see /rescue log.)"
        return bool(done), "\n".join(lines), summary

    # ---- guard switch -------------------------------------------------------------------------
    def allow(self, rest: list[str]) -> str:
        try:
            minutes = int(rest[0]) if rest else 10
        except ValueError:
            minutes = 10
        minutes = max(1, min(120, minutes))
        until = time.time() + minutes * 60
        store = self.rescue.store()
        store.set("allow_until", str(until))
        store.add_incident("guard_switch", f"Guard turned off for {minutes} min")
        return (f"🛟 Guard is off for {minutes} min (until {_clock(until)}). Risky changes go through, "
                "but Rescue still saves a snapshot first (/rescue undo). /rescue lock turns it back on.")

    def lock(self, _rest: list[str]) -> str:
        store = self.rescue.store()
        store.set("allow_until", "0")
        store.add_incident("guard_switch", "Guard turned back on")
        return "🛟 Guard is back on."
