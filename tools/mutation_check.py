#!/usr/bin/env python3
"""Prove the test suite can actually fail.

A green test suite is not evidence that a rule is enforced. It is evidence that
the suite ran. The two are only the same thing if you have watched each test go
red for the right reason.

This script plants one defect at a time in a COPY of the tree — the kind of
defect a well-meaning refactor produces, not gibberish — and asserts that the
test which claims to guard that rule fails. A mutant that survives means the
rule is untested no matter how many assertions surround it.

Usage:
    python tools/mutation_check.py            # every mutant
    python tools/mutation_check.py evidence   # only mutants whose name matches

Honest limits, stated because this file is about not fooling yourself:
  * It proves detection, not coverage. Rules with no mutant here are unproven.
  * Race conditions (two slots claiming one job) are not mutation-testable this
    way; a mutant that removes the atomic claim still usually passes, because
    the race rarely loses. That property is argued from the code, not measured.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# name, file, exact text to replace, replacement, tests that MUST then fail
MUTANTS = [
    ("evidence-nonzero-exit-accepted", "bevis/core.py",
     '    elif verify_exit != 0:',
     '    elif False:',
     ["test_close_with_nonzero_exit_is_refused",
      "test_close_run_with_a_failing_command_is_refused"]),

    ("evidence-empty-output-accepted", "bevis/core.py",
     '    if not isinstance(verify_output, str) or not verify_output.strip():',
     '    if False:',
     ["test_close_with_empty_output_is_refused",
      "test_close_run_with_a_silent_command_is_refused"]),

    ("evidence-empty-command-accepted", "bevis/core.py",
     '    if not isinstance(verify_cmd, str) or not verify_cmd.strip():',
     '    if False:',
     ["test_close_with_empty_command_is_refused"]),

    ("evidence-missing-exit-accepted", "bevis/core.py",
     '    if verify_exit is None:',
     '    if False:',
     ["test_close_with_no_evidence_at_all_is_refused"]),

    # ── The vacuity lexicon ─────────────────────────────────────────────────
    # A command that exits 0 having measured nothing has not verified anything.
    ("vacuous-output-accepted", "bevis/core.py",
     '''    vacuous = vacuity_problem(verify_output)
    if vacuous:''',
     '''    vacuous = vacuity_problem(verify_output)
    if False:''',
     ["test_close_with_vacuous_output_is_refused",
      "test_cli_close_on_a_vacuous_run_exits_1",
      "test_the_dispatcher_cannot_close_on_a_check_that_measured_nothing"]),

    # The other direction, and the one that matters more: a lexicon that also
    # refuses a real build log mentioning one empty sub-run is not a gate, it is
    # an obstacle, and people route around obstacles.
    ("vacuity-refuses-a-log-that-measured-something", "bevis/core.py",
     '''    if not needle or measured_something(text):''',
     '''    if not needle:''',
     ["test_a_log_that_measured_something_is_not_vacuous"]),

    # `0 passed` must not fire inside `100 passed`. Asserted against the lexicon
    # itself, because the counter-evidence rule above would otherwise mask it.
    ("vacuity-needle-matches-a-bigger-number", "bevis/core.py",
     '''    re.compile(r"(?i)(?<![\\d.,])0 pass(?:ed|ing)\\b"),''',
     '''    re.compile(r"(?i)0 pass(?:ed|ing)\\b"),''',
     ["test_a_needle_does_not_match_a_bigger_number"]),

    # ── The negative control ────────────────────────────────────────────────
    ("control-that-also-passed-is-accepted", "bevis/core.py",
     '''    if control_exit == 0:''',
     '''    if False:''',
     ["test_a_control_that_also_passes_is_refused",
      "test_cli_close_with_a_control_that_passes_exits_1",
      "test_a_negative_control_that_passes_is_409_over_http"]),

    ("control-that-never-ran-counts-as-a-failure", "bevis/core.py",
     '''    if control_exit in CONTROL_DID_NOT_RUN:''',
     '''    if False:''',
     ["test_a_control_that_never_ran_is_not_a_control_that_failed",
      "test_a_control_that_is_not_executable_is_refused_too"]),

    # The gate as a stored string: bevis records a control it never executed.
    ("control-command-never-actually-run", "bevis/core.py",
     '''    elif control_cmd:
        control_exit, cout, cerr = run_command(control_cmd, timeout=timeout, env=env)''',
     '''    elif control_cmd:
        control_exit, cout, cerr = 1, "the control was not run", ""''',
     ["test_the_control_is_actually_run_not_merely_recorded",
      "test_a_control_that_also_passes_is_refused",
      "test_the_control_is_not_told_that_it_is_the_control"]),

    ("old-board-drops-the-control-it-was-given", "bevis/core.py",
     '''        if not has_negative_control(conn):
            raise UsageError(_OLD_BOARD)''',
     '''        if False:
            raise UsageError(_OLD_BOARD)''',
     ["test_close_job_on_an_old_board_refuses_a_control_rather_than_dropping_it"]),

    ("negative-control-accepted-without-run", "bevis/cli.py",
     '''                if args.negative_control:''',
     '''                if False:''',
     ["test_cli_negative_control_without_run_is_refused"]),

    ("acceptance-bar-optional", "bevis/core.py",
     '''    if not acceptance:
        raise UsageError(''',
     '''    if False:
        raise UsageError(''',
     ["test_acceptance_is_required"]),

    ("reparenting-silently-ignored", "bevis/core.py",
     '''    if "parent_id" in fields or "parent" in fields:
        raise Refusal(''',
     '''    fields.pop("parent_id", None)
    fields.pop("parent", None)
    if False:
        raise Refusal(''',
     ["test_parent_id_cannot_be_set_on_update", "test_cli_refuses_reparenting"]),

    ("failing-blocking-check-does-not-stop-close", "bevis/core.py",
     '''    failing = failing_blocking_checks(conn, job_id)
    if failing:''',
     '''    failing = failing_blocking_checks(conn, job_id)
    if False:''',
     ["test_failing_blocking_check_makes_the_job_unclosable"]),

    ("unrun-blocking-check-does-not-stop-close", "bevis/core.py",
     '''    unproven = unproven_blocking_checks(conn, job_id)
    if unproven:''',
     '''    unproven = unproven_blocking_checks(conn, job_id)
    if False:''',
     ["test_a_blocking_check_that_never_ran_blocks_the_close"]),

    ("failing-blocking-check-still-ready", "bevis/core.py",
     '''    own = failing_blocking_checks(conn, job_id)
    if own:''',
     '''    own = failing_blocking_checks(conn, job_id)
    if False:''',
     ["test_failing_blocking_check_makes_the_job_unready",
      "test_failing_blocking_check_makes_the_job_unclaimable"]),

    ("upstream-check-failure-not-propagated", "bevis/core.py",
     '''        failing = failing_blocking_checks(conn, upstream_id)
        if failing:''',
     '''        failing = failing_blocking_checks(conn, upstream_id)
        if False:''',
     ["test_a_failing_blocking_check_on_a_blocker_stops_the_dependent",
      "test_a_failing_blocking_check_on_the_parent_stops_the_children",
      "test_check_failure_propagates_transitively"]),

    ("open-blocker-does-not-hold-the-dependent", "bevis/core.py",
     '        if blocker["status"] not in DONE_STATUSES:',
     '        if False:',
     ["test_a_dependent_stays_unready_while_its_blocker_is_open",
      "test_a_blocker_that_merely_failed_does_not_release_the_dependent"]),

    ("parent-closes-over-open-children", "bevis/core.py",
     '''    open_children = _unfinished_children(conn, job_id)
    if open_children:''',
     '''    open_children = _unfinished_children(conn, job_id)
    if False:''',
     ["test_a_parent_cannot_close_while_a_child_is_open"]),

    ("parent-with-open-children-is-ready", "bevis/core.py",
     '''    unfinished = _unfinished_children(conn, job_id)
    if unfinished:''',
     '''    unfinished = _unfinished_children(conn, job_id)
    if False:''',
     ["test_a_parent_with_unfinished_children_is_not_ready"]),

    ("self-verification-allowed", "bevis/core.py",
     '    if closer and closer.casefold() == actor.casefold():',
     '    if False:',
     ["test_verify_by_the_actor_who_closed_it_is_refused",
      "test_cli_verify_by_the_closer_exits_1"]),

    ("closed-status-writable-directly", "bevis/core.py",
     '    if status in GATED_STATUSES:',
     '    if False:',
     ["test_there_is_no_status_command_that_writes_closed"]),

    ("unknown-status-stored", "bevis/model.py",
     '    if status not in STATUSES:',
     '    if False:',
     ["test_the_vocabulary_is_small_and_closed",
      "test_list_filter_rejects_an_unknown_status"]),

    ("stale-claim-never-reclaimed", "bevis/core.py",
     '        if parse_ts(row["claimed_at"]) <= cutoff:',
     '        if False:',
     ["test_a_stale_claim_is_reclaimable"]),

    ("fresh-claim-wrongly-reclaimed", "bevis/core.py",
     '        if parse_ts(row["claimed_at"]) <= cutoff:',
     '        if True:',
     ["test_a_fresh_claim_is_left_alone"]),

    ("dispatcher-decides-success-itself", "bevis/dispatch.py",
     '''    checks = core.list_checks(conn, job_id)
    if not checks:''',
     '''    checks = core.list_checks(conn, job_id)
    if not checks:
        core.close_job(conn, job_id, cmd, exit_code, out or "(no output)", actor=actor)
        return {"job": job["display_id"], "outcome": "closed",
                "detail": "the adapter exited 0", "run_id": run_id}
    if False:''',
     ["test_adapter_exit_zero_alone_does_not_close_a_job"]),

    ("adapter-values-not-shell-quoted", "bevis/dispatch.py",
     '        return shlex.quote("" if value is None else str(value))',
     '        return "" if value is None else str(value)',
     ["test_placeholders_are_substituted_and_shell_quoted"]),

    ("quoted-placeholder-allowed", "bevis/dispatch.py",
     '''    quoted = _quoted_placeholder(template)
    if quoted:''',
     '''    quoted = _quoted_placeholder(template)
    if False:''',
     ["test_a_placeholder_inside_quotes_is_refused"]),

    # ── The adapter registry ────────────────────────────────────────────────
    ("registered-adapter-name-not-resolved", "bevis/adapters.py",
     '''    if NAME_RE.match(text) and has_registry(conn):''',
     '''    if False:''',
     ["test_a_registered_adapter_name_resolves_to_its_command",
      "test_cli_run_accepts_a_registered_name"]),

    ("registry-stores-a-pasted-credential", "bevis/adapters.py",
     '''    problem = credential_problem(cmd)
    if problem:''',
     '''    problem = credential_problem(cmd)
    if False:''',
     ["test_registering_an_inline_credential_is_refused",
      "test_the_shapes_a_pasted_secret_takes_are_refused",
      "test_the_cli_refuses_to_store_a_credential"]),

    # The same gate in the other direction. A credential rule that also refuses
    # `--api-key $MY_KEY` teaches people to hide the literal instead of moving
    # it out of bevis, so the permissive half is a rule too, and is tested.
    # The same gate in the other direction: a rule that also refused
    # `--api-key $MY_KEY` would teach people to hide the literal rather than
    # move it out of bevis, so the permissive half is a rule too.
    ("credential-lint-refuses-an-environment-reference", "bevis/adapters.py",
     '''_REFERENCE_STARTS = ("$", "`")''',
     '''_REFERENCE_STARTS = ()''',
     ["test_a_secret_referenced_from_the_environment_is_not_a_secret"]),

    # Two quote characters used to walk every pasted secret past the rule.
    ("credential-lint-fooled-by-quotes", "bevis/adapters.py",
     '''    text = rest.lstrip()
    if text[:1] in ("'", '"'):
        text = text[1:]''',
     '''    text = rest.lstrip()''',
     ["test_the_shapes_a_pasted_secret_takes_are_refused"]),

    ("adapter-name-may-shadow-a-program", "bevis/adapters.py",
     '''    if shutil.which(name):''',
     '''    if False:''',
     ["test_a_name_that_is_also_a_program_is_refused"]),

    ("unrunnable-template-registered", "bevis/adapters.py",
     '''    render_adapter(cmd, {"id": 0, "display_id": "0", "title": "", "description": "",
                         "acceptance": "", "assignee": ""})''',
     '''    pass''',
     ["test_a_template_that_can_never_render_is_refused_at_registration"]),

    ("garbage-database-reaches-the-user-as-a-traceback", "bevis/db.py",
     '''    except sqlite3.DatabaseError as exc:''',
     '''    except _NeverRaised as exc:''',
     ["test_doctor_reports_a_file_that_is_not_a_database_instead_of_crashing",
      "test_a_file_that_is_not_a_database_is_a_refusal_for_every_command"]),

    ("doctor-board-note-counts-jobs-the-dispatcher-never-sees", "bevis/doctor.py",
     '''    without = [job for job in core.ready_jobs(conn)
               if not core.list_checks(conn, job["id"])]''',
     '''    without = [dict(row) for row in conn.execute(
        "SELECT j.id FROM job j WHERE j.status=\'open\' AND NOT EXISTS "
        "(SELECT 1 FROM job_check c WHERE c.job_id=j.id)")]''',
     ["test_the_board_note_counts_only_what_the_dispatcher_will_reach"]),

    ("doctor-drops-the-probe-it-was-asked-for", "bevis/doctor.py",
     '''    if probe_name and not any(r["section"] == "adapters" for r in results):''',
     '''    if False:''',
     ["test_doctor_still_answers_about_the_adapter_it_was_asked_about"]),

    ("old-board-gets-a-raw-sqlite-error", "bevis/adapters.py",
     '''    if not has_registry(conn):
        raise UsageError(''',
     '''    if False:
        raise UsageError(''',
     ["test_a_board_that_predates_the_registry_names_the_one_command_that_fixes_it"]),

    # ── doctor ──────────────────────────────────────────────────────────────
    ("doctor-exits-zero-with-failures", "bevis/doctor.py",
     '''    return 1 if any(r["status"] == FAIL for r in results) else 0''',
     '''    return 0''',
     ["test_cli_doctor_exits_nonzero_when_something_is_broken",
      "test_doctor_fails_when_there_is_no_database",
      "test_doctor_fails_when_the_probed_adapter_exits_nonzero"]),

    ("doctor-ignores-a-non-executable-adapter", "bevis/doctor.py",
     '''        if not os.access(path, os.X_OK):''',
     '''        if False:''',
     ["test_doctor_fails_when_a_registered_adapter_is_not_executable"]),

    ("doctor-ignores-an-adapter-that-is-not-on-path", "bevis/doctor.py",
     '''    if shutil.which(program) is None:''',
     '''    if False:''',
     ["test_doctor_fails_when_the_adapter_program_is_not_on_path"]),

    ("doctor-reports-a-failed-probe-as-ok", "bevis/doctor.py",
     '''    if exit_code == 0:''',
     '''    if True:''',
     ["test_doctor_fails_when_the_probed_adapter_exits_nonzero",
      "test_doctor_fails_when_the_probed_adapter_never_returns"]),

    # The tidy-looking refactor that costs somebody an agent run per diagnosis,
    # and turns "unproven" into "ok" for every adapter on the board.
    ("doctor-calls-every-adapter-it-finds", "bevis/doctor.py",
     '''        elif row["name"] == probe_name:
            continue                # probed below, where the real answer is
        else:''',
     '''        elif row["name"] == probe_name:
            continue                # probed below, where the real answer is
        elif probe(row["cmd"], db_path, timeout=timeout)[0] == 0:
            results.append(_result("adapters", OK, "%s ran" % row["name"]))
        else:''',
     ["test_doctor_does_not_call_an_adapter_it_was_not_asked_to_call"]),
]

IGNORE = shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc",
                                ".bevis", "build", "dist", "*.egg-info")


def run_pytest(tree: Path, selectors) -> tuple:
    """Run only the tests named in `selectors`; return (exit_code, failed_names)."""
    args = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]
    if selectors:
        args += ["-k", " or ".join(selectors)]
    proc = subprocess.run(args, cwd=str(tree), capture_output=True, text=True)
    failed = set()
    for line in proc.stdout.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            name = line.split(" ", 1)[1].split(" ")[0]
            failed.add(name.rsplit("::", 1)[-1])
    return proc.returncode, failed


def main(argv) -> int:
    pattern = argv[1] if len(argv) > 1 else ""
    mutants = [m for m in MUTANTS if pattern in m[0]]
    if not mutants:
        print("no mutants match %r" % pattern)
        return 2

    with tempfile.TemporaryDirectory() as workspace:
        clean = Path(workspace) / "clean"
        shutil.copytree(ROOT, clean, ignore=IGNORE)
        print("baseline: running the whole suite on unmutated source ...")
        code, failed = run_pytest(clean, [])
        if code != 0:
            print("BASELINE IS RED (%s). Fix the suite before trusting any mutant."
                  % (", ".join(sorted(failed)) or "unknown"))
            return 1
        print("baseline: green\n")

        survivors = []
        for index, (name, rel, old, new, expected) in enumerate(mutants, 1):
            tree = Path(workspace) / ("mutant_%d" % index)
            shutil.copytree(clean, tree)
            target = tree / rel
            source = target.read_text()
            if source.count(old) != 1:
                print("!! %-42s CANNOT APPLY (matched %d times in %s)"
                      % (name, source.count(old), rel))
                survivors.append(name)
                continue
            target.write_text(source.replace(old, new))
            _, failed = run_pytest(tree, expected)
            caught = sorted(set(expected) & failed)
            missed = sorted(set(expected) - failed)
            status = "CAUGHT " if caught else "SURVIVED"
            print("%-8s %-42s by: %s%s" % (
                status, name, ", ".join(caught) or "-",
                "   (did not fire: %s)" % ", ".join(missed) if missed and caught else ""))
            if not caught:
                survivors.append(name)

    print()
    if survivors:
        print("%d/%d mutants SURVIVED — those rules are not actually tested:"
              % (len(survivors), len(mutants)))
        for name in survivors:
            print("  - %s" % name)
        return 1
    print("all %d mutants were caught: every rule above has a test that fails "
          "when the rule is removed." % len(mutants))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
