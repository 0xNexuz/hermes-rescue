"""Hooks + store + snapshots + /rescue, driven the way Hermes calls them, on a temp home."""

import importlib
import sqlite3
import time
from pathlib import Path

import pytest

runtime = importlib.import_module("hermes_rescue.runtime")
commands = importlib.import_module("hermes_rescue.commands")
store_mod = importlib.import_module("hermes_rescue.store")


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "hermes-home"
    (h / "memories").mkdir(parents=True)
    (h / "memories" / "USER.md").write_text("likes jollof\n")
    (h / "config.yaml").write_text("model:\n  default: a\n")
    (h / "SOUL.md").write_text("You are Hermes.\n")
    db = sqlite3.connect(h / "state.db")
    db.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, content TEXT)")
    db.execute("CREATE TABLE session_admissions (id TEXT PRIMARY KEY)")   # Hermes's runtime bookkeeping
    db.executemany("INSERT INTO sessions VALUES (?)", [(f"s{i}",) for i in range(237)])
    db.executemany("INSERT INTO messages (session_id, content) VALUES (?, ?)", [(f"s{i}", "hi") for i in range(237)])
    db.commit()
    db.close()
    return h


@pytest.fixture
def rescue(home, tmp_path):
    data = tmp_path / "plugin-data"
    data.mkdir()

    class Ctx:
        def session_cwd(self):
            return str(home)

    return runtime.Rescue(Ctx(), home_fn=lambda: home, data_dir_fn=lambda: data,
                          store_factory=lambda: store_mod.Store(lambda: sqlite3.connect(data / "rescue.db",
                                                                                        check_same_thread=False)),
                          detect=False)


def call_tool(rescue, tool, args, turn="t1", call="c1"):
    return rescue.on_pre_tool_call(tool_name=tool, args=args, session_id="s", tool_call_id=call, turn_id=turn,
                                   task_id="", api_request_id="", middleware_trace=[])


# ---- guard ---------------------------------------------------------------------------------------

def test_deleting_sessions_is_blocked_and_explained_in_the_reply(rescue, home):
    out = call_tool(rescue, "terminal", {"command": f"sqlite3 {home}/state.db 'DELETE FROM sessions'"})
    assert out["action"] == "block" and "hermes sessions prune --dry-run" in out["message"]
    reply = rescue.on_transform_llm_output(response_text="I cleaned up old sessions.", session_id="s",
                                           platform="telegram", turn_id="t1", model="m")
    assert reply.startswith("I cleaned up old sessions.")
    assert "Rescue stopped a risky change" in reply and "/rescue allow 10" in reply
    assert rescue.store().counts(0)["guard_block"] == 1


def test_editing_settings_asks_and_snapshots_first(rescue, home):
    out = call_tool(rescue, "terminal", {"command": f"sed -i 's/a/b/' {home}/config.yaml"})
    assert out["action"] == "approve" and "config.yaml" in out["message"]
    snaps = rescue.store().snapshots()
    assert len(snaps) == 1 and Path(snaps[0]["files"][0]["stored"]).read_text() == "model:\n  default: a\n"


def test_denied_approval_marks_snapshot_unused(rescue, home):
    call_tool(rescue, "write_file", {"path": str(home / "SOUL.md"), "content": "pirate"}, call="c9")
    rescue.on_post_tool_call(tool_name="write_file", tool_call_id="c9", status="blocked")
    assert rescue.store().snapshots() == []  # nothing to undo: the change never happened


def test_guard_off_lets_it_through_but_keeps_a_snapshot(rescue, home):
    cmd = commands.RescueCommand(rescue)
    assert "Guard is off for 5 min" in cmd("allow 5")
    out = call_tool(rescue, "terminal", {"command": f"rm -rf {home}/memories"})
    assert out is None
    snap = rescue.store().snapshots()[0]
    assert snap["files"][0]["kind"] == "dir"
    assert "Guard: OFF" in cmd("")
    assert "Guard is back on" in cmd("lock")
    assert call_tool(rescue, "terminal", {"command": f"rm -rf {home}/memories"})["action"] == "block"


