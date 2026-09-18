"""Deterministic classification of a child's proposed tool call.

This is the load-bearing half of supervised execution. The supervisor model
only ever sees what this module cannot decide, which keeps the dangerous cases
independent of any model's judgement and keeps latency and cost off the common
path.

Classification runs on **parsed argv**, never on raw command text. A substring
matcher produces false positives on quoted strings, comments and documentation,
and false negatives on anything obfuscated.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

ALLOW = "allow"
DENY = "deny"
ESCALATE = "escalate"

# Read-only commands. Safe to run anywhere: they inspect, they do not mutate.
READ_ONLY = frozenset({
    "ls", "cat", "head", "tail", "wc", "file", "stat", "pwd", "echo", "true",
    "grep", "egrep", "fgrep", "rg", "ag", "find", "which", "type", "basename",
    "dirname", "realpath", "readlink", "date", "env", "printenv", "uname",
    "sort", "uniq", "cut", "tr", "diff", "cmp", "md5", "shasum", "sha256sum",
    "tree", "du", "df", "ps", "id", "whoami", "man", "help", "jq", "column",
    "test", "[",
})

# Options that turn a read-only verb into one that writes a file or runs a
# program. `find . -delete` and `env rm …` used to pass as read-only.
FIND_ACTIONS = frozenset({
    "-exec", "-execdir", "-ok", "-okdir", "-delete",
    "-fprint", "-fprint0", "-fprintf", "-fls",
})

# Test and build runners: mutating in principle, routine in practice, and the
# entire point of delegating coding work.
TEST_RUNNERS = frozenset({"pytest", "tox", "nose2", "unittest", "make", "cargo", "go"})
# Runners reached through a package manager: `uv run pytest`, `npx tsc`.
JS_RUNNERS = frozenset({"vitest", "tsc"})
GRADLE_TASKS = frozenset({"test", "check", "build"})

# Variables a `NAME=value` prefix or `export` may set without escalating. An
# allowlist: almost every tool reads some variable that changes which program
# runs or where it writes (PYTHONPATH, PYTEST_ADDOPTS, RUSTC_WRAPPER, GOFLAGS,
# MAKEFLAGS, npm_config_*, XDG_CONFIG_HOME, …), and a denylist of them was
# always one name short. Single-letter names are placeholders no tool reads.
SAFE_ENV = frozenset({
    "CI", "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CLICOLOR_FORCE", "TERM",
    "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "TZ", "COLUMNS", "LINES",
    "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONHASHSEED",
    "PYTHONIOENCODING", "PYTHONUTF8", "RUST_BACKTRACE", "RUST_LOG", "NODE_ENV",
    "DEBUG", "VERBOSE",
})
ASSIGNMENT = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# git subcommands that only read. `branch`, `remote`, `config` and `stash` are
# here for their listing forms only; `_git_exposure` escalates the rest.
GIT_READ_ONLY = frozenset({
    "status", "diff", "log", "show", "branch", "remote", "rev-parse", "describe",
    "blame", "shortlog", "ls-files", "config", "stash",
})
# Options git takes before the subcommand that cannot move where it reads
# config or the repository from.
GIT_GLOBAL_OK = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs"})
# Subcommand options that write a file or run a program.
GIT_EXEC_OPTIONS = frozenset({"--output", "--ext-diff", "--contents"})
GIT_CONFIG_READ = frozenset({
    "--list", "-l", "--get", "--get-all", "--get-regexp", "--get-urlmatch",
    "--show-origin", "--show-scope", "--local", "--name-only", "-z", "--null",
    "--includes", "--no-includes",
})
GIT_BRANCH_READ = frozenset({
    "-a", "--all", "-r", "--remotes", "-l", "--list", "-v", "-vv", "--verbose",
    "--show-current", "--contains", "--no-contains", "--merged", "--no-merged",
    "--points-at", "--no-color", "--color",
})

# Paths a delegated coding agent has no business touching, relative to $HOME.
SENSITIVE_HOME = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker/config.json", ".netrc",
    ".config/gh", ".claude.json", ".claude", ".omlx", ".glm-subagent",
    "Library/Keychains",
    # Shell and REPL history: commands typed with tokens in them.
    ".zsh_history", ".bash_history", ".history", ".python_history",
    ".node_repl_history", ".psql_history", ".mysql_history", ".sqlite_history",
    ".lesshst", ".zsh_sessions", ".bash_sessions",
)
# Basenames that carry secrets wherever they appear, including in a workspace.
SENSITIVE_NAMES = frozenset({
    ".env", ".env.local", ".env.production", ".netrc", ".npmrc", ".pypirc",
    "id_rsa", "id_ed25519", "credentials", ".credentials.yaml", ".credentials.json",
})
# The child's own API key lives in its environment. Printing it hands the key
# to whatever the child writes next.
SECRET_ENV = re.compile(
    r"\$\{?(ANTHROPIC_AUTH_TOKEN|ANTHROPIC_API_KEY|GLM_API_KEY|ZAI_API_KEY"
    r"|CLAUDE_CODE_OAUTH_TOKEN)\b"
)

# Directories this server writes its own per-agent settings, hook config and
# session transcripts into. A child that can write there can rewrite its own
# guard, so they are refused as write targets wherever they sit. The server
# adds its session root at startup (`protect`).
PROTECTED_ROOTS: set[Path] = set()

# Commands that are never routine for a delegated coding agent.
NEVER = frozenset({
    "sudo", "doas", "su", "shutdown", "reboot", "halt", "mkfs", "fdisk",
    "diskutil", "launchctl", "systemctl", "crontab", "at", "kextload",
    "csrutil", "spctl", "defaults", "scutil", "networksetup", "dscl",
})

# Interpreters. Running the workspace's own code is the job; running inline
# source or a script from outside the workspace is not.
INTERPRETERS = frozenset({
    "python", "python3", "node", "ruby", "perl", "deno", "bun", "tsx", "bash",
})
INLINE_CODE_FLAGS = frozenset({"-c", "-e", "--eval", "--eval-file", "-p", "--print"})
# Per interpreter: short-option letters that mean "run this inline source", and
# the no-value letters allowed before the script. Anything else before the
# script -- an option that takes a value (`-X`, `-I`, `--import=`), a module
# loader (`-M`, `-r`) -- escalates rather than being mistaken for the script.
INLINE_LETTERS = {
    "python": "c", "python3": "c", "node": "ep", "ruby": "e", "perl": "eE",
    "bash": "c", "deno": "", "bun": "e", "tsx": "e",
}
SAFE_LETTERS = {
    "python": "BbdEIOqsSuRPv", "python3": "BbdEIOqsSuRPv", "node": "",
    "ruby": "wv", "perl": "wWT", "bash": "euxvn", "deno": "", "bun": "", "tsx": "",
}
SAFE_LONG_OPTIONS = frozenset({
    "--no-warnings", "--trace-warnings", "--enable-source-maps", "--no-deprecation",
    "--trace-uncaught",
})
# Modules `python -m` may run: the test runners.
TEST_MODULES = frozenset({"pytest", "unittest", "nose2", "tox"})
# Redirects that feed a program its input: for an interpreter, its program.
STDIN_REDIRECTS = frozenset({"<", "<<", "<<<", "<&", "<>"})

# Per runner: options that load code or config from elsewhere, or move where
# the run happens. Path-valued options are also caught by the workspace check;
# these escalate even with a workspace-looking value.
RUNNER_OPTIONS = {
    "pytest": {"-p", "-c", "-o", "--override-ini", "--pyargs", "--confcutdir"},
    "tox": {"-c", "--conf", "-x", "--override"},
    "cargo": {"--config", "-Z"},
    "go": {"-exec", "-toolexec", "-modfile", "-overlay"},
    "make": {"-f", "--file", "--makefile", "-C", "--directory", "-I", "--include-dir",
             "-e", "--environment-overrides", "-E", "--eval"},
    "npm": {"--script-shell", "--prefix", "--userconfig", "--globalconfig",
            "--node-options", "-C"},
    "pnpm": {"--script-shell", "--dir", "-C", "--node-options", "--config"},
    "gradlew": {"-I", "--init-script", "-c", "--settings-file", "-p", "--project-dir",
                "-b", "--build-file", "-D", "-P", "-g", "--gradle-user-home",
                "--include-build"},
    "vitest": {"-c", "--config", "--root", "--dir"},
    "tsc": set(),
    "nose2": {"-c", "--config", "-s", "--start-dir", "-t", "--top-level-directory"},
    "unittest": {"-s", "--start-directory", "-t", "--top-level-directory"},
}
# Subcommands a runner may be given; anything else (`install`, `run`,
# `generate`, `publish`, `env -w`) escalates.
RUNNER_SUBCOMMANDS = {
    "cargo": frozenset({"test", "check", "build", "clippy", "bench"}),
    "go": frozenset({"test", "build", "vet"}),
}
MAKE_TARGETS_REFUSED = frozenset({
    "install", "uninstall", "publish", "release", "deploy", "upload", "dist",
})

# Commands whose whole purpose is to put bytes somewhere. For these the
# destination is the decision, so a destination outside the workspace -- or one
# that cannot be resolved at all -- is refused outright rather than escalated.
# Measured: given `cp note.txt $TMPDIR/copy.txt` and facts correctly reporting
# the target as unresolved, a model reviewer reasoned that $TMPDIR "is the same
# OS temp root the workspace lives under" and allowed it. The write escaped. A
# reviewer is a judgement; the workspace boundary needs to be a rule.
WRITE_COMMANDS = frozenset({
    "cp", "mv", "tee", "ln", "install", "dd", "truncate", "chmod", "chown", "touch",
})

# Commands that reach the network and can execute what they fetch.
NETWORK_FETCH = frozenset({"curl", "wget", "nc", "ncat", "telnet", "ssh", "scp", "sftp", "rsync"})

# Kill-by-pattern verbs. `pkill -f <pattern>` matches the whole command line of
# every process, so a short pattern is a mass kill: on 2026-09-06 a child ran
# `pkill -f "cat"` to stop a stray cat and took down every process launched from
# /Applications ("Appli-cat-ions"), including the user's browsers and the local
# model server. These never reach the supervisor: a bare pattern is refused
# outright, and only a path-shaped pattern is allowed to escalate.
KILL_BY_PATTERN = frozenset({"pkill", "killall"})
KILL_PATTERN_MIN_LEN = 8

# Constructs that run a second command inside the first. They never split into
# segments the policy can judge, so a line carrying one escalates.
SUBSTITUTION = re.compile(r"`|\$\(|<\(|>\(")

# Tokenizer punctuation. An unquoted newline separates commands exactly as `;`
# does, so it is punctuation here, not whitespace.
PUNCTUATION = "();<>|&\n"
SEPARATORS = frozenset({";", "|", "||", "&&"})
REDIRECTS = frozenset({">", ">>", ">|", "<", "<<", "<<<", ">&", "<&", "&>", "&>>", "<>"})
OUTPUT_REDIRECTS = frozenset({">", ">>", ">|", "&>", "&>>", "<>", ">&"})


@dataclass(frozen=True)
class Verdict:
    action: str
    reason: str
    # Structured facts the supervisor is allowed to see. Deliberately excludes
    # any prose written by the child.
    facts: dict[str, object] = field(default_factory=dict)


def _home() -> Path:
    return Path.home()


def is_sensitive(path: Path) -> str | None:
    """Return why a path is off-limits, or None.

    Compared case-folded: APFS is case-insensitive, so `~/.SSH/config` is
    `~/.ssh/config`.
    """
    name = path.name.lower()
    if name in SENSITIVE_NAMES or name.startswith(".env."):
        return f"{path.name} carries credentials"
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        resolved = path
    home = str(_home().resolve()).lower().rstrip("/")
    for candidate in {str(resolved).lower(), os.path.normpath(str(path)).lower()}:
        if candidate != home and not candidate.startswith(home + "/"):
            continue
        rel = candidate[len(home):].lstrip("/")
        for entry in SENSITIVE_HOME:
            folded = entry.lower()
            if rel == folded or rel.startswith(folded + "/"):
                return f"~/{entry} is off-limits to a delegated agent"
    return None


def protect(path: Path) -> None:
    """Refuse writes under `path` from now on (see PROTECTED_ROOTS)."""
    PROTECTED_ROOTS.add(Path(path).expanduser().resolve())


def protected(path: Path) -> str | None:
    """Why a write to `path` is refused wherever it sits, or None."""
    for candidate in (Path(os.path.normpath(str(path))), path.resolve()):
        parts = [p.lower() for p in candidate.parts]
        for index, part in enumerate(parts[:-1]):
            following = parts[index + 1]
            if part == ".git" and (
                following == "hooks" or (following == "config" and index + 2 == len(parts))
            ):
                return (
                    f"{candidate} is code git runs later (.git/hooks, .git/config), "
                    "including in the parent's own git calls"
                )
        for root in PROTECTED_ROOTS:
            if inside(candidate, root):
                return f"{root} holds this server's per-agent settings and sessions"
    return None


def inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, OSError, RuntimeError):
        return False
    return True


def _inside_logically(path: Path, root: Path) -> bool:
    """`path` with `..` removed textually, as bash's `cd` does, under `root`."""
    logical = os.path.normpath(str(path))
    for base in {os.path.normpath(str(root)), str(root.resolve())}:
        if logical == base or logical.startswith(base.rstrip("/") + "/"):
            return True
    return False


