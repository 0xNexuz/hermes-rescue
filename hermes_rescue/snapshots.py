"""Copy-before-change snapshots and a reversible undo.

Before Rescue lets a risky change through (the human approved it, or lifted the guard), it copies
the files that change into ``plugin-data/hermes-rescue/snapshots/<id>/``. ``undo`` puts them back,
first setting whatever is there now aside inside the same snapshot, so an undo can itself be
undone.

Three rules keep this safe:

* Keys and sign-ins are never copied, moved or overwritten: Hermes's list of credential stores
  (``CREDENTIAL_PATHS``, mirrored from ``hermes_cli.profiles``) is skipped on the way in and left
  alone on the way back.
* SQLite databases are copied with SQLite's backup API, so a copy is consistent while Hermes
  has the database open. Hermes's own session database (``state.db``) is never rolled back
  wholesale on undo: Hermes is using it right then (the running gateway's bookkeeping lives in
  it too), so undo puts back the conversations and messages that are missing and leaves every
  other row as it is. The full copy stays in the snapshot. Other databases are written back
  through the backup API, so the live file and its journal agree.
* Undo only writes inside the Hermes folders it was given, from inside the snapshot folder.
"""

from __future__ import annotations

import filecmp
import functools
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional, Sequence

from .guard import _KEY_NAME

MAX_SNAPSHOT_BYTES = 300 * 1024 * 1024   # skip anything bigger (and say so)
KEEP_SNAPSHOTS = 30
_JOURNAL_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")

# Hermes's credential stores (hermes_cli.profiles.PROFILE_CREDENTIAL_PATHS on main, Nov 2026), as paths
# relative to a profile home. Copied rather than imported: plugins may only use public Hermes surfaces.
CREDENTIAL_PATHS = frozenset({
    "auth.json", ".env", "auth", ".op.env", "npmrc", ".anthropic_oauth.json", "google_token.json",
    "google_oauth_pending.json", "google_client_secret.json", "google_chat_user_tokens",
    "google_chat_user_client_secret.json", "google_chat_user_oauth_pending.json", "google_chat_user_token.json",
    "google_chat_user_oauth_pending", "slack_tokens.json", "honcho.json", "mem0.json",
    "webhook_subscriptions.json", "teams_pipeline_store.json", "mcp-tokens", "vault", "browser-profile",
    "browser_auth", "bot-desktop", "webapp", "browser-profiles", "browser_profiles", "pairing",
    "platforms/pairing", "feishu_comment_pairing.json", "whatsapp/session", "platforms/whatsapp/session",
    "matrix/store", "platforms/matrix/store", "bws_cache.json", "bws_cache.enc.json", "op_cache.json",
    "photon-sidecar.json", "cache/bws_cache.json", "cache/bws_cache.enc.json", "cache/op_cache.json",
    "runtime/photon-sidecar.json", "workspace/meetings/node_token.json", "weixin/accounts",
    ".copilot_jwt.json", "proxy", "chrome-debug", "home", "backups", "state-snapshots", ".ssh", ".aws",
    ".gnupg", ".kube", ".docker", ".azure", ".config/gh", ".config/gcloud", ".envrc",
})


@functools.lru_cache(maxsize=1)
def _credential_parts() -> tuple[tuple[str, ...], ...]:
    return tuple(tuple(p.casefold().split("/")) for p in sorted(CREDENTIAL_PATHS))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_secret(path: Path, roots: Sequence[Path]) -> bool:
    """True for a key file or anything inside one of Hermes's credential stores."""
    path = Path(os.path.normpath(str(path)))
    for root in roots:
        if not _within(path, root):
            continue
        parts = path.relative_to(root).parts
        if any(_KEY_NAME.match(p) for p in parts):
            return True
        views = [parts]
        if len(parts) >= 3 and parts[0] == "profiles":   # a profile is a home of its own
            views.append(parts[2:])
        for view in views:
            folded = tuple(p.casefold() for p in view)
            if any(folded[:len(c)] == c for c in _credential_parts()):
                return True
    return bool(_KEY_NAME.match(path.name))