def test_undo_puts_files_back_and_keeps_the_replaced_version(rescue, home):
    call_tool(rescue, "write_file", {"path": str(home / "config.yaml")}, call="c1")
    rescue.on_post_tool_call(tool_call_id="c1", status="ok")
    (home / "config.yaml").write_text("broken: [\n")          # the agent's bad edit lands
    msg = commands.RescueCommand(rescue)("undo 1")
    assert "put back snapshot #1" in msg
    assert (home / "config.yaml").read_text() == "model:\n  default: a\n"
    replaced = list(Path(rescue.store().snapshot(1)["folder"]).glob("_replaced-*/home/config.yaml"))
    assert replaced and replaced[0].read_text() == "broken: [\n"
    assert "already put back" in commands.RescueCommand(rescue)("undo 1")


def test_undo_with_buttons_uses_ask_choice(rescue, home):
    call_tool(rescue, "write_file", {"path": str(home / "SOUL.md")}, call="c1")
    (home / "SOUL.md").write_text("You are a pirate.\n")
    asked = {}

    def ask(question, choices):
        asked["choices"] = choices
        return choices[0]["value"]

    msg = commands.RescueCommand(rescue, ask=ask)("undo")
    assert asked["choices"][-1]["value"] == "cancel"
    assert "put back" in msg and (home / "SOUL.md").read_text() == "You are Hermes.\n"


def test_sqlite_database_snapshot_is_consistent(rescue, home):
    cmd = commands.RescueCommand(rescue)
    cmd("allow 1")
    call_tool(rescue, "terminal", {"command": f"sqlite3 {home}/state.db 'DELETE FROM sessions'"})
    stored = rescue.store().snapshots()[0]["files"][0]
    assert stored["kind"] == "sqlite"
    copy = sqlite3.connect(stored["stored"])
    assert copy.execute("SELECT count(*) FROM sessions").fetchone()[0] == 237
    assert copy.execute("SELECT count(*) FROM messages").fetchone()[0] == 237


def test_a_bug_in_the_guard_never_blocks_work(rescue, monkeypatch):
    monkeypatch.setattr(runtime.guard, "check_tool_call", lambda *a, **k: 1 / 0)
    assert call_tool(rescue, "terminal", {"command": "ls"}) is None


def test_ordinary_tool_calls_add_nothing(rescue):
    assert call_tool(rescue, "terminal", {"command": "npm test"}) is None
    assert rescue.on_transform_llm_output(response_text="done", turn_id="t1", session_id="s") is None


# ---- real cause ----------------------------------------------------------------------------------

def api_error(rescue, turn="t2", platform="cli", **kw):
    rescue.on_api_request_error(turn_id=turn, session_id="s", platform=platform, task_id="", api_request_id="r",
                                **kw)


def test_failed_turn_gets_the_whole_chain(rescue):
    api_error(rescue, provider="openrouter", model="anthropic/claude-sonnet-4.6",
              base_url="https://openrouter.ai/api/v1", status_code=401, reason="auth",
              error={"type": "AuthenticationError", "message": "invalid_api_key"})
    api_error(rescue, provider="custom", model="llama3", base_url="http://localhost:11434/v1",
              status_code=404, reason="model_not_found", error={"type": "NotFoundError", "message": "model not found"})
    reply = rescue.on_transform_llm_output(response_text="Your request was not processed.", turn_id="t2",
                                           session_id="s", platform="cli")
    assert "1. OpenRouter · claude-sonnet-4.6 → key rejected (401)" in reply
    assert "2. Ollama · llama3 (backup) → model not loaded (404)" in reply
    assert "hermes auth add openrouter" in reply
    log = commands.RescueCommand(rescue)("log")
    assert "reply failed" in log and "key rejected" in log


def test_recovered_turn_after_rate_limit_stays_quiet(rescue):
    api_error(rescue, provider="openrouter", model="m", status_code=429, reason="rate_limit", error={})
    rescue.on_post_api_request(turn_id="t2", session_id="s")
    assert rescue.on_transform_llm_output(response_text="Here you go", turn_id="t2", session_id="s") is None


def test_backup_answering_for_a_dead_key_is_flagged(rescue):
    api_error(rescue, provider="anthropic", model="claude-x", status_code=401, reason="auth", error={})
    rescue.on_post_api_request(turn_id="t2", session_id="s")
    reply = rescue.on_transform_llm_output(response_text="Here you go", turn_id="t2", session_id="s")
    assert reply.startswith("Here you go") and "came from a backup" in reply


# ---- /rescue -------------------------------------------------------------------------------------

def test_status_when_all_is_well(rescue):
    assert "Nothing went wrong" in commands.RescueCommand(rescue)("")


