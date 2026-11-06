"""Self-repair guard: stop the agent from wrecking the Hermes it runs in.

Users ask their agent to "fix Hermes", and the agent deletes its own sessions, rewrites config
by hand or hand-prunes ``state.db`` (NousResearch/hermes-agent#144562). Core already refuses git
operations on the running checkout; nothing refuses ``rm`` on ``memories/`` or a raw
``DELETE FROM`` on the session database. This module decides, for one tool call, whether it
touches Hermes's own data and how hard to push back:

* ``block``   — destroys something Hermes keeps for the user (sessions, memories, skills, keys,
                profiles, the whole home). The agent gets the supported command instead.
* ``approve`` — edits something a person may legitimately want edited (settings, SOUL.md, a
                memory file, Hermes's program files). Hermes asks the human, naming the target.
* ``allow``   — everything else, including reads and every ``hermes ...`` CLI command.

Pure functions over (tool name, args, homes, install roots, cwd); no I/O beyond ``Path`` math,
so the decision is fast, testable, and never depends on the model. When it cannot tell where a
relative path points (no known working directory), it lets the call through rather than guess.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

BLOCK, APPROVE, ALLOW = "block", "approve", "allow"
PLUGIN_ID = "hermes-rescue"


@dataclass(frozen=True)
class Target:
    rel: str            # path relative to a Hermes home ("" = the home itself)
    name: str           # what a person calls it
    delete: str         # verdict for deleting / moving / truncating it
    edit: str           # verdict for writing into it
    safe: str           # the supported way to do the job
    is_dir: bool = False
    liftable: bool = True   # False: refused even while /rescue allow is on


# What Hermes keeps in its home, and what the agent should use instead of raw file surgery.
TARGETS: tuple[Target, ...] = (
    Target("state.db", "your saved conversations (state.db)", BLOCK, BLOCK,
           "`hermes sessions prune --dry-run …` to preview, `hermes sessions delete <id>`, or "
           "`hermes sessions repair` / `optimize`"),
    Target("state.db-wal", "your saved conversations (state.db journal)", BLOCK, BLOCK,
           "`hermes sessions repair` — never delete the journal by hand"),
    Target("state.db-shm", "your saved conversations (state.db journal)", BLOCK, BLOCK,
           "`hermes sessions repair` — never delete the journal by hand"),
    Target("sessions", "your saved conversations (sessions/)", BLOCK, BLOCK,
           "`hermes sessions prune --dry-run …` or `hermes sessions delete <id>`", is_dir=True),
    Target("memories", "what Hermes remembers about you (memories/)", BLOCK, APPROVE,
           "the memory tool to add or remove single entries", is_dir=True),
    Target("SOUL.md", "Hermes's personality file (SOUL.md)", BLOCK, APPROVE,
           "editing SOUL.md only when the user asked for it"),
    Target("skills", "your skills (skills/)", BLOCK, APPROVE,
           "`hermes skills uninstall <name>` or `hermes skills reset <name>`", is_dir=True),
    Target("config.yaml", "your settings (config.yaml)", BLOCK, APPROVE,
           "`hermes config set <key> <value>` / `hermes config unset <key>`"),
    Target("cron", "your scheduled tasks (cron/)", BLOCK, APPROVE, "the cronjob tool or `hermes cron`", is_dir=True),
    Target("plugins", "your installed plugins (plugins/)", BLOCK, APPROVE,
           "`hermes plugins remove <name>` / `hermes plugins update`", is_dir=True),
    Target("checkpoints", "your undo history (checkpoints/)", BLOCK, BLOCK, "`/rollback`", is_dir=True),
    Target("plugin-data", "data your plugins keep (plugin-data/)", BLOCK, ALLOW,
           "the plugin's own command (for example `/disk-cleanup`)", is_dir=True),
)
WHOLE_HOME = Target("", "everything Hermes knows (the whole Hermes home)", BLOCK, ALLOW,
                    "`hermes doctor` to diagnose, `hermes debug share` to ask for help", is_dir=True,
                    liftable=False)
KEYS = Target(".env", "your API keys and sign-ins (.env / auth.json)", BLOCK, BLOCK,
              "`hermes auth add <provider>`, `hermes auth remove` or `hermes setup`", liftable=False)
WHOLE_PROFILE = Target("profiles", "a whole Hermes profile, its keys included (profiles/)", BLOCK, APPROVE,
                       "`hermes profile delete <name>`", is_dir=True, liftable=False)
RESCUE_DATA = Target(f"plugin-data/{PLUGIN_ID}", "Rescue's own records and snapshots", BLOCK, BLOCK,
                     "/rescue snapshots and /rescue undo (Rescue removes old snapshots itself)", is_dir=True,
                     liftable=False)
ALL_PLUGIN_DATA = Target("plugin-data", "every plugin's data, Rescue's records included (plugin-data/)",
                         BLOCK, ALLOW, "the plugin's own command (for example `/disk-cleanup`)", is_dir=True,
                         liftable=False)
INSTALL_TARGET = Target("", "Hermes's own program files", BLOCK, APPROVE,
                        "`hermes update` to repair the install, `hermes doctor` to diagnose", is_dir=True)

# Folders under a home that are safe to clear (rebuilt automatically or scratch).
SAFE_SUBDIRS = ("logs", "cache", "tmp", "image_cache", "audio_cache", "lib")
# Build leftovers inside the program files that are always safe to clear.
SAFE_INSTALL_PARTS = {"build", "dist", ".pytest_cache", ".mypy_cache", ".ruff_cache", "__pycache__",
                      "htmlcov", ".tox", ".nox"}
# Key files are protected wherever they sit in a home (the root, a profile, a backup copy).
_KEY_NAME = re.compile(r"^(auth\.json|\.env)(\..+)?$", re.I)
# Other stores of keys and tokens, relative to a home (from Hermes's credential list; caches,
# browser profiles and backups are left out: clearing those costs a sign-in, not a key).
KEY_STORES = frozenset({
    "auth", ".op.env", "npmrc", ".anthropic_oauth.json", "google_token.json", "google_client_secret.json",
    "google_chat_user_tokens", "google_chat_user_token.json", "google_chat_user_client_secret.json",
    "slack_tokens.json", "honcho.json", "mem0.json", "mcp-tokens", "vault", ".copilot_jwt.json",
    "webhook_subscriptions.json",
})
# Names worth protecting when a glob or a `find -name` pattern could hit them.
_PROTECTED_NAMES = tuple({t.rel for t in TARGETS} | {".env", "auth.json", "profiles", "rescue.db"})


@dataclass
class Verdict:
    action: str = ALLOW
    target: Optional[Target] = None
    operation: str = ""                       # "delete" | "overwrite" | "edit" | "database write" ...
    paths: list[Path] = field(default_factory=list)   # protected files touched (for snapshots)
    detail: str = ""                          # the command or path, shortened

    @property
    def blocked(self) -> bool:
        return self.action == BLOCK

    @property
    def liftable(self) -> bool:
        return self.target.liftable if self.target else True

    def agent_message(self) -> str:
        """What the model reads in place of the tool result when the call is blocked."""
        t = self.target
        name = t.name if t else "Hermes's own data"
        safe = t.safe if t else "`hermes doctor`"
        escape = ("If they really want this exact command, they can send /rescue allow 10 and ask again."
                  if self.liftable else
                  "Rescue never lets the agent do this; if the user really wants it, they do it themselves.")
        return (f"BLOCKED by the Rescue plugin: this would {self.operation} {name}. Hermes keeps that "
                f"for the user and hand-editing it is how installs get broken. Nothing was changed. "
                f"Use {safe} instead, or tell the user what you wanted to do and why. {escape} "
                f"Do not try another command that does the same thing.")

    def approval_reason(self, snapshot: str = "saved") -> str:
        """Shown to the human at the approval prompt. *snapshot*: "saved" | "none" | "partial"."""
        t = self.target
        name = t.name if t else "Hermes's own data"
        safe = f" Safer: {t.safe}." if t else ""
        saved = {"saved": " A snapshot is saved first (/rescue undo).",
                 "none": " Rescue can't snapshot what this touches, so /rescue undo won't cover it."}.get(
            snapshot, " Rescue could not save a full snapshot this time, so /rescue undo may not cover it.")
        return f"Rescue: this will {self.operation} {name}.{saved}{safe}"

    def user_line(self) -> str:
        t = self.target
        name = t.name if t else "Hermes's own data"
        return f"the agent tried to {self.operation} {name}"


# ---- where to look ------------------------------------------------------------------------------

def _norm(p: Path | str) -> Path:
    return Path(os.path.normpath(str(p)))


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Scope:
    homes: tuple[Path, ...]                 # the active Hermes home first, then the default root
    installs: tuple[Path, ...] = ()         # Hermes's program files (a source checkout, <home>/hermes-agent)
    user_home: Path = Path("~")             # what `~` means in the agent's shell

    @classmethod
    def make(cls, home: Path, install: Optional[Path] = None, *, root: Optional[Path] = None,
             user_home: Optional[Path] = None, cwd: Optional[Path] = None) -> "Scope":
        homes = [_norm(home)]
        if root is not None and _norm(root) not in homes:
            homes.append(_norm(root))
        installs: list[Path] = []
        for p in ([install] if install is not None else []) + [h / "hermes-agent" for h in homes]:
            p = _norm(p)
            # Working inside Hermes's own code (a contributor's checkout) is deliberate: not guarded.
            if p not in installs and not (cwd is not None and _within(_norm(cwd), p)):
                installs.append(p)
        uh = _norm(user_home) if user_home is not None else _norm(os.path.expanduser("~"))
        return cls(tuple(homes), tuple(installs), uh)


def _match_in_home(parts: tuple[str, ...], depth: int = 0) -> Optional[Target]:
    """Target for a path given as parts relative to one Hermes home (never empty)."""
    if any(_KEY_NAME.match(p) for p in parts) or parts[0] in KEY_STORES:
        return KEYS
    first = parts[0]
    if first in SAFE_SUBDIRS:
        return None
    if first == "profiles" and depth == 0:
        if len(parts) <= 2:      # the profiles folder, or one whole profile
            return WHOLE_PROFILE
        return _match_in_home(parts[2:], depth + 1)  # a profile is a home of its own
    if first == "plugin-data":
        if len(parts) == 1:
            return ALL_PLUGIN_DATA
        if parts[1] == PLUGIN_ID:
            return RESCUE_DATA
    rel = "/".join(parts)
    for target in TARGETS:
        if target.is_dir and first == target.rel:
            return target
        if not target.is_dir and rel == target.rel:
            return target
    return None


def match_target(path: Path, scope: Scope) -> Optional[Target]:
    """The protected target ``path`` falls in (or contains), most specific first."""
    path = _norm(path)
    for home in scope.homes:
        if _within(home, path):          # the home itself, or a folder that contains it
            return WHOLE_HOME
    for inst in scope.installs:
        if _within(path, inst) or _within(inst, path):
            rel = path.relative_to(inst).parts if _within(path, inst) else ()
            if any(p in SAFE_INSTALL_PARTS for p in rel) or any(p.endswith(".egg-info") for p in rel):
                return None
            return INSTALL_TARGET
    for home in scope.homes:             # most specific (the active profile) first
        if _within(path, home):
            return _match_in_home(path.relative_to(home).parts)
    return None


# ---- path resolution ---------------------------------------------------------------------------

_GLOB_CHARS = re.compile(r"[*?\[]")


_VAR = re.compile(r"\$\{(\w+)(?::?[-=]([^{}]*))?\}|\$(\w+)")


def _expand_vars(token: str, scope: Scope) -> str:
    def value(m: re.Match) -> str:
        name, default = m.group(1) or m.group(3), m.group(2)
        if name == "HERMES_HOME":
            return str(scope.homes[0])
        if name == "HOME":
            return str(scope.user_home)
        return _VAR.sub(value, default) if default is not None else m.group(0)   # `${X:-/fallback}`

    t = _VAR.sub(value, token)
    if t == "~" or t.startswith("~/"):
        t = str(scope.user_home) + t[1:]
    return t


def _resolve(raw: str, cwd: Optional[Path]) -> Optional[Path]:
    p = Path(raw)
    if not p.is_absolute():
        if cwd is None:      # unknown working directory: don't guess where it points
            return None
        p = cwd / p
    try:
        return _norm(p)
    except (OSError, ValueError):
        return None


def _candidates(token: str, scope: Scope, cwd: Optional[Path], *, literal: bool = False) -> list[Path]:
    """The absolute paths one shell word can touch (several for a glob or braces), or [] if it
    isn't a path. *literal*: a file tool's path, where ``*`` and ``{`` are just characters."""
    if not token or token.startswith("-") or "://" in token or _dynamic(token):
        return []
    if literal:
        p = _resolve(_expand_vars(token, scope), cwd)
        return [p] if p is not None else []
    found: list[Path] = []
    for word in _brace_expand(token):
        found += [p for p in _glob_candidates(word, scope, cwd) if p not in found]
    return found


def _glob_candidates(token: str, scope: Scope, cwd: Optional[Path]) -> list[Path]:
    t = _expand_vars(token, scope)
    m = _GLOB_CHARS.search(t)
    if not m:
        p = _resolve(t, cwd)
        return [p] if p is not None else []
    # A glob: the folder before the first wildcard component, then that component as a pattern.
    head, _, _ = t[:m.start()].rpartition("/")
    pattern = t[len(head) + 1 if head or t.startswith("/") else 0:].split("/", 1)[0]
    base = _resolve(head or ("/" if t.startswith("/") else "."), cwd)
    if base is None:
        return []
    found: list[Path] = []
    for root in scope.homes + scope.installs:
        if base == root:
            if root in scope.homes:  # `~/.hermes/*.log` hits only the names the pattern can match
                found += [root / n for n in _PROTECTED_NAMES if fnmatch.fnmatch(n, pattern)]
            else:
                found.append(root / _GLOB_CHARS.sub("x", pattern))
        elif _within(root, base):    # `~/.her*` may name the home itself
            nxt = root.relative_to(base).parts[0]
            if fnmatch.fnmatch(nxt, pattern):
                found.append(base / nxt)
        elif _within(base, root):    # `sessions/*`: inside a protected folder
            found.append(base)
    return found


# ---- shell parsing ------------------------------------------------------------------------------

_SEPARATORS = {";", "&&", "||", "|", "|&", "&", "\n", "(", ")", ";;", ";&"}
_REDIR_OUT = {">", ">>", ">|", "&>", "&>>", ">&"}
_REDIR_IN = {"<", "<<", "<<<", "<&", "<>"}
_WRAPPERS = {"sudo", "env", "command", "builtin", "nohup", "nice", "time", "exec", "doas", "xargs",
             "timeout", "stdbuf", "ionice", "chronic", "unbuffer"}
_DELETE_VERBS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "srm"}
_TRUNCATE_VERBS = {"truncate"}
_MOVE_VERBS = {"mv"}
_COPY_VERBS = {"cp", "install", "rsync", "ln"}
_INPLACE_VERBS = {"sed", "perl", "ruby", "yq"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "fish"}
_PYTHONS = re.compile(r"^python(3(\.\d+)?)?$|^pypy3?$")
_SQLITE_WRITE = re.compile(
    r"\b(delete\s+from|drop\s+(table|index|trigger|view)|update\s+(or\s+\w+\s+)?[\w.\"`\[\]]+\s+set|"
    r"insert\s+(or\s+\w+\s+)?into|"
    r"replace\s+into|alter\s+table|vacuum|pragma\s+\w+\s*=|attach\s+database)\b"
    r"|(^|\s|;)\.(restore|import|read|load|shell|system)\b", re.I)
_SQLITE_READ = re.compile(r"\bselect\b|(^|\s)\.(tables|schema|dump|indexes|dbinfo)\b", re.I)
_SQLITE_OPTS_WITH_VALUE = {"-cmd", "-separator", "-newline", "-nullvalue", "-init", "-vfs", "-maxsize",
                           "-mmap", "-pagecache", "-lookaside", "-heap"}
_FIND_NAME_TESTS = {"-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex", "-iregex"}
# Taking access away from others (what `hermes doctor` does to .env) keeps Hermes working.
_SAFE_MODE = re.compile(r"^0?[67]00$|^[go]+-[rwxXst]+(,[go]+-[rwxXst]+)*$|^u\+[rwxX]+$")
_MAX_DEPTH = 3


@dataclass
class _Segment:
    words: list[str] = field(default_factory=list)
    redirects: list[tuple[str, str]] = field(default_factory=list)
    stdin: str = ""                 # here-document / here-string text
    piped: bool = False             # stdin is the previous segment's output
    prev: Optional["_Segment"] = None

    @property
    def text(self) -> str:
        return " ".join(self.words)


_OPERATORS = ("&>>", "<<<", "<<-", "&&", "||", "|&", ";;", ";&", ">>", ">|", ">&", "<&", "<>", "&>", "<<",
              ";", "|", "&", ">", "<", "(", ")")
_OP_CHARS = set(";|&<>()")
_SPACE = " \t\r"


def _skip_quoted(text: str, i: int) -> int:
    """Index just past the quoted string that starts at text[i] (' or ")."""
    q, n = text[i], len(text)
    i += 1
    while i < n:
        c = text[i]
        if q == '"' and c == "\\":
            i += 2
            continue
        if c == q:
            return i + 1
        i += 1
    return n


def _close_paren(text: str, i: int, depth: int = 1) -> int:
    """Index just past the ``)`` that closes a ``$(`` / ``$((`` opened before *i*."""
    n = len(text)
    while i < n:
        c = text[i]
        if c in "'\"":
            i = _skip_quoted(text, i)
            continue
        if c == "\\":
            i += 2
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _close_brace(text: str, i: int) -> int:
    depth, n = 1, len(text)
    while i < n:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _lex(text: str) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Split a command line the way the shell does, in one pass (linear time).

    Returns (tokens as ("w", word) / ("op", operator) / ("hd", body index), here-document bodies,
    command substitutions). Quotes are removed from words but ``$(…)`` and backticks stay in the
    word text, so a word that depends on another command's output is recognisable. Comments are
    dropped and here-document bodies are data, not commands."""
    toks: list[tuple[str, str]] = []
    bodies: list[str] = []
    subs: list[str] = []
    pending: list[tuple[str, int]] = []
    word: list[str] = []
    in_word = False
    i, n = 0, len(text)

    def flush() -> None:
        nonlocal word, in_word
        if in_word:
            toks.append(("w", "".join(word)))
        word, in_word = [], False

    while i < n:
        c = text[i]
        if c in _SPACE:
            flush()
            i += 1
            continue
        if c == "\n":
            flush()
            toks.append(("op", "\n"))
            i += 1
            for delim, idx in pending:             # here-document bodies follow this line
                lines = []
                while i < n:
                    j = text.find("\n", i)
                    j = n if j < 0 else j
                    line, i = text[i:j], j + 1
                    if line.strip() == delim:
                        break
                    lines.append(line)
                bodies[idx] = "\n".join(lines)
            pending = []
            continue
        if c == "#" and not in_word:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2
                continue
            word.append(text[i + 1:i + 2])
            in_word = True
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            j = n if j < 0 else j
            word.append(text[i + 1:j])
            in_word = True
            i = j + 1
            continue
        if c == '"':
            j = _skip_quoted(text, i)
            inner = text[i + 1:j - 1 if j <= n and text[j - 1:j] == '"' else j]
            k = 0
            while k < len(inner):                   # substitutions still run inside double quotes
                if inner.startswith("$((", k):
                    k = _close_paren(inner, k + 3, 2)
                elif inner.startswith("$(", k):
                    e = _close_paren(inner, k + 2)
                    subs.append(inner[k + 2:e - 1])
                    k = e
                elif inner[k] == "`":
                    e = inner.find("`", k + 1)
                    e = len(inner) if e < 0 else e
                    subs.append(inner[k + 1:e])
                    k = e + 1
                elif inner[k] == "\\" and k + 1 < len(inner) and inner[k + 1] in '\\"$`\n':
                    k += 2
                else:
                    k += 1
            word.append(re.sub(r'\\([\\"$`])', r"\1", inner))
            in_word = True
            i = j
            continue
        if c == "`":
            j = text.find("`", i + 1)
            j = n if j < 0 else j
            subs.append(text[i + 1:j])
            word.append(text[i:j + 1])
            in_word = True
            i = j + 1
            continue
        if c == "$" and text.startswith("$((", i):
            j = _close_paren(text, i + 3, 2)
            word.append(text[i:j])
            in_word = True
            i = j
            continue
        if c == "$" and text.startswith("$(", i):
            j = _close_paren(text, i + 2)
            subs.append(text[i + 2:j - 1])
            word.append(text[i:j])
            in_word = True
            i = j
            continue
        if c == "$" and text.startswith("${", i):
            j = _close_brace(text, i + 2)
            word.append(text[i:j])
            in_word = True
            i = j
            continue
        if c in _OP_CHARS:
            if c in "<>" and in_word and "".join(word).isdigit():   # `2>`: a stream number, not a word
                word, in_word = [], False
            flush()
            op = next(o for o in _OPERATORS if text.startswith(o, i))
            i += len(op)
            if op in ("<<", "<<-"):
                while i < n and text[i] in " \t":
                    i += 1
                j = i
                while j < n and text[j] not in _SPACE + "\n" and text[j] not in _OP_CHARS:
                    j = _skip_quoted(text, j) if text[j] in "'\"" else j + 1
                delim = re.sub(r"[\\'\"]", "", text[i:j])
                bodies.append("")
                pending.append((delim, len(bodies) - 1))
                toks.append(("op", "<<"))
                toks.append(("hd", str(len(bodies) - 1)))
                i = j
                continue
            toks.append(("op", op))
            continue
        word.append(c)
        in_word = True
        i += 1
    flush()
    return toks, bodies, subs


_SEPARATOR_OPS = {";", "&&", "||", "|", "|&", "&", "\n", "(", ")", ";;", ";&"}


def _segments(command: str) -> tuple[list[_Segment], list[str]]:
    toks, bodies, subs = _lex(command)
    segs: list[_Segment] = []
    cur = _Segment()
    i = 0
    while i < len(toks):
        kind, tok = toks[i]
        if kind == "op" and tok in _SEPARATOR_OPS:
            segs.append(cur)
            cur = _Segment(piped=tok in ("|", "|&"), prev=cur)
            i += 1
            continue
        if kind == "op":
            nkind, target = toks[i + 1] if i + 1 < len(toks) else ("w", "")
            if tok == "<<" and nkind == "hd":
                cur.stdin += bodies[int(target)] + "\n"
            elif tok == "<<<":
                cur.stdin += target + "\n"
            else:
                cur.redirects.append((tok, target))
            i += 2
            continue
        cur.words.append(tok)
        i += 1
    segs.append(cur)
    return [s for s in segs if s.words or s.redirects], subs


def _brace_expand(word: str, limit: int = 64) -> list[str]:
    """``a/{b,c}`` → ``a/b``, ``a/c`` (what the shell does before running the command)."""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    out: list[str] = []
    for part in m.group(1).split(","):
        out += _brace_expand(word[:m.start()] + part + word[m.end():], limit)
        if len(out) >= limit:
            break
    return out[:limit]


def _dynamic(word: str) -> bool:
    return "$(" in word or "`" in word


def _sub_commands(word: str) -> list[str]:
    """The commands inside ``$(…)`` / backticks in one word."""
    _toks, _bodies, subs = _lex(word.replace('"', ""))
    return subs


# Options that take a value, per wrapper (so `xargs -I {} rm {}` still finds `rm`).
_WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U"}, "doas": {"-u", "-C"},
    "xargs": {"-I", "-L", "-P", "-d", "-a", "-E", "-n", "-s"}, "env": {"-u", "-C", "-S"},
    "nice": {"-n"}, "timeout": {"-s", "-k"}, "ionice": {"-c", "-n", "-p"}, "stdbuf": set(),
}


def _strip_wrappers(words: list[str]) -> tuple[set[str], list[str], Optional[str]]:
    """(wrappers seen, the command's own words, a folder the wrapper switches to: `env -C dir`)."""
    lead: set[str] = set()
    chdir: Optional[str] = None
    while words:
        w = words[0]
        if w in _WRAPPERS:
            lead.add(w)
            words = words[1:]
            takes_value = _WRAPPER_VALUE_OPTS.get(w, set())
            # skip the wrapper's own options: `timeout 30 rm …`, `nice -n 5 rm …`, `sudo -u bob rm …`
            while words and (words[0].startswith("-") or (w in ("timeout", "nice") and words[0][:1].isdigit())):
                if w in ("env", "sudo") and words[0] in ("-C", "--chdir", "-D") and len(words) > 1:
                    chdir = words[1]
                elif w == "env" and words[0].startswith("--chdir="):
                    chdir = words[0].split("=", 1)[1]
                words = words[2:] if words[0] in takes_value else words[1:]
            continue
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w) or w in _KEYWORDS:
            words = words[1:]
            continue
        break
    return lead, words, chdir


_KEYWORDS = {"{", "}", "!", "if", "then", "else", "elif", "fi", "do", "done", "while", "until", "coproc"}


def _positional(words: list[str], value_opts: Iterable[str] = ()) -> list[str]:
    """The command's arguments that aren't options (nor the values of *value_opts*)."""
    out, end_of_opts, skip = [], False, False
    for w in words[1:]:
        if skip:
            skip = False
            continue
        if w == "--" and not end_of_opts:
            end_of_opts = True
            continue
        if end_of_opts or not w.startswith("-"):
            out.append(w)
        elif w in value_opts:
            skip = True
    return out


# Options of copy-like commands that take a separate value (`install -m 644 a b`).
_COPY_VALUE_OPTS = {
    "install": {"-m", "-o", "-g", "-S", "--mode", "--owner", "--group", "--suffix"},
    "cp": {"-S", "--suffix"}, "mv": {"-S", "--suffix"}, "ln": {"-S", "--suffix"},
    "rsync": {"-e", "--rsh", "--exclude", "--include", "--filter", "-f", "--chmod", "--chown", "--files-from",
              "--log-file", "--password-file", "-T", "--temp-dir", "--backup-dir", "--exclude-from",
              "--include-from", "--rsync-path", "-B", "--block-size", "--max-size", "--min-size"},
}


def _worst(verdicts: Iterable[Verdict]) -> Verdict:
    """The strictest verdict; its ``paths`` list every protected path the command touches, so a
    snapshot taken for it covers the whole command."""
    rank = {ALLOW: 0, APPROVE: 1, BLOCK: 2}
    hits = [v for v in verdicts if v.action != ALLOW]
    if not hits:
        return Verdict()
    best = max(hits, key=lambda v: (rank[v.action], v.action == BLOCK and not v.liftable))
    result = Verdict(best.action, best.target, best.operation, [], best.detail)
    seen: set[Path] = set()
    for v in hits:
        for p in v.paths:
            if p not in seen:
                seen.add(p)
                result.paths.append(p)
    return result


class _Checker:
    def __init__(self, scope: Scope, detail: str):
        self.scope, self.detail = scope, detail[:200]

    def hit_path(self, path: Optional[Path], operation: str, kind: str) -> Verdict:
        if path is None:
            return Verdict()
        target = match_target(path, self.scope)
        if target is None:
            return Verdict()
        action = target.delete if kind == "delete" else target.edit
        if action == ALLOW:
            return Verdict()
        return Verdict(action, target, operation, [path], self.detail)

    def hit(self, token: str, operation: str, kind: str, cwd: Optional[Path]) -> list[Verdict]:
        return [self.hit_path(p, operation, kind) for p in _candidates(token, self.scope, cwd)]

    # -- one command line ------------------------------------------------------------------------
    def shell(self, command: str, cwd: Optional[Path], depth: int = 0) -> list[Verdict]:
        if not command or not command.strip():
            return []
        if depth > _MAX_DEPTH:   # nested too deep to read: ask if it mentions Hermes at all
            mentions = ".hermes" in command or any(str(h) in command for h in self.scope.homes)
            return [Verdict(APPROVE, _NESTED_TARGET, "run", [], self.detail)] if mentions else []
        out: list[Verdict] = []
        segs, subs = _segments(command)
        for sub in subs:                           # `$(…)` and backticks run as commands of their own
            out += self.shell(sub, cwd, depth + 1)
        prev_cwd = cwd
        for seg in segs:
            lead, words, chdir = _strip_wrappers(seg.words)
            here = cwd
            if chdir is not None:
                found = _candidates(chdir, self.scope, cwd)
                here = found[0] if found else None
            for op, raw in seg.redirects:
                if op in _REDIR_OUT and not raw.startswith("/dev/") and not (op == ">&" and raw.isdigit()) \
                        and raw != "-":
                    # Writing a file is an edit: settings and SOUL.md ask the human, the session DB is refused.
                    out += self.hit(raw, "append to" if op.endswith(">>") else "overwrite", "edit", here)
            if not words:
                continue
            verb = os.path.basename(words[0])
            if verb in ("cd", "pushd"):
                args = _positional(words)
                new: Optional[Path]
                if words[1:2] == ["-"]:
                    new = prev_cwd
                elif not args:
                    new = self.scope.user_home
                else:
                    found = _candidates(args[0], self.scope, cwd)
                    new = found[0] if found else None
                prev_cwd, cwd = cwd, new
                continue
            out += self.segment(verb, words, lead, seg, here, depth)
        return out

    def segment(self, verb: str, words: list[str], lead: set[str], seg: _Segment, cwd: Optional[Path],
                depth: int) -> list[Verdict]:
        args = _positional(words)
        if verb == "hermes":
            return []
        if verb in _SHELLS:
            script = _flag_value(words, "c")
            if script is not None:
                return self.shell(script, cwd, depth + 1)
            if not args or args == ["-s"] or "-s" in words[1:]:
                if seg.stdin:
                    return self.shell(seg.stdin, cwd, depth + 1)
                prev = seg.prev.words if seg.piped and seg.prev is not None else []
                if prev and os.path.basename(prev[0]) in ("echo", "printf"):   # a script printed into a shell
                    return self.shell(" ".join(prev[1:]), cwd, depth + 1)
            return []
        if verb == "eval":
            return self.shell(" ".join(words[1:]), cwd, depth + 1)
        if _PYTHONS.match(verb):
            code = _flag_value(words, "c")
            if code is not None:
                return [check_python(code, self.scope)]
            if "-m" in words[1:] and "pip" in words[1:]:
                return self._pip(words)
            if seg.stdin and (not args or args == ["-"]):
                return [check_python(seg.stdin, self.scope)]
            return []
        if verb in ("pip", "pip3", "pipx") or (verb == "uv" and words[1:2] in (["pip"], ["tool"])):
            return self._pip(words)
        if "xargs" in lead and verb in _DELETE_VERBS and seg.piped and seg.prev is not None:
            return [v for a in _producer_paths(seg.prev.words) for v in self.hit(a, "delete files in", "delete", cwd)]
        if verb in _DELETE_VERBS or verb in _TRUNCATE_VERBS:
            op = "delete" if verb in _DELETE_VERBS else "empty"
            res: list[Verdict] = []
            for a in args:
                if _dynamic(a):   # `rm $(find ~/.hermes/sessions …)`: what the inner command lists
                    for sub in _sub_commands(a):
                        for inner in _segments(sub)[0][:1]:
                            res += [v for p in _producer_paths(inner.words)
                                    for v in self.hit(p, "delete files in", "delete", cwd)]
                else:
                    res += self.hit(a, op, "delete", cwd)
            return res
        if verb in _MOVE_VERBS | _COPY_VERBS:
            return self._copy(verb, words, args, cwd)
        if verb == "dd":
            return [v for w in words[1:] if w.startswith("of=") for v in self.hit(w[3:], "overwrite", "edit", cwd)]
        if verb == "find":
            return self._find(words, cwd)
        if verb == "tar" and (re.match(r"^-?[a-zA-Z]*x", words[1] if len(words) > 1 else "") or "--extract" in words
                              or "--get" in words):
            dest = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-C", "--directory")), None)
            dest = dest or next((w.split("=", 1)[1] for w in words if w.startswith("--directory=")), None)
            res = []
            for path in (_candidates(dest, self.scope, cwd) if dest else ([cwd] if cwd else [])):
                if path in self.scope.homes:          # unpacking over the whole home
                    res.append(Verdict(APPROVE, WHOLE_HOME, "unpack files over", [], self.detail))
                elif any(_within(path, h) for h in self.scope.homes):
                    res.append(self.hit_path(path, "unpack files into", "edit"))
            return res
        if verb in _INPLACE_VERBS and _in_place(verb, words):
            return [v for a in args for v in self.hit(a, "rewrite", "edit", cwd)]
        if verb == "tee":
            return [v for a in args for v in self.hit(a, "overwrite", "edit", cwd)]
        if verb in ("sqlite3", "sqlite"):
            return self._sqlite(words, seg, cwd)
        if verb == "git":
            return self._git(words, cwd)
        if verb in ("chmod", "chown") and len(args) >= 2:
            if verb == "chmod" and _SAFE_MODE.match(args[0]):
                return []
            op = "change permissions on" if verb == "chmod" else "change the owner of"
            recursive = any(w in ("-R", "--recursive") or re.fullmatch(r"-[a-zA-Z]*R[a-zA-Z]*", w) for w in words[1:])
            res: list[Verdict] = []
            for a in args[1:]:
                for path in _candidates(a, self.scope, cwd):
                    if recursive and any(_within(h, path) for h in self.scope.homes):
                        # every file Hermes keeps at once: ask, it can lock Hermes out or expose the keys
                        res.append(Verdict(APPROVE, WHOLE_HOME, op, [], self.detail))
                    else:
                        res.append(self.hit_path(path, op, "edit"))
            return res
        return []

    def _copy(self, verb: str, words: list[str], args: list[str], cwd: Optional[Path]) -> list[Verdict]:
        args = _positional(words, _COPY_VALUE_OPTS.get(verb, set()))
        target_dir = next((words[i + 1] for i, w in enumerate(words[:-1]) if w in ("-t", "--target-directory")), None)
        target_dir = target_dir or next((w.split("=", 1)[1] for w in words if w.startswith("--target-directory=")), None)
        if target_dir is not None:
            sources, dest_raw, into = [a for a in args if a != target_dir], target_dir, True
        elif len(args) >= 2:
            sources, dest_raw, into = args[:-1], args[-1], args[-1].endswith("/") or len(args) > 2
        else:
            return []
        if verb == "ln" and not any(w.startswith("-") and "f" in w for w in words[1:]):
            return []          # without -f, ln never replaces an existing file
        res: list[Verdict] = []
        if verb in _MOVE_VERBS:
            res += [v for a in sources for v in self.hit(a, "move away", "delete", cwd)]
        mirror = verb == "rsync" and any(w.startswith("--delete") for w in words)
        for dest in _candidates(dest_raw, self.scope, cwd):
            if mirror:          # rsync --delete removes what the source doesn't have
                res.append(self.hit_path(dest, "delete files in", "delete"))
            folder = into or match_target(dest, self.scope) in (WHOLE_HOME, WHOLE_PROFILE) or any(
                dest == h / t.rel for h in self.scope.homes for t in TARGETS if t.is_dir)
            if folder and verb != "rsync":
                # `cp config.yaml ~/.hermes/profiles/work/` writes work/config.yaml, not the whole profile
                res += [self.hit_path(dest / os.path.basename(src.rstrip("/")), "overwrite", "edit")
                        for src in sources if src and not _dynamic(src)]
            else:
                res.append(self.hit_path(dest, "overwrite", "edit"))
        return res

    def _pip(self, words: list[str]) -> list[Verdict]:
        if "uninstall" in words and any(re.fullmatch(r"hermes[-_]agent(\[.*\])?", w, re.I) for w in words):
            return [Verdict(BLOCK, INSTALL_TARGET, "uninstall", [], self.detail)]
        return []

    def _find(self, words: list[str], cwd: Optional[Path]) -> list[Verdict]:
        deletes = "-delete" in words or any(
            w in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(words)
            and os.path.basename(words[i + 1]) in _DELETE_VERBS for i, w in enumerate(words))
        if not deletes:
            return []
        tests = [(w, words[i + 1]) for i, w in enumerate(words[:-1]) if w in _FIND_NAME_TESTS]
        negated = any(w in _FIND_NAME_TESTS and i >= 1 and words[i - 1] in ("!", "-not") for i, w in enumerate(words))
        out: list[Verdict] = []
        for start in _find_starts(words):
            for path in _candidates(start, self.scope, cwd):
                broad = any(_within(h, path) for h in self.scope.homes)  # the home, or a folder holding it
                if not broad or not tests or negated:
                    out.append(self.hit_path(path, "delete files in", "delete"))
                    continue
                # `find ~/.hermes -name '*.log' -delete` only touches what the patterns can name:
                # check them against the kinds of files the protected folders hold.
                for test, pat in tests:
                    out += [self.hit_path(h / rel, "delete files in", "delete")
                            for h in self.scope.homes for rel, names in _TYPICAL_FILES
                            if _find_pattern_hits(test, pat, h / rel, names)]
        return out

    def _sqlite(self, words: list[str], seg: _Segment, cwd: Optional[Path]) -> list[Verdict]:
        rest, sql_parts, db = words[1:], [], None
        i = 0
        while i < len(rest):
            w = rest[i]
            if w in _SQLITE_OPTS_WITH_VALUE:
                if w == "-cmd" and i + 1 < len(rest):
                    sql_parts.append(rest[i + 1])
                i += 2
                continue
            if w.startswith("-"):
                i += 1
                continue
            if db is None:
                db = w
            else:
                sql_parts.append(w)
            i += 1
        sql = " ".join(sql_parts) + "\n" + seg.stdin
        prev = seg.prev if seg.piped and seg.prev is not None else None
        literal_input = prev is not None and prev.words and os.path.basename(prev.words[0]) in ("echo", "printf")
        if literal_input:
            sql += "\n" + " ".join(prev.words[1:])
        res: list[Verdict] = []
        for m in re.finditer(r"(?:^|[\s;])\.(?:shell|system)\s+([^\n;]*)", sql):   # SQLite runs these in a shell
            res += self.shell(m.group(1), cwd, _MAX_DEPTH)
        if db is None or "-readonly" in words or "--readonly" in words:
            return res
        unknown_input = any(op == "<" for op, _ in seg.redirects) or (prev is not None and not literal_input
                                                                      and not prev.stdin)
        if prev is not None and prev.stdin:
            sql += "\n" + prev.stdin
        if _SQLITE_WRITE.search(sql) or unknown_input:
            res += self.hit(db, "run raw database writes on", "delete", cwd)
        return res

    def _git(self, words: list[str], cwd: Optional[Path]) -> list[Verdict]:
        i, workdir = 1, cwd
        while i < len(words) and words[i].startswith("-"):
            if words[i] == "-C" and i + 1 < len(words):
                found = _candidates(words[i + 1], self.scope, workdir)
                workdir = found[0] if found else None
                i += 2
            elif words[i] in ("-c", "--git-dir", "--work-tree", "--namespace"):
                i += 2
            else:
                i += 1
        if i >= len(words) or workdir is None:
            return []
        sub, rest = words[i], words[i + 1:]
        if sub == "clean":
            if any(w in ("-n", "--dry-run") or (w.startswith("-") and not w.startswith("--") and "n" in w)
                   for w in rest):
                return []
            paths = [w for w in rest if not w.startswith("-")]
            targets = paths or ["."]
            return [v for a in targets for v in self._inside_only(a, workdir, "delete untracked files in")]
        if sub == "rm":
            if "--cached" in rest:
                return []
            paths = [w for w in rest if not w.startswith("-")]
            return [v for a in paths for v in self._inside_only(a, workdir, "delete")]
        return []

    def _inside_only(self, token: str, workdir: Path, operation: str) -> list[Verdict]:
        """git acts inside its work tree: only matters when that tree is Hermes's own folder."""
        out = []
        for path in _candidates(token, self.scope, workdir):
            inside = any(_within(path, h) for h in self.scope.homes + self.scope.installs)
            if inside:
                out.append(self.hit_path(path, operation, "delete"))
        return out


def _in_place(verb: str, words: list[str]) -> bool:
    for w in words[1:]:
        if w.startswith(("--in-place", "--inplace")):
            return True
        if verb == "sed" and re.fullmatch(r"-[nErsuz]*i.*", w):
            return True
        if verb != "sed" and re.fullmatch(r"-[0-9a-zA-HJ-LN-Z]*i.*", w):   # perl -pi, -i.bak (not -I/-M)
            return True
    return False


def _find_pattern_hits(test: str, pattern: str, folder: Path, names: tuple[str, ...]) -> bool:
    """Could ``find … TEST PATTERN`` match files that *folder* typically holds?"""
    if ".hermes" in pattern:
        return True
    if test in ("-regex", "-iregex"):
        try:
            rx = re.compile(pattern, re.I if test == "-iregex" else 0)
        except re.error:
            return True
        return any(rx.fullmatch(str(folder / n)) or rx.fullmatch(str(folder.parent / n)) for n in names)
    base = os.path.basename(pattern.rstrip("/")) or pattern
    if test in ("-path", "-ipath", "-wholename", "-iwholename"):
        return any(fnmatch.fnmatch(str(folder / n), pattern) for n in names) or folder.name in pattern
    return any(fnmatch.fnmatch(n.lower() if test == "-iname" else n, base.lower() if test == "-iname" else base)
               for n in names)


def _flag_value(words: list[str], flag: str) -> Optional[str]:
    """The argument of ``-c`` for ``bash -c``, ``bash -lc``, ``sh -ec``, ``python -Ic`` …"""
    takes_arg = False
    for w in words[1:]:
        if takes_arg:
            return w
        if w.startswith("-") and not w.startswith("--") and flag in w[1:]:
            takes_arg = True
        elif w in ("-o", "-O", "+o", "+O", "--rcfile", "--init-file", "-W", "-X"):
            continue
        elif not w.startswith(("-", "+")):
            return None
    return "" if takes_arg else None


def _producer_paths(words: list[str]) -> list[str]:
    """Where the paths come from in `find X … | xargs rm` / `rm $(ls X)`."""
    if not words:
        return []
    if os.path.basename(words[0]) == "find":
        return _find_starts(words)
    return _positional(words)


def _find_starts(words: list[str]) -> list[str]:
    starts = []
    for w in words[1:]:
        if w.startswith(("-", "(", "!", ")")) or w in ("(", ")", "!"):
            break
        starts.append(w)
    return starts or ["."]


def _scope(home, install, cwd, root, user_home) -> Scope:
    if isinstance(home, Scope):
        return home
    return Scope.make(Path(home), install, root=root, user_home=user_home, cwd=cwd)


def check_shell(command: str, home: Path | Scope, install: Optional[Path] = None, cwd: Optional[Path] = None,
                *, root: Optional[Path] = None, user_home: Optional[Path] = None) -> Verdict:
    """Verdict for a terminal command (every part of a compound command is checked)."""
    scope = _scope(home, install, cwd, root, user_home)
    return _worst(_Checker(scope, command or "").shell(command or "", _norm(cwd) if cwd else None))


# ---- execute_code -------------------------------------------------------------------------------

_PY_DESTRUCTIVE = re.compile(
    r"\b(os\.(remove|unlink|rmdir|removedirs|rename|renames|replace|truncate)|"
    r"shutil\.(rmtree|move))\s*\("
    r"|\.(unlink|rmdir|write_text|write_bytes|rename|replace|truncate)\s*\("
    r"|\.open\s*\(\s*['\"][wax]"
    r"|\bopen\s*\([^\n]*?,\s*(mode\s*=\s*)?['\"][wax]b?\+?t?['\"]", re.I)
_PY_HERMES_HINT = re.compile(r"\.hermes\b|HERMES_HOME|get_hermes_home|hermes_constants")


def check_python(code: str, home: Path | Scope, install: Optional[Path] = None, *,
                 root: Optional[Path] = None) -> Verdict:
    """Coarse check for ``execute_code``: Python that deletes or rewrites Hermes's own data.

    Python can build paths at runtime, so this cannot be exact; it asks the human (never blocks)
    when a destructive call and a path into a Hermes home appear in the same code.
    """
    scope = _scope(home, install, None, root, None)
    if not code:
        return Verdict()
    anchored = _PY_HERMES_HINT.search(code) or any(str(h) in code for h in scope.homes)
    if not anchored:
        return Verdict()
    if not (_PY_DESTRUCTIVE.search(code) or ("sqlite3" in code and _SQLITE_WRITE.search(code))):
        return Verdict()
    target = next((t for t in TARGETS if t.rel and re.search(rf"(?<![\w.]){re.escape(t.rel)}\b", code)),
                  _SCRIPT_TARGET)
    label = Target(target.rel, target.name, APPROVE, APPROVE, target.safe, target.is_dir)
    return Verdict(APPROVE, label, "change", [], code[:200])


_SCRIPT_TARGET = Target("", "Hermes's own data (from a Python script)", APPROVE, APPROVE,
                        "the matching `hermes` command, or doing it only if the user asked")
_NESTED_TARGET = Target("", "Hermes's own data (a command nested too deep to read)", APPROVE, APPROVE,
                        "running the inner command directly")
# What the protected folders typically hold, for judging `find … -name PATTERN -delete`.
_TYPICAL_FILES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("state.db", ("state.db", "state.db-wal", "state.db-shm", "x.db")),
    ("sessions", ("x.json", "x.jsonl", "session_x.json")),
    ("memories", ("MEMORY.md", "USER.md", "x.md", "x.json")),
    ("skills", ("SKILL.md", "x.md", "x.py", "x.sh", "x.js", "x.json", "x.yaml", "x.txt")),
    ("config.yaml", ("config.yaml", "x.yaml", "x.yml")),
    ("SOUL.md", ("SOUL.md",)),
    (".env", (".env", "auth.json", "x.json", "x.env")),
    ("cron", ("jobs.json", "x.json")),
    ("checkpoints", ("x.json", "x.tar", "x.patch")),
    ("profiles", ("x.yaml", "x.db", "x.md", "x.json")),
)


# ---- tool calls ---------------------------------------------------------------------------------

_WRITE_TOOLS = {"write_file", "patch", "edit_file", "str_replace_editor", "create_file"}
_SHELL_TOOLS = {"terminal", "shell", "bash", "run_command"}
_CODE_TOOLS = {"execute_code", "python", "code_interpreter"}


def check_tool_call(tool_name: str, args: dict, home: Path, install: Optional[Path], cwd: Optional[Path], *,
                    root: Optional[Path] = None, user_home: Optional[Path] = None) -> Verdict:
    if not isinstance(args, dict):
        return Verdict()
    cwd = _norm(cwd) if cwd else None
    if tool_name in _SHELL_TOOLS:
        workdir = args.get("workdir")
        if isinstance(workdir, str) and workdir.strip():
            scope0 = Scope.make(Path(home), install, root=root, user_home=user_home)
            found = _candidates(workdir.strip(), scope0, cwd)
            cwd = found[0] if found else None
        scope = Scope.make(Path(home), install, root=root, user_home=user_home, cwd=cwd)
        return check_shell(str(args.get("command") or ""), scope, cwd=cwd)
    scope = Scope.make(Path(home), install, root=root, user_home=user_home, cwd=cwd)
    if tool_name in _CODE_TOOLS:
        return check_python(str(args.get("code") or ""), scope)
    if tool_name in _WRITE_TOOLS:
        raw = args.get("path") or args.get("file_path") or args.get("target_file")
        if isinstance(raw, str) and raw:
            paths = _candidates(raw, scope, cwd, literal=True)
            checker = _Checker(scope, raw)
            return _worst(checker.hit_path(p, "edit", "edit") for p in paths)
    return Verdict()
