"""The negative control: proving the check could have failed.

The evidence invariant answers "did a command run and exit 0". It cannot answer
the question underneath it — would that command have said anything different if
the work had NOT been done? `--cmd true` passes. So does a test runner that
found no tests, a scanner whose pattern never matches, and a checker whose
failure path prints FAIL and returns 0.

`--negative-control` is how you answer it: a second command that MUST fail. Both
answers are required, and a control that passes is a refusal, because a check
that passes either way is not a check, it is a constant.

DOCTRINE 2 said this was the one rule bevis could not enforce. It is now the
rule bevis can enforce when you ask it to.
"""
from __future__ import annotations

import pytest

from bevis import core
from bevis.db import connect
from bevis.errors import Refusal, UsageError
from bevis.model import EXIT_REFUSED

#: A checker whose failure path prints FAIL and forgets to set the exit code —
#: the defect this feature exists for, written out so a test can plant it.
BROKEN_CHECKER = (
    "#!/bin/sh\n"
    'grep -q SECRET "$1" && echo "FAIL: secret in $1"\n'
    'echo "scanned $1"\n'
)
FIXED_CHECKER = (
    "#!/bin/sh\n"
    'if grep -q SECRET "$1"; then echo "FAIL: secret in $1"; exit 1; fi\n'
    'echo "scanned $1: clean"\n'
)


@pytest.fixture()
def checker(tmp_path):
    """A scanner, a clean file and a file with a planted secret in it."""
    script = tmp_path / "leakcheck.sh"
    script.write_text(BROKEN_CHECKER)
    script.chmod(0o755)
    (tmp_path / "clean.txt").write_text("nothing to see here\n")
    (tmp_path / "planted.txt").write_text("SECRET=hunter2\n")
    return script


def test_a_control_that_also_passes_is_refused(conn, job, checker, tmp_path):
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(
            conn, job["id"], "%s %s/clean.txt" % (checker, tmp_path),
            negative_control="%s %s/planted.txt" % (checker, tmp_path))
    message = str(excinfo.value)
    assert "a check that cannot fail" in message
    assert "negative control exited 0" in message
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_the_same_close_succeeds_once_the_checker_can_fail(conn, job, checker, tmp_path):
    checker.write_text(FIXED_CHECKER)
    closed = core.close_by_running(
        conn, job["id"], "%s %s/clean.txt" % (checker, tmp_path),
        negative_control="%s %s/planted.txt" % (checker, tmp_path))
    assert closed["status"] == "closed"
    assert closed["control_exit"] == 1
    assert "planted.txt" in closed["control_cmd"]
    assert "FAIL: secret in" in closed["control_output"]


def test_a_control_that_never_ran_is_not_a_control_that_failed(conn, job):
    """127 is the shell's "command not found". A control that could not start
    has been shown not to exist, not shown to fail — which is exactly the
    DOCTRINE 2 incident (a checker that errored out and was read as clean)."""
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "echo real evidence",
                              negative_control="./no-such-control.sh")
    message = str(excinfo.value)
    assert "the negative control never ran" in message
    assert "127" in message
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_a_control_that_is_not_executable_is_refused_too(conn, job, tmp_path):
    script = tmp_path / "not-executable.sh"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o644)
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "echo real evidence",
                              negative_control=str(script))
    assert "the negative control never ran" in str(excinfo.value)


def test_the_control_is_actually_run_not_merely_recorded(conn, job, tmp_path):
    """The control has to leave a trace on the file system, or bevis never ran
    it and the whole gate is a stored string."""
    marker = tmp_path / "the-control-ran"
    closed = core.close_by_running(
        conn, job["id"], "echo real evidence",
        negative_control="touch %s; exit 1" % marker)
    assert marker.exists(), "bevis recorded a control it never executed"
    assert closed["control_exit"] == 1


def test_no_control_is_run_when_the_verification_already_failed(conn, job, tmp_path):
    """A close that is going to be refused anyway does not get to spend a second
    subprocess. The control is the expensive half; the verification decides
    whether it is worth running."""
    marker = tmp_path / "should-not-exist"
    with pytest.raises(Refusal):
        core.close_by_running(conn, job["id"], "echo nope; exit 7",
                              negative_control="touch %s; exit 1" % marker)
    assert not marker.exists()


