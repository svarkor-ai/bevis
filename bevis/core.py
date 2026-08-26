"""The rules. Everything that can refuse lives here, so there is exactly one
copy of each rule and the CLI and the HTTP API cannot disagree about it.

The invariant this file exists to defend:

    A job cannot reach `closed` without a command, an exit code of 0, and the
    output that command produced.

There is no flag, no environment variable and no "force" argument that turns
that off. If you find one, it is a bug, and it is the only kind of bug in this
project that is worth a CVE.

No function in this module calls a language model. Where you might expect one —
deciding whether work is "good enough" — bevis runs a command and reads the
exit code instead. That is the entire product.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from typing import List, Optional, Tuple

from .db import display_id, get_job, has_negative_control, log_event, resolve_id
from .errors import NotFound, Refusal, UsageError
from .model import (
    DONE_STATUSES,
    GATED_STATUSES,
    check_transition,
    now_ts,
    parse_duration,
    parse_ts,
    validate_status,
)

#: Captured output is stored in full up to this many characters. Beyond it the
#: middle is dropped and the cut is stated in the stored text — a silent
#: truncation would be evidence that lies about its own completeness.
MAX_STORED_OUTPUT = 64_000

#: Default wall-clock ceiling for any command bevis runs on your behalf.
DEFAULT_TIMEOUT = 900

#: What a POSIX shell returns when it could not run the command at all: 126
#: "found but not executable", 127 "not found". A negative control that came
#: back with one of these did not fail. It never started, which is the DOCTRINE
#: 2 defect — a checker that errored out and was read as a clean result —
#: wearing the costume of a gate that worked.
CONTROL_DID_NOT_RUN = (126, 127)


# ── The vacuity lexicon ──────────────────────────────────────────────────────
# A command that exits 0 having examined NOTHING has not verified anything. It
# returned success because there was nothing there to disagree with.
#
# Every pattern below is on one side of a single distinction, and the whole
# calibration of this lexicon rests on it:
#
#     zero SUBJECTS  — "Ran 0 tests", "collected 0 items", "no files to check"
#                      -> the run measured nothing. Refused.
#     zero DEFECTS   — "0 errors", "0 leaks found", "0 tests failed", "(0 rows)"
#                      -> the run measured something and found it clean. This is
#                         the answer you WANTED, and refusing it would make the
#                         rule worse than useless.
#
# The shapes that read both ways — "no matches", "no files matched", "(0 rows)",
# "nothing to commit" — are deliberately absent. Each of them is the passing
# output of some real check (a secret scan finds no matches; an integrity query
# returns no orphan rows), and a lexicon that refused them would refuse real
# evidence. tests/test_vacuity.py holds both halves of that corpus.
_VACUOUS_PHRASES = (
    # unittest, pytest, jest, rspec: the runner ran and collected nothing.
    re.compile(r"(?i)\bran 0 tests\b"),
    re.compile(r"(?i)\bno tests (?:ran|were run|executed)\b"),
    re.compile(r"(?i)\bno tests (?:were )?found\b"),
    re.compile(r"(?i)\bcollected 0 items\b"),
    # The negative lookahead is the entire difference between "0 tests ran"
    # (nothing was measured) and "0 tests failed" (everything was, and passed).
    re.compile(r"(?i)(?<![\d.,])0 (?:tests?|test cases?|examples?|specs?|"
               r"scenarios?|assertions?)\b"
               r"(?!\s+(?:failed|failing|failures?|errors?|issues?|problems?|"
               r"warnings?|skipped|remaining))"),
    re.compile(r"(?i)(?<![\d.,])0 pass(?:ed|ing)\b"),
    # `go test` on a package with no _test.go files. Bracketed and literal, so
    # there is no honest sentence it can be a fragment of.
    re.compile(r"(?i)\[no test files\]"),
    # Scanners and linters, in both word orders.
    re.compile(r"(?i)(?<![\d.,])0 (?:files?|matches|items?|records?|lines?|paths?)"
               r" (?:were )?(?:scanned|checked|searched|examined|inspected|"
               r"processed|analysed|analyzed|linted|compared)\b"),
    re.compile(r"(?i)\b(?:scanned|checked|searched|examined|inspected|processed|"
               r"analysed|analyzed|linted|compared)\s+0 "
               r"(?:files?|matches|items?|records?|lines?|paths?)\b"),
    # "no files to check" is a tool saying it was handed no input. "no files
    # matched" and "no files found" are deliberately NOT here: those are what a
    # check asserting the ABSENCE of something prints when it passes.
    re.compile(r"(?i)\bno (?:input )?(?:\w+ )?files? (?:to (?:check|scan|lint|"
               r"format|process|test|analyse|analyze)\b|given\b|specified\b|"
               r"provided\b)"),
    re.compile(r"(?i)\bnothing to (?:check|scan|verify|test|analyse|analyze)\b"),
)

#: Counter-evidence: somewhere in the same output, a NON-ZERO count of subjects.
#: A four-thousand-line build log that mentions one empty sub-run and also says
#: "312 passed" measured plenty, and refusing it would be a false accusation.
#: This is the second-order half of the calibration, and it is what keeps the
#: lexicon usable against real logs rather than only against toy ones.
_MEASURED_SOMETHING = re.compile(
    r"(?i)(?<![\d.,])[1-9][\d,]*\s+(?:tests?|test cases?|examples?|specs?|"
    r"scenarios?|assertions?|checks?|items?|files?|matches|results?|rows?|"
    r"records?|lines?|passed|passing|selected|collected)\b")


def first_vacuity_needle(text: str) -> Optional[str]:
    """The literal "this measured nothing" phrase found in `text`, or None.

    It returns what it actually matched, not a category name, because the whole
    refusal rests on quoting the output back at the person reading it.


    Deliberately separate from vacuity_problem(): the counter-evidence rule
    below can mask a badly written pattern, so the lexicon is testable on its
    own, in both directions, without the rescue hiding a miscalibration.
    """
    for pattern in _VACUOUS_PHRASES:
        match = pattern.search(text or "")
        if match:
            return match.group(0).strip()
    return None


def measured_something(text: str) -> Optional[str]:
    """A non-zero count of subjects somewhere in the same output, or None."""
    match = _MEASURED_SOMETHING.search(text or "")
    return match.group(0).strip() if match else None


def vacuity_problem(text: str) -> Optional[str]:
    """Name what makes this output vacuous, or None if it measured something."""
    needle = first_vacuity_needle(text)
    if not needle or measured_something(text):
        return None
    return needle


# ── Small helpers ────────────────────────────────────────────────────────────
def default_actor() -> str:
    """$BEVIS_ACTOR, else the OS login name. Never empty: `verified` is only
    meaningful if we know who closed the job, so provenance is not optional."""
    actor = (os.environ.get("BEVIS_ACTOR") or "").strip()
    if actor:
        return actor
    try:
        import getpass

        return getpass.getuser()
    except Exception:  # pragma: no cover - exotic environments only
        return "unknown"


def truncate(text: str, limit: int = MAX_STORED_OUTPUT) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    dropped = len(text) - limit
    return "%s\n... [bevis truncated %d characters] ...\n%s" % (
        text[:head], dropped, text[-tail:],
    )


def run_command(cmd: str, timeout: int = DEFAULT_TIMEOUT, env: Optional[dict] = None,
                cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run a shell command and report (exit, stdout, stderr).

    A timeout is a failure with exit code 124 (the `timeout(1)` convention) and
    the partial output is kept: a hung verification is evidence of a problem,
    not an excuse to record nothing.
    """
    merged = dict(os.environ)
    merged.update(env or {})
    try:
        proc = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout, env=merged, cwd=cwd,
        )
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        return 124, out, err + "\n[bevis] command exceeded timeout of %ds" % timeout


