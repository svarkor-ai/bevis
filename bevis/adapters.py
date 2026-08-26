"""The adapter registry: a NAME for a command line, and nothing else.

An adapter is a command bevis executes. It is not a plugin, not a driver and
not a connection: bevis never learns what is on the other end of it. That is
the whole architectural position, so this module is deliberately unable to
undermine it — there is no field here for an endpoint, a model name, a GPU
address or a key, and no code path that reads one.

What the registry stores is the two things that are safe to store:

    name    what you type            "myagent"
    cmd     what bevis executes      "./my-agent.sh"

Your adapter owns its own configuration. Where its model lives, which model it
is, and what it authenticates with are questions the adapter answers for
itself, out of its own environment or its own config file, on the machine where
it runs. bevis passes the job in and reads the exit code back.

To keep that true in practice and not only in the docs, `add()` REFUSES a
command with a credential written into it — see credential_problem(). That is a
lint, not a guarantee (§ README, Limitations): it catches the shapes people
actually paste, and a determined obfuscation walks past it. What it does
guarantee is that the obvious way to get a secret into this database is closed,
and that the refusal names the two places the secret belongs instead.
"""
from __future__ import annotations

import re
import shlex
import shutil
import sqlite3
from typing import List, Optional, Tuple

from .core import default_actor
from .db import log_event
from .errors import NotFound, Refusal, UsageError
from .model import now_ts

#: A name is an identifier, not a sentence: it has to be distinguishable from
#: the command templates `--adapter` also accepts. No whitespace, no shell
#: metacharacters, nothing that could be mistaken for a path or a pipeline.
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

#: What a value has to start with to be a REFERENCE the adapter resolves at run
#: time rather than a secret sitting in the database: `$MY_KEY`, `"$MY_KEY"`,
#: `$(pass show model/key)`, `` `cat /run/secrets/key` ``. Writing it that way is
#: the right answer, so it must not be refused — a rule that refused the
#: reference would teach people to obfuscate the literal instead of moving it
#: out of bevis.
_REFERENCE_STARTS = ("$", "`")


def _looks_like_a_literal(rest: str) -> bool:
    """Does the text after a delimiter look like a pasted value?

    One opening quote is skipped first, because `--api-key "$MY_KEY"` means
    exactly what `--api-key $MY_KEY` means. Nothing else is skipped: a quote
    followed by anything BUT a reference is a quoted literal, which is the shape
    this rule used to be fooled by (`--token="hunter2"` was accepted while
    `--token=hunter2` was refused).
    """
    text = rest.lstrip()
    if text[:1] in ("'", '"'):
        text = text[1:]
    if not text or text[0] in ("'", '"'):
        return False                      # nothing, or an empty quoted value
    if text[0] in _REFERENCE_STARTS:
        return False                      # $MY_KEY / `cat ...` / $(...)
    return text[0] != "-"                 # the next thing is another flag


#: Narrow on purpose, and each one matches only up to the DELIMITER — whether
#: what follows is a secret or a reference is decided by _looks_like_a_literal,
#: in Python, where it can be read.
_SECRET_PATTERNS = (
    (re.compile(r"(?i)authorization\s*:\s*(?:bearer|basic|token)\s+"),
     "an Authorization header with a literal value in it"),
    (re.compile(r"(?i)(?<![A-Za-z0-9_-])x?-?api[-_]?key\s*:\s*"),
     "an API-key header with a literal value in it"),
    (re.compile(r"(?i)--(?:api[-_]?key|apikey|token|password|passwd|secret)(?:=|\s+)"),
     "an --api-key/--token/--password flag with a literal value"),
    # The lookahead is what keeps `http://127.0.0.1:8080/v1` out of this: a port
    # is not a password, and only an `@` before the next `/` makes it userinfo.
    (re.compile(r"//[^\s/:@]+:(?=[^\s/@]*@)"),
     "a URL with a password in front of the @"),
)

#: Anywhere at all, quoted or not: these are nobody's build flag.
_TOKEN_LITERAL = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})")

#: A shell assignment only becomes an environment variable in the LEADING
#: position, before the command word. `make KEY=value`, `helm --set
#: TOKEN=managed` and `sed 's/KEY=old/KEY=new/'` are arguments, and an earlier
#: version of this rule refused all three.
_LEADING_ASSIGNMENT = re.compile(r"\s*([A-Za-z_][A-Za-z0-9_]*)=(\S*)")
_SECRET_NAME = re.compile(r"(?i)(?:^|_)(?:api_?key|key|token|secret|password|passwd|pwd)$")


def _leading_assignment_problem(cmd: str) -> Optional[str]:
    """`OPENAI_API_KEY=sk-... ./agent.sh` — a secret handed to the command as an
    environment variable, with the literal stored on the board."""
    index = 0
    while True:
        match = _LEADING_ASSIGNMENT.match(cmd, index)
        if not match:
            return None
        name, value = match.group(1), match.group(2)
        if _SECRET_NAME.search(name) and _looks_like_a_literal(value):
            return "an inline %s=<value> assignment" % name
        index = match.end()


