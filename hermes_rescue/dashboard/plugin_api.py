"""Backend for the Rescue Desktop pane — mounted by Hermes under ``/api/plugins/hermes-rescue/``.

Hermes imports this file on its own (not as part of the plugin package), so it loads the
plugin's modules from the folder above by path. Every answer comes from the same store and the
same ``/rescue`` code the chat command uses, so the pane and the chat never disagree.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter

router = APIRouter()

_PKG_DIR = Path(__file__).resolve().parents[1]
_PKG_NAME = "hermes_rescue_desktop_backend"


def _pkg():
    """The plugin package, loaded once from the folder above this file."""
    if _PKG_NAME not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            _PKG_NAME, _PKG_DIR / "__init__.py", submodule_search_locations=[str(_PKG_DIR)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[_PKG_NAME] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(_PKG_NAME, None)
            raise
    return sys.modules[_PKG_NAME]


_rescue = None


def _command():
    global _rescue
    pkg = _pkg()
    if _rescue is None:
        _rescue = pkg.runtime.Rescue(None)
    return pkg.commands.RescueCommand(_rescue)


def _event(e: dict) -> dict:
    labels = _pkg().commands._KIND_LABEL
    return {"id": e["id"], "ts": e["ts"], "kind": e["kind"], "label": labels.get(e["kind"], e["kind"]),
            "title": e["title"], "fix": e["fix"]}


# Plain ``def`` routes: they read SQLite and copy files, so FastAPI runs them in a worker thread
# instead of on the server's event loop.
@router.get("/health")
def health() -> dict[str, Any]:
    command = _command()
    store = command.rescue.store()
    now = time.time()
    counts = store.counts(now - 86400)
    recent_bad = [e for e in store.incidents(50, now - 3600) if e["kind"] in ("guard_block", "failed_turn")]
    lifted = store.guard_lifted(now)
    tone = "bad" if recent_bad else "warn" if (lifted or any(counts.get(k) for k in
                                                              ("guard_block", "failed_turn", "degraded_turn"))) else "good"
    return {
        "tone": tone,
        "guard": {"on": not lifted, "until": store.allowed_until() if lifted else None},
        "counts": counts,
        "events": [_event(e) for e in store.incidents(15, now - 7 * 86400)],
        "snapshots": [{"id": s["id"], "ts": s["ts"], "label": s["label"], "items": len(s["files"])}
                      for s in store.snapshots(5)],
    }


@router.post("/undo")
def undo(body: dict) -> dict[str, Any]:
    try:
        snap_id = int(body.get("id"))
    except (TypeError, ValueError):
        return {"ok": False, "message": "Pick a snapshot to put back."}
    command = _command()
    snap = command.rescue.store().snapshot(snap_id)
    if snap is None:
        return {"ok": False, "message": f"No snapshot #{snap_id}."}
    ok, message = command.put_back(snap)
    return {"ok": ok, "message": message}


@router.post("/guard")
def guard(body: dict) -> dict[str, Any]:
    command = _command()
    if body.get("lock"):
        return {"ok": True, "message": command("lock")}
    try:
        minutes = int(body.get("minutes") or 10)
    except (TypeError, ValueError):
        minutes = 10
    return {"ok": True, "message": command(f"allow {minutes}")}