def combined(stdout: str, stderr: str) -> str:
    """Evidence is what a human would have seen in the terminal: both streams,
    in one block, labelled when both are non-empty."""
    out, err = (stdout or "").rstrip("\n"), (stderr or "").rstrip("\n")
    if out and err:
        return "%s\n[stderr]\n%s" % (out, err)
    return out or err


def job_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    data = dict(row)
    data["display_id"] = display_id(conn, int(row["id"]), row["parent_id"])
    data["blockers"] = [
        int(r["blocker_id"])
        for r in conn.execute(
            "SELECT blocker_id FROM job_dep WHERE job_id=? ORDER BY blocker_id",
            (int(row["id"]),),
        )
    ]
    ok, reason = readiness(conn, row)
    data["ready"] = ok
    data["not_ready_reason"] = reason
    return data


# ── Create ───────────────────────────────────────────────────────────────────
def create_job(conn, title, acceptance, description="", parent=None, after=(),
               assignee="", actor="") -> dict:
    """Create a job.

    `acceptance` — the bar, in prose, stating what must be true for this job to
    be done — is REQUIRED. A job with no bar cannot exist, because a job with no
    bar cannot be verified, and an unverifiable job is a wish.

    `after` blockers must already exist, which is what makes the dependency
    graph acyclic by construction rather than by a cycle check.

    `parent` is accepted ONLY here. There is no re-parenting anywhere in bevis:
    a plan's shape is a decision you make when you write the plan, and quietly
    moving a finished child under a different epic rewrites history.
    """
    title = (title or "").strip()
    acceptance = (acceptance or "").strip()
    if not title:
        raise UsageError("title is required")
    if not acceptance:
        raise UsageError(
            "acceptance is required — state, in prose, what must be true for this "
            "job to be done. A job with no bar cannot be created."
        )
    parent_id = None
    if parent not in (None, ""):
        parent_id = resolve_id(conn, parent)  # raises NotFound on a dangling parent
    blockers = [resolve_id(conn, ref) for ref in (after or ())]

    ts = now_ts()
    actor = actor or default_actor()
    cur = conn.execute(
        "INSERT INTO job (parent_id, title, description, status, acceptance, "
        "assignee, created_at, updated_at) VALUES (?,?,?,'open',?,?,?,?)",
        (parent_id, title, (description or "").strip(), acceptance,
         (assignee or "").strip(), ts, ts),
    )
    job_id = int(cur.lastrowid)
    # Blockers must already exist, and a job is always created after them, so
    # every dependency edge points at a lower id. The graph cannot contain a
    # cycle — not by a check, but by construction.
    for blocker_id in blockers:
        conn.execute(
            "INSERT OR IGNORE INTO job_dep (job_id, blocker_id) VALUES (?,?)",
            (job_id, blocker_id),
        )
    log_event(conn, job_id, actor, "created", title)
    return job_dict(conn, get_job(conn, job_id))