class Policy:
    """What a snapshot may touch: the Hermes folders, and what to leave alone inside them."""

    def __init__(self, roots: Sequence[Path], keep_out: Iterable[Path] = ()):
        self.roots = [Path(os.path.normpath(str(r))) for r in roots]
        self.keep_out = [Path(os.path.normpath(str(p))) for p in keep_out]   # Rescue's own folder

    def skip(self, path: Path) -> bool:
        return (path.name.endswith(_JOURNAL_SUFFIXES) and path.name.rsplit("-", 1)[0].endswith(_SQLITE_SUFFIXES)
                or any(_within(path, k) for k in self.keep_out)
                or is_secret(path, self.roots))

    def allowed(self, path: Path) -> bool:
        return any(_within(path, r) for r in self.roots)


def _walk(top: Path, skip: Callable[[Path], bool]) -> Iterator[Path]:
    """Files and symlinks under *top* (not following links), minus what *skip* rejects."""
    for root, dirs, files in os.walk(top, followlinks=False):
        r = Path(root)
        keep = []
        for d in dirs:
            p = r / d
            if skip(p):
                continue
            if p.is_symlink():
                yield p
            else:
                keep.append(d)
        dirs[:] = keep
        for f in files:
            p = r / f
            if not skip(p):
                yield p


def _size(path: Path, skip: Callable[[Path], bool]) -> int:
    if not path.is_dir():
        return path.stat().st_size
    total = 0
    for p in _walk(path, skip):
        try:
            total += p.lstat().st_size
        except OSError:
            pass
    return total


def _is_sqlite(path: Path) -> bool:
    if path.is_symlink() or not path.is_file() or path.suffix not in _SQLITE_SUFFIXES:
        return False
    try:
        with open(path, "rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _uri(path: Path, read_only: bool = False) -> str:
    """A SQLite URI for *path* that works on Windows too (``file:///C:/…``)."""
    return Path(os.path.abspath(path)).as_uri() + ("?mode=ro" if read_only else "")


def _copy_sqlite(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(_uri(src, True), uri=True, timeout=15)
    try:
        target = sqlite3.connect(str(dst))
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def _copy_one(src: Path, dst: Path) -> str:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_symlink():
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        try:
            os.symlink(os.readlink(src), dst)
            return "link"
        except OSError:          # Windows without the symlink right: keep the linked file's content
            if not src.is_file():
                raise
            shutil.copy2(src, dst)
            return "file"
    if _is_sqlite(src):
        _copy_sqlite(src, dst)
        return "sqlite"
    shutil.copy2(src, dst)
    return "file"


def _stored_name(orig: Path, home: Path) -> str:
    try:
        return str(Path("home") / orig.relative_to(home))
    except ValueError:
        return str(Path("abs") / str(orig).lstrip("/\\").replace(":", "_"))


def take(paths: list[Path], home: Path, snap_root: Path,
         policy: Optional[Policy] = None) -> tuple[Path, list[dict], list[str]]:
    """Copy *paths* into a new folder under *snap_root*. Returns (folder, file records, skipped)."""
    policy = policy or Policy([home], [snap_root.parent])
    snap_root.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix=time.strftime("%Y%m%d-%H%M%S-"), dir=snap_root))
    records, skipped = [], []
    seen: set[str] = set()
    for orig in paths:
        orig = Path(os.path.normpath(str(orig)))
        if orig.name.endswith(_JOURNAL_SUFFIXES):          # state.db-wal → back up state.db itself
            orig = orig.with_name(orig.name.rsplit("-", 1)[0])
        key = str(orig)
        if key in seen:
            continue
        seen.add(key)
        if is_secret(orig, policy.roots):
            skipped.append(f"{orig.name} (keys and sign-ins are never copied)")
            continue
        if any(_within(orig, k) for k in policy.keep_out):
            skipped.append(f"{orig.name} (Rescue doesn't snapshot its own records)")
            continue
        if _whole_home_or_profile(orig, policy.roots):
            skipped.append(f"{orig.name} (a whole Hermes home or profile is never snapshotted)")
            continue
        stored = folder / _stored_name(orig, home)
        if not orig.exists() and not orig.is_symlink():
            records.append(dict(orig=key, stored=str(stored), kind="absent", existed=False, size=0))
            continue
        try:
            size = _size(orig, policy.skip)
            if size > MAX_SNAPSHOT_BYTES:
                skipped.append(f"{orig} ({size // (1024 * 1024)} MB is too big to copy)")
                continue
            if orig.is_dir():   # a symlinked folder is copied through the link, not as the link
                stored.mkdir(parents=True, exist_ok=True)
                for p in _walk(orig, policy.skip):
                    _copy_one(p, stored / p.relative_to(orig))
                kind = "dir"
            else:
                kind = _copy_one(orig, stored)
        except (OSError, sqlite3.Error, shutil.Error) as exc:
            skipped.append(f"{orig} ({exc})")
            continue
        records.append(dict(orig=key, stored=str(stored), kind=kind, existed=True, size=size))
    return folder, records, skipped