def _inside_both(path: Path, root: Path) -> bool:
    """Inside `root` both textually and after following symlinks."""
    return _inside_logically(path, root) and inside(path, root)


def _resolve(word: str, base: Path) -> Path:
    """`word` as a file tool would name it, relative to `base`.

    For shell words, check `_unresolvable` first: bash expands `~+` and `~-`
    to directories this module cannot know, and this keeps them literal.
    """
    try:
        path = Path(word).expanduser()
    except RuntimeError:
        path = Path(word)
    return path if path.is_absolute() else base / path


def _unresolvable(word: str) -> bool:
    """A shell word whose path cannot be known statically.

    `$VAR`, a command substitution, `~+` ($PWD), `~-` ($OLDPWD), and `~name`
    for a user this machine does not know.
    """
    if UNEXPANDED.search(word):
        return True
    if word.startswith("~"):
        head = word.split("/", 1)[0]
        if head.startswith(("~+", "~-")):
            return True
        try:
            Path(head).expanduser()
        except RuntimeError:
            return True
    return False


def _path_like(word: str) -> bool:
    return "/" in word or word.startswith((".", "~"))


def _values(word: str) -> list[str]:
    """The path-bearing parts of one argument.

    `--file=X` and `NAME=X` carry X; a glued short option `-fX` carries X;
    `@X` (curl's "read this file") carries X.
    """
    if word.startswith("-"):
        if "=" in word:
            value = word.split("=", 1)[1]
        elif len(word) > 2 and word[1] != "-":
            value = word[2:]
        else:
            return []
    elif ASSIGNMENT.match(word):
        value = word.split("=", 1)[1]
    else:
        value = word
    if "=" in value:
        value = value.rsplit("=", 1)[1]
    if value.startswith("@"):
        value = value[1:]
    return [value] if value else []


