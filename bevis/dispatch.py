"""The dispatcher: claims ready jobs, runs an adapter, and then refuses to be
the one who decides whether the work succeeded.

This is the part people get wrong. A dispatcher that closes a job because the
worker process exited 0 has learned nothing about the work — it has learned that
the worker did not crash. bevis therefore splits the two questions:

    did the adapter run?        -> the adapter's exit code answers this
    did the work meet the bar?  -> only the job's CHECKS answer this

If a job has no checks, the dispatcher cannot close it and says so. That is not
a limitation to work around; it is the tool telling you the job was never
verifiable in the first place.

There is no model call in this file. The adapter it launches may well be an AI
agent — bevis neither knows nor cares, and holds no LLM dependency to find out.
"""
from __future__ import annotations

import re
import shlex
import threading
from typing import List, Optional

from . import core
from .db import connect, get_job, log_event
from .errors import Refusal, UsageError
from .model import now_ts

_PLACEHOLDER_RE = re.compile(r"\{(id|display_id|title|description|acceptance|assignee)\}")


def _quoted_placeholder(template: str):
    """Return the first placeholder that sits inside a quoted region, or None.

    render_adapter() shell-quotes every value it substitutes, which is only
    correct where the placeholder stands in an UNQUOTED position. Written inside
    quotes — `bash -c 'run {title}'` — the quoting nests wrong and the value can
    break out and execute. bevis refuses that template instead of producing a
    command that works right up until a job title contains a semicolon.
    """
    in_single = in_double = False
    index = 0
    while index < len(template):
        char = template[index]
        if char == "\\" and not in_single:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif char == "{" and (in_single or in_double):
            match = _PLACEHOLDER_RE.match(template, index)
            if match:
                return match.group(0)
        index += 1
    return None


def render_adapter(template: str, job: dict) -> str:
    """Substitute {id} {display_id} {title} {description} {acceptance} {assignee}.

    Three deliberate choices:

    * Only those tokens are touched. str.format() would choke on an adapter like
      `awk '{print $1}'`, so bevis replaces exactly the names it knows and leaves
      every other brace alone.
    * Every substituted value is shell-quoted. Job titles are written by humans
      and agents; a title containing `; rm -rf ~` must end up as an argument, not
      as a second command.
    * A placeholder written inside quotes is a refusal, not a warning — see
      _quoted_placeholder(). Use the BEVIS_JOB_* environment variables there.
    """
    if not (template or "").strip():
        raise UsageError("--adapter needs a command template")
    quoted = _quoted_placeholder(template)
    if quoted:
        raise UsageError(
            "placeholder %s appears inside quotes in the adapter template. bevis "
            "shell-quotes every value it substitutes, which only works in an "
            "unquoted position — nested like this a job title could break out and "
            "run as a command. Move it outside the quotes, or read $BEVIS_JOB_%s "
            "from the environment instead."
            % (quoted, quoted.strip("{}").upper()))

    def replace(match):
        key = match.group(1)
        value = job.get("display_id") if key == "display_id" else job.get(key)
        return shlex.quote("" if value is None else str(value))

    return _PLACEHOLDER_RE.sub(replace, template)


def adapter_env(job: dict, db_path, slot: int) -> dict:
    """The same values as environment variables, for adapters that would rather
    read the environment than have their command line rewritten."""
    return {
        "BEVIS_DB": str(db_path),
        "BEVIS_JOB_ID": str(job["id"]),
        "BEVIS_JOB_DISPLAY_ID": str(job["display_id"]),
        "BEVIS_JOB_TITLE": job["title"] or "",
        "BEVIS_JOB_DESCRIPTION": job["description"] or "",
        "BEVIS_JOB_ACCEPTANCE": job["acceptance"] or "",
        "BEVIS_JOB_ASSIGNEE": job["assignee"] or "",
        "BEVIS_SLOT": str(slot),
    }


