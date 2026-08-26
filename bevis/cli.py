"""The command line. Stdlib only — argparse, sqlite3, subprocess.

`pip install bevis` pulls in nothing. That is a design point, not an accident:
a gate that is expensive to adopt does not get adopted, and a gate that is not
adopted gates nothing. The HTTP API is an extra (`pip install bevis[api]`).
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import __version__, adapters, core, doctor
from .db import connect, get_job, init_db, resolve_db_path
from .dispatch import dispatch
from .errors import BevisError
from .model import EXIT_OK, STATUSES


# ── Output ───────────────────────────────────────────────────────────────────
def emit(data, as_json: bool, plain=None) -> None:
    if as_json:
        print(json.dumps(data, indent=2, sort_keys=True, default=str))
    elif plain is not None:
        print(plain)


def fmt_job_line(job: dict) -> str:
    mark = "*" if job["ready"] else " "
    return "%s %-6s %-9s %s" % (mark, job["display_id"], job["status"], job["title"])


def print_job(job: dict, conn) -> None:
    print("job        %s (internal id %s)" % (job["display_id"], job["id"]))
    print("title      %s" % job["title"])
    print("status     %s" % job["status"])
    if job.get("blocked_reason"):
        print("blocked    %s" % job["blocked_reason"])
    print("acceptance %s" % job["acceptance"])
    if job.get("description"):
        print("desc       %s" % job["description"])
    if job.get("assignee"):
        print("assignee   %s" % job["assignee"])
    if job.get("parent_id"):
        print("parent     %s" % job["parent_id"])
    if job.get("blockers"):
        print("after      %s" % ", ".join(str(b) for b in job["blockers"]))
    print("ready      %s%s" % ("yes" if job["ready"] else "no",
                               "" if job["ready"] else " — " + job["not_ready_reason"]))
    print("created    %s" % job["created_at"])
    checks = core.list_checks(conn, job["id"])
    if checks:
        print("checks:")
        for c in checks:
            state = "never run" if c["last_exit"] is None else (
                "PASS" if c["last_exit"] == 0 else "FAIL exit=%s" % c["last_exit"])
            print("  - %-12s %-9s %s%s" % (
                c["name"], "[blocking]" if c["blocking"] else "", state,
                "  $ " + c["cmd"]))
    print("evidence:")
    if job.get("verify_cmd"):
        print("  closed_by   %s at %s" % (job.get("closed_by"), job.get("closed_at")))
        print("  verify_cmd  %s" % job["verify_cmd"])
        print("  verify_exit %s" % job["verify_exit"])
        print("  verify_output:")
        for line in (job.get("verify_output") or "").splitlines():
            print("    | %s" % line)
        if job.get("control_cmd"):
            print("  negative control:")
            print("    cmd    %s" % job["control_cmd"])
            print("    exit   %s" % job["control_exit"])
            if (job.get("control_output") or "").strip():
                print("    output:")
                for line in (job.get("control_output") or "").splitlines():
                    print("      | %s" % line)
    else:
        print("  none — this job has not been closed")
    if job.get("verified_by"):
        print("verified   by %s at %s" % (job["verified_by"], job["verified_at"]))


# ── Argument parsing ─────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bevis",
        description="A job board where a job cannot close without machine-checkable "
                    "evidence.")
    parser.add_argument("--db", help="path to the bevis database "
                                     "(default: $BEVIS_DB or ./.bevis/bevis.db)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version="bevis %s" % __version__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database")

    p = sub.add_parser("add", help="create a job (acceptance bar required)")
    p.add_argument("title")
    p.add_argument("--acceptance", required=True,
                   help="the bar: what must be true for this job to be done")
    p.add_argument("--description", default="")
    p.add_argument("--parent", help="parent job id — settable ONLY at create")
    p.add_argument("--after", action="append", default=[],
                   help="this job stays unready until job <id> is closed (repeatable)")
    p.add_argument("--assignee", default="")
    p.add_argument("--actor", default="")

    p = sub.add_parser("update", help="edit a job's prose fields")
    p.add_argument("id")
    p.add_argument("--title")
    p.add_argument("--description")
    p.add_argument("--acceptance")
    p.add_argument("--assignee")
    p.add_argument("--parent", help="(refused — parent_id is create-only)")
    p.add_argument("--actor", default="")

    p = sub.add_parser("list", help="list jobs")
    p.add_argument("--status", choices=STATUSES)
    p.add_argument("--parent")

    p = sub.add_parser("show", help="show one job, its checks and its evidence")
    p.add_argument("id")

    p = sub.add_parser("events", help="show the audit trail for one job")
    p.add_argument("id")

    sub.add_parser("ready", help="jobs that can be worked on right now")

    p = sub.add_parser("claim", help="take a ready job")
    p.add_argument("id")
    p.add_argument("--actor", default="")

    p = sub.add_parser("status", help="set a non-gated status")
    p.add_argument("id")
    p.add_argument("new_status", choices=[s for s in STATUSES])
    p.add_argument("--reason", default="")
    p.add_argument("--actor", default="")

    p = sub.add_parser(
        "close", help="close a job — refused without evidence")
    p.add_argument("id")
    p.add_argument("--run", dest="run_cmd",
                   help="bevis runs this command and records cmd/exit/output itself")
    p.add_argument("--verify-cmd", help="evidence produced elsewhere: the command")
    p.add_argument("--verify-exit", type=int, help="evidence produced elsewhere: exit code")
    p.add_argument("--verify-output-file",
                   help="evidence produced elsewhere: file with the output ('-' = stdin)")
    p.add_argument("--negative-control", dest="negative_control",
                   help="a command that MUST fail. bevis runs it as well and "
                        "refuses the close if it passes too, because a check that "
                        "passes either way checks nothing (use with --run)")
    p.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    p.add_argument("--actor", default="")

    p = sub.add_parser("verify", help="confirm a closed job (different actor required)")
    p.add_argument("id")
    p.add_argument("--actor", required=True)
    p.add_argument("--note", default="")

    p = sub.add_parser("reopen", help="undo a close (reason required)")
    p.add_argument("id")
    p.add_argument("--reason", required=True)
    p.add_argument("--actor", default="")

    p = sub.add_parser("check", help="checks: the gates that block a job")
    csub = p.add_subparsers(dest="check_command", required=True)
    c = csub.add_parser("add")
    c.add_argument("id")
    c.add_argument("--name", required=True)
    c.add_argument("--cmd", required=True)
    c.add_argument("--blocking", action="store_true",
                   help="a failing blocking check makes this job and everything "
                        "downstream of it unready, and makes this job unclosable")
    c.add_argument("--actor", default="")
    c = csub.add_parser("list")
    c.add_argument("id")
    c = csub.add_parser("run")
    c.add_argument("id")
    c.add_argument("--name", help="run only this check")
    c.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    c.add_argument("--actor", default="")
    c = csub.add_parser("rm")
    c.add_argument("id")
    c.add_argument("--name", required=True)
    c.add_argument("--actor", default="")

    p = sub.add_parser("adapter", help="name a command so you can stop retyping it")
    asub = p.add_subparsers(dest="adapter_command", required=True)
    a = asub.add_parser("add")
    a.add_argument("name")
    a.add_argument("--cmd", required=True,
                   help="the command bevis will run; it owns its own "
                        "configuration, and bevis never reads it")
    a.add_argument("--note", default="", help="what this adapter is, for humans")
    a.add_argument("--actor", default="")
    asub.add_parser("list")
    a = asub.add_parser("remove", aliases=["rm"])
    a.add_argument("name")
    a.add_argument("--actor", default="")

    p = sub.add_parser("doctor", help="say what is working here and what is not")
    p.add_argument("--adapter",
                   help="also CALL this registered adapter (or command) against a "
                        "throwaway probe job and report what it did")
    p.add_argument("--timeout", type=int, default=doctor.PROBE_TIMEOUT)

    p = sub.add_parser("run", help="claim ready jobs and run an adapter on them")
    p.add_argument("--adapter", required=True,
                   help="a registered adapter name, or a command template; "
                        "{id} {display_id} {title} {description} {acceptance} "
                        "{assignee} are substituted, shell-quoted")
    p.add_argument("--slots", type=int, default=1, help="parallel workers")
    p.add_argument("--max-jobs", type=int, default=None)
    p.add_argument("--timeout", type=int, default=core.DEFAULT_TIMEOUT)
    p.add_argument("--actor", default="")

    p = sub.add_parser("reclaim", help="return jobs whose worker went away")
    p.add_argument("--stale", default="30m", help="e.g. 90s, 30m, 2h, 1d")
    p.add_argument("--actor", default="")

    p = sub.add_parser("serve", help="HTTP API (requires the [api] extra)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8420)

    return parser


# ── Dispatch table ───────────────────────────────────────────────────────────
def _read_output_file(path: Optional[str]) -> Optional[str]:
    if path is None:
        return None
    if path == "-":
        return sys.stdin.read()
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


def run_command_line(args, db_path) -> int:
    as_json = args.json

    if args.command == "init":
        path = init_db(db_path)
        emit({"db": str(path)}, as_json, "initialised bevis database at %s" % path)
        return EXIT_OK

    if args.command == "doctor":
        results = doctor.diagnose(db_path, probe_name=args.adapter,
                                  timeout=args.timeout)
        emit(results, as_json, doctor.render(results))
        return doctor.exit_code(results)

    if args.command == "serve":
        from .api import serve

        serve(db_path, host=args.host, port=args.port)
        return EXIT_OK

    conn = connect(db_path)
    try:
        if args.command == "add":
            job = core.create_job(
                conn, args.title, args.acceptance, description=args.description,
                parent=args.parent, after=args.after, assignee=args.assignee,
                actor=args.actor)
            emit(job, as_json, "created job %s: %s" % (job["display_id"], job["title"]))

        elif args.command == "update":
            fields = {k: getattr(args, k) for k in
                      ("title", "description", "acceptance", "assignee")
                      if getattr(args, k) is not None}
            if args.parent is not None:
                fields["parent_id"] = args.parent
            job = core.update_job(conn, args.id, actor=args.actor, **fields)
            emit(job, as_json, "updated job %s" % job["display_id"])

        elif args.command == "list":
            jobs = core.list_jobs(conn, status=args.status, parent=args.parent)
            emit(jobs, as_json, "\n".join(fmt_job_line(j) for j in jobs)
                 or "(no jobs)")

        elif args.command == "show":
            job = core.job_dict(conn, get_job(conn, args.id))
            if as_json:
                job["checks"] = core.list_checks(conn, job["id"])
                job["runs"] = core.job_runs(conn, job["id"])
                emit(job, True)
            else:
                print_job(job, conn)

        elif args.command == "events":
            events = core.job_events(conn, args.id)
            emit(events, as_json, "\n".join(
                "%s  %-14s %-16s %s" % (e["ts"], e["kind"], e["actor"], e["detail"])
                for e in events) or "(no events)")

        elif args.command == "ready":
            jobs = core.ready_jobs(conn)
            emit(jobs, as_json, "\n".join(fmt_job_line(j) for j in jobs)
                 or "(nothing ready)")

        elif args.command == "claim":
            job = core.claim(conn, args.id, actor=args.actor)
            emit(job, as_json, "claimed job %s as %s"
                 % (job["display_id"], job["claimed_by"]))

        elif args.command == "status":
            job = core.set_status(conn, args.id, args.new_status,
                                  reason=args.reason, actor=args.actor)
            emit(job, as_json, "job %s is now %s" % (job["display_id"], job["status"]))

        elif args.command == "close":
            if args.run_cmd:
                if args.verify_cmd or args.verify_exit is not None or args.verify_output_file:
                    raise BevisError("use either --run or the --verify-* trio, not both")
                job = core.close_by_running(conn, args.id, args.run_cmd,
                                            timeout=args.timeout, actor=args.actor,
                                            negative_control=args.negative_control)
            else:
                if args.negative_control:
                    # The control is only worth anything because bevis watched it
                    # fail. Pairing an observed control with a transcribed
                    # verification would let the strong half launder the weak one.
                    raise BevisError(
                        "--negative-control needs --run: bevis has to run the "
                        "control itself for its failure to mean anything, and the "
                        "--verify-* form is evidence produced somewhere bevis "
                        "cannot see. Run the control where the work is, or make "
                        "--verify-cmd the command that runs both.")
                job = core.close_job(
                    conn, args.id, args.verify_cmd, args.verify_exit,
                    _read_output_file(args.verify_output_file), actor=args.actor)
            plain = ("closed job %s with evidence (exit %s from: %s)"
                     % (job["display_id"], job["verify_exit"], job["verify_cmd"]))
            if job.get("control_cmd"):
                plain += ("\nnegative control exited %s, as a control must: %s"
                          % (job["control_exit"], job["control_cmd"]))
            emit(job, as_json, plain)

        elif args.command == "verify":
            job = core.verify_job(conn, args.id, args.actor, note=args.note)
            emit(job, as_json, "job %s verified by %s"
                 % (job["display_id"], job["verified_by"]))

        elif args.command == "reopen":
            job = core.reopen_job(conn, args.id, args.reason, actor=args.actor)
            emit(job, as_json, "job %s reopened (evidence discarded, see `bevis events`)"
                 % job["display_id"])

        elif args.command == "check":
            if args.check_command == "add":
                row = core.add_check(conn, args.id, args.name, args.cmd,
                                     blocking=args.blocking, actor=args.actor)
                emit(row, as_json, "added %scheck %r to job %s"
                     % ("blocking " if args.blocking else "", args.name, args.id))
            elif args.check_command == "list":
                rows = core.list_checks(conn, args.id)
                emit(rows, as_json, "\n".join(
                    "%-12s %-10s %-9s %s" % (
                        r["name"], "blocking" if r["blocking"] else "advisory",
                        "never run" if r["last_exit"] is None else "exit=%s" % r["last_exit"],
                        r["cmd"]) for r in rows) or "(no checks)")
            elif args.check_command == "run":
                rows = core.run_checks(conn, args.id, name=args.name,
                                       timeout=args.timeout, actor=args.actor)
                emit(rows, as_json, "\n".join(
                    "%-12s exit=%s" % (r["name"], r["last_exit"]) for r in rows))
                if any(r["last_exit"] != 0 for r in rows):
                    return 1
            elif args.check_command == "rm":
                core.remove_check(conn, args.id, args.name, actor=args.actor)
                emit({"removed": args.name}, as_json,
                     "removed check %r from job %s" % (args.name, args.id))

        elif args.command == "adapter":
            if args.adapter_command == "add":
                row = adapters.add(conn, args.name, args.cmd, note=args.note,
                                   actor=args.actor)
                emit(row, as_json, "registered adapter %s = %s"
                     % (row["name"], row["cmd"]))
            elif args.adapter_command == "list":
                rows = adapters.list_all(conn)
                emit(rows, as_json, "\n".join(
                    "%-12s %s%s" % (r["name"], r["cmd"],
                                    "   # " + r["note"] if r["note"] else "")
                    for r in rows) or "(no adapters registered)")
            else:
                adapters.remove(conn, args.name, actor=args.actor)
                emit({"removed": args.name}, as_json,
                     "removed adapter %s" % args.name)

        elif args.command == "run":
            results = dispatch(db_path, args.adapter, slots=args.slots,
                               max_jobs=args.max_jobs, timeout=args.timeout,
                               actor=args.actor)
            emit(results, as_json, "\n".join(
                "job %-6s %-8s %s" % (r["job"], r["outcome"], r["detail"])
                for r in results) or "(nothing ready to dispatch)")

        elif args.command == "reclaim":
            jobs = core.reclaim(conn, stale=args.stale, actor=args.actor)
            emit(jobs, as_json, "\n".join(
                "reclaimed job %s" % j["display_id"] for j in jobs)
                or "(nothing stale)")

        else:  # pragma: no cover - argparse rejects unknown commands first
            raise BevisError("unknown command %r" % args.command)
    finally:
        conn.close()
    return EXIT_OK


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db_path = resolve_db_path(args.db)
    try:
        return run_command_line(args, db_path)
    except BevisError as exc:
        print("bevis: %s" % exc, file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover
        print("bevis: interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