def _expand_home(word: str) -> str:
    """`$HOME/x` and `${HOME}/x` as the path they name."""
    return re.sub(r"^\$(?:HOME\b|\{HOME\})", lambda _m: str(_home()), word)


def classify_path_write(path_str: str, workspace: Path) -> Verdict:
    """A file-tool write or edit."""
    path = _resolve(path_str, workspace)
    facts = {"path": str(path), "inside_workspace": inside(path, workspace)}
    sensitive = is_sensitive(path) or protected(path)
    if sensitive:
        return Verdict(DENY, f"refused: {sensitive}", facts)
    if inside(path, workspace):
        return Verdict(ALLOW, "write inside the workspace", facts)
    return Verdict(ESCALATE, "write outside the workspace", facts)


def classify_bash(command: str, workspace: Path, cwd: Path | None = None) -> Verdict:
    """A bash command line.

    `cwd` is the directory relative paths resolve against. It is the workspace
    unless an earlier `cd` in the same line moved it; the boundary checks are
    always against `workspace`.
    """
    cwd = cwd or workspace
    facts: dict[str, object] = {"command_length": len(command)}
    tokens, error = _tokenize(command)
    if tokens is None:
        return Verdict(ESCALATE, f"command does not parse as shell words: {error}", facts)
    argv = [t for t in tokens if not _is_operator(t)]
    if not argv:
        return Verdict(ALLOW, "empty command", facts)
    if SECRET_ENV.search(command):
        return Verdict(DENY, "refused: reads the child's API key from its environment", facts)

    segments, unsupported = _split(tokens)
    substitution = bool(SUBSTITUTION.search(command))
    compound = len(segments) > 1 or unsupported is not None or substitution
    facts["compound"] = compound

    # Every executable named anywhere in the line, so a pipeline cannot hide a
    # dangerous tail behind a harmless head.
    heads = _command_heads(command, tokens)
    facts["programs"] = heads

    if len(segments) > 1 and unsupported is None and not substitution:
        # Cross-segment dangers first: they are the pipe or the chain itself
        # (`curl … | sh`), which no single segment shows.
        danger = _dangerous_head(heads, command, argv, workspace, cwd, compound, facts)
        if danger is not None:
            return danger
        return _classify_segments(segments, workspace, cwd, facts)

    # A read command is not automatically safe: `cat ~/.ssh/id_rsa` is a
    # read-only tool applied to a secret. Sensitive paths are refused whatever
    # the verb, before the per-command rules get a say.
    leaked = _sensitive_argument(argv, cwd)
    if leaked is not None:
        path, why = leaked
        facts["sensitive_path"] = str(path)
        return Verdict(DENY, f"refused: {why}", facts)

    # Populate the path facts BEFORE any early return. A compound line escalates
    # on the next few lines, and an escalation that carries only "compound: true"
    # asks the supervisor to judge blind -- which a careful one answers by
    # denying. Paths are structure, not the child's prose, so they are safe to
    # show and they are exactly what the decision turns on.
    _path_facts(argv, tokens, workspace, cwd, facts)

    danger = _dangerous_head(heads, command, argv, workspace, cwd, compound, facts)
    if danger is not None:
        return danger

    if facts.get("redirect_targets_outside"):
        return Verdict(
            DENY,
            "refused: redirects output outside the workspace "
            f"({', '.join(str(t) for t in facts['redirect_targets_outside'])})",
            facts,
        )

    guarded = _protected_target(tokens, heads, cwd)
    if guarded is not None:
        return Verdict(DENY, f"refused: {guarded}", facts)

    escaping = _escaping_write(heads, facts)
    if escaping is not None:
        return Verdict(DENY, f"refused: {escaping}", facts)

    if compound:
        detail = f" (`{unsupported}`)" if unsupported else ""
        return Verdict(ESCALATE, f"compound command line{detail}", facts)

    unresolved = next(
        (v for w in argv[1:] for v in _values(w) if _path_like(v) and _unresolvable(v)), None
    )
    if unresolved is not None:
        return Verdict(ESCALATE, f"names a path that cannot be resolved: {unresolved}", facts)

    words, risky = _strip_prefixes(argv)
    if risky is not None:
        return Verdict(
            ESCALATE,
            f"sets `{risky}`, which changes what runs or where paths resolve",
            facts,
        )
    if not words:
        return Verdict(ALLOW, "variable assignment", facts)
    base = PurePosixPath(words[0]).name
    if words[0] == "cd":
        return _classify_cd(words, workspace, cwd, facts)
    if base in {"printenv", "env"} and not _env_runs_a_command(words):
        return Verdict(
            DENY, "refused: prints the child's environment, which holds its API key", facts
        )
    if base in READ_ONLY:
        exposed = _read_only_exposure(base, words)
        if exposed is not None:
            return Verdict(ESCALATE, exposed, facts)
        return Verdict(ALLOW, f"`{base}` is read-only", facts)
    if base == "git":
        exposed = _git_exposure(words)
        if exposed is not None:
            return Verdict(ESCALATE, exposed, facts)
        return Verdict(ALLOW, "read-only git", facts)
    if base in TEST_RUNNERS:
        refused = _runner_refusal(base, words[1:], workspace, cwd)
        if refused is not None:
            return Verdict(ESCALATE, refused, facts)
        return Verdict(ALLOW, "test or build runner", facts)
    if base in INTERPRETERS:
        return _classify_interpreter(base, words, tokens, workspace, cwd, facts)
    runner, refused = _package_runner(base, words, workspace, cwd)
    if refused is not None:
        return Verdict(ESCALATE, refused, facts)
    if runner is not None:
        return Verdict(ALLOW, runner, facts)
    return Verdict(ESCALATE, f"`{base}` is not on the read-only list", facts)