def claim_next_ready(conn, actor: str) -> Optional[dict]:
    """Atomically take one ready job, or return None.

    BEGIN IMMEDIATE plus a status-guarded UPDATE is the whole concurrency story:
    two slots that pick the same candidate cannot both get rowcount 1, so a job
    is never handed to two workers.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        for row in conn.execute("SELECT * FROM job WHERE status='open' ORDER BY id"):
            ok, _ = core.readiness(conn, row)
            if not ok:
                continue
            ts = now_ts()
            cur = conn.execute(
                "UPDATE job SET status='claimed', claimed_by=?, claimed_at=?, "
                "updated_at=? WHERE id=? AND status='open'",
                (actor, ts, ts, int(row["id"])))
            if cur.rowcount == 1:
                log_event(conn, int(row["id"]), actor, "claimed", "dispatcher")
                job = core.job_dict(conn, get_job(conn, int(row["id"])))
                conn.execute("COMMIT")
                return job
        conn.execute("COMMIT")
        return None
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _evidence_from_checks(checks: List[dict]) -> tuple:
    """Turn passing check rows into the evidence triple stored on the job.

    The recorded verify_cmd is the checks joined with `&&`, which is a command a
    human can paste into a shell and re-run. The output is every check's output,
    labelled. Nothing is summarised: a summary is a claim about evidence, not
    evidence.
    """
    cmd = " && ".join(c["cmd"] for c in checks)
    blocks = []
    for c in checks:
        blocks.append("$ %s\n[check %s exit=%s]\n%s"
                      % (c["cmd"], c["name"], c["last_exit"], (c["last_output"] or "").rstrip()))
    return cmd, 0, "\n\n".join(blocks)


def process_job(conn, job: dict, adapter: str, slot: int, timeout: int, actor: str,
                db_path) -> dict:
    """Run one claimed job to an outcome. Returns a small result dict."""
    job_id = int(job["id"])
    cmd = render_adapter(adapter, job)
    conn.execute("UPDATE job SET status='running', updated_at=? WHERE id=?",
                 (now_ts(), job_id))
    cur = conn.execute(
        "INSERT INTO job_run (job_id, slot, actor, adapter_cmd, started_at) "
        "VALUES (?,?,?,?,?)", (job_id, slot, actor, cmd, now_ts()))
    run_id = int(cur.lastrowid)

    exit_code, out, err = core.run_command(
        cmd, timeout=timeout, env=adapter_env(job, db_path, slot))
    conn.execute(
        "UPDATE job_run SET exit_code=?, stdout=?, stderr=?, finished_at=? WHERE id=?",
        (exit_code, core.truncate(out), core.truncate(err), now_ts(), run_id))

    if exit_code != 0:
        core.set_status(conn, job_id, "failed",
                        reason="adapter exited %d" % exit_code, actor=actor)
        return {"job": job["display_id"], "outcome": "failed",
                "detail": "adapter exited %d" % exit_code, "run_id": run_id}

    # The adapter finished. That is not success — it is permission to ask the
    # checks whether the bar was met.
    #
    # Every outcome below except `closed` is sticky: `blocked` and `failed` are
    # not `open`, so the dispatcher will not pick the job up again on the next
    # drain. That is deliberate. A job that could not be proved is a job a human
    # should look at, and a queue that silently retries it forever is how a
    # broken step burns a fleet's worth of compute overnight. Each reason says
    # how to requeue.
    checks = core.list_checks(conn, job_id)
    if not checks:
        reason = ("no checks defined, so nothing can prove this job is done — "
                  "add one with `bevis check add %s --name <name> --cmd <cmd>`, "
                  "then `bevis status %s open` to requeue"
                  % (job["display_id"], job["display_id"]))
        core.set_status(conn, job_id, "blocked", reason=reason, actor=actor)
        return {"job": job["display_id"], "outcome": "blocked", "detail": reason,
                "run_id": run_id}

    results = core.run_checks(conn, job_id, timeout=timeout, actor=actor)
    failed = [c for c in results if c["last_exit"] != 0]
    if failed:
        reason = ("check %r failed (exit %s) — fix it, then `bevis status %s open` "
                  "to requeue" % (failed[0]["name"], failed[0]["last_exit"],
                                  job["display_id"]))
        core.set_status(conn, job_id, "blocked", reason=reason, actor=actor)
        return {"job": job["display_id"], "outcome": "blocked", "detail": reason,
                "run_id": run_id}

    verify_cmd, verify_exit, verify_output = _evidence_from_checks(results)
    try:
        core.close_job(conn, job_id, verify_cmd, verify_exit, verify_output, actor=actor)
    except Refusal as exc:
        # close_job is the authority even here. If it refuses the dispatcher,
        # the dispatcher records the refusal — it does not overrule it.
        core.set_status(conn, job_id, "blocked", reason=str(exc), actor=actor)
        return {"job": job["display_id"], "outcome": "blocked", "detail": str(exc),
                "run_id": run_id}
    return {"job": job["display_id"], "outcome": "closed",
            "detail": "%d check(s) passed" % len(results), "run_id": run_id}


def dispatch(db_path, adapter: str, slots: int = 1, max_jobs: Optional[int] = None,
             timeout: int = core.DEFAULT_TIMEOUT, actor: str = "") -> List[dict]:
    """Drain the ready queue with `slots` workers. One job per slot, always.

    Each slot is a thread with its OWN sqlite connection; the database is the
    only shared state, and the claim is the only thing that needs to be atomic.
    Threads are enough because the work happens in a subprocess — the Python
    side is waiting on a pipe, not computing.
    """
    if slots < 1:
        raise UsageError("--slots must be at least 1")
    # Validate the template BEFORE claiming anything. A bad template that only
    # blows up mid-run would leave a job claimed by a worker that never existed.
    render_adapter(adapter, {"id": 0, "display_id": "0", "title": "", "description": "",
                             "acceptance": "", "assignee": ""})
    actor = actor or core.default_actor()
    results: List[dict] = []
    lock = threading.Lock()
    budget = {"left": max_jobs if max_jobs is not None else -1}

    def worker(slot: int) -> None:
        conn = connect(db_path)
        try:
            while True:
                with lock:
                    if budget["left"] == 0:
                        return
                    if budget["left"] > 0:
                        budget["left"] -= 1
                job = claim_next_ready(conn, "%s#%d" % (actor, slot))
                if job is None:
                    with lock:
                        if budget["left"] > 0:
                            budget["left"] += 1  # hand the unused budget back
                    return
                try:
                    outcome = process_job(conn, job, adapter, slot, timeout,
                                          "%s#%d" % (actor, slot), db_path)
                except Exception as exc:  # noqa: BLE001 - the point is to not lose it
                    # The job stays claimed. bevis does not guess an outcome for
                    # work it lost track of; `bevis reclaim` hands it back.
                    log_event(conn, int(job["id"]), actor, "dispatch_error", repr(exc))
                    outcome = {"job": job["display_id"], "outcome": "error",
                               "detail": repr(exc), "run_id": -1}
                with lock:
                    results.append(outcome)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(slots)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return sorted(results, key=lambda r: r["run_id"])