def test_the_control_is_not_told_that_it_is_the_control(conn, job):
    """bevis sets no environment variable announcing which run is which.

    A command that could see it was the control could satisfy this gate by
    failing on sight of the flag — a check that cannot fail, wearing a proof
    that it can. So the control gets byte-identical BEVIS_* variables, and this
    test compares the two runs' own view of them rather than trusting the code
    above it."""
    closed = core.close_by_running(
        conn, job["id"], "env | grep '^BEVIS_' | sort",
        negative_control="env | grep '^BEVIS_' | sort; exit 1")
    assert closed["verify_output"] == closed["control_output"]
    assert "CONTROL" not in closed["control_output"]


def test_the_control_is_stored_on_the_job_not_folded_into_the_evidence(conn, job):
    closed = core.close_by_running(conn, job["id"], "echo real evidence",
                                   negative_control="echo planted; exit 1")
    assert closed["verify_output"] == "real evidence"
    assert closed["control_cmd"] == "echo planted; exit 1"
    assert closed["control_exit"] == 1
    assert closed["control_output"] == "planted"


def test_a_close_without_a_control_stores_no_control(conn, job):
    closed = core.close_by_running(conn, job["id"], "echo real evidence")
    assert closed["control_cmd"] is None
    assert closed["control_exit"] is None
    assert closed["control_output"] is None


def test_a_refused_control_is_recorded_in_the_event_log(conn, job, checker, tmp_path):
    with pytest.raises(Refusal):
        core.close_by_running(
            conn, job["id"], "%s %s/clean.txt" % (checker, tmp_path),
            negative_control="%s %s/planted.txt" % (checker, tmp_path))
    kinds = [(e["kind"], e["detail"]) for e in core.job_events(conn, job["id"])]
    assert any(kind == "close_refused" and "negative control" in detail
               for kind, detail in kinds), kinds


def test_a_passing_control_is_named_in_the_closed_event(conn, job):
    core.close_by_running(conn, job["id"], "echo real evidence",
                          negative_control="echo planted; exit 1")
    closed = [e for e in core.job_events(conn, job["id"]) if e["kind"] == "closed"]
    assert "negative control exit=1" in closed[-1]["detail"]


def test_reopen_files_the_control_before_discarding_it(conn, job):
    core.close_by_running(conn, job["id"], "echo real evidence",
                          negative_control="echo planted; exit 1")
    reopened = core.reopen_job(conn, job["id"], "the bar was wrong")
    assert reopened["control_cmd"] is None
    assert reopened["control_exit"] is None
    detail = [e for e in core.job_events(conn, job["id"])
              if e["kind"] == "reopened"][-1]["detail"]
    assert "discarded negative control" in detail
    assert "echo planted; exit 1" in detail


def test_close_job_on_an_old_board_refuses_a_control_rather_than_dropping_it(tmp_path):
    """close_job() is the authority, so the guard has to be there too and not
    only in close_by_running(). Without it the control would be silently
    discarded — a field the caller believed was applied, quietly dropped, which
    is the one thing this project will not do."""
    conn = _old_board(tmp_path / "old.db")
    try:
        core.create_job(conn, "old job", "the bar")
        with pytest.raises(UsageError) as excinfo:
            core.close_job(conn, 1, "make test", 0, "41 passed",
                           control_cmd="make broken-test", control_exit=1)
        assert "`bevis init` again" in str(excinfo.value)
        assert core.get_job(conn, 1)["status"] == "open"
    finally:
        conn.close()


def test_close_job_refuses_a_control_with_no_exit_code(conn, job):
    """Reachable only by calling core directly. bevis runs the control itself,
    so there is no transcribed form to trust."""
    with pytest.raises(UsageError) as excinfo:
        core.close_job(conn, job["id"], "make test", 0, "ok",
                       control_cmd="make broken-test")
    assert "needs the exit code" in str(excinfo.value)