# ---- undo ---------------------------------------------------------------------------------------

def _set_aside(path: Path, aside: Path, home: Path) -> None:
    """Move the current *path* into the snapshot's ``_replaced-…`` folder."""
    dest = aside / _stored_name(path, home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.is_symlink() or dest.is_file():
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    shutil.move(str(path), str(dest))


def _keep_copy(path: Path, aside: Path, home: Path) -> None:
    """Copy the current *path* aside (it stays in place: a live database is restored into)."""
    _copy_one(path, aside / _stored_name(path, home))


def _restore_sqlite(stored: Path, orig: Path) -> None:
    """Write the saved database into the live one through SQLite, so Hermes's open connections and
    its journal see the restored rows."""
    orig.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(_uri(stored, True), uri=True)
    try:
        target = sqlite3.connect(str(orig), timeout=30)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


# Hermes's conversation history, in insert order (messages refer to sessions). Rows are only ever
# added back: Hermes's triggers keep its search index and display order up to date as they land.
_HERMES_CONTENT_TABLES = ("sessions", "messages")


class _ShapeChanged(Exception):
    pass


def _whole_home_or_profile(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        if _within(root, path):
            return True
        if _within(path, root):
            parts = path.relative_to(root).parts
            if parts[:1] == ("profiles",) and len(parts) <= 2:
                return True
    return False


def is_hermes_state_db(path: Path, roots: Sequence[Path]) -> bool:
    for root in roots:
        if _within(path, root):
            parts = path.relative_to(root).parts
            return parts == ("state.db",) or (len(parts) == 3 and parts[0] == "profiles" and parts[2] == "state.db")
    return False


def merge_rows(stored: Path, orig: Path) -> dict[str, int]:
    """Add back the conversations and messages in *stored* that *orig* no longer has. Messages
    only come back with their conversation. Returns counts (``sessions``, ``messages``,
    ``not_back``: saved conversations still missing, e.g. a newer one took the same title).
    Raises ``_ShapeChanged`` (nothing written) when a table is missing on either side."""
    con = sqlite3.connect(_uri(orig), uri=True, timeout=30)
    try:
        con.execute("ATTACH DATABASE ? AS snap", (_uri(stored, True),))
        added: dict[str, int] = {}
        try:
            with con:
                for table in _HERMES_CONTENT_TABLES:
                    live = [r[1] for r in con.execute(f'PRAGMA main.table_info("{table}")')]
                    saved = {r[1] for r in con.execute(f'PRAGMA snap.table_info("{table}")')}
                    if not live or not saved:
                        raise _ShapeChanged(table)
                    cols = ", ".join(f'"{c}"' for c in live if c in saved)
                    where = ' WHERE "session_id" IN (SELECT "id" FROM main."sessions")' \
                        if table == "messages" and "session_id" in saved and "session_id" in live else ""
                    cur = con.execute(f'INSERT OR IGNORE INTO main."{table}" ({cols}) '
                                      f'SELECT {cols} FROM snap."{table}"{where}')
                    added[table] = max(cur.rowcount, 0)
                added["not_back"] = con.execute(
                    'SELECT count(*) FROM snap."sessions" WHERE "id" NOT IN (SELECT "id" FROM main."sessions")'
                ).fetchone()[0]
        finally:
            con.execute("DETACH DATABASE snap")
        return added
    finally:
        con.close()


def _merge_message(name: str, added: dict[str, int]) -> str:
    n_s, n_m, left = added.get("sessions", 0), added.get("messages", 0), added.get("not_back", 0)
    text = (f"{name}: brought back {n_s} conversation{'s' if n_s != 1 else ''} and {n_m} "
            f"message{'s' if n_m != 1 else ''}; everything else in it is left as Hermes has it now "
            f"(the full copy from before is kept in the snapshot)")
    if left:
        text += (f". {left} conversation{'s' if left != 1 else ''} couldn't come back because a newer one "
                 f"uses the same title")
    return text


def _restore_file(stored: Path, orig: Path, aside: Path, home: Path) -> bool:
    """Put one saved file back. Returns False when it was already identical."""
    if orig.is_dir() and not orig.is_symlink():
        _set_aside(orig, aside, home)
    elif orig.exists() or orig.is_symlink():
        live_db = _is_sqlite(stored) and _is_sqlite(orig)
        # (a live database's main file can match while its journal holds the change: always restore it)
        if not live_db and not stored.is_symlink() and not orig.is_symlink() \
                and filecmp.cmp(stored, orig, shallow=False):
            return False
        if live_db:
            _keep_copy(orig, aside, home)
            try:
                _restore_sqlite(stored, orig)
                return True
            except sqlite3.DatabaseError:
                pass   # unreadable live file: fall through to replacing it whole
        else:
            _keep_copy(orig, aside, home)
    elif _is_sqlite(stored):
        _restore_sqlite(stored, orig)
        return True
    orig.parent.mkdir(parents=True, exist_ok=True)
    tmp = orig.with_name(f".{orig.name}.rescue-tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    _copy_one(stored, tmp)
    os.replace(tmp, orig)
    if _is_sqlite(orig):   # the old file's journal must not be replayed over the restored one
        for suffix in ("-wal", "-shm"):
            stale = orig.with_name(orig.name + suffix)
            if stale.exists():
                _set_aside(stale, aside, home)
    return True


def _restore_dir(stored: Optional[Path], orig: Path, aside: Path, home: Path, policy: Policy,
                 notes: Optional[list[str]] = None) -> tuple[int, int]:
    """Merge a saved folder back: saved files return, files added since are set aside, and keys,
    sign-ins and databases in use are never moved. Returns (files put back, files set aside)."""
    saved = {p.relative_to(stored) for p in _walk(stored, lambda _p: False)} if stored and stored.is_dir() else set()
    back = moved = 0
    notes = notes if notes is not None else []
    if orig.is_dir():        # through a symlink too: the link stays, its contents are restored
        for p in list(_walk(orig, policy.skip)):
            if p.relative_to(orig) in saved:
                continue
            if _is_sqlite(p):    # a database added since may be open right now: leave it where it is
                notes.append(f"left {p.name} in place (a database that may be in use)")
                continue
            _set_aside(p, aside, home)
            moved += 1
    elif orig.exists() or orig.is_symlink():
        _set_aside(orig, aside, home)
        moved += 1
    for rel in sorted(saved):
        dst = orig / rel
        if policy.skip(dst):
            continue
        src = stored / rel
        if is_hermes_state_db(dst, policy.roots) and _is_sqlite(dst) and _is_sqlite(src):
            notes.append(_merge_message(f"{orig.name}/{rel}", merge_rows(src, dst)))
            continue
        if _restore_file(src, dst, aside, home):
            back += 1
    return back, moved


def restore(records: list[dict], home: Path, folder: Path, policy: Optional[Policy] = None,
            snap_root: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """Put snapshot *records* back. Current versions go to ``<folder>/_replaced-<time>/``.
    Returns (restored descriptions, problems)."""
    policy = policy or Policy([home], [folder.parent.parent] if snap_root is None else [snap_root.parent])
    folder = Path(os.path.normpath(str(folder)))
    if snap_root is not None and folder.parent != Path(os.path.normpath(str(snap_root))):
        return [], ["this snapshot's folder is outside Rescue's own folder, so Rescue won't use it"]
    aside = folder / time.strftime("_replaced-%Y%m%d-%H%M%S")
    done, problems = [], []
    for rec in records:
        orig = Path(os.path.normpath(str(rec.get("orig", ""))))
        stored = Path(os.path.normpath(str(rec.get("stored", ""))))
        if not policy.allowed(orig) or not _within(stored, folder) or _within(stored, aside):
            problems.append(f"{orig.name}: outside the Hermes folders, left alone")
            continue
        if is_secret(orig, policy.roots):
            problems.append(f"{orig.name}: keys and sign-ins are never restored by Rescue")
            continue
        try:
            if not rec.get("existed"):
                if orig.is_dir() and not orig.is_symlink():
                    notes: list[str] = []
                    _back, moved = _restore_dir(None, orig, aside, home, policy, notes)
                    if moved:
                        done.append(f"set aside {orig.name}/ (it did not exist before)")
                    done += notes
                elif _is_sqlite(orig):
                    problems.append(f"{orig.name}: left in place (a database that may be in use)")
                elif orig.exists() or orig.is_symlink():
                    _set_aside(orig, aside, home)
                    done.append(f"set aside {orig.name} (it did not exist before)")
                continue
            if not stored.exists() and not stored.is_symlink():
                problems.append(f"{orig.name}: the saved copy is missing")
                continue
            if rec.get("kind") == "sqlite" and is_hermes_state_db(orig, policy.roots) and _is_sqlite(orig):
                try:
                    added = merge_rows(stored, orig)
                except (_ShapeChanged, sqlite3.DatabaseError) as exc:
                    problems.append(f"{orig.name}: couldn't merge ({exc}). The full copy from before is kept at "
                                    f"{stored}; to roll back everything, stop Hermes and copy it over {orig.name}.")
                    continue
                done.append(_merge_message(orig.name, added))
                continue
            if rec.get("kind") == "dir":
                notes = []
                back, moved = _restore_dir(stored, orig, aside, home, policy, notes)
                extra = f", {moved} newer file{'s' if moved != 1 else ''} set aside" if moved else ""
                done.append(f"restored {orig.name}/ ({back} file{'s' if back != 1 else ''} put back{extra})")
                done += notes
            else:
                changed = _restore_file(stored, orig, aside, home)
                done.append(f"restored {orig.name}" if changed else f"{orig.name} was already as saved")
        except (OSError, shutil.Error, sqlite3.Error) as exc:
            problems.append(f"{orig.name}: {exc}")
    return done, problems


def prune(store, snap_root: Path, keep: int = KEEP_SNAPSHOTS) -> None:
    """Delete snapshot folders beyond the newest *keep*."""
    root = Path(os.path.normpath(str(snap_root)))
    for snap_id in store.old_snapshot_ids(keep):
        snap = store.snapshot(snap_id)
        folder = Path(os.path.normpath(snap["folder"])) if snap and snap.get("folder") else None
        if folder is not None and folder.parent == root:  # never delete outside our own folder
            shutil.rmtree(folder, ignore_errors=True)
        store.delete_snapshot_row(snap_id)
