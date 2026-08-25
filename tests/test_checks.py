"""Checks — the borrowed idea that makes bevis more than a task list.

A check is a command attached to a job whose outcome is a row in the database.
A BLOCKING check that has failed stops the job being claimed or closed, and
stops everything downstream of it being ready. The failure survives the process
that discovered it, which is the whole difference between a gate and a warning.
"""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import NotFound, Refusal, UsageError
from bevis.model import EXIT_REFUSED


@pytest.fixture()
def job_with_failing_check(conn):
    job = core.create_job(conn, "shipit", "lint is clean")
    core.add_check(conn, job["id"], "lint", "echo lint failure; exit 1", blocking=True)
    core.run_checks(conn, job["id"])
    return job


def test_check_outcome_is_a_durable_row(conn, job):
    core.add_check(conn, job["id"], "unit", "echo 3 passed")
    [result] = core.run_checks(conn, job["id"])
    assert result["last_exit"] == 0
    assert "3 passed" in result["last_output"]
    assert result["last_run_at"]
    # and it is still there for a different connection later
    stored = core.list_checks(conn, job["id"])[0]
    assert stored["last_exit"] == 0


def test_a_check_needs_a_command(conn, job):
    with pytest.raises(UsageError):
        core.add_check(conn, job["id"], "empty", "")


def test_duplicate_check_names_are_refused(conn, job):
    core.add_check(conn, job["id"], "lint", "true")
    with pytest.raises(UsageError):
        core.add_check(conn, job["id"], "lint", "false")


def test_failing_blocking_check_makes_the_job_unready(conn, job_with_failing_check):
    row = core.get_job(conn, job_with_failing_check["id"])
    ok, reason = core.readiness(conn, row)
    assert ok is False
    assert "lint" in reason
    assert job_with_failing_check["id"] not in [j["id"] for j in core.ready_jobs(conn)]


def test_failing_blocking_check_makes_the_job_unclosable(conn, job_with_failing_check):
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job_with_failing_check["id"], "echo shipped anyway")
    assert "blocking check 'lint' failed" in str(excinfo.value)
    assert core.get_job(conn, job_with_failing_check["id"])["status"] == "open"


def test_failing_blocking_check_makes_the_job_unclaimable(conn, job_with_failing_check):
    with pytest.raises(Refusal):
        core.claim(conn, job_with_failing_check["id"])


def test_fixing_the_check_restores_readiness_and_closability(conn, job_with_failing_check):
    job_id = job_with_failing_check["id"]
    core.remove_check(conn, job_id, "lint")
    core.add_check(conn, job_id, "lint", "echo lint clean", blocking=True)
    core.run_checks(conn, job_id)
    ok, reason = core.readiness(conn, core.get_job(conn, job_id))
    assert ok, reason
    closed = core.close_by_running(conn, job_id, "echo shipped")
    assert closed["status"] == "closed"


def test_an_advisory_check_reports_but_does_not_gate(conn, job):
    core.add_check(conn, job["id"], "typos", "echo two typos; exit 1", blocking=False)
    core.run_checks(conn, job["id"])
    ok, _ = core.readiness(conn, core.get_job(conn, job["id"]))
    assert ok is True
    assert core.close_by_running(conn, job["id"], "echo shipped")["status"] == "closed"


def test_a_blocking_check_that_never_ran_blocks_the_close(conn, job):
    # Unproven is not the same as passing. A check nobody ran is a claim, and
    # claims are what this tool exists to refuse.
    core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "echo done")
    assert "never been run" in str(excinfo.value)
    core.run_checks(conn, job["id"])
    assert core.close_by_running(conn, job["id"], "echo done")["status"] == "closed"


def test_a_blocking_check_that_never_ran_does_not_block_readiness(conn, job):
    # The checks usually run AFTER the work. If an unrun check made a job
    # unready, nothing would ever start.
    core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    ok, reason = core.readiness(conn, core.get_job(conn, job["id"]))
    assert ok, reason


def test_running_a_missing_check_is_a_404(conn, job):
    with pytest.raises(NotFound):
        core.run_checks(conn, job["id"], name="nope")


def test_cli_check_run_exits_nonzero_when_a_check_fails(cli, conn, job):
    core.add_check(conn, job["id"], "unit", "echo boom; exit 2", blocking=True)
    code, out, _ = cli("check", "run", str(job["id"]))
    assert code == EXIT_REFUSED
    assert "exit=2" in out


def test_check_command_sees_the_job_in_its_environment(conn, job):
    core.add_check(conn, job["id"], "env", "echo job=$BEVIS_JOB_ID name=$BEVIS_CHECK_NAME")
    [result] = core.run_checks(conn, job["id"])
    assert "job=%d" % job["id"] in result["last_output"]
    assert "name=env" in result["last_output"]