def update_job(conn, ref, actor="", **fields) -> dict:
    """Edit the prose fields of a job. Refuses `parent_id` by name.

    The refusal is explicit rather than an ignored keyword, because silently
    dropping a field the caller believed was applied is how a board starts
    lying to the people reading it.
    """
    row = get_job(conn, ref)
    if "parent_id" in fields or "parent" in fields:
        raise Refusal(
            "parent_id is settable only at create time; bevis has no re-parenting. "
            "Create the job under the right parent, or reference it from the parent."
        )
    editable = ("title", "description", "acceptance", "assignee")
    unknown = sorted(set(fields) - set(editable))
    if unknown:
        raise UsageError(
            "cannot update %s — updatable fields are: %s"
            % (", ".join(unknown), ", ".join(editable))
        )
    sets, values = [], []
    for key in editable:
        if key in fields and fields[key] is not None:
            value = str(fields[key]).strip()
            if key in ("title", "acceptance") and not value:
                raise UsageError("%s cannot be emptied" % key)
            sets.append("%s=?" % key)
            values.append(value)
    if not sets:
        raise UsageError("nothing to update")
    values.extend([now_ts(), int(row["id"])])
    conn.execute("UPDATE job SET %s, updated_at=? WHERE id=?" % ", ".join(sets), values)
    log_event(conn, int(row["id"]), actor or default_actor(), "updated",
              ", ".join(sorted(k for k in fields)))
    return job_dict(conn, get_job(conn, int(row["id"])))


# ── Checks ───────────────────────────────────────────────────────────────────
def add_check(conn, ref, name, cmd, blocking=False, actor="") -> dict:
    row = get_job(conn, ref)
    name = (name or "").strip()
    cmd = (cmd or "").strip()
    if not name:
        raise UsageError("check name is required")
    if not cmd:
        raise UsageError("check cmd is required — a check with no command checks nothing")
    try:
        conn.execute(
            "INSERT INTO job_check (job_id, name, cmd, blocking, created_at) "
            "VALUES (?,?,?,?,?)",
            (int(row["id"]), name, cmd, 1 if blocking else 0, now_ts()),
        )
    except sqlite3.IntegrityError:
        raise UsageError(
            "job %s already has a check named %r — remove it first"
            % (display_id(conn, int(row["id"]), row["parent_id"]), name)
        )
    log_event(conn, int(row["id"]), actor or default_actor(), "check_added",
              "%s%s" % (name, " (blocking)" if blocking else ""))
    return dict(conn.execute(
        "SELECT * FROM job_check WHERE job_id=? AND name=?",
        (int(row["id"]), name)).fetchone())


