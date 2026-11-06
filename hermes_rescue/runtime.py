"""Rescue's runtime: the hook callbacks and the state they share.

Kept separate from ``__init__`` so tests can drive every hook with a fake home and store, the
same way Hermes calls them (keyword arguments, extra fields ignored).
"""

from __future__ import annotations

import contextvars
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import causes, guard, snapshots
from .store import Store

logger = logging.getLogger(__name__)

PLUGIN_ID = "hermes-rescue"
_TURN_TTL_S = 3600.0
# In a chat app, wait this long after the last failed attempt so the note lands after Hermes's own
# failure message (and a backup that starts in the meantime cancels it). CLI, TUI and Desktop show
# the note as a separate notice, so it is posted at once, while the session is certainly still live.
SETTLE_SECONDS = 1.5
LOCAL_SURFACES = {"", "cli", "tui", "desktop", "acp", "local"}


@dataclass
class TurnState:
    ts: float = field(default_factory=time.time)
    session_id: str = ""
    platform: str = ""
    errors: list = field(default_factory=list)
    recovered: bool = False
    stopped: list = field(default_factory=list)   # guard.Verdict objects blocked this turn
    timer: Optional[threading.Timer] = None       # pending "this turn failed" note
    posted: Any = None                            # the note's message handle, once sent
    reported: bool = False                        # the failure note reached the user
    incident_id: Optional[int] = None             # this turn's failed/degraded entry in the log
    recovery_noted: bool = False                  # the posted note was already updated after a recovery
    post_lock: threading.Lock = field(default_factory=threading.Lock)  # one note at a time per turn


def _default_home() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home())


def _default_data_dir() -> Path:
    from plugins.plugin_storage import plugin_data_dir
    return plugin_data_dir(PLUGIN_ID)


def _default_root() -> Optional[Path]:
    """The default Hermes home that holds every profile (differs from the active home in a profile)."""
    from hermes_constants import get_default_hermes_root
    return Path(get_default_hermes_root())


def _default_user_home() -> Optional[Path]:
    """What ``~`` means in the agent's shell (Hermes may point HOME elsewhere, e.g. in a container)."""
    from hermes_constants import get_subprocess_home
    home = get_subprocess_home(dict(os.environ))
    return Path(home) if home else None


def _default_store() -> Store:
    from plugins.plugin_storage import plugin_db
    return Store(lambda: plugin_db(PLUGIN_ID, "rescue.db"))


def detect_install() -> Optional[Path]:
    """The Hermes source checkout this process runs from; None for a pip/site-packages install
    (a package manager owns those files, so they are not ours to guard)."""
    try:
        import hermes_constants
        root = Path(hermes_constants.__file__).resolve().parent
    except Exception:
        return None
    if root.name in ("site-packages", "dist-packages"):
        return None
    return root if (root / "hermes_cli").is_dir() else None