def test_help_and_unknown_option(rescue):
    cmd = commands.RescueCommand(rescue)
    assert "/rescue undo" in cmd("help")
    assert "Unknown option" in cmd("frobnicate")


def test_snapshots_are_pruned_to_the_newest(rescue, home, monkeypatch):
    monkeypatch.setattr(runtime.snapshots, "KEEP_SNAPSHOTS", 3)
    monkeypatch.setattr(runtime.snapshots.prune, "__defaults__", (3,))
    for i in range(6):
        call_tool(rescue, "write_file", {"path": str(home / "config.yaml")}, call=f"c{i}")
    rows = rescue.store().snapshots(10, usable_only=False)
    assert len(rows) == 3
    folders = [p for p in (rescue.snap_root()).iterdir() if p.is_dir()]
    assert len(folders) == 3


# ---- failed turns that end without transform_llm_output -----------------------------------------

class Posting:
    def __init__(self):
        self.posts, self.edits = [], []

    def session_cwd(self):
        return ""

    def post_message(self, text):
        posting = self

        class Handle:
            ok = True

            def edit(self, new):
                posting.edits.append(new)
                return {"ok": True}
        self.posts.append(text)
        return Handle()


@pytest.fixture
def posting_rescue(home, tmp_path):
    data = tmp_path / "pd"
    data.mkdir()
    ctx = Posting()
    r = runtime.Rescue(ctx, home_fn=lambda: home, data_dir_fn=lambda: data,
                       store_factory=lambda: store_mod.Store(lambda: sqlite3.connect(data / "r.db", check_same_thread=False)),
                       detect=False)
    r.settle_seconds = 0.05  # chat-app delay; the tests use platform="cli" (posted at once)
    return r, ctx


def settle(r):
    time.sleep(0.25)


def test_retries_exhausted_posts_the_real_cause_once(posting_rescue):
    r, ctx = posting_rescue
    for n in range(3):  # the shape traced from a real Hermes run: three 404s, reason "unknown"
        api_error(r, provider="custom", model="missing-model", base_url="http://127.0.0.1:11434/v1",
                  status_code=404, reason="unknown", retry_count=n, max_retries=3, retryable=True,
                  error={"type": "NotFoundError", "message": "model 'missing-model' not found, try pulling it first"})
    settle(r)
    assert len(ctx.posts) == 1
    assert "Ollama · missing-model → model not loaded (404) ×3" in ctx.posts[0]
    assert "ollama pull missing-model" in ctx.posts[0]
    assert r.on_transform_llm_output(response_text="x", turn_id="t2", session_id="s") is None  # said once


def test_no_post_while_retries_remain(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, provider="openrouter", status_code=429, reason="rate_limit", retry_count=0, max_retries=3,
              retryable=True, error={})
    settle(r)
    assert ctx.posts == []


def test_in_a_chat_app_a_backup_attempt_cancels_the_note(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, platform="telegram", provider="anthropic", model="a", status_code=401, reason="auth",
              retryable=False, error={})
    r.on_post_api_request(turn_id="t2", session_id="s")  # the backup answered before the delay ran out
    settle(r)
    assert ctx.posts == []
    reply = r.on_transform_llm_output(response_text="answer", turn_id="t2", session_id="s")
    assert "came from a backup" in reply


def test_locally_the_note_posts_at_once_and_is_updated_when_a_backup_answers(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, provider="anthropic", model="a", status_code=401, reason="auth", retryable=False, error={})
    settle(r)
    assert len(ctx.posts) == 1 and "key rejected (401)" in ctx.posts[0]
    r.on_post_api_request(turn_id="t2", session_id="s")
    settle(r)
    # the key is still dead, so the note now says a backup answered, and how to fix the key
    assert ctx.edits and "a backup answered after all" in ctx.edits[-1] and "hermes auth add anthropic" in ctx.edits[-1]
    assert r.on_transform_llm_output(response_text="answer", turn_id="t2", session_id="s") is None
    log = r.store().incidents(5)
    assert [e["kind"] for e in log if e["kind"].endswith("_turn")] == ["degraded_turn"]  # one entry, updated