#: A board exactly as bevis 0.1.x created it: this release's schema with the
#: three columns of this release removed. Built from the real SCHEMA rather than
#: from a copy of it, so the two cannot drift and quietly stop testing anything.
def _old_board(path):
    from bevis import db as db_module

    old_schema = db_module.SCHEMA.replace(db_module.SCHEMA[
        db_module.SCHEMA.index("  control_cmd    TEXT,"):
        db_module.SCHEMA.index("  -- Provenance of the state changes")], "")
    assert "control_cmd" not in old_schema
    conn = connect(path, create=True)
    conn.executescript(old_schema)
    return conn


def test_a_board_that_predates_negative_controls_names_the_fix(tmp_path):
    """A 0.1.x board has no control columns. The refusal names `bevis init`,
    which is idempotent, rather than letting a stranger meet a raw sqlite error
    about an unknown column."""
    conn = _old_board(tmp_path / "old.db")
    try:
        core.create_job(conn, "old job", "the bar")
        with pytest.raises(UsageError) as excinfo:
            core.close_by_running(conn, 1, "echo evidence",
                                  negative_control="false")
        assert "`bevis init` again" in str(excinfo.value)
        # ...and an ordinary close on that same old board still works, which is
        # the whole reason the columns are optional rather than required.
        assert core.close_by_running(conn, 1, "echo evidence")["status"] == "closed"
    finally:
        conn.close()


def test_init_adds_the_columns_to_an_old_board_without_touching_a_job(tmp_path):
    """The migration itself: additive, idempotent, and it must not disturb the
    evidence already on the board."""
    from bevis.db import columns, has_negative_control, init_db

    path = tmp_path / "old.db"
    conn = _old_board(path)
    try:
        core.create_job(conn, "old job", "the bar")
        core.close_by_running(conn, 1, "echo evidence from before the upgrade")
        assert not has_negative_control(conn)
        before = dict(core.get_job(conn, 1))
    finally:
        conn.close()

    init_db(path)                       # the upgrade, and it is one command

    conn = connect(path)
    try:
        assert has_negative_control(conn)
        assert {"control_cmd", "control_exit", "control_output"} <= columns(conn, "job")
        after = dict(core.get_job(conn, 1))
        assert after["status"] == "closed"
        assert after["verify_output"] == before["verify_output"]
        assert after["control_cmd"] is None
        init_db(path)                   # idempotent: running it again is a no-op
        assert core.get_job(conn, 1)["verify_output"] == before["verify_output"]
    finally:
        conn.close()


# ── The command line ─────────────────────────────────────────────────────────
def test_cli_close_with_a_control_that_passes_exits_1(cli, conn, job, checker, tmp_path):
    code, _, err = cli("close", str(job["id"]),
                       "--run", "%s %s/clean.txt" % (checker, tmp_path),
                       "--negative-control", "%s %s/planted.txt" % (checker, tmp_path))
    assert code == EXIT_REFUSED
    assert "a check that cannot fail" in err
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_cli_close_reports_the_control_that_failed(cli, conn, job):
    code, out, _ = cli("close", str(job["id"]), "--run", "echo real evidence",
                       "--negative-control", "echo planted; exit 1")
    assert code == 0
    assert "negative control exited 1, as a control must" in out


def test_cli_negative_control_without_run_is_refused(cli, conn, job, tmp_path):
    """A control bevis did not run itself is a claim about a claim, and pairing
    it with transcribed evidence would let the strong half launder the weak
    one."""
    evidence = tmp_path / "ci.log"
    evidence.write_text("41 passed\n")
    code, _, err = cli("close", str(job["id"]), "--verify-cmd", "make test",
                       "--verify-exit", "0", "--verify-output-file", str(evidence),
                       "--negative-control", "false")
    assert code == EXIT_REFUSED
    assert "--negative-control needs --run" in err
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_cli_show_prints_the_negative_control(cli, conn, job):
    cli("close", str(job["id"]), "--run", "echo real evidence",
        "--negative-control", "echo planted; exit 1")
    _, out, _ = cli("show", str(job["id"]))
    assert "negative control:" in out
    assert "cmd    echo planted; exit 1" in out
    assert "exit   1" in out
    assert "| planted" in out


def test_cli_show_says_nothing_about_a_control_that_was_never_run(cli, conn, job):
    cli("close", str(job["id"]), "--run", "echo real evidence")
    _, out, _ = cli("show", str(job["id"]))
    assert "negative control" not in out