def _dangerous_head(
    heads: list[str],
    command: str,
    argv: list[str],
    workspace: Path,
    cwd: Path,
    compound: bool,
    facts: dict,
) -> Verdict | None:
    """A verdict for any program in the line that is dangerous wherever it sits."""
    for head in heads:
        base = PurePosixPath(head).name
        if base in NEVER:
            return Verdict(DENY, f"refused: `{base}` is not available to a delegated agent", facts)
        if base in {"rm", "rmdir", "shred", "unlink"}:
            return _classify_delete(command, argv, workspace, cwd, facts)
        if base in KILL_BY_PATTERN:
            return _classify_kill(command, facts)
        if base in NETWORK_FETCH:
            if compound:
                return Verdict(
                    DENY,
                    f"refused: `{base}` piped or chained into another command "
                    "(fetch-and-execute)",
                    facts,
                )
            return Verdict(ESCALATE, f"`{base}` reaches the network", facts)
        if base == "git":
            sub = _git_subcommand(command, base)
            if sub and sub not in GIT_READ_ONLY:
                return Verdict(ESCALATE, f"`git {sub}` changes repository state", facts)
    return None


def _tokenize(command: str) -> tuple[list[str] | None, str | None]:
    """Shell words and operators, respecting quotes.

    A quoted operator (`grep ';' f`) comes back as the same string as a real
    one, so it is read as an operator. That splits a line the shell would not,
    and the stray segment escalates: the safe direction to be wrong in.
    """
    lexer = shlex.shlex(
        command.replace("\\\n", ""), posix=True, punctuation_chars=PUNCTUATION
    )
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    try:
        return list(lexer), None
    except ValueError as exc:
        return None, str(exc)


def _is_operator(token: str) -> bool:
    return bool(token) and all(ch in PUNCTUATION for ch in token)


def _separator(token: str) -> str | None:
    """The separator a token stands for, or None if it is not one."""
    if token in SEPARATORS:
        return token
    if "\n" in token:
        rest = token.replace("\n", "")
        if rest == "" or rest in SEPARATORS:
            return rest or "\n"
    return None


def _split(tokens: list[str]) -> tuple[list[tuple[str | None, list[str]]], str | None]:
    """Segments between `;`, `|`, `||`, `&&` and newlines.

    Each segment comes with the separator before it (None for the first).
    Also returns the first operator this does not model -- `&`, `(`, `;;` --
    so the caller can escalate rather than guess.
    """
    segments: list[tuple[str | None, list[str]]] = [(None, [])]
    unsupported: str | None = None
    for token in tokens:
        if _is_operator(token):
            separator = _separator(token)
            if separator is not None:
                if segments[-1][1]:
                    segments.append((separator, []))
                continue
            if token not in REDIRECTS and unsupported is None:
                unsupported = token
        segments[-1][1].append(token)
    return [seg for seg in segments if seg[1]], unsupported


def _join(tokens: list[str]) -> str:
    """A segment back as a command line that tokenizes to the same tokens."""
    return " ".join(t if _is_operator(t) else shlex.quote(t) for t in tokens)


def _strip_prefixes(words: list[str]) -> tuple[list[str], str | None]:
    """Drop a leading `!`, `export NAME=value` and `NAME=value` words.

    Returns the command that is left and, when a variable is not in SAFE_ENV,
    its name so the caller can escalate.
    """
    words = list(words)
    if words[:1] == ["!"]:
        words = words[1:]
    if words[:1] == ["export"] and all(
        ASSIGNMENT.match(w) or NAME.match(w) for w in words[1:]
    ):
        names = [(ASSIGNMENT.match(w) or NAME.match(w)).group(0).rstrip("=") for w in words[1:]]
        risky = next((n for n in names if not _safe_env(n)), None)
        return [], risky
    while words and ASSIGNMENT.match(words[0]):
        name = ASSIGNMENT.match(words[0]).group(1)
        if not _safe_env(name):
            return words, name
        words = words[1:]
    return words, None


def _safe_env(name: str) -> bool:
    return name in SAFE_ENV or len(name) == 1


def _cd_target(words: list[str], cwd: Path) -> Path | None:
    """Where `cd` lands, or None when that cannot be known statically."""
    args = words[1:]
    if len(args) > 1 or (args and args[0].startswith("-")):
        return None
    target = args[0] if args else "~"
    if _unresolvable(target):
        return None
    return _resolve(target, cwd)


def _cd_lands(target: Path) -> Path:
    """The directory `cd target` leaves the shell in.

    Bash's `cd` removes `..` textually (logical mode) before it changes
    directory, so `cd link/..` lands beside the link, not beside its target.
    """
    return Path(os.path.normpath(str(target))).resolve()


def _classify_cd(words: list[str], workspace: Path, cwd: Path, facts: dict) -> Verdict:
    target = _cd_target(words, cwd)
    if target is None:
        return Verdict(ESCALATE, "`cd` to a directory that cannot be resolved", facts)
    lands = _cd_lands(target)
    facts["cd_target"] = str(lands)
    # Both readings must stay inside: logical (what bash does by default) and
    # physical (what `cd -P`, or a failed logical cd, does).
    if _inside_both(target, workspace) and inside(lands, workspace):
        return Verdict(ALLOW, "`cd` inside the workspace", facts)
    return Verdict(ESCALATE, f"`cd` to {lands}, outside the workspace", facts)