def test_a_failing_backup_grows_the_same_note(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, provider="anthropic", model="a", status_code=401, reason="auth", retryable=False, error={})
    settle(r)
    api_error(r, provider="custom", model="llama3", base_url="http://localhost:11434/v1", status_code=404,
              reason="model_not_found", retryable=False, error={"message": "model not found"})
    settle(r)
    assert len(ctx.posts) == 1  # one note, edited to show the whole chain
    assert "1. Anthropic · a → key rejected (401)" in ctx.edits[-1]
    assert "2. Ollama · llama3 (backup) → model not loaded (404)" in ctx.edits[-1]


def test_a_late_recovery_updates_the_posted_note(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, provider="openrouter", status_code=503, reason="server_error", retry_count=2, max_retries=3,
              retryable=True, error={})
    settle(r)
    assert len(ctx.posts) == 1 and "service error (503)" in ctx.posts[0]
    api_error(r, provider="openrouter", status_code=503, reason="server_error", retry_count=0, max_retries=3,
              retryable=True, error={})
    r.on_post_api_request(turn_id="t2", session_id="s")  # Hermes' auto-recovery got through later
    settle(r)
    assert ctx.edits and "a later attempt got through" in ctx.edits[0]
    assert r.store().incidents(1)[0]["kind"] == "recovered_turn"


# ---- things the guard never unlocks -------------------------------------------------------------

def test_keys_and_the_whole_home_stay_blocked_even_with_the_guard_off(rescue, home):
    (home / ".env").write_text("OPENROUTER_API_KEY=sk-test\n")
    commands.RescueCommand(rescue)("allow 10")
    for cmd in (f"rm {home}/.env", f"rm -r {home}", f"rm {home}/auth.json"):
        out = call_tool(rescue, "terminal", {"command": cmd})
        assert out and out["action"] == "block", cmd
        assert "never lets the agent" in out["message"]
    assert (home / ".env").exists()
    assert not any(p.name == ".env" for p in rescue.snap_root().rglob("*"))  # never copied


def test_directory_snapshots_leave_credentials_out(rescue, home):
    (home / "skills" / "mine").mkdir(parents=True)
    (home / "skills" / "mine" / "SKILL.md").write_text("# mine\n")
    (home / "skills" / "mine" / ".env").write_text("SECRET=1\n")
    commands.RescueCommand(rescue)("allow 10")
    assert call_tool(rescue, "terminal", {"command": f"rm -r {home}/skills"}) is None
    names = {p.name for p in rescue.snap_root().rglob("*")}
    assert "SKILL.md" in names and ".env" not in names


# ---- review regressions --------------------------------------------------------------------------

def test_undo_of_the_live_session_database_brings_deleted_conversations_back(rescue, home):
    live = sqlite3.connect(home / "state.db", check_same_thread=False)   # Hermes keeps it open
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("PRAGMA wal_autocheckpoint=0")                          # the damage stays in the -wal
    cmd = commands.RescueCommand(rescue)
    cmd("allow 5")
    assert call_tool(rescue, "terminal", {"command": f"sqlite3 {home}/state.db 'DELETE FROM messages'"}) is None
    live.execute("DELETE FROM messages")
    live.execute("DELETE FROM sessions WHERE id <> 's0'")
    live.execute("INSERT INTO sessions VALUES ('current')")             # what Hermes wrote since
    live.execute("INSERT INTO session_admissions VALUES ('in-flight')")  # the running gateway's own row
    live.commit()
    assert (home / "state.db-wal").exists()
    msg = cmd("undo 1")
    assert "brought back 236 conversations and 237 messages" in msg
    fresh = sqlite3.connect(home / "state.db")
    assert fresh.execute("SELECT count(*) FROM messages").fetchone()[0] == 237
    assert fresh.execute("SELECT count(*) FROM sessions").fetchone()[0] == 238      # nothing new was lost
    assert fresh.execute("SELECT id FROM session_admissions").fetchall() == [("in-flight",)]
    assert live.execute("SELECT count(*) FROM messages").fetchone()[0] == 237


def test_folder_undo_merges_and_never_moves_keys(rescue, home):
    skill = home / "skills" / "mine"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mine\n")
    (skill / ".env").write_text("SECRET=1\n")
    cmd = commands.RescueCommand(rescue)
    cmd("allow 5")
    assert call_tool(rescue, "terminal", {"command": f"rm -r {home}/skills/mine/SKILL.md"}) is None
    (skill / "SKILL.md").unlink()
    (home / "skills" / "added.md").write_text("new\n")
    call_tool(rescue, "terminal", {"command": f"rm -rf {home}/skills"}, call="c2")   # snapshot of the folder
    (home / "skills" / "added.md").write_text("newer\n")
    snap = rescue.store().snapshots()[0]
    msg = cmd(f"undo {snap['id']}")
    assert "restored skills/" in msg
    assert (skill / ".env").read_text() == "SECRET=1\n"                 # left exactly where it was
    assert not any(p.name == ".env" for p in rescue.snap_root().rglob("*"))


