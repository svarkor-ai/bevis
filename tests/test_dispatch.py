"""The dispatcher — and the thing it is forbidden from doing.

`bevis run` claims work, launches an adapter, and then hands the question of
success to the job's checks. An adapter that exits 0 has proved that it did not
crash. Nothing more.
"""
from __future__ import annotations

import pytest

from bevis import core
from bevis.db import connect
from bevis.dispatch import dispatch, render_adapter
from bevis.errors import Refusal, UsageError
from bevis.model import parse_ts

ECHO_ADAPTER = "bash -c 'echo did the work'"


def test_adapter_exit_zero_alone_does_not_close_a_job(conn, db_path):
    job = core.create_job(conn, "unprovable", "somebody says it is fine")
    [result] = dispatch(db_path, ECHO_ADAPTER)
    assert result["outcome"] == "blocked"
    assert "no checks defined" in result["detail"]
    row = core.get_job(conn, job["id"])
    assert row["status"] == "blocked"
    assert row["verify_cmd"] is None


def test_a_job_with_a_passing_check_is_closed_with_that_evidence(conn, db_path):
    job = core.create_job(conn, "provable", "the marker file exists")
    core.add_check(conn, job["id"], "unit", "echo 2 passed", blocking=True)
    [result] = dispatch(db_path, ECHO_ADAPTER)
    assert result["outcome"] == "closed"
    row = core.get_job(conn, job["id"])
    assert row["status"] == "closed"
    assert row["verify_exit"] == 0
    assert "2 passed" in row["verify_output"]
    assert "echo 2 passed" in row["verify_cmd"]


def test_a_failing_check_blocks_instead_of_closing(conn, db_path):
    job = core.create_job(conn, "broken", "tests pass")
    core.add_check(conn, job["id"], "unit", "echo 1 failed; exit 1", blocking=True)
    [result] = dispatch(db_path, ECHO_ADAPTER)
    assert result["outcome"] == "blocked"
    row = core.get_job(conn, job["id"])
    assert row["status"] == "blocked"
    assert "unit" in row["blocked_reason"]
    assert row["verify_cmd"] is None


def test_an_adapter_that_fails_marks_the_job_failed(conn, db_path):
    job = core.create_job(conn, "crashy", "bar")
    core.add_check(conn, job["id"], "unit", "echo passed")
    [result] = dispatch(db_path, "bash -c 'echo broke >&2; exit 3'")
    assert result["outcome"] == "failed"
    row = core.get_job(conn, job["id"])
    assert row["status"] == "failed"
    # the checks are NOT consulted: the attempt did not finish
    assert core.list_checks(conn, job["id"])[0]["last_exit"] is None


def test_the_run_is_recorded_as_a_row_with_both_streams(conn, db_path):
    core.create_job(conn, "loud", "bar")
    dispatch(db_path, "bash -c 'echo to-stdout; echo to-stderr >&2'")
    [run] = core.job_runs(conn, 1)
    assert run["exit_code"] == 0
    assert "to-stdout" in run["stdout"]
    assert "to-stderr" in run["stderr"]
    assert run["started_at"] and run["finished_at"]


def test_the_dispatcher_only_picks_ready_jobs(conn, db_path):
    blocker = core.create_job(conn, "first", "bar")
    core.create_job(conn, "second", "bar", after=[blocker["id"]])
    for ref in (blocker["id"], 2):
        core.add_check(conn, ref, "unit", "echo ok", blocking=True)
    results = dispatch(db_path, ECHO_ADAPTER, max_jobs=1)
    assert [r["job"] for r in results] == ["1"]
    # once the blocker is closed, the dependent becomes dispatchable
    results = dispatch(db_path, ECHO_ADAPTER)
    assert [r["job"] for r in results] == ["2"]


def test_placeholders_are_substituted_and_shell_quoted(conn, db_path):
    core.create_job(conn, "title with $(whoami) and ; semicolons",
                    "the bar", description="desc")
    core.add_check(conn, 1, "unit", "echo ok")
    dispatch(db_path, "printf '%s' {title}")
    [run] = core.job_runs(conn, 1)
    # the dangerous title arrived as data, not as a command
    assert run["stdout"] == "title with $(whoami) and ; semicolons"


def test_a_placeholder_inside_quotes_is_refused(conn, db_path):
    # This template looks fine and works until a title contains a quote or a
    # semicolon, at which point the value escapes into the command. bevis will
    # not render it at all.
    with pytest.raises(UsageError) as excinfo:
        render_adapter("bash -c 'echo {title}'", {"title": "x", "id": 1,
                                                  "display_id": "1"})
    assert "inside quotes" in str(excinfo.value)
    assert "BEVIS_JOB_TITLE" in str(excinfo.value)