def remove_check(conn, ref, name, actor="") -> None:
    row = get_job(conn, ref)
    cur = conn.execute("DELETE FROM job_check WHERE job_id=? AND name=?",
                       (int(row["id"]), (name or "").strip()))
    if cur.rowcount == 0:
        raise NotFound("job %s has no check named %r" % (ref, name))
    log_event(conn, int(row["id"]), actor or default_actor(), "check_removed", name)


def list_checks(conn, ref) -> List[dict]:
    row = get_job(conn, ref)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job_check WHERE job_id=? ORDER BY id", (int(row["id"]),))]


def run_checks(conn, ref, name=None, timeout=DEFAULT_TIMEOUT, actor="") -> List[dict]:
    """Run a job's checks and store each outcome as a row.

    The outcome is written to the database, not printed. A check whose result
    exists only in a terminal scrollback cannot gate anything tomorrow.
    """
    row = get_job(conn, ref)
    job_id = int(row["id"])
    query = "SELECT * FROM job_check WHERE job_id=?"
    params: list = [job_id]
    if name:
        query += " AND name=?"
        params.append(name)
    checks = conn.execute(query + " ORDER BY id", params).fetchall()
    if not checks:
        raise NotFound("job %s has no checks%s" % (ref, " named %r" % name if name else ""))
    results = []
    for check in checks:
        exit_code, out, err = run_command(
            check["cmd"], timeout=timeout,
            env={"BEVIS_JOB_ID": str(job_id), "BEVIS_CHECK_NAME": check["name"]},
        )
        output = truncate(combined(out, err))
        conn.execute(
            "UPDATE job_check SET last_exit=?, last_output=?, last_run_at=? WHERE id=?",
            (exit_code, output, now_ts(), int(check["id"])),
        )
        log_event(conn, job_id, actor or default_actor(), "check_run",
                  "%s exit=%d" % (check["name"], exit_code))
        results.append(dict(conn.execute(
            "SELECT * FROM job_check WHERE id=?", (int(check["id"]),)).fetchone()))
    return results


def failing_blocking_checks(conn, job_id: int) -> List[sqlite3.Row]:
    """Blocking checks that RAN and did not pass. A never-run check is not a
    failure — it is unproven, which matters at close time but not at ready time."""
    return list(conn.execute(
        "SELECT * FROM job_check WHERE job_id=? AND blocking=1 "
        "AND last_exit IS NOT NULL AND last_exit!=0 ORDER BY id", (job_id,)))


def unproven_blocking_checks(conn, job_id: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM job_check WHERE job_id=? AND blocking=1 AND last_exit IS NULL "
        "ORDER BY id", (job_id,)))


def _unfinished_children(conn, job_id: int) -> List[sqlite3.Row]:
    return list(conn.execute(
        "SELECT id, parent_id, status FROM job WHERE parent_id=? AND status NOT IN "
        "(%s) ORDER BY id" % ",".join("?" * len(DONE_STATUSES)),
        (job_id, *DONE_STATUSES)))


def _upstream_ids(conn, job_id: int) -> List[int]:
    """Ancestors (parent chain) plus blockers, transitively, cycle-safe.

    Both edges propagate a failing blocking check downstream, for different
    reasons: if the epic's own gate is broken there is no point doing its steps,
    and if a blocker's gate is broken the thing you are waiting for is not
    coming.
    """
    seen, stack, out = {job_id}, [job_id], []
    while stack:
        current = stack.pop()
        row = conn.execute("SELECT parent_id FROM job WHERE id=?", (current,)).fetchone()
        neighbours = []
        if row and row["parent_id"] is not None:
            neighbours.append(int(row["parent_id"]))
        neighbours += [int(r["blocker_id"]) for r in conn.execute(
            "SELECT blocker_id FROM job_dep WHERE job_id=?", (current,))]
        for nid in neighbours:
            if nid not in seen:
                seen.add(nid)
                out.append(nid)
                stack.append(nid)
    return out