def credential_problem(cmd: str) -> Optional[str]:
    """Name the credential shape in `cmd`, or None.

    Tested in both directions, which is the only way a gate like this is worth
    anything: `tests/test_adapters.py` carries a list of pasted secrets that
    must all be refused AND a list of references, near misses and ordinary
    build commands that must all be allowed.
    """
    cmd = cmd or ""
    if _TOKEN_LITERAL.search(cmd):
        return "a literal that is shaped like an API token"
    for pattern, description in _SECRET_PATTERNS:
        for match in pattern.finditer(cmd):
            if _looks_like_a_literal(cmd[match.end():]):
                return description
    return _leading_assignment_problem(cmd)


def program_of(cmd: str) -> Optional[str]:
    """The program a command line will actually execute, or None if unparseable.

    Leading `NAME=value` assignments are skipped so that the program can still
    be found behind them. That is not an endorsement of putting configuration
    there: a command line is stored verbatim, so anything written on it is on
    your board. The adapter's own environment is the better home for it.
    """
    try:
        tokens = shlex.split(cmd or "")
    except ValueError:
        return None
    for token in tokens:
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return token
    return None


def has_registry(conn: sqlite3.Connection) -> bool:
    """Does this board have the adapter table at all?

    A board created by an earlier bevis does not. That is a fact about the file,
    so it is asked of the file rather than inferred from an exception.
    """
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='adapter'"
    ).fetchone())


def _require_registry(conn: sqlite3.Connection) -> None:
    """Refuse usefully on a database that predates the registry.

    `bevis init` is idempotent and adds the table without touching a single
    job, so the fix is one command — and naming it here is cheaper than letting
    a stranger meet a raw `sqlite3.OperationalError`.
    """
    if not has_registry(conn):
        raise UsageError(
            "this database has no adapter registry — it was created by an older "
            "bevis. Run `bevis init` again: it is idempotent and adds the table "
            "without touching your jobs.")


def add(conn, name: str, cmd: str, note: str = "", actor: str = "") -> dict:
    """Register a name for a command. Refuses a command carrying a secret."""
    _require_registry(conn)
    name = (name or "").strip()
    cmd = (cmd or "").strip()
    if not NAME_RE.match(name):
        raise UsageError(
            "%r is not a usable adapter name — use letters, digits, dot, dash or "
            "underscore, starting with a letter or digit (e.g. myagent)" % name)
    if not cmd:
        raise UsageError("--cmd is required — an adapter is a command bevis runs")
    if shutil.which(name):
        # `bevis run --adapter true` means the command `true` today. Letting an
        # adapter take that name would change what an existing command line
        # means, silently, for everyone else using this board.
        raise Refusal(
            "refusing to register adapter %r: %r is also a program on this "
            "PATH, and `bevis run --adapter %s` would stop meaning what it "
            "means now. Pick a name that is not a command."
            % (name, name, name))
    problem = credential_problem(cmd)
    if problem:
        raise Refusal(
            "refusing to register adapter %r: the command contains %s.\n"
            "bevis stores the command line you give it, verbatim, and is not a "
            "secret store — it never asks for your keys, endpoints or model "
            "names and has no field for them. Put the secret in the adapter's "
            "own environment and reference it ($MY_API_KEY), or move the whole "
            "call into a wrapper script and register that script instead."
            % (name, problem))
    # A template bevis can never render is a registration that only fails later,
    # at `bevis run`, when a job has already been claimed for it.
    from .dispatch import render_adapter   # here, not at the top: dispatch
                                           # imports this module to resolve names
    render_adapter(cmd, {"id": 0, "display_id": "0", "title": "", "description": "",
                         "acceptance": "", "assignee": ""})
    try:
        conn.execute(
            "INSERT INTO adapter (name, cmd, note, created_at) VALUES (?,?,?,?)",
            (name, cmd, (note or "").strip(), now_ts()))
    except sqlite3.IntegrityError:
        raise UsageError(
            "an adapter named %r is already registered — `bevis adapter remove %s` "
            "first, or pick another name" % (name, name))
    log_event(conn, None, actor or default_actor(), "adapter_added", "%s = %s"
              % (name, cmd))
    return get(conn, name)


def get(conn, name: str) -> Optional[dict]:
    """The registered adapter, or None. Never a guess."""
    _require_registry(conn)
    row = conn.execute("SELECT * FROM adapter WHERE name=?",
                       ((name or "").strip(),)).fetchone()
    return dict(row) if row else None


def list_all(conn) -> List[dict]:
    _require_registry(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM adapter ORDER BY name")]


def remove(conn, name: str, actor: str = "") -> None:
    _require_registry(conn)
    name = (name or "").strip()
    cur = conn.execute("DELETE FROM adapter WHERE name=?", (name,))
    if cur.rowcount == 0:
        raise NotFound("no adapter named %r is registered" % name)
    log_event(conn, None, actor or default_actor(), "adapter_removed", name)


def resolve(conn, value: str) -> Tuple[str, Optional[str]]:
    """Map what the user typed to (command, registered-name-or-None).

    `--adapter` keeps accepting a raw command template — that is how bevis has
    always worked and nothing about it is deprecated. A registered NAME is a
    shorthand for one, resolved here, and the resolved command is what gets
    recorded on the run, so the evidence names what actually executed rather
    than the alias it was reached by.

    Only a bare identifier is looked up. Anything with a space, a slash or a
    shell character cannot collide with a name, so `--adapter true` still means
    the command `true` on a board where nobody registered an adapter called
    `true`.
    """
    text = (value or "").strip()
    if NAME_RE.match(text) and has_registry(conn):
        row = get(conn, text)
        if row:
            return row["cmd"], text
    return value, None
