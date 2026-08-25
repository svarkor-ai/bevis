"""Readiness: what may be worked on right now, and why not otherwise."""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import Refusal


def test_a_plain_open_job_with_a_bar_is_ready(conn, job):
    assert [j["id"] for j in core.ready_jobs(conn)] == [job["id"]]


def test_a_dependent_stays_unready_while_its_blocker_is_open(conn):
    blocker = core.create_job(conn, "schema migration", "migration applied")
    dependent = core.create_job(conn, "backfill", "rows backfilled",
                                after=[blocker["id"]])
    ready = [j["id"] for j in core.ready_jobs(conn)]
    assert blocker["id"] in ready
    assert dependent["id"] not in ready

    ok, reason = core.readiness(conn, core.get_job(conn, dependent["id"]))
    assert ok is False
    assert "waiting on job %d" % blocker["id"] in reason


def test_the_dependent_becomes_ready_when_the_blocker_closes(conn):
    blocker = core.create_job(conn, "migration", "applied")
    dependent = core.create_job(conn, "backfill", "done", after=[blocker["id"]])
    core.close_by_running(conn, blocker["id"], "echo migrated")
    assert dependent["id"] in [j["id"] for j in core.ready_jobs(conn)]


def test_a_blocker_that_merely_failed_does_not_release_the_dependent(conn):
    blocker = core.create_job(conn, "migration", "applied")
    dependent = core.create_job(conn, "backfill", "done", after=[blocker["id"]])
    core.set_status(conn, blocker["id"], "failed")
    assert dependent["id"] not in [j["id"] for j in core.ready_jobs(conn)]


def test_a_failing_blocking_check_on_a_blocker_stops_the_dependent(conn):
    blocker = core.create_job(conn, "migration", "applied")
    dependent = core.create_job(conn, "backfill", "done", after=[blocker["id"]])
    core.add_check(conn, blocker["id"], "schema", "echo drifted; exit 1", blocking=True)
    core.run_checks(conn, blocker["id"])
    ok, reason = core.readiness(conn, core.get_job(conn, dependent["id"]))
    assert ok is False
    assert "upstream job" in reason and "schema" in reason


def test_a_failing_blocking_check_on_the_parent_stops_the_children(conn):
    parent = core.create_job(conn, "epic", "all steps done")
    child = core.create_job(conn, "T1: step", "step done", parent=parent["id"])
    core.add_check(conn, parent["id"], "budget", "echo over budget; exit 1",
                   blocking=True)
    core.run_checks(conn, parent["id"])
    ok, reason = core.readiness(conn, core.get_job(conn, child["id"]))
    assert ok is False
    assert "budget" in reason
    assert core.ready_jobs(conn) == []


def test_check_failure_propagates_transitively(conn):
    root = core.create_job(conn, "root", "bar")
    middle = core.create_job(conn, "middle", "bar", after=[root["id"]])
    leaf = core.create_job(conn, "leaf", "bar", after=[middle["id"]])
    core.add_check(conn, root["id"], "gate", "exit 1", blocking=True)
    core.run_checks(conn, root["id"])
    ok, reason = core.readiness(conn, core.get_job(conn, leaf["id"]))
    assert ok is False
    assert "upstream" in reason


def test_a_parent_does_not_block_its_own_children(conn):
    # Decomposition is not sequencing: the steps of an open epic are exactly
    # what should be ready.
    parent = core.create_job(conn, "epic", "all steps done")
    child = core.create_job(conn, "T1: step", "step done", parent=parent["id"])
    assert child["id"] in [j["id"] for j in core.ready_jobs(conn)]


def test_a_parent_with_unfinished_children_is_not_ready(conn):
    # `ready` and `close` must agree: offering a dispatcher a job whose close
    # would be refused just burns a slot.
    parent = core.create_job(conn, "epic", "all steps done")
    child = core.create_job(conn, "T1: step", "step done", parent=parent["id"])
    assert [j["id"] for j in core.ready_jobs(conn)] == [child["id"]]
    ok, reason = core.readiness(conn, core.get_job(conn, parent["id"]))
    assert ok is False and reason == "1 unfinished child job(s)"
    core.close_by_running(conn, child["id"], "echo step done")
    assert parent["id"] in [j["id"] for j in core.ready_jobs(conn)]


def test_a_parent_cannot_close_while_a_child_is_open(conn):
    parent = core.create_job(conn, "epic", "all steps done")
    core.create_job(conn, "T1: step", "step done", parent=parent["id"])
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, parent["id"], "echo epic done")
    assert "unfinished child" in str(excinfo.value)


def test_a_parent_closes_once_its_children_are_closed(conn):
    parent = core.create_job(conn, "epic", "all steps done")
    child = core.create_job(conn, "T1: step", "step done", parent=parent["id"])
    core.close_by_running(conn, child["id"], "echo step done")
    assert core.close_by_running(conn, parent["id"],
                                 "echo epic done")["status"] == "closed"


def test_non_open_jobs_are_never_ready(conn, job):
    core.set_status(conn, job["id"], "blocked", reason="waiting on a human")
    assert core.ready_jobs(conn) == []
    ok, reason = core.readiness(conn, core.get_job(conn, job["id"]))
    assert ok is False and "status is blocked" in reason


def test_a_job_whose_bar_was_erased_in_the_database_is_not_ready(conn, job):
    # bevis cannot stop somebody editing the SQLite file by hand, but it can
    # refuse to dispatch what it finds there.
    conn.execute("UPDATE job SET acceptance='' WHERE id=?", (job["id"],))
    ok, reason = core.readiness(conn, core.get_job(conn, job["id"]))
    assert ok is False and reason == "no acceptance bar"


def test_cli_ready_lists_and_excludes(cli, conn):
    core.create_job(conn, "ready one", "bar")
    blocked = core.create_job(conn, "blocked one", "bar")
    core.add_check(conn, blocked["id"], "gate", "exit 1", blocking=True)
    core.run_checks(conn, blocked["id"])
    code, out, _ = cli("ready")
    assert code == 0
    assert "ready one" in out
    assert "blocked one" not in out