# ── Readiness ────────────────────────────────────────────────────────────────
def readiness(conn, row: sqlite3.Row) -> Tuple[bool, str]:
    """Is this job ready to be worked on, and if not, why not in one sentence."""
    job_id = int(row["id"])
    if row["status"] != "open":
        return False, "status is %s, not open" % row["status"]
    if not (row["acceptance"] or "").strip():
        return False, "no acceptance bar"
    unfinished = _unfinished_children(conn, job_id)
    if unfinished:
        # Consistency with close_job(): if closing this would be refused because
        # its steps are unfinished, dispatching a worker at it wastes a slot.
        # The work of an epic lives in its children.
        return False, "%d unfinished child job(s)" % len(unfinished)
    own = failing_blocking_checks(conn, job_id)
    if own:
        return False, "blocking check %r failed (exit %s)" % (
            own[0]["name"], own[0]["last_exit"])
    for upstream_id in _upstream_ids(conn, job_id):
        failing = failing_blocking_checks(conn, upstream_id)
        if failing:
            up = conn.execute("SELECT * FROM job WHERE id=?", (upstream_id,)).fetchone()
            return False, "blocking check %r failed (exit %s) on upstream job %s" % (
                failing[0]["name"], failing[0]["last_exit"],
                display_id(conn, upstream_id, up["parent_id"]))
    for blocker in conn.execute(
        "SELECT j.* FROM job_dep d JOIN job j ON j.id=d.blocker_id "
        "WHERE d.job_id=? ORDER BY j.id", (job_id,)
    ):
        if blocker["status"] not in DONE_STATUSES:
            return False, "waiting on job %s (status %s)" % (
                display_id(conn, int(blocker["id"]), blocker["parent_id"]),
                blocker["status"])
    return True, ""


def ready_jobs(conn) -> List[dict]:
    out = []
    for row in conn.execute("SELECT * FROM job WHERE status='open' ORDER BY id"):
        ok, _ = readiness(conn, row)
        if ok:
            out.append(job_dict(conn, row))
    return out


# ── Claim / status ───────────────────────────────────────────────────────────
def claim(conn, ref, actor="") -> dict:
    """Take a ready job. Refuses anything `ready` would not have listed."""
    row = get_job(conn, ref)
    ok, reason = readiness(conn, row)
    if not ok:
        raise Refusal("cannot claim job %s: %s" % (ref, reason))
    actor = actor or default_actor()
    ts = now_ts()
    cur = conn.execute(
        "UPDATE job SET status='claimed', claimed_by=?, claimed_at=?, updated_at=? "
        "WHERE id=? AND status='open'", (actor, ts, ts, int(row["id"])))
    if cur.rowcount != 1:
        # Someone else won the race between our read and our write.
        raise Refusal("job %s was claimed by someone else" % ref)
    log_event(conn, int(row["id"]), actor, "claimed", "")
    return job_dict(conn, get_job(conn, int(row["id"])))


def set_status(conn, ref, status, reason="", actor="") -> dict:
    """The generic status setter — which cannot write `closed` or `verified`.

    Those two are the statuses that mean something was proven. They are
    reachable only through close() and verify(), which is what stops this
    command from being a back door around the whole tool.
    """
    validate_status(status)
    if status in GATED_STATUSES:
        raise Refusal(
            "%r cannot be set directly — use `bevis %s` (it requires %s)"
            % (status, "close" if status == "closed" else "verify",
               "evidence" if status == "closed"
               else "a different actor than the one who closed the job")
        )
    row = get_job(conn, ref)
    check_transition(row["status"], status)
    if status == "blocked" and not (reason or "").strip():
        raise UsageError("--reason is required when blocking a job")
    ts = now_ts()
    conn.execute(
        "UPDATE job SET status=?, blocked_reason=?, updated_at=? WHERE id=?",
        (status, (reason or "").strip() or None, ts, int(row["id"])))
    if status == "open":
        conn.execute("UPDATE job SET claimed_by=NULL, claimed_at=NULL WHERE id=?",
                     (int(row["id"]),))
    log_event(conn, int(row["id"]), actor or default_actor(),
              "status_%s" % status, reason or "")
    return job_dict(conn, get_job(conn, int(row["id"])))


# ── The invariant ────────────────────────────────────────────────────────────
def _evidence_problems(verify_cmd, verify_exit, verify_output) -> List[str]:
    """Every missing piece, named. Not the first one — all of them, so a caller
    fixing a close does not have to discover the requirements one round trip at
    a time."""
    problems = []
    if not isinstance(verify_cmd, str) or not verify_cmd.strip():
        problems.append("verify_cmd is missing or empty (what command proves this?)")
    if verify_exit is None:
        problems.append("verify_exit is missing (what did that command exit with?)")
    elif isinstance(verify_exit, bool) or not isinstance(verify_exit, int):
        problems.append("verify_exit must be an integer, got %r" % (verify_exit,))
    elif verify_exit != 0:
        problems.append("verify_exit is %d, not 0 — a failing command is not evidence "
                        "of success" % verify_exit)
    if not isinstance(verify_output, str) or not verify_output.strip():
        problems.append("verify_output is missing or empty (a command that printed "
                        "nothing proved nothing)")
    return problems