def _short_letters(words: list[str]) -> set[str]:
    """Letters of every short-option cluster: `-uo/tmp/x` gives {u, o}."""
    letters: set[str] = set()
    for word in words:
        if word.startswith("-") and not word.startswith("--"):
            for ch in word[1:]:
                if not ch.isalpha():
                    break
                letters.add(ch)
    return letters


def _long(words: list[str], *names: str) -> str | None:
    """The first `--name` or `--name=value` among `words`."""
    for word in words:
        if word.split("=", 1)[0] in names:
            return word
    return None


def _env_runs_a_command(words: list[str]) -> bool:
    if PurePosixPath(words[0]).name != "env":
        return False
    return any(not w.startswith("-") and not ASSIGNMENT.match(w) for w in words[1:])


def _read_only_exposure(base: str, words: list[str]) -> str | None:
    """Why a read-only verb is not read-only as written, or None."""
    rest = words[1:]
    letters = _short_letters(rest)
    if base == "find":
        action = next((w for w in rest if w in FIND_ACTIONS), None)
        if action:
            return f"`find {action}` runs a command or writes a file"
    if base == "env":
        return "`env` running a command"
    if base == "sort":
        if "o" in letters or _long(rest, "--output"):
            return "`sort -o` writes a file"
        if _long(rest, "--compress-program"):
            return "`sort --compress-program` runs a program"
    if base == "uniq" and len([w for w in rest if not w.startswith("-")]) > 1:
        return "`uniq` with an output file"
    if base == "tree" and ("o" in letters or _long(rest, "--output")):
        return "`tree -o` writes a file"
    if base == "rg" and any(w.startswith("--pre") for w in rest):
        return "`rg --pre` runs a command per file"
    if base == "man" and any(w.startswith("-") for w in rest):
        return "`man` with options (`-P`, `--pager`, `-H`) can run a program"
    return None


def _git_exposure(words: list[str]) -> str | None:
    """Why a git line is not a read, or None."""
    rest = words[1:]
    index = 0
    while index < len(rest) and rest[index].startswith("-"):
        if rest[index] not in GIT_GLOBAL_OK:
            return f"`git {rest[index]}` changes where git reads config or the repository"
        index += 1
    if index >= len(rest):
        return None
    sub, args = rest[index], rest[index + 1:]
    if sub not in GIT_READ_ONLY:
        return f"`git {sub}` changes repository state"
    option = next((w for w in args if w.split("=", 1)[0] in GIT_EXEC_OPTIONS), None)
    if option is not None:
        return f"`git {sub} {option}` writes a file or runs a program"
    options = [w for w in args if w.startswith("-")]
    positional = [w for w in args if not w.startswith("-")]
    if sub == "config":
        unknown = next(
            (w for w in options if w not in GIT_CONFIG_READ and not w.startswith("--type=")),
            None,
        )
        if unknown is not None:
            return f"`git config {unknown}` is not a read"
        limit = 2 if {"--get-regexp", "--get-urlmatch"} & set(options) else 1
        if len(positional) > limit or (positional and not options and len(positional) > 1):
            return "`git config` with a value sets it"
    if sub == "stash" and args[:1] not in (["list"], ["show"]):
        return "`git stash` other than `list`/`show` changes the worktree or the stash"
    if sub == "branch":
        unknown = next(
            (w for w in options
             if w.split("=", 1)[0] not in GIT_BRANCH_READ
             and not w.startswith(("--sort", "--format"))),
            None,
        )
        if unknown is not None:
            return f"`git branch {unknown}` changes branches"
        if positional and not {"-l", "--list", "--contains", "--no-contains", "--merged",
                               "--no-merged", "--points-at"} & set(options):
            return "`git branch <name>` creates a branch"
    if sub == "remote" and not (
        args in ([], ["-v"], ["--verbose"]) or args[:1] == ["get-url"]
    ):
        return "`git remote` other than listing changes remotes or reaches the network"
    return None


def _runner_refusal(name: str, args: list[str], workspace: Path, cwd: Path) -> str | None:
    """Why a test or build runner's arguments escalate, or None.

    Allowed: flags, test selectors, and paths inside the workspace. Not
    allowed: options that load code or config from elsewhere
    (RUNNER_OPTIONS), subcommands that install, run or publish, variable
    assignments, and any path that is outside the workspace or unresolvable.
    """
    refused = RUNNER_OPTIONS.get(name, set())
    short_refused = {o[1] for o in refused if len(o) == 2 and o[0] == "-" and o[1] != "-"}
    after_dashdash = False
    for word in args:
        if word == "--":
            after_dashdash = True
            continue
        if word.startswith("-") and not after_dashdash:
            option = word.split("=", 1)[0]
            if option in refused:
                return f"`{name} {option}` loads code or config from elsewhere"
            if not word.startswith("--") and short_refused & _short_letters([word]):
                return f"`{name} {word}` loads code or config from elsewhere"
            if "=" in word and word.split("=", 1)[1].startswith("-"):
                return f"`{name} {word}` passes options through an option"
        elif ASSIGNMENT.match(word):
            return f"`{name} {word}` sets a variable the runner reads"
        for value in _values(word):
            if _unresolvable(value):
                return f"`{name}` argument {value} cannot be resolved"
            if _path_like(value) and not _inside_both(_resolve(value, cwd), workspace):
                return f"`{name}` argument {value} is outside the workspace"
    positional = [w for w in args if not w.startswith("-")]
    allowed = RUNNER_SUBCOMMANDS.get(name)
    if allowed is not None and (not positional or positional[0] not in allowed):
        sub = positional[0] if positional else "(none)"
        return f"`{name} {sub}` is not a test, check or build"
    if name == "make":
        target = next((w for w in positional if w in MAKE_TARGETS_REFUSED), None)
        if target is not None:
            return f"`make {target}` installs or publishes"
    return None


