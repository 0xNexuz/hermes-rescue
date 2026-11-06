"""Self-repair guard: which agent actions on Hermes's own data are blocked, asked, or allowed."""

import importlib
from pathlib import Path

import pytest

guard = importlib.import_module("hermes_rescue.guard")

HOME = Path("/home/u/.hermes")
INSTALL = Path("/home/u/.hermes/hermes-agent")
ELSEWHERE = Path("/home/u/projects/app")


def shell(cmd, cwd=ELSEWHERE):
    return guard.check_shell(cmd, HOME, INSTALL, cwd)


@pytest.mark.parametrize("cmd", [
    "rm -rf ~/.hermes/sessions",
    "rm -rf ~/.hermes/sessions/*",
    "rm ~/.hermes/state.db",
    "rm -f $HERMES_HOME/state.db-wal",
    "sqlite3 ~/.hermes/state.db 'DELETE FROM sessions WHERE started_at < 1700000000'",
    "sqlite3 ~/.hermes/state.db \"VACUUM\"",
    "find ~/.hermes/sessions -name '*.json' -mtime +7 -delete",
    "rm -rf ~/.hermes/memories",
    "rm -rf ~/.hermes",
    "rm -rf ~",
    "mv ~/.hermes/skills /tmp/old-skills",
    "truncate -s 0 ~/.hermes/state.db",
    "cd /tmp && rm -rf ~/.hermes/profiles/work",
    "pip uninstall -y hermes-agent",
    "rm -rf ~/.hermes/hermes-agent",
    "sudo rm -rf /home/u/.hermes/checkpoints",
])
def test_destructive_commands_are_blocked(cmd):
    v = shell(cmd)
    assert v.action == guard.BLOCK, cmd
    msg = v.agent_message()
    assert "BLOCKED by the Rescue plugin" in msg
    # Keys, the whole home and the install can never be unlocked; everything else can, for a while.
    assert ("/rescue allow" in msg) == v.liftable
    assert ("never lets the agent" in msg) == (not v.liftable)


def test_relative_paths_inside_home_are_caught():
    assert shell("rm -rf sessions", cwd=HOME).action == guard.BLOCK
    assert shell("rm state.db", cwd=HOME).action == guard.BLOCK


@pytest.mark.parametrize("cmd", [
    "sed -i 's/model: a/model: b/' ~/.hermes/config.yaml",
    "echo 'You are a pirate' > ~/.hermes/SOUL.md",
    "echo 'likes tea' >> ~/.hermes/memories/USER.md",
    "cp backup.yaml ~/.hermes/config.yaml",
    "tee ~/.hermes/config.yaml < new.yaml",
])
def test_edits_to_settings_and_personality_ask_the_human(cmd):
    v = shell(cmd)
    assert v.action == guard.APPROVE, cmd
    assert "snapshot" in v.approval_reason().lower()
    assert v.paths  # the file to snapshot first


@pytest.mark.parametrize("cmd", [
    "ls -la ~/.hermes",
    "cat ~/.hermes/config.yaml",
    "sqlite3 ~/.hermes/state.db 'SELECT count(*) FROM sessions'",
    "hermes sessions prune --older-than 90d --dry-run",
    "hermes config set model.default anthropic/claude-sonnet-4.6",
    "rm -rf ~/.hermes/logs/old",
    "rm -rf ~/.hermes/cache",
    "rm -rf node_modules && npm install",
    "rm -rf /tmp/build",
    "git status",
    "python manage.py migrate 2>&1 | tail -5",
    "echo done > /dev/null",
    "",
])
def test_ordinary_work_is_allowed(cmd):
    assert shell(cmd).action == guard.ALLOW, cmd


def test_compound_command_takes_the_worst_part():
    v = shell("ls ~/.hermes && rm -rf ~/.hermes/memories && echo ok")
    assert v.action == guard.BLOCK and "remembers" in v.target.name