def test_undo_refuses_paths_outside_hermes(rescue, home, tmp_path):
    outside = tmp_path / "outside.txt"
    folder = rescue.snap_root() / "planted"
    folder.mkdir(parents=True)
    (folder / "x").write_text("pwned")
    rescue.store().add_snapshot("planted", [dict(orig=str(outside), stored=str(folder / "x"), kind="file",
                                                 existed=True, size=5)], folder=str(folder))
    msg = commands.RescueCommand(rescue)("undo 1")
    assert "left alone" in msg and not outside.exists()


def test_a_planted_far_future_guard_switch_is_ignored(rescue):
    rescue.store().set("allow_until", "4102444800")
    assert not rescue.store().guard_lifted()


class BrokenStore:
    def __getattr__(self, name):
        raise sqlite3.OperationalError("database or disk is full")


def test_the_guard_still_enforces_when_its_records_cannot_be_written(home, tmp_path):
    r = runtime.Rescue(None, home_fn=lambda: home, data_dir_fn=lambda: tmp_path / "ro" / "data",
                       store_factory=BrokenStore, detect=False, root_fn=lambda: None)
    (tmp_path / "ro").mkdir()
    (tmp_path / "ro").chmod(0o500)   # snapshots can't be written either
    try:
        assert call_tool(r, "terminal", {"command": f"rm -rf {home}/sessions"})["action"] == "block"
        out = call_tool(r, "terminal", {"command": f"sed -i 's/a/b/' {home}/config.yaml"})
        assert out["action"] == "approve" and "could not save" in out["message"]
    finally:
        (tmp_path / "ro").chmod(0o700)


def test_a_call_that_ran_and_errored_keeps_its_snapshot(rescue, home):
    call_tool(rescue, "terminal", {"command": f"sed -i 's/a/b/' {home}/config.yaml && npm test"}, call="c7")
    rescue.on_post_tool_call(tool_call_id="c7", status="error")   # npm test failed; config was still edited
    assert rescue.store().snapshots()


