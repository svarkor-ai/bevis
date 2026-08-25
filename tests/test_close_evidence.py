"""THE INVARIANT.

A job cannot reach `closed` without a command, an exit code of 0, and the output
that command produced. Every test in this file is one way somebody might try to
close a job by asserting it is done, and the refusal that stops them.

If a change to bevis makes any test in this file pass for the wrong reason, the
project has lost its only real feature.
"""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import Refusal, UsageError
from bevis.model import EXIT_REFUSED


def test_close_with_no_evidence_at_all_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], None, None, None)
    message = str(excinfo.value)
    assert "verify_cmd is missing" in message
    assert "verify_exit is missing" in message
    assert "verify_output is missing" in message
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_close_with_nonzero_exit_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], "pytest", 1, "1 failed")
    assert "verify_exit is 1, not 0" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_close_with_empty_output_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], "true", 0, "   \n  ")
    assert "verify_output is missing or empty" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_close_with_empty_command_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], "   ", 0, "it worked, trust me")
    assert "verify_cmd is missing or empty" in str(excinfo.value)


def test_exit_must_be_an_integer_not_a_lookalike(conn, job):
    for bogus in ("0", 0.0, True, [0]):
        with pytest.raises(Refusal):
            core.close_job(conn, job["id"], "true", bogus, "output")
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_refusal_names_every_missing_piece_not_just_the_first(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], "", 3, "")
    message = str(excinfo.value)
    assert message.count("  - ") == 3


def test_close_with_real_evidence_succeeds_and_stores_it(conn, job):
    closed = core.close_job(conn, job["id"], "pytest -q", 0, "12 passed",
                            actor="worker")
    assert closed["status"] == "closed"
    assert closed["verify_cmd"] == "pytest -q"
    assert closed["verify_exit"] == 0
    assert closed["verify_output"] == "12 passed"
    assert closed["closed_by"] == "worker"
    assert closed["closed_at"]


def test_close_run_observes_the_exit_code_itself(conn, job):
    closed = core.close_by_running(conn, job["id"], "echo proof of work",
                                   actor="worker")
    assert closed["status"] == "closed"
    assert closed["verify_exit"] == 0
    assert "proof of work" in closed["verify_output"]
    assert closed["verify_cmd"] == "echo proof of work"


def test_close_run_with_a_failing_command_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "echo nope; exit 7")
    assert "verify_exit is 7, not 0" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_close_run_with_a_silent_command_is_refused(conn, job):
    # `true` exits 0 and prints nothing. Zero is not enough on its own: a
    # command that produced no output produced no evidence.
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "true")
    assert "verify_output is missing or empty" in str(excinfo.value)


def test_close_run_records_stderr_as_evidence_too(conn, job):
    closed = core.close_by_running(conn, job["id"], "echo warned >&2")
    assert "warned" in closed["verify_output"]


def test_a_closed_job_cannot_be_closed_again(conn, job):
    core.close_by_running(conn, job["id"], "echo done")
    with pytest.raises(UsageError) as excinfo:
        core.close_by_running(conn, job["id"], "echo done again")
    assert "already" in str(excinfo.value)


def test_cli_close_without_evidence_is_refused_with_exit_1(cli, conn, job):
    code, _, err = cli("close", str(job["id"]))
    assert code == EXIT_REFUSED
    assert "refusing to close" in err
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_cli_close_run_stores_evidence(cli, conn, job):
    code, out, _ = cli("close", str(job["id"]), "--run", "echo built and tested")
    assert code == 0
    assert "closed job" in out
    row = core.get_job(conn, job["id"])
    assert row["status"] == "closed"
    assert "built and tested" in row["verify_output"]


def test_cli_close_from_a_file_of_evidence_produced_elsewhere(cli, conn, job, tmp_path):
    evidence = tmp_path / "ci.log"
    evidence.write_text("== 41 passed in 3.10s ==\n")
    code, _, _ = cli("close", str(job["id"]), "--verify-cmd", "make test",
                     "--verify-exit", "0", "--verify-output-file", str(evidence))
    assert code == 0
    assert "41 passed" in core.get_job(conn, job["id"])["verify_output"]


def test_cli_close_from_a_file_still_refuses_a_nonzero_exit(cli, conn, job, tmp_path):
    evidence = tmp_path / "ci.log"
    evidence.write_text("FAILED test_thing\n")
    code, _, err = cli("close", str(job["id"]), "--verify-cmd", "make test",
                       "--verify-exit", "1", "--verify-output-file", str(evidence))
    assert code == EXIT_REFUSED
    assert "not 0" in err


def test_there_is_no_status_command_that_writes_closed(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.set_status(conn, job["id"], "closed")
    assert "cannot be set directly" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_evidence_output_is_truncated_loudly_not_silently(conn, job):
    huge = "x" * (core.MAX_STORED_OUTPUT + 5000)
    closed = core.close_job(conn, job["id"], "produce-lots", 0, huge)
    assert "bevis truncated" in closed["verify_output"]
    assert len(closed["verify_output"]) < len(huge)


def test_timeout_is_a_failure_not_a_pass(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], "sleep 5", timeout=1)
    assert "verify_exit is 124, not 0" in str(excinfo.value)