#: The refusal a board that predates negative controls gets, naming the one
#: command that fixes it rather than letting a stranger meet a raw
#: sqlite3.OperationalError about an unknown column.
_OLD_BOARD = (
    "this database cannot store a negative control — it was created by an older "
    "bevis. Run `bevis init` again: it is idempotent and adds the columns "
    "without touching your jobs."
)


def _control_refusal(ref, verify_cmd, control_cmd, control_exit) -> Optional[str]:
    """Why this negative control does not license the close, or None.

    A negative control is a command that MUST fail. It is how you answer the one
    question the evidence cannot answer about itself: would this command have
    said anything different if the work had not been done? `--cmd true` passes.
    So does a test runner that found no tests, a scanner whose pattern never
    matches, and a checker whose failure path prints FAIL and returns 0. Run the
    same command against a case that must fail; if it passes there too, it is not
    a check, it is a constant.
    """
    if not isinstance(control_cmd, str) or not control_cmd.strip():
        return None
    if control_exit in CONTROL_DID_NOT_RUN:
        return (
            "refusing to close job %s: the negative control never ran:\n"
            "  - negative control exited %d: %s\n"
            "%d is how a shell reports a command it could not run at all. A "
            "control that failed to start has not been shown to fail — it has "
            "been shown not to exist. Fix the command."
            % (ref, control_exit, control_cmd.strip(), control_exit)
        )
    if control_exit == 0:
        return (
            "refusing to close job %s on a check that cannot fail:\n"
            "  - verify           exited 0: %s\n"
            "  - negative control exited 0: %s\n"
            "The control was supposed to fail and it passed, so this command "
            "reports success whether the work was done or not. That is not "
            "evidence, it is a constant. Fix the check, or point "
            "--negative-control at a case that must fail."
            % (ref, (verify_cmd or "").strip(), control_cmd.strip())
        )
    return None


def close_job(conn, ref, verify_cmd, verify_exit, verify_output, actor="",
              control_cmd=None, control_exit=None, control_output=None) -> dict:
    """Close a job — the only path to status `closed`.

    Refuses unless all three pieces of evidence are present and the exit code is
    zero, unless the output shows the command measured something, unless a
    negative control (if one was run) actually failed, unless every blocking
    check on the job has been run and passed, and unless the job's children are
    finished. Each refusal names what was missing.
    """
    row = get_job(conn, ref)
    job_id = int(row["id"])
    check_transition(row["status"], "closed")

    problems = _evidence_problems(verify_cmd, verify_exit, verify_output)
    if problems:
        raise Refusal(
            "refusing to close job %s without evidence:\n  - %s\n"
            "A job closes on a command that exited 0 and printed something. "
            "Use `bevis close %s --run \"<command>\"` to have bevis run it for you."
            % (ref, "\n  - ".join(problems), ref)
        )

    # Cheapest first, and it needs no subprocess: the evidence is already in
    # hand, and it can say in its own words that it measured nothing.
    vacuous = vacuity_problem(verify_output)
    if vacuous:
        raise Refusal(
            "refusing to close job %s on evidence that measured nothing:\n"
            "  - the output says %r, and nothing else in it reports a non-zero count\n"
            "A run that examined no tests, no files and no rows exited 0 because "
            "there was nothing there to disagree with. That is a constant, not a "
            "check. Point the command at the work and run it again."
            % (ref, vacuous)
        )

    if isinstance(control_cmd, str) and control_cmd.strip():
        if not has_negative_control(conn):
            raise UsageError(_OLD_BOARD)
        if control_exit is None or isinstance(control_exit, bool) \
                or not isinstance(control_exit, int):
            raise UsageError(
                "a negative control needs the exit code it produced — bevis runs "
                "the control itself, with `bevis close %s --run \"<command>\" "
                "--negative-control \"<a case that must fail>\"`" % ref)
    refusal = _control_refusal(ref, verify_cmd, control_cmd, control_exit)
    if refusal:
        raise Refusal(refusal)

    failing = failing_blocking_checks(conn, job_id)
    if failing:
        raise Refusal(
            "refusing to close job %s: blocking check %r failed (exit %s). "
            "Fix it and re-run `bevis check run %s`."
            % (ref, failing[0]["name"], failing[0]["last_exit"], ref)
        )
    unproven = unproven_blocking_checks(conn, job_id)
    if unproven:
        raise Refusal(
            "refusing to close job %s: blocking check %r has never been run. "
            "An unrun check proves nothing — run `bevis check run %s`."
            % (ref, unproven[0]["name"], ref)
        )
    open_children = _unfinished_children(conn, job_id)
    if open_children:
        names = ", ".join(
            "%s (%s)" % (display_id(conn, int(c["id"]), c["parent_id"]), c["status"])
            for c in open_children)
        raise Refusal(
            "refusing to close job %s: %d unfinished child job(s): %s. "
            "An epic is done when its steps are done."
            % (ref, len(open_children), names)
        )

    ts = now_ts()
    actor = actor or default_actor()
    fields = ("status='closed', closed_at=?, updated_at=?, closed_by=?, "
              "verify_cmd=?, verify_exit=?, verify_output=?, blocked_reason=NULL")
    values = [ts, ts, actor, verify_cmd.strip(), int(verify_exit),
              truncate(verify_output)]
    detail = "exit=0 cmd=%s" % verify_cmd.strip()
    if has_negative_control(conn):
        ran_control = isinstance(control_cmd, str) and bool(control_cmd.strip())
        fields += ", control_cmd=?, control_exit=?, control_output=?"
        values += [control_cmd.strip() if ran_control else None,
                   int(control_exit) if ran_control else None,
                   truncate(control_output or "") if ran_control else None]
        if ran_control:
            detail += " | negative control exit=%d cmd=%s" % (
                int(control_exit), control_cmd.strip())
    conn.execute("UPDATE job SET %s WHERE id=?" % fields, (*values, job_id))
    log_event(conn, job_id, actor, "closed", detail)
    return job_dict(conn, get_job(conn, job_id))