def test_unknown_session_folder_does_not_guess(home, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    r = runtime.Rescue(Posting(), home_fn=lambda: home, data_dir_fn=lambda: data,
                       store_factory=lambda: store_mod.Store(lambda: sqlite3.connect(data / "r.db", check_same_thread=False)),
                       detect=False, root_fn=lambda: None)
    assert call_tool(r, "terminal", {"command": "rm -rf build"}) is None
    assert call_tool(r, "write_file", {"path": "src/app.py"}) is None


class SlowPosting(Posting):
    def post_message(self, text):
        time.sleep(0.3)
        return super().post_message(text)


def test_two_failures_during_a_slow_post_still_make_one_note(home, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    ctx = SlowPosting()
    r = runtime.Rescue(ctx, home_fn=lambda: home, data_dir_fn=lambda: data,
                       store_factory=lambda: store_mod.Store(lambda: sqlite3.connect(data / "r.db", check_same_thread=False)),
                       detect=False, root_fn=lambda: None)
    api_error(r, provider="anthropic", model="a", status_code=401, reason="auth", retryable=False, error={})
    time.sleep(0.05)
    api_error(r, provider="custom", model="llama3", base_url="http://localhost:11434/v1", status_code=404,
              reason="model_not_found", retryable=False, error={"message": "model not found"})
    time.sleep(0.9)
    assert len(ctx.posts) == 1
    assert "(backup)" in (ctx.edits[-1] if ctx.edits else ctx.posts[0])
    assert sum(1 for e in r.store().incidents(10) if e["kind"] == "failed_turn") == 1


class RefusingPosting(Posting):
    def post_message(self, text):
        class Refused:
            ok = False
        return Refused()


def test_a_refused_post_still_logs_once_and_lets_the_reply_explain(home, tmp_path):
    data = tmp_path / "d"
    data.mkdir()
    r = runtime.Rescue(RefusingPosting(), home_fn=lambda: home, data_dir_fn=lambda: data,
                       store_factory=lambda: store_mod.Store(lambda: sqlite3.connect(data / "r.db", check_same_thread=False)),
                       detect=False, root_fn=lambda: None)
    api_error(r, provider="anthropic", model="a", status_code=401, reason="auth", retryable=False, error={})
    settle(r)
    r.on_post_api_request(turn_id="t2", session_id="s")     # a backup answered after all
    reply = r.on_transform_llm_output(response_text="answer", turn_id="t2", session_id="s")
    assert reply and "came from a backup" in reply
    kinds = [e["kind"] for e in r.store().incidents(10) if e["kind"].endswith("_turn")]
    assert kinds == ["degraded_turn"]


def test_in_a_chat_app_a_slow_backup_turns_the_note_into_a_heads_up(posting_rescue):
    r, ctx = posting_rescue
    api_error(r, platform="telegram", provider="anthropic", model="a", status_code=401, reason="auth",
              retryable=False, error={})
    settle(r)                                               # the backup took longer than the delay
    assert len(ctx.posts) == 1
    r.on_post_api_request(turn_id="t2", session_id="s")
    settle(r)
    assert ctx.edits and "a backup answered after all" in ctx.edits[-1]


def test_merge_never_leaves_messages_without_their_conversation(rescue, home):
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE UNIQUE INDEX titles ON sessions(id)")   # stand-in for Hermes's unique title index
    db.close()
    cmd = commands.RescueCommand(rescue)
    cmd("allow 5")
    call_tool(rescue, "terminal", {"command": f"sqlite3 {home}/state.db 'DELETE FROM messages'"})
    live = sqlite3.connect(home / "state.db")
    live.execute("DELETE FROM messages WHERE session_id = 's1'")
    live.execute("DELETE FROM sessions WHERE id = 's1'")
    live.commit()
    snaps = runtime.snapshots
    folder = Path(rescue.store().snapshots()[0]["folder"])
    stored = next(folder.rglob("state.db"))
    s = sqlite3.connect(stored)                                   # the saved copy has an orphan message
    s.execute("INSERT INTO messages (session_id, content) VALUES ('ghost', 'x')")
    s.commit()
    s.close()
    added = snaps.merge_rows(stored, home / "state.db")
    assert added["sessions"] == 1 and added["messages"] == 1     # s1 and its one message; not the ghost
    orphans = live.execute("SELECT count(*) FROM messages WHERE session_id NOT IN (SELECT id FROM sessions)")
    assert orphans.fetchone()[0] == 0


def test_folder_undo_leaves_a_database_added_since_in_place(rescue, home):
    cmd = commands.RescueCommand(rescue)
    cmd("allow 5")
    (home / "skills").mkdir(exist_ok=True)
    call_tool(rescue, "terminal", {"command": f"rm -rf {home}/skills"})
    live = sqlite3.connect(home / "skills" / "cache.db")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t (x)")
    live.commit()
    msg = cmd("undo 1")
    assert "left cache.db in place" in msg and (home / "skills" / "cache.db").exists()


def test_a_symlinked_protected_folder_is_snapshot_through_the_link(rescue, home, tmp_path):
    dot = tmp_path / "dotfiles" / "skills" / "foo"
    dot.mkdir(parents=True)
    (dot / "SKILL.md").write_text("original")
    (home / "skills").symlink_to(tmp_path / "dotfiles" / "skills")
    commands.RescueCommand(rescue)("allow 5")
    call_tool(rescue, "terminal", {"command": f"rm -rf {home}/skills"})
    (dot / "SKILL.md").write_text("clobbered")
    assert "restored skills/" in commands.RescueCommand(rescue)("undo 1")
    assert (dot / "SKILL.md").read_text() == "original" and (home / "skills").is_symlink()


def test_undo_before_the_change_happens_keeps_the_snapshot(rescue, home):
    original = (home / "config.yaml").read_text()
    out = call_tool(rescue, "terminal", {"command": f"echo '# t' >> {home}/config.yaml"}, call="c9")
    assert out["action"] == "approve"            # the snapshot is saved while the approval waits
    cmd = commands.RescueCommand(rescue)
    assert "Nothing to undo yet" in cmd("undo 1")
    rescue.on_post_tool_call(tool_call_id="c9", status="ok")
    (home / "config.yaml").write_text(original + "# t\n")   # the human approved; it ran
    assert "put back snapshot #1" in cmd("undo 1")
    assert (home / "config.yaml").read_text() == original