def test_write_file_tool_into_config_asks_and_into_state_db_blocks():
    v = guard.check_tool_call("write_file", {"path": "~/.hermes/config.yaml", "content": "x"}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.APPROVE and v.paths == [HOME / "config.yaml"]
    v = guard.check_tool_call("write_file", {"path": "/home/u/.hermes/state.db", "content": ""}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.BLOCK


def test_editing_hermes_program_files_asks():
    v = guard.check_tool_call("patch", {"path": "/home/u/.hermes/hermes-agent/run_agent.py"}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.APPROVE and v.target.name == "Hermes's own program files"


def test_project_files_are_untouched():
    v = guard.check_tool_call("write_file", {"path": "src/app.py"}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.ALLOW


def test_python_that_deletes_hermes_data_asks():
    code = "import os\nos.remove(os.path.expanduser('~/.hermes/state.db'))"
    v = guard.check_tool_call("execute_code", {"code": code}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.APPROVE
    v = guard.check_tool_call("execute_code", {"code": "print(open('/etc/hosts').read())"}, HOME, INSTALL, ELSEWHERE)
    assert v.action == guard.ALLOW


def test_custom_hermes_home_location():
    home = Path("/srv/hermes-data")
    assert guard.check_shell("rm -rf /srv/hermes-data/memories", home, None, ELSEWHERE).action == guard.BLOCK
    assert guard.check_shell("rm -rf ~/.hermes/memories", home, None, ELSEWHERE).action == guard.ALLOW


# ---- review regressions --------------------------------------------------------------------------
USER = Path("/home/u")
CHECKOUT = Path("/home/u/code/hermes-agent")


def tool(name, args, cwd=ELSEWHERE, **kw):
    return guard.check_tool_call(name, args, HOME, kw.pop("install", INSTALL), cwd, **kw)


@pytest.mark.parametrize("cmd", [
    "git clean -n", "git rm --cached notes.txt", "find . -name '*.tmp' -delete",
    "find . -name node_modules -prune -exec rm -rf {} +", "cd ~/projects/app && git clean -fdx",
    "rm -rf ./Downloads/old", "chmod -R u+w .",
])
def test_working_from_the_users_home_folder_is_not_wiping_hermes(cmd):
    # the gateway's default terminal folder is the user's home, which *contains* ~/.hermes
    assert shell(cmd, cwd=USER).action == guard.ALLOW, cmd


@pytest.mark.parametrize("cmd", [
    "rm -rf build dist *.egg-info", "rm -rf .pytest_cache", "mv tools/a.py tools/b.py",
    "sed -i 's/foo/bar/' tools/terminal_tool.py", "pytest -q > results.txt", "make 2>&1 | tee build.log",
    "git rm tools/legacy_tool.py",
])
def test_contributors_can_work_inside_their_hermes_checkout(cmd):
    assert guard.check_shell(cmd, HOME, CHECKOUT, CHECKOUT).action == guard.ALLOW, cmd


def test_working_in_the_checkout_still_protects_the_users_data():
    assert guard.check_shell("rm -rf ~/.hermes/sessions", HOME, CHECKOUT, CHECKOUT).action == guard.BLOCK
    assert tool("write_file", {"path": "tools/new_tool.py"}, cwd=CHECKOUT, install=CHECKOUT).action == guard.ALLOW


def test_program_files_from_elsewhere_ask_and_can_be_unlocked():
    v = guard.check_shell("rm -rf ~/code/hermes-agent/tools", HOME, CHECKOUT, ELSEWHERE)
    assert v.action == guard.BLOCK and v.liftable
    assert guard.check_shell("rm -rf ~/code/hermes-agent/build", HOME, CHECKOUT, ELSEWHERE).action == guard.ALLOW


def test_unknown_working_folder_leaves_relative_paths_alone():
    assert shell("rm -rf build", cwd=None).action == guard.ALLOW
    assert tool("write_file", {"path": "src/app.py"}, cwd=None).action == guard.ALLOW
    assert shell("rm -rf ~/.hermes/sessions", cwd=None).action == guard.BLOCK  # absolute paths still count


def test_the_terminal_tools_workdir_is_used():
    assert tool("terminal", {"command": "git clean -fd", "workdir": str(ELSEWHERE)}, cwd=USER).action == guard.ALLOW
    assert tool("terminal", {"command": "rm -rf sessions", "workdir": "~/.hermes"}).action == guard.BLOCK


@pytest.mark.parametrize("cmd", [
    "rm ~/.hermes/profiles/work/.env", "echo K=1 > ~/.hermes/profiles/work/.env",
    "rm ~/.hermes/auth.json.bak", "cp other.env ~/.hermes/.env",
])
def test_keys_are_protected_at_any_depth_and_never_unlockable(cmd):
    v = shell(cmd)
    assert v.action == guard.BLOCK and not v.liftable, cmd


def test_tightening_key_permissions_is_fine():
    assert shell("chmod 600 ~/.hermes/.env").action == guard.ALLOW       # what `hermes doctor` does
    assert shell("chmod 644 ~/.hermes/.env").action == guard.BLOCK


@pytest.mark.parametrize("cmd", [
    "echo \"INSERT OR REPLACE INTO kv VALUES('allow_until','4102444800');\" | "
    "sqlite3 ~/.hermes/plugin-data/hermes-rescue/rescue.db",
    "rm -rf ~/.hermes/plugin-data", "rm -rf ~/.hermes/plugin-data/hermes-rescue/snapshots",
])
def test_rescues_own_records_cannot_be_rewritten_by_the_agent(cmd):
    v = shell(cmd)
    assert v.action == guard.BLOCK and not v.liftable, cmd


def test_another_plugins_data_can_still_be_cleared_with_the_guard_off():
    v = shell("rm -rf ~/.hermes/plugin-data/disk-cleanup")
    assert v.action == guard.BLOCK and v.liftable


def test_profiles_are_homes_of_their_own():
    v = shell("rm -rf ~/.hermes/profiles/work")
    assert v.action == guard.BLOCK and not v.liftable and "hermes profile delete" in v.target.safe
    assert shell("rm -rf ~/.hermes/profiles/work/memories").action == guard.BLOCK
    assert shell("sed -i 's/a/b/' ~/.hermes/profiles/work/config.yaml").action == guard.APPROVE
    assert shell("rm -rf ~/.hermes/profiles/work/logs").action == guard.ALLOW


def test_the_default_home_is_guarded_while_a_profile_is_active():
    profile = HOME / "profiles" / "work"
    v = guard.check_shell("rm ~/.hermes/state.db", profile, None, ELSEWHERE, root=HOME)
    assert v.action == guard.BLOCK


@pytest.mark.parametrize("cmd", [
    "cd ~/.hermes && rm -rf sessions", "(cd ~/.hermes && rm -rf memories)",
    "echo 'DELETE FROM sessions;' | sqlite3 ~/.hermes/state.db", "sqlite3 ~/.hermes/state.db < cleanup.sql",
    "sqlite3 -cmd 'DELETE FROM sessions' ~/.hermes/state.db", "bash -c 'rm -rf ~/.hermes/sessions'",
    "find ~/.hermes/sessions -mtime +30 | xargs rm", "git -C ~/.hermes/hermes-agent clean -fdx",
    "dd if=/dev/zero of=/home/u/.hermes/state.db bs=1 count=0", "bash <<'EOF'\nrm -rf ~/.hermes/sessions\nEOF",
    "rm ~/.hermes/state.db*",
])
def test_destructive_shapes_the_review_found(cmd):
    assert shell(cmd).action == guard.BLOCK, cmd


@pytest.mark.parametrize("cmd", [
    "echo x 1> ~/.hermes/config.yaml", "echo x &> ~/.hermes/config.yaml", "yq -i '.a = 1' ~/.hermes/config.yaml",
])
def test_more_ways_to_rewrite_settings_ask(cmd):
    assert shell(cmd).action == guard.APPROVE, cmd


@pytest.mark.parametrize("cmd", [
    'git commit -m "docs: explain pip uninstall hermes-agent"',
    'echo "tip: pip uninstall hermes-agent then reinstall"',
    "cat > docs/x.md <<'EOF'\n# Reset\nrm -rf ~/.hermes/sessions\nEOF",
    'git commit -m "guard: block > ~/.hermes/config.yaml rewrites"',
    'grep -rn "rm -rf ~/.hermes/sessions" docs/',
    "rm -f ~/.hermes/*.pid", "rm ~/.hermes/*.log",
    "sqlite3 ~/.hermes/state.db '.tables'",
])
def test_text_that_only_mentions_hermes_paths_is_allowed(cmd):
    assert shell(cmd).action == guard.ALLOW, cmd


def test_python_checks_are_anchored_to_a_hermes_path():
    approve = [
        "from pathlib import Path\nPath('/home/u/.hermes/config.yaml').write_text('x')",
        "from pathlib import Path\nPath.home().joinpath('.hermes/state.db').unlink()",
    ]
    allow = [
        "sessions = []\nwith open('report.json','w') as f: f.write('x')",
        "import sqlite3\nsqlite3.connect('app.db').execute('DELETE FROM sessions')",
        "import os\nos.remove('tmp/sessions.lock')",
    ]
    for code in approve:
        assert tool("execute_code", {"code": code}).action == guard.APPROVE, code
    for code in allow:
        assert tool("execute_code", {"code": code}).action == guard.ALLOW, code
    assert shell("python3 -c \"import shutil; shutil.rmtree('/home/u/.hermes/sessions')\"").action == guard.APPROVE


def test_python_open_for_writing_with_nested_calls_asks():
    code = "import os\nopen(os.path.join(os.path.expanduser('~/.hermes'), 'config.yaml'), 'w').write('x')"
    assert tool("execute_code", {"code": code}).action == guard.APPROVE
    assert tool("execute_code", {"code": "print(open(os.path.expanduser('~/.hermes/config.yaml')).read())"}).action \
        == guard.ALLOW


def test_wrapper_options_with_values_are_skipped():
    assert shell("find ~/.hermes/sessions -name '*.json' | xargs -I {} rm {}").action == guard.BLOCK
    assert shell("sudo -u bob rm -rf ~/.hermes/memories").action == guard.BLOCK
    assert shell("sudo -E npm install -g pnpm").action == guard.ALLOW


# ---- second review regressions -------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "# Clean up Hermes's old sessions\nrm -rf ~/.hermes/sessions\n# that's it",
    "rm -rf ~/.hermes/{sessions,memories}", "rm -f ~/.hermes/state.db{,-wal,-shm}",
    "bash -lc 'rm -rf ~/.hermes/sessions'", "sh -xc 'rm -rf ~/.hermes/memories'",
    "echo 'rm -rf ~/.hermes/sessions' | bash",
    "find ~/.hermes -name '*.jsonl' -mtime +30 -delete", "find ~/.hermes -name 'MEMORY.md' -delete",
    "find ~ -name '*.jsonl' -delete", "find ~/.hermes -type f ! -name '*.log' -delete",
    "find ~/.hermes -regex '.*\\.jsonl' -delete",
    "rm -f $(find ~/.hermes/sessions -name '*.json' -mtime +30)", 'rm -rf "$(ls -d ~/.hermes/sessions)"',
    "out=\"$(sqlite3 ~/.hermes/state.db 'DELETE FROM messages')\"",
    "git commit -m \"Support <<EOF in docs\"\nrm -rf ~/.hermes/sessions",
    "x=$((1 << n))\nrm -rf ~/.hermes/sessions",
    "cat <<EOF-NOTES > n.md\nhi\nEOF-NOTES\nrm -rf ~/.hermes/sessions",
    "sqlite3 ~/.hermes/state.db 'UPDATE \"messages\" SET content = NULL'",
    "sqlite3 ~/.hermes/state.db 'UPDATE main.sessions SET title = NULL'",
    "sqlite3 -readonly ~/.hermes/state.db '.shell rm -rf ~/.hermes/sessions'",
    "grep -v select cleanup.sql | sqlite3 ~/.hermes/state.db",
    "rsync -a --delete /tmp/empty/ ~/.hermes/", "mv -t /tmp ~/.hermes/memories",
    "rm -rf ${HERMES_HOME:-$HOME/.hermes}/sessions", "rm -rf ~/.hermes/mcp-tokens",
    "env -C ~/.hermes rm -rf sessions", "cd ~/.hermes && cd /tmp && cd - && rm -rf sessions",
    "{ rm -rf ~/.hermes/sessions; }", "if true; then rm -rf ~/.hermes/memories; fi",
])
def test_second_review_slip_throughs_are_caught(cmd):
    assert shell(cmd).action == guard.BLOCK, cmd


def test_unpacking_over_the_home_asks_but_not_into_the_users_home():
    assert shell("tar xzf old.tgz -C ~/.hermes").action == guard.APPROVE
    assert shell("wget -qO- https://x.y/z | tar xz", cwd=USER).action == guard.ALLOW


def test_copying_into_a_profile_folder_targets_the_file_not_the_profile():
    v = shell("cp config.yaml ~/.hermes/profiles/work/")
    assert v.action == guard.APPROVE and v.paths == [HOME / "profiles" / "work" / "config.yaml"]


def test_a_huge_one_liner_is_fast():
    import time
    big = "echo '" + "A" * 2_000_000 + "' | base64 -d > logo.png"
    t = time.time()
    assert shell(big).action == guard.ALLOW
    assert time.time() - t < 3


def test_write_file_paths_are_literal():
    assert tool("write_file", {"path": "~/.hermes/sessions/[x].json"}).action == guard.BLOCK


@pytest.mark.parametrize("cmd", [
    "cat <<\\EOF > notes.md\nrm -rf ~/.hermes/sessions\nEOF",
    "echo \"don't\" # it's fine",
    "ls -la ~/.hermes/{sessions,memories}",
    "find ~ -name '*.tmp' -delete", "find ~/.hermes -name '*.log' -mtime +7 -delete",
])
def test_second_review_false_positives_stay_allowed(cmd):
    assert shell(cmd).action == guard.ALLOW, cmd


def test_copy_options_with_values_are_skipped():
    assert shell("install -m 644 new.yaml ~/.hermes/config.yaml").action == guard.APPROVE
    assert shell("rsync -a --exclude logs ~/backup/memories/ ~/.hermes/memories/").action == guard.APPROVE