def close_by_running(conn, ref, cmd, timeout=DEFAULT_TIMEOUT, actor="",
                     negative_control=None) -> dict:
    """The strong form: bevis runs the command itself and records what happened.

    Nothing here can be talked around — the exit code is observed, not reported.
    A non-zero exit reaches close_job() unchanged and is refused there, with the
    real output attached so you can see why.

    With `negative_control`, bevis runs a SECOND command that must fail, and the
    close needs both answers: the verification passed AND the control did not.
    Three deliberate choices about how that second run happens:

    * The control runs only after the verification has already passed. A close
      that is about to be refused anyway does not get to spend a second run.
    * The control gets the SAME environment as the verification, and bevis sets
      no variable announcing which of the two it is. A command that could see it
      was the control could satisfy the gate by failing on sight of the flag,
      and would then be a check that cannot fail wearing a proof that it can.
    * bevis runs it. There is no way to hand in a control's exit code the way
      `--verify-exit` hands in a verification's, because a transcribed control
      is a claim about a claim, and the whole point of this one is observation.
    """
    row = get_job(conn, ref)
    if not (cmd or "").strip():
        raise UsageError("--run needs a command")
    control_cmd = (negative_control or "").strip() or None
    if control_cmd and not has_negative_control(conn):
        raise UsageError(_OLD_BOARD)
    env = {"BEVIS_JOB_ID": str(int(row["id"])), "BEVIS_JOB_TITLE": row["title"]}
    exit_code, out, err = run_command(cmd, timeout=timeout, env=env)
    output = combined(out, err)
    control_exit = control_output = None
    if exit_code != 0 or not output.strip():
        # Record the attempt even though the close is about to be refused: a
        # failed verification is information, and losing it wastes the run.
        log_event(conn, int(row["id"]), actor or default_actor(),
                  "close_refused", "exit=%d cmd=%s" % (exit_code, cmd))
        control_cmd = None
    elif control_cmd:
        control_exit, cout, cerr = run_command(control_cmd, timeout=timeout, env=env)
        control_output = combined(cout, cerr)
        if control_exit == 0 or control_exit in CONTROL_DID_NOT_RUN:
            log_event(conn, int(row["id"]), actor or default_actor(),
                      "close_refused", "negative control exit=%d cmd=%s"
                      % (control_exit, control_cmd))
    return close_job(conn, ref, cmd, exit_code, output, actor=actor,
                     control_cmd=control_cmd, control_exit=control_exit,
                     control_output=control_output)