def _package_runner(
    base: str, words: list[str], workspace: Path, cwd: Path
) -> tuple[str | None, str | None]:
    """A test run through a package manager: (description, refusal)."""
    rest = words[1:]

    def runner(name: str) -> bool:
        return name in TEST_RUNNERS or name in JS_RUNNERS

    def judged(label: str, tool: str, args: list[str]) -> tuple[str | None, str | None]:
        refused = _runner_refusal(tool, args, workspace, cwd)
        return (None, refused) if refused else (label, None)

    if base == "uv" and rest[:1] == ["run"] and len(rest) > 1 and runner(rest[1]):
        return judged(f"`uv run {rest[1]}`", rest[1], rest[2:])
    if base == "pnpm" and rest[:1] in (["test"], ["vitest"]):
        tool = "vitest" if rest[0] == "vitest" else "pnpm"
        return judged(f"`pnpm {rest[0]}`", tool, rest[1:])
    if base == "pnpm" and rest[:2] == ["exec", "tsc"]:
        return judged("`pnpm exec tsc`", "tsc", rest[2:])
    if base == "npm" and rest[:1] == ["test"]:
        return judged("`npm test`", "npm", rest[1:])
    if base == "npx" and rest[:1] in (["tsc"], ["vitest"]):
        return judged(f"`npx {rest[0]}`", rest[0], rest[1:])
    if base == "gradlew" and "/" in words[0] and rest[:1] and rest[0] in GRADLE_TASKS:
        if _unresolvable(words[0]) or not _inside_both(_resolve(words[0], cwd), workspace):
            return None, f"`{words[0]}` is not the workspace's own gradlew"
        return judged(f"`gradlew {rest[0]}` from the workspace", "gradlew", rest[1:])
    return None, None


def _classify_interpreter(
    base: str, argv: list[str], tokens: list[str], workspace: Path, cwd: Path, facts: dict
) -> Verdict:
    """`python foo.py` where foo.py is the agent's own work is routine.

    `python -c '...'` is arbitrary code with no artifact to inspect, so it
    escalates however harmless it looks. So does any program the interpreter
    would read from stdin, a here-string or a redirect, and any option before
    the script this table does not know: an option that takes a value is
    otherwise misread as the script.
    """
    if any(t in STDIN_REDIRECTS for t in tokens):
        return Verdict(ESCALATE, f"`{base}` reading its program from a redirect", facts)
    rest = argv[1:]
    inline = INLINE_LETTERS.get(base, "")
    safe = SAFE_LETTERS.get(base, "")
    index = 0
    while index < len(rest) and rest[index].startswith("-") and rest[index] != "-":
        word = rest[index]
        if word in INLINE_CODE_FLAGS or word.split("=", 1)[0] in INLINE_CODE_FLAGS:
            return Verdict(ESCALATE, f"`{base}` running inline source", facts)
        if word.startswith("--"):
            if word in SAFE_LONG_OPTIONS:
                index += 1
                continue
            return Verdict(ESCALATE, f"`{base} {word}` is an option the policy does not read",
                               facts)
        letters = word[1:]
        if any(ch in inline for ch in letters):
            return Verdict(ESCALATE, f"`{base}` running inline source", facts)
        if base in ("python", "python3") and "m" in letters:
            head, _, tail = letters.partition("m")
            if any(ch not in safe for ch in head):
                return Verdict(ESCALATE, f"`{base} {word}` is an option the policy does not read",
                               facts)
            module = tail or (rest[index + 1] if index + 1 < len(rest) else "")
            args = rest[index + 1:] if tail else rest[index + 2:]
            facts["module"] = module
            root = module.split(".")[0]
            if root not in TEST_MODULES:
                return Verdict(ESCALATE, f"`{base} -m {module}`", facts)
            refused = _runner_refusal(root, args, workspace, cwd)
            if refused is not None:
                return Verdict(ESCALATE, refused, facts)
            return Verdict(ALLOW, f"`{base} -m {module}`", facts)
        if letters and all(ch in safe for ch in letters):
            index += 1
            continue
        return Verdict(ESCALATE, f"`{base} {word}` is an option the policy does not read",
                               facts)
    if index < len(rest) and rest[index] == "--":
        index += 1
    if index >= len(rest) or rest[index] == "-":
        return Verdict(ESCALATE, f"`{base}` with no script argument", facts)
    script, args = rest[index], rest[index + 1:]
    if _unresolvable(script) or "://" in script:
        return Verdict(ESCALATE, f"`{base}` script {script} cannot be resolved", facts)
    path = _resolve(script, cwd)
    if not (_inside_both(path, workspace) and path.resolve().is_file()):
        facts["scripts_outside_workspace"] = [str(path)]
        return Verdict(
            ESCALATE,
            f"`{base}` running {path}, which is not a file in the workspace",
            facts,
        )
    outside = []
    for word in args:
        for value in _values(word):
            candidate = _resolve(value, cwd)
            if _unresolvable(value) or (
                (candidate.suffix or _path_like(value)) and _path_like(value)
                and not inside(candidate, workspace)
            ):
                outside.append(value)
    if outside:
        facts["scripts_outside_workspace"] = outside
        return Verdict(ESCALATE, f"`{base}` given paths outside the workspace", facts)
    facts["scripts"] = [script]
    return Verdict(ALLOW, f"`{base}` running workspace code", facts)


# A word carrying an unexpanded shell variable or a command substitution cannot
# be resolved statically, and resolving it anyway produces a confident lie --
# `$TMPDIR/x` looks like a relative path and would be reported as inside the
# workspace. Say "unresolved" instead; that is the fact the supervisor needs.
UNEXPANDED = re.compile(r"[$`]")


def _looks_like_a_path(word: str) -> bool:
    # A bare `sample.txt` counts: telling the supervisor the file sits inside the
    # workspace is what turns a blind ruling into an informed one.
    return "/" in word or word.startswith(".") or "." in word


def _describe_path(word: str, workspace: Path, cwd: Path | None = None) -> dict[str, object]:
    if _unresolvable(word):
        return {"path": word, "resolved": False}
    path = _resolve(word, cwd or workspace)
    return {"path": str(path), "inside_workspace": inside(path, workspace)}


def _path_facts(
    argv: list[str], tokens: list[str], workspace: Path, cwd: Path, facts: dict
) -> None:
    """Every path this command names, resolved where that is honest.

    Runs for every bash call, including the compound ones that escalate, so the
    supervisor is never asked to rule on a write without being told where.
    """
    named = [
        _describe_path(word, workspace, cwd)
        for word in argv[1:]
        if not word.startswith("-") and _looks_like_a_path(word)
    ]
    if named:
        facts["paths"] = named[:12]
    _redirects_outside(tokens, workspace, cwd, facts)


