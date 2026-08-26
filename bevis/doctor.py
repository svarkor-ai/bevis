"""`bevis doctor` — what is working here, what is not, and what to type next.

A new user's first hour is spent on things that have nothing to do with the
idea: the database is somewhere else, the adapter script is not executable, the
agent binary is not on PATH from the directory bevis runs in. None of those
produce a good error at the moment they bite, because they bite three commands
later.

Two rules this file follows, both borrowed from the tool it is diagnosing:

* **It fails, it does not reassure.** Every problem is a FAIL with the command
  that fixes it, and the process exits non-zero. A doctor that prints a green
  list is a status page, and a status page is the thing DOCTRINE §3 is about.
* **It never reports something it did not run.** Whether an adapter is
  *executable* can be answered by looking. Whether it *responds* cannot — that
  needs the adapter to actually be called, which is a side effect on somebody
  else's machine. So doctor calls only the adapter you name with `--adapter`,
  and reports every other one as `unproven` rather than as `ok`. Unproven is
  not the same as passing; that is the same rule bevis applies to a blocking
  check that has never been run.

There is no model call here and no network call here. Everything below is a
file-system question or a subprocess.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import __version__, adapters, core
from .db import connect
from .dispatch import adapter_env, render_adapter
from .errors import BevisError

#: A short ceiling on purpose: doctor is a diagnostic, and a diagnostic that
#: hangs for fifteen minutes is one you stop running.
PROBE_TIMEOUT = 30

#: The job handed to an adapter during a probe. It is not stored, has no id on
#: the board, and cannot be closed — an adapter that honours BEVIS_DOCTOR_PROBE
#: should print something and exit 0 without doing any work.
PROBE_JOB = {
    "id": 0,
    "display_id": "probe",
    "title": "bevis doctor probe",
    "description": "bevis doctor is checking that this adapter runs. Do no work.",
    "acceptance": "the adapter starts and exits 0; nothing is closed by a probe",
    "assignee": "",
}

OK, FAIL, NOTE, UNPROVEN = "ok", "FAIL", "note", "unproven"


def _result(section, status, detail, fix=""):
    return {"section": section, "status": status, "detail": detail, "fix": fix}


# ── The individual examinations ──────────────────────────────────────────────
def check_bevis(results: list) -> None:
    results.append(_result(
        "bevis", OK, "bevis %s on Python %d.%d.%d (%s)"
        % (__version__, *sys.version_info[:3], sys.executable)))


def check_database(db_path, results: list):
    """Return an open connection, or None if the board is not usable."""
    path = Path(db_path)
    if not path.exists():
        results.append(_result(
            "database", FAIL, "no bevis database at %s" % path,
            "run `bevis init` in the directory you want the board to live in, or "
            "point --db / $BEVIS_DB at an existing one"))
        return None
    if not os.access(path, os.W_OK):
        results.append(_result(
            "database", FAIL, "%s is not writable by this user" % path,
            "fix the file's permissions; bevis writes on every command that "
            "records anything"))
        return None
    try:
        conn = connect(path)
    except BevisError as exc:
        # connect() refuses a file that is not a SQLite database, which is what
        # a mistyped $BEVIS_DB produces. Reported, not raised: doctor is the
        # command you run when you do not know what is wrong.
        results.append(_result(
            "database", FAIL, str(exc),
            "point --db / $BEVIS_DB at a bevis board, or `bevis init` a new one"))
        return None
    try:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "job" not in tables:
            results.append(_result(
                "database", FAIL, "%s is a SQLite file but not a bevis board "
                "(no `job` table)" % path,
                "point --db / $BEVIS_DB somewhere else, or `bevis init` a new board"))
            conn.close()
            return None
        total = conn.execute("SELECT COUNT(*) FROM job").fetchone()[0]
        ready = len(core.ready_jobs(conn))
        results.append(_result(
            "database", OK, "%s — %d job(s), %d ready" % (path, total, ready)))
        if "adapter" not in tables:
            results.append(_result(
                "database", FAIL,
                "this board has no adapter registry (it was created by an older "
                "bevis)",
                "run `bevis init` again — it is idempotent and adds the table "
                "without touching your jobs"))
    except Exception:
        conn.close()                      # do not leak the handle on the way out
        raise
    return conn


def check_actor(results: list) -> None:
    actor = (os.environ.get("BEVIS_ACTOR") or "").strip()
    if actor:
        results.append(_result(
            "actor", OK, "$BEVIS_ACTOR=%s — closes will be recorded under that name"
            % actor))
        return
    results.append(_result(
        "actor", NOTE,
        "$BEVIS_ACTOR is not set, so bevis will use the OS login name %r"
        % core.default_actor(),
        "export BEVIS_ACTOR=<you> — `bevis verify` refuses the actor who closed "
        "the job, and that check is only worth anything if the names differ"))


def check_board(conn, results: list) -> None:
    """The one board condition that reliably confuses a new user.

    Counted over READY jobs, not open ones. `bevis run` only ever claims what
    `bevis ready` lists, so an open job waiting behind a blocker is not a job
    the dispatcher is about to block — saying it was would be a number that
    does not read off anything.
    """
    without = [job for job in core.ready_jobs(conn)
               if not core.list_checks(conn, job["id"])]
    if without:
        results.append(_result(
            "board", NOTE,
            "%d ready job(s) have no checks, so `bevis run` will block them "
            "instead of closing them" % len(without),
            "give each one a gate: `bevis check add <id> --name <name> "
            "--cmd <command> --blocking`"))


def _executable_problem(cmd: str):
    """Can this command line actually start? (detail, fix) or None."""
    program = adapters.program_of(cmd)
    if program is None:
        return ("the command cannot be parsed as a shell command line "
                "(unbalanced quote?)",
                "re-register it with `bevis adapter add ... --cmd '<command>'`")
    if "/" in program:
        path = Path(program).expanduser()
        if not path.exists():
            return ("%s does not exist (relative paths resolve from the directory "
                    "you run bevis in, not from the board)" % program,
                    "register an absolute path, or run bevis from the directory "
                    "that contains it")
        if not os.access(path, os.X_OK):
            return ("%s is not executable" % program, "chmod +x %s" % program)
        return None
    if shutil.which(program) is None:
        return ("%s is not on PATH from here" % program,
                "install it, or register the absolute path to it instead")
    return None


def probe(cmd: str, db_path, timeout: int = PROBE_TIMEOUT):
    """Actually run an adapter against a throwaway probe job.

    This is the literal artifact, not a stand-in for it: the same rendering, the
    same BEVIS_JOB_* environment and the same subprocess call `bevis run` makes,
    with BEVIS_DOCTOR_PROBE=1 added so an adapter that talks to a real agent can
    answer cheaply instead of doing a job's worth of work.

    Being literal has a cost worth knowing: $BEVIS_DB points at the real board,
    because a real run points there too. An adapter that writes to the board
    will write to it during a diagnostic. The probe job itself is not stored and
    has no id, so nothing here can close anything.
    """
    rendered = render_adapter(cmd, PROBE_JOB)
    env = adapter_env(PROBE_JOB, db_path, 0)
    env["BEVIS_DOCTOR_PROBE"] = "1"
    exit_code, out, err = core.run_command(rendered, timeout=timeout, env=env)
    return exit_code, core.combined(out, err)


def check_adapters(conn, results: list, db_path, probe_name=None,
                   timeout=PROBE_TIMEOUT) -> None:
    registered = adapters.list_all(conn)
    if not registered:
        results.append(_result(
            "adapters", NOTE, "no adapters are registered",
            "register one with `bevis adapter add <name> --cmd '<command>'`, then "
            "`bevis doctor --adapter <name>` to call it"))
    for row in registered:
        problem = _executable_problem(row["cmd"])
        if problem:
            results.append(_result(
                "adapters", FAIL, "%s — %s" % (row["name"], problem[0]), problem[1]))
        elif row["name"] == probe_name:
            continue                # probed below, where the real answer is
        else:
            results.append(_result(
                "adapters", UNPROVEN, "%s — %s (executable, never called)"
                % (row["name"], row["cmd"]),
                "run `bevis doctor --adapter %s` to actually call it" % row["name"]))

    if probe_name is None:
        return

    cmd, name = adapters.resolve(conn, probe_name)
    if name is None and adapters.NAME_RE.match(probe_name.strip()):
        results.append(_result(
            "adapters", FAIL, "no adapter named %r is registered" % probe_name,
            "`bevis adapter list` shows the names; register this one with "
            "`bevis adapter add %s --cmd '<command>'`" % probe_name))
        return
    label = name or cmd
    problem = _executable_problem(cmd)
    if problem:
        if name is None:            # a raw command; the loop above never saw it
            results.append(_result(
                "adapters", FAIL, "%s — %s" % (label, problem[0]), problem[1]))
        return
    try:
        exit_code, output = probe(cmd, db_path, timeout=timeout)
    except BevisError as exc:
        results.append(_result(
            "adapters", FAIL, "%s — %s" % (label, exc),
            "fix the adapter template and re-register it"))
        return
    first = next((line for line in (output or "").splitlines() if line.strip()), "")
    if exit_code == 0:
        results.append(_result(
            "adapters", OK, "%s — ran and exited 0%s"
            % (label, (": " + first[:100]) if first else " (printed nothing)")))
    elif exit_code == 124:
        results.append(_result(
            "adapters", FAIL, "%s — still running after %ds" % (label, timeout),
            "make the adapter answer a probe cheaply: it is handed "
            "BEVIS_DOCTOR_PROBE=1 and may exit 0 without doing any work"))
    else:
        results.append(_result(
            "adapters", FAIL, "%s — exited %d%s"
            % (label, exit_code, (": " + first[:100]) if first else
               " and printed nothing"),
            "run it yourself with the same environment; `bevis run` will record "
            "this as a failed job, not a closed one"))


# ── The whole examination, and how it prints ─────────────────────────────────
def diagnose(db_path, probe_name=None, timeout=PROBE_TIMEOUT) -> list:
    results: list = []
    check_bevis(results)
    conn = check_database(db_path, results)
    check_actor(results)
    if conn is not None:
        try:
            check_board(conn, results)
            # A board with no adapter table has already been reported, with the
            # one command that fixes it. Reading the registry now would only add
            # a second, less useful error on top of the useful one.
            if not any(r["section"] == "database" and r["status"] == FAIL
                       for r in results):
                check_adapters(conn, results, db_path, probe_name=probe_name,
                               timeout=timeout)
        finally:
            conn.close()
    if probe_name and not any(r["section"] == "adapters" for r in results):
        # The user asked doctor to CALL something and doctor could not get far
        # enough to try. Saying nothing would read as "fine".
        results.append(_result(
            "adapters", UNPROVEN,
            "%s — not called: the database above has to work first" % probe_name,
            "fix the database problem, then run this command again"))
    return results


def render(results: list) -> str:
    lines, section = [], None
    for item in results:
        if item["section"] != section:
            section = item["section"]
            lines.append(section)
        lines.append("  %-8s %s" % (item["status"], item["detail"]))
        if item["fix"]:
            lines.append("           -> %s" % item["fix"])
    failures = [r for r in results if r["status"] == FAIL]
    unproven = [r for r in results if r["status"] == UNPROVEN]
    lines.append("")
    if failures:
        lines.append("%d problem(s) above, each with the command that fixes it."
                     % len(failures))
    else:
        lines.append("no problems found.%s"
                     % ("" if not unproven else
                        " %d adapter(s) were never called, so doctor says nothing "
                        "about whether they answer." % len(unproven)))
    return "\n".join(lines)


def exit_code(results: list) -> int:
    """Non-zero if anything is broken. `not proven` is not broken — it is unknown,
    and doctor refuses to turn one into the other in either direction."""
    return 1 if any(r["status"] == FAIL for r in results) else 0
