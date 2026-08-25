"""The status vocabulary, the transitions, and the two-actor rule on `verified`."""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import Refusal, UsageError
from bevis.model import EXIT_REFUSED, EXIT_USAGE, STATUSES, validate_status


def test_the_vocabulary_is_small_and_closed(conn, job):
    assert STATUSES == ("open", "claimed", "running", "blocked", "failed",
                        "closed", "verified")
    with pytest.raises(UsageError) as excinfo:
        validate_status("done")
    assert "unknown status 'done'" in str(excinfo.value)
    # the message tells you what IS valid
    assert "open" in str(excinfo.value) and "verified" in str(excinfo.value)


def test_an_unknown_status_is_never_stored(conn, job):
    with pytest.raises(UsageError):
        core.set_status(conn, job["id"], "in-progress")
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_list_filter_rejects_an_unknown_status(conn, job):
    # A read path, where the transition table cannot help: filtering on a status
    # that does not exist must be an error, not an empty list. An empty list
    # reads as "no such jobs" when the truth is "no such status".
    with pytest.raises(UsageError) as excinfo:
        core.list_jobs(conn, status="done")
    assert "unknown status" in str(excinfo.value)
    assert len(core.list_jobs(conn, status="open")) == 1


def test_cli_rejects_an_unknown_status_before_it_reaches_the_database(cli, job):
    with pytest.raises(SystemExit) as excinfo:
        cli("status", str(job["id"]), "done")
    assert excinfo.value.code == EXIT_USAGE


def test_illegal_transitions_are_refused_by_name(conn, job):
    core.close_by_running(conn, job["id"], "echo done")
    with pytest.raises(UsageError) as excinfo:
        core.set_status(conn, job["id"], "failed")
    assert "illegal transition closed -> failed" in str(excinfo.value)
    assert "you may go to: verified" in str(excinfo.value)


def test_blocking_a_job_requires_a_reason(conn, job):
    with pytest.raises(UsageError):
        core.set_status(conn, job["id"], "blocked")
    blocked = core.set_status(conn, job["id"], "blocked", reason="waiting on legal")
    assert blocked["blocked_reason"] == "waiting on legal"


def test_reopening_from_blocked_clears_the_claim(conn, job):
    core.claim(conn, job["id"], actor="worker")
    core.set_status(conn, job["id"], "blocked", reason="disk full")
    reopened = core.set_status(conn, job["id"], "open")
    assert reopened["claimed_by"] is None


def test_verify_requires_a_closed_job(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.verify_job(conn, job["id"], "reviewer")
    assert "only a closed job can be verified" in str(excinfo.value)


def test_verify_by_the_actor_who_closed_it_is_refused(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    with pytest.raises(Refusal) as excinfo:
        core.verify_job(conn, job["id"], "worker")
    assert "cannot verify it" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "closed"


def test_verify_ignores_case_when_comparing_actors(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="Worker")
    with pytest.raises(Refusal):
        core.verify_job(conn, job["id"], "worker")


def test_verify_by_a_different_actor_succeeds(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    verified = core.verify_job(conn, job["id"], "reviewer", note="read the log")
    assert verified["status"] == "verified"
    assert verified["verified_by"] == "reviewer"


def test_verify_needs_an_actor_at_all(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    with pytest.raises(UsageError):
        core.verify_job(conn, job["id"], "  ")


def test_verified_is_terminal(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    core.verify_job(conn, job["id"], "reviewer")
    with pytest.raises(UsageError):
        core.set_status(conn, job["id"], "open")
    with pytest.raises(Refusal) as excinfo:
        core.reopen_job(conn, job["id"], reason="changed my mind")
    assert "terminal" in str(excinfo.value)


def test_verified_cannot_be_set_directly(conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    with pytest.raises(Refusal) as excinfo:
        core.set_status(conn, job["id"], "verified")
    assert "different actor" in str(excinfo.value)


def test_cli_verify_by_the_closer_exits_1(cli, conn, job):
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    code, _, err = cli("verify", str(job["id"]), "--actor", "worker")
    assert code == EXIT_REFUSED
    assert "cannot verify it" in err


def test_reopen_requires_a_reason_and_files_the_discarded_evidence(conn, job):
    core.close_by_running(conn, job["id"], "echo bogus evidence", actor="worker")
    with pytest.raises(UsageError):
        core.reopen_job(conn, job["id"], reason="")
    reopened = core.reopen_job(conn, job["id"], reason="evidence was from the wrong branch")
    assert reopened["status"] == "open"
    assert reopened["verify_cmd"] is None
    detail = [e for e in core.job_events(conn, job["id"]) if e["kind"] == "reopened"][0]
    assert "bogus evidence" in detail["detail"]
    assert "wrong branch" in detail["detail"]


def test_the_event_log_records_who_did_what(conn, job):
    core.claim(conn, job["id"], actor="worker")
    core.close_by_running(conn, job["id"], "echo done", actor="worker")
    core.verify_job(conn, job["id"], "reviewer")
    kinds = [e["kind"] for e in core.job_events(conn, job["id"])]
    assert kinds == ["created", "claimed", "closed", "verified"]
    actors = {e["kind"]: e["actor"] for e in core.job_events(conn, job["id"])}
    assert actors["closed"] == "worker" and actors["verified"] == "reviewer"
