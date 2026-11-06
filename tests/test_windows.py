"""Native Windows: the Hermes home is %LOCALAPPDATA%\\hermes and the agent's terminal is Git Bash,
which may also call PowerShell or cmd. These run on any machine by switching the path rules."""

from pathlib import PureWindowsPath as W

import pytest

from hermes_rescue import guard

USER = W(r"C:\Users\eli")
HOME = W(r"C:\Users\eli\AppData\Local\hermes")
PROJECT = W(r"C:\Users\eli\projects\app")


@pytest.fixture(autouse=True)
def windows(monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\eli\AppData\Local")
    guard.use_windows_paths(True)
    yield
    guard.use_windows_paths(False)


def shell(cmd, cwd=PROJECT):
    return guard.check_shell(cmd, HOME, None, cwd, user_home=USER)


@pytest.mark.parametrize("cmd", [
    "rm -rf /c/Users/eli/AppData/Local/hermes/memories",
    'rm -rf "$LOCALAPPDATA/hermes/memories"',
    "rm -rf ~/AppData/Local/hermes/sessions",
    r'rm -rf "C:\Users\eli\AppData\Local\hermes\memories"',
    r"rm -f 'c:\users\ELI\appdata\local\hermes\state.db'",
    "cd ~/AppData/Local/hermes && rm -rf memories",
    r"powershell -Command 'Remove-Item -Recurse -Force $env:LOCALAPPDATA\hermes\memories'",
    r"powershell.exe -NoProfile -Command 'ri -r -fo C:\Users\eli\AppData\Local\hermes\sessions'",
    r'cmd /c "rd /s /q %LOCALAPPDATA%\hermes\skills"',
    r"pwsh -c 'Move-Item $env:LOCALAPPDATA\hermes\memories C:\temp'",
])
def test_deleting_hermes_data_on_windows_is_blocked(cmd):
    assert shell(cmd).action == guard.BLOCK, cmd


def test_keys_on_windows_are_never_unlockable():
    v = shell(r"powershell -c 'Remove-Item $env:LOCALAPPDATA\hermes\.env'")
    assert v.action == guard.BLOCK and not v.liftable


def test_settings_edits_on_windows_ask():
    assert shell(r"pwsh -c 'Set-Content -Path $env:LOCALAPPDATA\hermes\config.yaml -Value x'").action == guard.APPROVE
    assert shell("echo x >> /c/Users/eli/AppData/Local/hermes/config.yaml").action == guard.APPROVE


@pytest.mark.parametrize("cmd", [
    "rm -rf /c/Users/eli/projects/app/build", "rm -rf build dist", "rm -rf /tmp/x",
    r"powershell -c 'Remove-Item -Recurse -Force node_modules'", "cmd /c dir", 'ls "$LOCALAPPDATA/hermes"',
    "rm -rf ~/Downloads/old", r"cmd /c 'del /q C:\Users\eli\Desktop\notes.txt'",
])
def test_everyday_windows_commands_are_allowed(cmd):
    assert shell(cmd).action == guard.ALLOW, cmd


def test_file_tools_with_windows_paths():
    tool = lambda path: guard.check_tool_call("write_file", {"path": path}, HOME, None, PROJECT, user_home=USER)
    assert tool("C:/Users/eli/AppData/Local/hermes/config.yaml").action == guard.APPROVE
    assert tool(r"C:\Users\eli\AppData\Local\hermes\sessions\x.json").action == guard.BLOCK
    assert tool(r"src\app.py").action == guard.ALLOW


def test_python_using_localappdata_asks():
    code = "import os\nos.remove(os.path.join(os.environ['LOCALAPPDATA'], 'hermes', 'memories', 'USER.md'))"
    assert guard.check_tool_call("execute_code", {"code": code}, HOME, None, PROJECT).action == guard.APPROVE
    code = "open('C:\\\\Users\\\\eli\\\\AppData\\\\Local\\\\hermes\\\\config.yaml', 'w').write('x')"
    assert guard.check_tool_call("execute_code", {"code": code}, HOME, None, PROJECT).action == guard.APPROVE