def verify_job(conn, ref, actor, note="") -> dict:
    """Mark a closed job `verified` — a second pair of eyes on the evidence.

    Refuses when the actor is the one who closed it. Grading your own homework
    is the failure mode this status exists to prevent; without the actor check
    `verified` would just be `closed` with extra typing.
    """
    row = get_job(conn, ref)
    actor = (actor or "").strip()
    if not actor:
        raise UsageError("--actor is required to verify (who is confirming this?)")
    if row["status"] != "closed":
        raise Refusal(
            "cannot verify job %s: status is %s, and only a closed job can be "
            "verified" % (ref, row["status"]))
    closer = (row["closed_by"] or "").strip()
    if closer and closer.casefold() == actor.casefold():
        raise Refusal(
            "refusing to verify job %s: %r closed it, so %r cannot verify it. "
            "Verification means a DIFFERENT actor read the evidence."
            % (ref, closer, actor))
    ts = now_ts()
    conn.execute(
        "UPDATE job SET status='verified', verified_by=?, verified_at=?, updated_at=? "
        "WHERE id=?", (actor, ts, ts, int(row["id"])))
    log_event(conn, int(row["id"]), actor, "verified", note or "")
    return job_dict(conn, get_job(conn, int(row["id"])))


def reopen_job(conn, ref, reason, actor="") -> dict:
    """Undo a close. Requires a reason; `verified` can never be reopened.

    The discarded evidence is copied into the event log first. A close that
    turned out to be wrong is a thing you want to be able to read about later.
    """
    row = get_job(conn, ref)
    reason = (reason or "").strip()
    if not reason:
        raise UsageError("--reason is required to reopen a closed job")
    if row["status"] == "verified":
        raise Refusal(
            "job %s is verified; verified is terminal in bevis. File a new job "
            "that references it." % ref)
    if row["status"] != "closed":
        raise Refusal("job %s is %s, not closed — nothing to reopen"
                      % (ref, row["status"]))
    detail = ("reason=%s | discarded evidence: cmd=%s exit=%s output=%s"
              % (reason, row["verify_cmd"], row["verify_exit"],
                 truncate(row["verify_output"] or "", 2000)))
    has_control = has_negative_control(conn)
    if has_control and row["control_cmd"]:
        detail += " | discarded negative control: cmd=%s exit=%s" % (
            row["control_cmd"], row["control_exit"])
    log_event(conn, int(row["id"]), actor or default_actor(), "reopened", detail)
    ts = now_ts()
    extra = (", control_cmd=NULL, control_exit=NULL, control_output=NULL"
             if has_control else "")
    conn.execute(
        "UPDATE job SET status='open', closed_at=NULL, closed_by=NULL, verify_cmd=NULL, "
        "verify_exit=NULL, verify_output=NULL, claimed_by=NULL, claimed_at=NULL, "
        "updated_at=?%s WHERE id=?" % extra, (ts, int(row["id"])))
    return job_dict(conn, get_job(conn, int(row["id"])))


# ── Recovery ─────────────────────────────────────────────────────────────────
def reclaim(conn, stale="30m", actor="") -> List[dict]:
    """Return jobs whose worker went away to `open`.

    A crashed worker leaves its job `claimed` or `running` with nothing driving
    it. That is deliberately recoverable rather than auto-failed: bevis cannot
    tell a dead worker from a slow one, so it hands the job back and says so
    in the event log instead of inventing an outcome.
    """
    window = parse_duration(stale)
    cutoff = parse_ts(now_ts()) - window
    reclaimed = []
    for row in conn.execute(
        "SELECT * FROM job WHERE status IN ('claimed','running') "
        "AND claimed_at IS NOT NULL ORDER BY id"
    ).fetchall():
        if parse_ts(row["claimed_at"]) <= cutoff:
            conn.execute(
                "UPDATE job SET status='open', claimed_by=NULL, claimed_at=NULL, "
                "updated_at=? WHERE id=?", (now_ts(), int(row["id"])))
            log_event(conn, int(row["id"]), actor or default_actor(), "reclaimed",
                      "held by %s since %s (stale after %s)"
                      % (row["claimed_by"], row["claimed_at"], stale))
            reclaimed.append(job_dict(conn, get_job(conn, int(row["id"]))))
    return reclaimed


def list_jobs(conn, status=None, parent=None) -> List[dict]:
    query, params = "SELECT * FROM job", []
    where = []
    if status:
        validate_status(status)
        where.append("status=?")
        params.append(status)
    if parent not in (None, ""):
        where.append("parent_id=?")
        params.append(resolve_id(conn, parent))
    if where:
        query += " WHERE " + " AND ".join(where)
    return [job_dict(conn, r) for r in conn.execute(query + " ORDER BY id", params)]


def job_events(conn, ref) -> List[dict]:
    row = get_job(conn, ref)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM event WHERE job_id=? ORDER BY id", (int(row["id"]),))]


def job_runs(conn, ref) -> List[dict]:
    row = get_job(conn, ref)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM job_run WHERE job_id=? ORDER BY id", (int(row["id"]),))]
