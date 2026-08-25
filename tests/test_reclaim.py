"""Recovery: a worker that goes away must not take the job with it."""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import UsageError
from bevis.model import now_ts, parse_ts


def _age_claim(conn, job_id, minutes):
    from datetime import timedelta

    from bevis.model import TS_FORMAT

    stamp = (parse_ts(now_ts()) - timedelta(minutes=minutes)).strftime(TS_FORMAT)
    conn.execute("UPDATE job SET claimed_at=? WHERE id=?", (stamp, job_id))


def test_a_stale_claim_is_reclaimable(conn, job):
    core.claim(conn, job["id"], actor="worker-that-died")
    _age_claim(conn, job["id"], minutes=45)
    [reclaimed] = core.reclaim(conn, stale="30m")
    assert reclaimed["status"] == "open"
    assert reclaimed["claimed_by"] is None
    assert reclaimed["ready"] is True


def test_a_fresh_claim_is_left_alone(conn, job):
    core.claim(conn, job["id"], actor="worker-still-going")
    assert core.reclaim(conn, stale="30m") == []
    assert core.get_job(conn, job["id"])["status"] == "claimed"


def test_a_running_job_is_reclaimable_too(conn, job):
    core.claim(conn, job["id"], actor="worker")
    core.set_status(conn, job["id"], "running")
    _age_claim(conn, job["id"], minutes=90)
    assert len(core.reclaim(conn, stale="1h")) == 1


def test_reclaiming_says_who_held_it(conn, job):
    core.claim(conn, job["id"], actor="worker-7")
    _age_claim(conn, job["id"], minutes=45)
    core.reclaim(conn, stale="30m", actor="janitor")
    event = [e for e in core.job_events(conn, job["id"]) if e["kind"] == "reclaimed"][0]
    assert "worker-7" in event["detail"]
    assert event["actor"] == "janitor"


def test_closed_work_is_never_reclaimed(conn, job):
    core.close_by_running(conn, job["id"], "echo done")
    _age_claim(conn, job["id"], minutes=10_000)
    assert core.reclaim(conn, stale="1m") == []


def test_a_bad_duration_is_refused_not_guessed(conn):
    for bogus in ("30", "30min", "half an hour", ""):
        with pytest.raises(UsageError):
            core.reclaim(conn, stale=bogus)


def test_cli_reclaim(cli, conn, job):
    core.claim(conn, job["id"], actor="ghost")
    _age_claim(conn, job["id"], minutes=45)
    code, out, _ = cli("reclaim", "--stale", "30m")
    assert code == 0
    assert "reclaimed job 1" in out