def _escaping_write(heads: list[str], facts: dict) -> str | None:
    """A write command aimed outside the workspace, or at a path nobody can resolve."""
    writers = [PurePosixPath(h).name for h in heads]
    if not any(w in WRITE_COMMANDS for w in writers):
        return None
    for described in facts.get("paths", []):
        if not described.get("resolved", True):
            return (
                f"`{next(w for w in writers if w in WRITE_COMMANDS)}` targets "
                f"{described['path']}, which cannot be resolved, so it cannot be "
                "confirmed inside the workspace"
            )
        if not described.get("inside_workspace", True):
            return (
                f"`{next(w for w in writers if w in WRITE_COMMANDS)}` targets "
                f"{described['path']}, outside the workspace"
            )
    return None


def _protected_target(tokens: list[str], heads: list[str], cwd: Path) -> str | None:
    """A redirect, or a write command's argument, aimed at a protected path."""
    targets = [
        tokens[i + 1] for i, t in enumerate(tokens[:-1])
        if t in OUTPUT_REDIRECTS and not _is_operator(tokens[i + 1])
    ]
    if any(PurePosixPath(h).name in WRITE_COMMANDS for h in heads):
        targets += [v for t in tokens if not _is_operator(t) for v in _values(t)]
    for word in targets:
        if _unresolvable(word):
            continue
        why = protected(_resolve(word, cwd))
        if why:
            return why
    return None


SEVERITY = {ALLOW: 0, ESCALATE: 1, DENY: 2}


def _classify_segments(
    segments: list[tuple[str | None, list[str]]], workspace: Path, cwd: Path, facts: dict
) -> Verdict:
    """Judge a compound line one segment at a time, and keep the worst.

    Escalating every compound line wholesale is the safe answer and the wrong
    one: it sent `pwd && ls` to a model, which cost eleven seconds and a model
    call to be told what the read-only list already knew. Measured, in a trace.

    Every segment is judged, so a deny later in the line is not hidden behind
    an earlier escalation.

    A `cd` moves where later segments' relative paths point. Each segment is
    judged in every directory the shell could be in when it runs. After
    `cd X && next`, `next` runs only if the cd succeeded, so it is judged in X
    alone. After `;`, `||`, `|` or a newline, the cd may have failed or been
    skipped, or run in a pipeline's subshell, so the old directory is live
    again alongside X.
    """
    facts["segments"] = len(segments)
    now: list[Path] = [cwd]       # directories the current segment may run in
    fallback: list[Path] = []     # directories live again after a non-`&&` separator
    worst: tuple[Verdict, str] | None = None
    for index, (separator, segment) in enumerate(segments):
        if separator is not None and separator != "&&":
            now = _unique(now + fallback)
            fallback = []
        text = _join(segment)
        words, _ = _strip_prefixes([t for t in segment if not _is_operator(t)])
        targets: list[Path] = []
        stays: list[Path] = []
        for here in now:
            verdict = classify_bash(text, workspace, here)
            if worst is None or SEVERITY[verdict.action] > SEVERITY[worst[0].action]:
                worst = (verdict, text)
            target = _cd_target(words, here) if words[:1] == ["cd"] else None
            if verdict.action == ALLOW and target is not None:
                targets.append(_cd_lands(target))
            else:
                stays.append(here)
        if targets:
            following = segments[index + 1][0] if index + 1 < len(segments) else None
            reached = separator in (None, ";", "\n", "&&") and following != "|"
            if reached:
                # Only a failed cd leaves the shell where it was, and a failed
                # cd stops an `&&` chain.
                fallback = _unique(fallback + [p for p in now if p not in stays])
                now = _unique(stays + targets)
            else:
                now = _unique(now + targets)
    assert worst is not None
    verdict, text = worst
    if verdict.action == ALLOW:
        return Verdict(ALLOW, f"{len(segments)} segments, each allowed by policy", facts)
    merged = {**facts, **verdict.facts, "segments": len(segments)}
    return Verdict(
        verdict.action,
        f"segment `{_clip_command(text)}`: {verdict.reason}",
        merged,
    )


def _unique(paths: list[Path]) -> list[Path]:
    return list(dict.fromkeys(paths))


def _sensitive_argument(argv: list[str], cwd: Path) -> tuple[Path | str, str] | None:
    """The first argument naming an off-limits path, or None.

    Only words that look like paths are considered -- containing a separator or
    starting with a dot. A bare word like `credentials` is far more often an
    argument than a filename, and denying it would be the substring-matching
    mistake this module exists to avoid.

    Values count too: `--file=X`, `-fX`, `NAME=X`, and curl's `@X`. `$HOME` is
    expanded. A `~+`, `~-` or unknown `~name` word is refused outright: it
    could name anything, and a secret is among the things it could name.
    """
    for word in argv[1:]:
        for value in _values(word):
            if value.startswith("~") and _unresolvable(value):
                return value, f"{value} cannot be resolved, so it cannot be cleared"
            value = _expand_home(value)
            if not _path_like(value):
                continue
            if UNEXPANDED.search(value):
                continue
            path = _resolve(value, cwd)
            why = is_sensitive(path)
            if why:
                return path, why
    return None


def _command_heads(command: str, tokens: list[str]) -> list[str]:
    """First word of each segment, so a pipeline's tail is not invisible.

    Two readings, merged: a raw split that ignores quoting (it over-reports,
    which is the safe direction) and the tokenizer's, which sees segments the
    raw split cannot -- a newline, a background `&`, a subshell `(`.
    """
    heads: list[str] = []
    for segment in re.split(r"\|\||&&|[;|\n]|\$\(|`", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        head = _head(words)
        if head is not None:
            heads.append(head)
    starts = [[]]
    after_redirect = False
    for token in tokens:
        if _is_operator(token):
            after_redirect = token in REDIRECTS
            if not after_redirect:
                starts.append([])
            continue
        if after_redirect:
            after_redirect = False
            continue
        starts[-1].append(token)
    for words in starts:
        head = _head(words)
        if head is not None and head not in heads:
            heads.append(head)
    return heads


def _head(words: list[str]) -> str | None:
    for word in words:
        if word == "!":
            continue
        if "=" in word and not word.startswith("/") and not word.startswith("-"):
            continue  # VAR=value prefix
        return word
    return None


def _git_subcommand(command: str, head: str) -> str | None:
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words):
        if PurePosixPath(word).name == head:
            for candidate in words[i + 1:]:
                if not candidate.startswith("-"):
                    return candidate
            return None
    return None