def test_braces_that_are_not_placeholders_are_left_alone():
    rendered = render_adapter("awk '{print $1}' --id {id}",
                              {"id": 4, "display_id": "4", "title": "t",
                               "description": "", "acceptance": "a", "assignee": ""})
    assert rendered == "awk '{print $1}' --id 4"


def test_the_adapter_gets_the_job_in_its_environment(conn, db_path):
    core.create_job(conn, "envjob", "bar is here", description="d", assignee="me")
    core.add_check(conn, 1, "unit", "echo ok")
    dispatch(db_path, 'bash -c \'echo "$BEVIS_JOB_ID|$BEVIS_JOB_ACCEPTANCE|$BEVIS_SLOT"\'')
    [run] = core.job_runs(conn, 1)
    assert run["stdout"].strip() == "1|bar is here|0"


def test_one_job_per_slot_never_two(conn, db_path):
    for index in range(6):
        job = core.create_job(conn, "job %d" % index, "bar")
        core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    results = dispatch(db_path, "bash -c 'sleep 0.2; echo worked'", slots=3)
    assert len(results) == 6
    assert all(r["outcome"] == "closed" for r in results)

    runs = list(connect(db_path).execute("SELECT * FROM job_run ORDER BY id"))
    # every job ran exactly once...
    assert sorted(r["job_id"] for r in runs) == [1, 2, 3, 4, 5, 6]
    # ...and no slot ever had two runs overlapping in time
    by_slot = {}
    for run in runs:
        by_slot.setdefault(run["slot"], []).append(
            (parse_ts(run["started_at"]), parse_ts(run["finished_at"])))
    for slot, intervals in by_slot.items():
        intervals.sort()
        for (_, end), (start, _) in zip(intervals, intervals[1:]):
            assert end <= start, "slot %d ran two jobs at once" % slot
    # and the parallelism was real: 3 slots were used
    assert len(by_slot) == 3


def test_max_jobs_bounds_the_drain(conn, db_path):
    for index in range(4):
        job = core.create_job(conn, "job %d" % index, "bar")
        core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    results = dispatch(db_path, ECHO_ADAPTER, max_jobs=2)
    assert len(results) == 2
    assert len(core.list_jobs(conn, status="open")) == 2


def test_dispatching_an_empty_board_is_not_an_error(db_path):
    assert dispatch(db_path, ECHO_ADAPTER) == []


def test_the_dispatcher_cannot_overrule_a_refusal(conn, db_path, monkeypatch):
    # close_job is the authority even over the dispatcher. If it refuses after
    # the checks have passed, the job is blocked with the refusal recorded --
    # the dispatcher has no path that closes a job around it.
    core.create_job(conn, "job", "bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    import bevis.dispatch as dispatch_module

    def refuse(*_args, **_kwargs):
        raise Refusal("a later rule said no")

    monkeypatch.setattr(dispatch_module.core, "close_job", refuse)
    [result] = dispatch(db_path, ECHO_ADAPTER)
    assert result["outcome"] == "blocked"
    assert "a later rule said no" in result["detail"]
    row = core.get_job(conn, 1)
    assert row["status"] == "blocked"
    assert row["verify_cmd"] is None


def test_cli_run_reports_each_outcome(cli, conn):
    job = core.create_job(conn, "provable", "bar")
    core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    code, out, _ = cli("run", "--adapter", ECHO_ADAPTER)
    assert code == 0
    assert "closed" in out


def test_a_bad_template_is_refused_before_any_job_is_claimed(conn, db_path):
    core.create_job(conn, "untouched", "bar")
    with pytest.raises(UsageError):
        dispatch(db_path, "bash -c 'echo {title}'")
    assert core.get_job(conn, 1)["status"] == "open"


def test_a_crash_mid_job_leaves_it_claimed_not_lost(conn, db_path, monkeypatch):
    core.create_job(conn, "doomed", "bar")
    import bevis.dispatch as dispatch_module

    def explode(*_args, **_kwargs):
        raise RuntimeError("worker died")

    monkeypatch.setattr(dispatch_module, "process_job", explode)
    [result] = dispatch(db_path, ECHO_ADAPTER)
    assert result["outcome"] == "error"
    # Not failed, not closed, not silently dropped: still held, and reclaimable.
    row = core.get_job(conn, 1)
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None
    assert core.reclaim(conn, stale="0s")[0]["status"] == "open"