class Rescue:
    def __init__(self, ctx: Any = None, *, home_fn: Callable[[], Path] = _default_home,
                 data_dir_fn: Callable[[], Path] = _default_data_dir,
                 store_factory: Callable[[], Store] = _default_store,
                 root_fn: Callable[[], Optional[Path]] = _default_root,
                 user_home_fn: Callable[[], Optional[Path]] = _default_user_home,
                 install: Optional[Path] = None, detect: bool = True):
        self.ctx = ctx
        self._home_fn, self._data_dir_fn, self._store_factory = home_fn, data_dir_fn, store_factory
        self._root_fn, self._user_home_fn = root_fn, user_home_fn
        self.install = install if install is not None else (detect_install() if detect else None)
        self._stores: dict[str, Store] = {}
        self._turns: dict[str, TurnState] = {}
        self._lock = threading.Lock()
        self.settle_seconds = SETTLE_SECONDS

    # ---- context ------------------------------------------------------------------------------
    def home(self) -> Path:
        return self._home_fn()

    def store(self) -> Store:
        key = str(self.home())  # one store per profile home; a gateway may serve several
        with self._lock:
            if key not in self._stores:
                self._stores[key] = self._store_factory()
            return self._stores[key]

    def snap_root(self) -> Path:
        return self._data_dir_fn() / "snapshots"

    def root(self) -> Optional[Path]:
        try:
            return self._root_fn()
        except Exception:
            return None

    def user_home(self) -> Optional[Path]:
        try:
            return self._user_home_fn()
        except Exception:
            return None

    def cwd(self) -> Optional[Path]:
        """The session's working directory, or None when Hermes doesn't say (a remote backend):
        then relative paths are left alone rather than guessed from this process's directory."""
        try:
            raw = self.ctx.session_cwd() if self.ctx is not None and hasattr(self.ctx, "session_cwd") else ""
        except Exception:
            raw = ""
        return Path(raw) if raw and os.path.isabs(str(raw)) else None

    def policy(self) -> snapshots.Policy:
        roots = [self.home()] + [r for r in [self.root()] if r is not None]
        installs = [self.install] if self.install is not None else []
        return snapshots.Policy(roots + installs, keep_out=[self._data_dir_fn()])

    def _turn_locked(self, turn_id: str, session_id: str = "", platform: str = "") -> TurnState:
        key = turn_id or session_id or "-"
        now = time.time()
        for stale in [k for k, t in self._turns.items() if now - t.ts > _TURN_TTL_S]:
            del self._turns[stale]
        state = self._turns.setdefault(key, TurnState(session_id=session_id, platform=platform))
        state.platform = state.platform or platform
        state.session_id = state.session_id or session_id
        return state

    # ---- incidents ----------------------------------------------------------------------------
    def incident(self, kind: str, title: str, fix: str = "", **kw: Any) -> Optional[int]:
        """Record an incident and tell any connected Desktop window (no-op without one)."""
        incident_id = self.store().add_incident(kind, title, fix, **kw)
        self._notify(kind, title)
        return incident_id

    @staticmethod
    def _notify(kind: str, title: str) -> None:
        try:
            from hermes_cli.plugin_events import broadcast_plugin_event
            broadcast_plugin_event(PLUGIN_ID, "changed", {"kind": kind, "title": title[:300]})
        except Exception:
            logger.debug("hermes-rescue: desktop event not sent", exc_info=True)

    def _log(self, kind: str, title: str, fix: str = "", **kw: Any) -> Optional[int]:
        """``incident`` that never raises: losing a log line must not change a guard decision."""
        try:
            return self.incident(kind, title, fix, **kw)
        except Exception:
            logger.warning("hermes-rescue: could not record %s", kind, exc_info=True)
            return None

    # ---- snapshots ----------------------------------------------------------------------------
    def take_snapshot(self, verdict: guard.Verdict, *, session_id: str = "",
                      tool_call_id: str = "") -> tuple[Optional[int], str]:
        """Copy what *verdict* would change. Returns (snapshot id, "saved" | "partial" | "none")."""
        if not verdict.paths:
            return None, "none"
        root = self.snap_root()
        folder, records, skipped = snapshots.take(verdict.paths, self.home(), root, self.policy())
        store = self.store()
        label = verdict.target.name if verdict.target else "Hermes data"
        snap_id = store.add_snapshot(label, records, folder=str(folder), session_id=session_id,
                                     tool_call_id=tool_call_id)
        if skipped:
            self._log("snapshot_skipped", f"Not in snapshot #{snap_id}: {'; '.join(skipped)}", session_id=session_id)
        snapshots.prune(store, root)
        risky = [s for s in skipped if "never copied" not in s]   # keys are left out on purpose
        return snap_id, ("partial" if risky or not records and skipped else "saved")

    def _snapshot_quietly(self, verdict: guard.Verdict, session_id: str, tool_call_id: str) -> tuple[Optional[int], str]:
        try:
            return self.take_snapshot(verdict, session_id=session_id, tool_call_id=tool_call_id)
        except Exception:
            logger.warning("hermes-rescue: snapshot failed", exc_info=True)
            return None, "partial"

    # ---- hooks --------------------------------------------------------------------------------
    def on_pre_tool_call(self, tool_name: str = "", args: Any = None, session_id: str = "",
                         tool_call_id: str = "", turn_id: str = "", **_: Any) -> Optional[dict]:
        """Guard Hermes's own data. Never raises: a raise here would block the tool (fail closed).
        A bug in the *check* lets the call through; a failure while *recording* it never does."""
        try:
            verdict = guard.check_tool_call(tool_name, args if isinstance(args, dict) else {}, self.home(),
                                            self.install, self.cwd(), root=self.root(), user_home=self.user_home())
        except Exception:
            logger.warning("hermes-rescue: guard check failed; letting the call through", exc_info=True)
            return None
        if verdict.action == guard.ALLOW:
            return None
        try:
            return self._enforce(verdict, tool_name, session_id, tool_call_id, turn_id)
        except Exception:
            logger.warning("hermes-rescue: guard bookkeeping failed; still enforcing", exc_info=True)
            if verdict.action == guard.BLOCK:
                return {"action": "block", "message": verdict.agent_message()}
            return {"action": "approve", "message": verdict.approval_reason("partial"),
                    "rule_key": self._rule_key(verdict)}

    @staticmethod
    def _rule_key(verdict: guard.Verdict) -> str:
        return f"hermes-rescue:{(verdict.target.rel if verdict.target else '') or 'install'}:{verdict.operation}"

    def _enforce(self, verdict: guard.Verdict, tool_name: str, session_id: str, tool_call_id: str,
                 turn_id: str) -> Optional[dict]:
        data = {"tool": tool_name, "detail": verdict.detail, "operation": verdict.operation}
        lifted = False
        if verdict.liftable:
            try:
                lifted = self.store().guard_lifted()
            except Exception:
                logger.warning("hermes-rescue: could not read the guard switch; guard stays on", exc_info=True)
        if lifted:
            snap_id, snap = self._snapshot_quietly(verdict, session_id, tool_call_id)
            if snap != "partial":
                self._log("guard_allowed", verdict.user_line(), "/rescue undo" if snap_id else "",
                          session_id=session_id, data={**data, "snapshot": snap_id})
                return None
            # The guard is off, but Rescue couldn't save a copy: hand the decision to the human.
            self._log("guard_ask", verdict.user_line(), "", session_id=session_id, data={**data, "snapshot": snap_id})
            return {"action": "approve", "rule_key": self._rule_key(verdict),
                    "message": verdict.approval_reason("partial") + " (The guard is off, so Rescue asks instead of blocking.)"}
        if verdict.action == guard.BLOCK:
            with self._lock:
                self._turn_locked(turn_id, session_id).stopped.append(verdict)
            self._log("guard_block", verdict.user_line(), f"Use {verdict.target.safe}" if verdict.target else "",
                      session_id=session_id, data=data)
            return {"action": "block", "message": verdict.agent_message()}
        snap_id, snap = self._snapshot_quietly(verdict, session_id, tool_call_id)
        self._log("guard_ask", verdict.user_line(), "/rescue undo" if snap_id and snap == "saved" else "",
                  session_id=session_id, data={**data, "snapshot": snap_id})
        return {"action": "approve", "message": verdict.approval_reason(snap), "rule_key": self._rule_key(verdict)}

    def on_post_tool_call(self, tool_call_id: str = "", status: str = "", **_: Any) -> None:
        """A snapshot is only "unused" when the call never ran (refused at the approval prompt or by
        another guard). A call that ran and then reported an error may still have changed files."""
        try:
            if tool_call_id:
                self.store().set_snapshot_status_for_call(tool_call_id, "unused" if status == "blocked" else "kept")
        except Exception:
            logger.debug("hermes-rescue: post_tool_call bookkeeping failed", exc_info=True)

    def on_api_request_error(self, turn_id: str = "", session_id: str = "", platform: str = "", **kw: Any) -> None:
        """Record a failed attempt. When it looks like the last one (no retries left, or not
        retryable), schedule the note: Hermes ends a turn that runs out of retries WITHOUT firing
        ``transform_llm_output`` or ``on_session_end``, so Rescue posts it as its own message.
        A later attempt (retry, backup) cancels or updates it."""
        try:
            retry, limit = kw.get("retry_count"), kw.get("max_retries")
            last_try = kw.get("retryable") is False or (
                isinstance(retry, int) and isinstance(limit, int) and retry + 1 >= limit)
            with self._lock:
                state = self._turn_locked(turn_id, session_id, platform)
                state.errors.append(causes.ErrorEvent.from_hook(**kw))
                state.recovered = False
                delay = 0.0 if (platform or state.platform) in LOCAL_SURFACES else self.settle_seconds
                self._arm(turn_id or session_id or "-", state, post=last_try, delay=delay)
        except Exception:
            logger.debug("hermes-rescue: could not record api error", exc_info=True)

    def on_post_api_request(self, turn_id: str = "", session_id: str = "", **_: Any) -> None:
        key = turn_id or session_id or "-"
        with self._lock:
            state = self._turns.get(key)
            if state is None or not state.errors or state.recovered:
                return
            state.recovered = True
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
            told = state.reported
        if told or state.post_lock.locked():  # we said (or are saying) it failed, but it got through
            self._spawn(self._post_recovered, state)

    # ---- failed-turn note ---------------------------------------------------------------------
    def _arm(self, key: str, state: TurnState, *, post: bool, delay: float) -> None:
        """(called with self._lock held)"""
        if state.timer is not None:
            state.timer.cancel()
            state.timer = None
        if not post or self.ctx is None or not hasattr(self.ctx, "post_message"):
            return
        run_in_turn_context = contextvars.copy_context().run  # keeps the session the note goes to
        timer = threading.Timer(delay, run_in_turn_context)
        timer.args = (self._post_failed, key, timer)
        timer.daemon = True
        state.timer = timer
        timer.start()

    @staticmethod
    def _spawn(fn: Callable, *args: Any) -> None:
        threading.Thread(target=contextvars.copy_context().run, args=(fn, *args), daemon=True,
                         name="hermes-rescue-note").start()

    @staticmethod
    def _safe_edit(handle: Any, text: str) -> bool:
        try:
            result = handle.edit(text)
            return not isinstance(result, dict) or bool(result.get("ok", True))
        except Exception:
            logger.debug("hermes-rescue: could not update the posted note", exc_info=True)
            return False

    def _post_failed(self, key: str, timer: Optional[threading.Timer]) -> None:
        with self._lock:
            state = self._turns.get(key)
            if state is None or state.recovered or state.timer is not timer:
                return
            state.timer = None
        with state.post_lock:   # one note per turn, even when a slow chat app overlaps two failures
            with self._lock:
                if state.recovered:
                    return
                errors, handle = list(state.errors), state.posted
            explanation = causes.explain(errors, recovered=False)
            if explanation is None:
                return
            text = explanation.render(standalone=True)
            sent = handle is not None and self._safe_edit(handle, text)  # a backup failed too: grow the note
            if not sent:
                try:
                    reply = self.ctx.post_message(text)
                except Exception:
                    logger.debug("hermes-rescue: could not post the failed-turn note", exc_info=True)
                    reply = None
                sent = bool(getattr(reply, "ok", False))
                handle = reply if sent else None
            with self._lock:
                if sent:
                    state.reported, state.posted = True, handle
                late_recovery = state.recovered
            self._record_turn(explanation, state)   # kept in /rescue log even when the post was refused
            if sent and late_recovery:
                self._apply_recovery(state)

    def _post_recovered(self, state: TurnState) -> None:
        with state.post_lock:
            self._apply_recovery(state)

    def _apply_recovery(self, state: TurnState) -> None:
        """(called with state.post_lock held) Rewrite the posted note now that an answer came."""
        with self._lock:
            if not state.reported or state.posted is None or state.recovery_noted:
                return
            state.recovery_noted = True
            errors, handle = list(state.errors), state.posted
        explanation = causes.explain(errors, recovered=True)
        if explanation is not None and explanation.headline is not None:
            # a backup answered, but something durable is broken: say what, and the fix
            first = explanation.attempts[0][0] if explanation.attempts else "your main model"
            text = (f"🛟 Rescue · update: a backup answered after all, but {first} failed — "
                    f"{explanation.headline.what} Fix: {explanation.headline.fix}")
            self._record_turn(explanation, state)
        else:
            text = ("🛟 Rescue · update: a later attempt got through, so this reply worked after all. "
                    "/rescue log keeps what went wrong first.")
            self._record_turn(None, state)
        self._safe_edit(handle, text)

    def on_transform_llm_output(self, response_text: str = "", session_id: str = "", platform: str = "",
                                turn_id: str = "", **_: Any) -> Optional[str]:
        """Append what actually happened. Returns None (leave the reply alone) when there is
        nothing to add, so other plugins' transforms keep working."""
        try:
            with self._lock:
                state = self._turns.pop(turn_id or session_id or "-", None)
                if state is not None and state.timer is not None:
                    state.timer.cancel()
                    state.timer = None
            if state is None:
                return None
            state.platform = state.platform or platform
            state.session_id = state.session_id or session_id
            parts: list[str] = []
            # Already posted on its own (or being posted right now): say nothing twice.
            in_flight = state.post_lock.locked()
            explanation = None if (state.reported or in_flight) else causes.explain(
                state.errors, recovered=state.recovered)
            if explanation is not None:
                parts.append(explanation.render())
                self._record_turn(explanation, state)
            if state.stopped:
                parts.append(self._guard_footer(state.stopped))
            if not parts:
                return None
            return (response_text or "").rstrip() + "".join(parts)
        except Exception:
            logger.warning("hermes-rescue: could not add the explanation", exc_info=True)
            return None

    def on_session_end(self, **_: Any) -> None:
        now = time.time()
        with self._lock:
            for stale in [k for k, t in self._turns.items() if now - t.ts > _TURN_TTL_S]:
                del self._turns[stale]

    # ---- rendering ----------------------------------------------------------------------------
    def _record_turn(self, x: Optional[causes.Explanation], state: TurnState) -> None:
        """One log entry per turn: created on the first explanation, updated when the story changes
        (a failure that a backup later answered becomes "a backup answered")."""
        try:
            if x is None:   # recovered from something transient: keep the entry, mark it recovered
                if state.incident_id is not None:
                    old = next((i for i in self.store().incidents(50) if i["id"] == state.incident_id), None)
                    if old is not None:
                        self.store().update_incident(state.incident_id, "recovered_turn", old["title"], "",
                                                     data=old["data"])
                        self._notify("recovered_turn", old["title"])
                return
            chain = " → then ".join(f"{label}: {cause.short}{f' ×{n}' if n > 1 else ''}"
                                    for label, cause, n in x.attempts)
            kind = "failed_turn" if x.kind == "failed" else "degraded_turn"
            fix = x.headline.fix if x.headline else ""
            data = {"what": x.headline.what if x.headline else ""}
            if state.incident_id is None:
                state.incident_id = self.incident(kind, chain, fix, session_id=state.session_id,
                                                  platform=state.platform, data=data)
            else:
                self.store().update_incident(state.incident_id, kind, chain, fix, data=data)
                self._notify(kind, chain)
        except Exception:
            logger.warning("hermes-rescue: could not log the turn", exc_info=True)

    @staticmethod
    def _guard_footer(stopped: list) -> str:
        first = stopped[0]
        more = f" (and {len(stopped) - 1} more)" if len(stopped) > 1 else ""
        safe = f" Safer: {first.target.safe}." if first.target else ""
        escape = (" If you really meant it, send /rescue allow 10 and ask again." if first.liftable
                  else " Rescue never lets the agent do this one; do it yourself if you really mean it.")
        return (f"\n\n---\n🛟 Rescue stopped a risky change: {first.user_line()}{more}. Nothing was changed."
                f"{safe}{escape}")