def _classify_delete(
    command: str, argv: list[str], workspace: Path, cwd: Path, facts: dict
) -> Verdict:
    targets = [a for a in argv[1:] if not a.startswith("-")]
    facts["delete_targets"] = targets
    if not targets:
        return Verdict(ESCALATE, "delete with no visible target", facts)
    for raw in targets:
        if _unresolvable(raw):
            return Verdict(
                DENY,
                f"refused: delete target {raw} cannot be resolved, so it cannot be "
                "confirmed inside the workspace",
                facts,
            )
        if any(ch in raw for ch in "*?["):
            return Verdict(ESCALATE, f"delete with a glob: {raw}", facts)
        path = _resolve(raw, cwd)
        if is_sensitive(path):
            return Verdict(DENY, f"refused: delete targets {path}", facts)
        if not inside(path, workspace):
            return Verdict(DENY, f"refused: delete targets {path}, outside the workspace", facts)
    return Verdict(ESCALATE, "delete inside the workspace", facts)


def _classify_kill(command: str, facts: dict) -> Verdict:
    """`pkill`/`killall` are refused unless `pkill -f` names a path-shaped pattern.

    A pattern without a `/` matches by substring against every command line on
    the machine; there is no safe short pattern. Children that need to stop a
    process they started should use `kill <pid>` or a full path pattern.
    """
    for segment in re.split(r"\|\||&&|[;|\n]|\$\(|`", command):
        try:
            words = shlex.split(segment)
        except ValueError:
            continue
        if not words:
            continue
        base = PurePosixPath(words[0]).name
        if base not in KILL_BY_PATTERN:
            continue
        if base == "killall":
            return Verdict(DENY, "refused: `killall` kills by process name; use kill <pid>", facts)
        patterns = [w for w in words[1:] if not w.startswith("-")]
        facts["kill_patterns"] = patterns
        if "-f" not in words[1:] or not patterns:
            return Verdict(
                DENY,
                "refused: `pkill` without `-f <path-pattern>` matches process names "
                "by substring; use kill <pid>",
                facts,
            )
        for pattern in patterns:
            if "/" not in pattern or len(pattern) < KILL_PATTERN_MIN_LEN:
                return Verdict(
                    DENY,
                    f"refused: `pkill -f {pattern}` is a bare pattern that matches any "
                    "command line containing it (one such call killed every /Applications "
                    "process); use a path pattern or kill <pid>",
                    facts,
                )
        return Verdict(ESCALATE, "kills processes by path pattern", facts)
    return Verdict(DENY, "refused: kill-by-pattern with unreadable arguments", facts)


def _redirects_outside(tokens: list[str], workspace: Path, cwd: Path, facts: dict) -> bool:
    """Output redirect targets outside the workspace, read from the tokens.

    Tokens, not a regex over the raw line: the regex missed `1>/etc/x` and
    `>| /etc/x`, and read `>` inside a quoted string as a redirect.
    """
    outside = []
    for index, token in enumerate(tokens[:-1]):
        if token not in OUTPUT_REDIRECTS:
            continue
        word = tokens[index + 1]
        if _is_operator(word):
            continue
        # `2>&1`, `>&-`: a descriptor, not a file.
        if token == ">&" and (word.isdigit() or word == "-"):
            continue
        if word in ("/dev/null", "/dev/stdout", "/dev/stderr"):
            continue
        described = _describe_path(word, workspace, cwd)
        # Unresolvable counts as outside: a destination nobody can determine is
        # not a destination anyone should have approved.
        if not described.get("inside_workspace", False):
            outside.append(described["path"])
    if outside:
        facts["redirect_targets_outside"] = outside
        return True
    return False


# Tool-name aliases Claude Code (and the old dsh bridge) may present.
BASH_TOOLS = frozenset({"bash", "shell", "run_command", "Bash"})
WRITE_TOOLS = frozenset({
    "write", "edit", "str_replace_editor", "Write", "Edit", "create", "NotebookEdit",
})
READ_TOOLS = frozenset({
    "read", "read_image", "Read", "list", "glob", "grep", "Grep", "Glob",
    "LS", "TodoRead", "TodoWrite",
})
NETWORK_TOOLS = frozenset({"WebFetch", "WebSearch", "web_fetch", "web_search"})
NESTED_TOOLS = frozenset({"Agent", "Task", "Skill", "TaskCreate", "TaskUpdate"})

PATH_KEYS = ("path", "file_path", "filePath", "target", "filename", "file")
COMMAND_KEYS = ("command", "cmd", "script", "input")


def classify(tool_name: str, tool_input: dict, workspace: Path) -> Verdict:
    """Classify one proposed tool call. Unknown shapes escalate, never allow."""
    name = (tool_name or "").strip()
    if name.startswith("mcp__") or name in NESTED_TOOLS:
        return Verdict(
            DENY,
            f"refused: `{name}` is not available to a delegated agent",
            {"tool": name},
        )
    if name in NETWORK_TOOLS:
        return Verdict(ESCALATE, f"`{name}` reaches the network", {"tool": name})
    if name in READ_TOOLS:
        path = _first(tool_input, PATH_KEYS)
        if path is not None:
            candidate = _resolve(str(path), workspace)
            why = is_sensitive(candidate)
            if why:
                return Verdict(
                    DENY,
                    f"refused: {why}",
                    {"tool": name, "sensitive_path": str(candidate)},
                )
        return Verdict(ALLOW, "read-only tool", {"tool": name})
    if name in BASH_TOOLS:
        command = _first(tool_input, COMMAND_KEYS)
        if command is None:
            return Verdict(ESCALATE, "shell call with no readable command", {"tool": name})
        verdict = classify_bash(str(command), workspace)
        return Verdict(verdict.action, verdict.reason, {"tool": name, **verdict.facts})
    if name in WRITE_TOOLS:
        path = _first(tool_input, PATH_KEYS)
        if path is None:
            return Verdict(ESCALATE, "write call with no readable path", {"tool": name})
        verdict = classify_path_write(str(path), workspace)
        return Verdict(verdict.action, verdict.reason, {"tool": name, **verdict.facts})
    return Verdict(ESCALATE, f"unrecognized tool `{name}`", {"tool": name})


def _first(payload: dict, keys: tuple[str, ...]):
    if not isinstance(payload, dict):
        return None
    for key in keys:
        if payload.get(key) is not None:
            return payload[key]
    return None


def classify_verification(command: str, workspace: Path) -> Verdict:
    """Classify a caller's acceptance command.

    This is deliberately the same policy the child's own calls get, rather than
    a second one that drifts. The caller is another agent and can be prompt
    injected, so "the caller asked for it" is not authorization; and an
    acceptance command is shaped exactly like ordinary dev work, which the
    policy already knows how to read.
    """
    if not command.strip():
        return Verdict(ESCALATE, "empty verification command", {"command": command})
    return classify_bash(command, workspace)


def _clip_command(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
