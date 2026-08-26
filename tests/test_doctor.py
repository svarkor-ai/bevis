"""`bevis doctor` has to be useful when things are broken, which is the only
time anyone runs it.

Two properties are worth more than the rest and are tested hardest:

* every failure names the command that fixes it, and the process exits non-zero;
* doctor never calls an adapter it was not asked to call, and never reports one
  it did not call as working. An unprobed adapter is `unproven`, exactly like a
  blocking check that has never been run.
"""
from __future__ import annotations

from bevis import adapters, core, doctor
from bevis.db import connect


def statuses(results, section):
    return [r["status"] for r in results if r["section"] == section]


def detail(results, section):
    return "\n".join("%s %s %s" % (r["status"], r["detail"], r["fix"])
                     for r in results if r["section"] == section)


def script(tmp_path, name, body, executable=True):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n%s\n" % body)
    path.chmod(0o755 if executable else 0o644)
    return path


# ── The database ─────────────────────────────────────────────────────────────
def test_doctor_fails_when_there_is_no_database(tmp_path):
    results = doctor.diagnose(tmp_path / ".bevis" / "bevis.db")
    assert doctor.exit_code(results) == 1
    assert statuses(results, "database") == ["FAIL"]
    assert "`bevis init`" in detail(results, "database")


def test_doctor_fails_on_a_sqlite_file_that_is_not_a_bevis_board(tmp_path):
    import sqlite3

    path = tmp_path / "something-else.db"
    other = sqlite3.connect(str(path))
    other.execute("CREATE TABLE unrelated (x INTEGER)")
    other.close()
    results = doctor.diagnose(path)
    assert doctor.exit_code(results) == 1
    assert "not a bevis board" in detail(results, "database")


def test_doctor_fails_on_a_board_that_predates_the_adapter_registry(db_path):
    conn = connect(db_path)
    conn.execute("DROP TABLE adapter")
    conn.close()
    results = doctor.diagnose(db_path)
    assert doctor.exit_code(results) == 1
    assert "`bevis init` again" in detail(results, "database")


def test_doctor_reports_a_file_that_is_not_a_database_instead_of_crashing(
        tmp_path):
    # A mistyped $BEVIS_DB is the most likely way a stranger meets this, and
    # doctor is the command they run to find out what is wrong.
    path = tmp_path / "notes.db"
    path.write_text("this is my notes file, not a database\n")
    results = doctor.diagnose(path)
    assert doctor.exit_code(results) == 1
    assert "not a SQLite database" in detail(results, "database")
    assert "$BEVIS_DB" in detail(results, "database")


def test_a_file_that_is_not_a_database_is_a_refusal_for_every_command(cli,
                                                                      tmp_path):
    # Fixed once, in connect(), rather than once per command: `bevis show` on
    # the same file must not traceback either.
    path = tmp_path / "notes.db"
    path.write_text("this is my notes file, not a database\n")
    code, _, err = cli("--db", str(path), "list")
    assert code == 2
    assert "not a SQLite database" in err


def test_doctor_still_answers_about_the_adapter_it_was_asked_about(
        db_path, conn, tmp_path):
    # The board is broken, so the registry cannot be read — but the user asked
    # doctor to CALL something, and silence would read as "fine".
    conn.execute("DROP TABLE adapter")
    conn.close()
    results = doctor.diagnose(db_path, probe_name="myagent")
    assert doctor.exit_code(results) == 1
    assert statuses(results, "adapters") == ["unproven"]
    assert "not called" in detail(results, "adapters")


def test_doctor_reports_a_healthy_board_and_exits_zero(db_path, conn):
    job = core.create_job(conn, "a job", "the bar")
    core.add_check(conn, job["id"], "unit", "echo ok", blocking=True)
    results = doctor.diagnose(db_path)
    assert doctor.exit_code(results) == 0
    assert statuses(results, "database") == ["ok"]
    assert "1 job(s), 1 ready" in detail(results, "database")


# ── The things a new user gets wrong ─────────────────────────────────────────
def test_doctor_notes_a_job_that_nothing_can_ever_prove(db_path, conn):
    core.create_job(conn, "unprovable", "somebody says it is fine")
    results = doctor.diagnose(db_path)
    # not a failure — it is a legal board — but it is the reason `bevis run`
    # will block instead of close, said before it happens rather than after
    assert statuses(results, "board") == ["note"]
    assert "no checks" in detail(results, "board")
    assert "bevis check add" in detail(results, "board")
    assert doctor.exit_code(results) == 0


def test_the_board_note_counts_only_what_the_dispatcher_will_reach(db_path, conn):
    """`bevis run` claims ready jobs, so the note may only count ready jobs.

    An epic and a job queued behind a blocker are both open and both have no
    checks, and the dispatcher is going to touch neither.
    """
    epic = core.create_job(conn, "epic", "the epic is done")
    core.create_job(conn, "a step", "the step is done", parent=epic["id"])
    core.create_job(conn, "queued behind it", "later", after=[epic["id"]])
    assert len(core.list_jobs(conn, status="open")) == 3
    assert len(core.ready_jobs(conn)) == 1

    results = doctor.diagnose(db_path)
    assert "1 ready job(s) have no checks" in detail(results, "board")


def test_doctor_notes_an_unset_actor(db_path, monkeypatch):
    monkeypatch.delenv("BEVIS_ACTOR", raising=False)
    results = doctor.diagnose(db_path)
    assert statuses(results, "actor") == ["note"]
    assert "BEVIS_ACTOR" in detail(results, "actor")
    assert doctor.exit_code(results) == 0


# ── Registered adapters ──────────────────────────────────────────────────────
def test_doctor_fails_when_a_registered_adapter_is_not_executable(
        db_path, conn, tmp_path):
    path = script(tmp_path, "my-agent.sh", "echo hi", executable=False)
    adapters.add(conn, "myagent", str(path))
    results = doctor.diagnose(db_path)
    assert doctor.exit_code(results) == 1
    assert "is not executable" in detail(results, "adapters")
    assert "chmod +x %s" % path in detail(results, "adapters")


def test_doctor_fails_when_the_adapter_program_is_not_on_path(db_path, conn):
    adapters.add(conn, "myagent", "definitely-not-a-real-program --go")
    results = doctor.diagnose(db_path)
    assert doctor.exit_code(results) == 1
    assert "not on PATH" in detail(results, "adapters")


def test_doctor_fails_when_the_adapter_file_does_not_exist(db_path, conn, tmp_path):
    adapters.add(conn, "myagent", str(tmp_path / "never-written.sh"))
    results = doctor.diagnose(db_path)
    assert doctor.exit_code(results) == 1
    assert "does not exist" in detail(results, "adapters")


def test_doctor_does_not_call_an_adapter_it_was_not_asked_to_call(
        db_path, conn, tmp_path):
    marker = tmp_path / "it-ran"
    adapters.add(conn, "myagent", str(script(
        tmp_path, "my-agent.sh", "touch %s" % marker)))

    results = doctor.diagnose(db_path)

    assert not marker.exists(), "doctor ran an adapter nobody asked it to run"
    assert statuses(results, "adapters") == ["unproven"]
    assert "never called" in detail(results, "adapters")
    assert "bevis doctor --adapter myagent" in detail(results, "adapters")
    # unproven is not broken, and it is not fine either
    assert doctor.exit_code(results) == 0
    assert "never called, so doctor says nothing" in doctor.render(results)


def test_doctor_probes_the_adapter_it_is_told_to_probe(db_path, conn, tmp_path):
    marker = tmp_path / "it-ran"
    adapters.add(conn, "myagent", str(script(
        tmp_path, "my-agent.sh", "touch %s\necho awake" % marker)))

    results = doctor.diagnose(db_path, probe_name="myagent")

    assert marker.exists()
    assert statuses(results, "adapters") == ["ok"]
    assert "ran and exited 0: awake" in detail(results, "adapters")
    assert doctor.exit_code(results) == 0


def test_doctor_fails_when_the_probed_adapter_exits_nonzero(db_path, conn, tmp_path):
    adapters.add(conn, "myagent", str(script(
        tmp_path, "my-agent.sh", "echo cannot reach my model >&2\nexit 7")))
    results = doctor.diagnose(db_path, probe_name="myagent")
    assert doctor.exit_code(results) == 1
    assert "exited 7: cannot reach my model" in detail(results, "adapters")


def test_doctor_fails_when_the_probed_adapter_never_returns(db_path, conn, tmp_path):
    adapters.add(conn, "myagent", str(script(tmp_path, "my-agent.sh", "sleep 30")))
    results = doctor.diagnose(db_path, probe_name="myagent", timeout=1)
    assert doctor.exit_code(results) == 1
    assert "still running after 1s" in detail(results, "adapters")
    assert "BEVIS_DOCTOR_PROBE" in detail(results, "adapters")


def test_the_probe_hands_the_adapter_a_job_and_says_it_is_a_probe(
        db_path, conn, tmp_path):
    out = tmp_path / "env"
    adapters.add(conn, "myagent", str(script(
        tmp_path, "my-agent.sh",
        'printf "%s|%s|%s" "$BEVIS_DOCTOR_PROBE" "$BEVIS_JOB_DISPLAY_ID" '
        '"$BEVIS_JOB_ACCEPTANCE" > ' + str(out))))
    doctor.diagnose(db_path, probe_name="myagent")
    probe_flag, display_id, acceptance = out.read_text().split("|")
    assert probe_flag == "1"          # so a real agent can answer without working
    assert display_id == "probe"
    assert acceptance


def test_doctor_names_an_adapter_that_was_never_registered(db_path):
    results = doctor.diagnose(db_path, probe_name="myagnt")
    assert doctor.exit_code(results) == 1
    assert "no adapter named 'myagnt' is registered" in detail(results, "adapters")
    assert "bevis adapter list" in detail(results, "adapters")


def test_doctor_probes_a_bare_command_too(db_path, tmp_path):
    marker = tmp_path / "it-ran"
    path = script(tmp_path, "my-agent.sh", "touch %s\necho awake" % marker)
    results = doctor.diagnose(db_path, probe_name=str(path))
    assert marker.exists()
    assert doctor.exit_code(results) == 0


def test_doctor_reports_a_broken_adapter_template_instead_of_rendering_it(
        db_path, conn):
    # render_adapter refuses a placeholder inside quotes; doctor has to surface
    # that refusal rather than crash on it.
    conn.execute("INSERT INTO adapter (name, cmd, note, created_at) "
                 "VALUES ('quoted', 'bash -c ''echo {title}''', '', '2026-01-01')")
    results = doctor.diagnose(db_path, probe_name="quoted")
    assert doctor.exit_code(results) == 1
    assert "inside quotes" in detail(results, "adapters")


# ── Through the command line ─────────────────────────────────────────────────
def test_cli_doctor_exits_nonzero_when_something_is_broken(cli, conn):
    adapters.add(conn, "myagent", "definitely-not-a-real-program")
    code, out, _ = cli("doctor")
    assert code == 1
    assert "FAIL" in out
    assert "problem(s) above" in out


def test_cli_doctor_exits_zero_on_a_clean_board(cli):
    code, out, _ = cli("doctor")
    assert code == 0
    assert "no problems found" in out


def test_cli_doctor_json_carries_the_fix_for_each_problem(cli):
    import json

    code, out, _ = cli("--json", "doctor", "--adapter", "myagnt")
    assert code == 1
    problems = [r for r in json.loads(out) if r["status"] == "FAIL"]
    assert problems and all(r["fix"] for r in problems)


def test_every_doctor_failure_names_a_fix(db_path, conn, tmp_path):
    """The README promises this of every FAIL, so it is asked of every FAIL.

    A promise about all of something, checked on one of them, is the shape of
    claim this project exists to be rude about.
    """
    adapters.add(conn, "notonpath", "definitely-not-a-real-program")
    adapters.add(conn, "missing", str(tmp_path / "never-written.sh"))
    adapters.add(conn, "notexec", str(script(
        tmp_path, "a.sh", "echo hi", executable=False)))
    adapters.add(conn, "crashes", str(script(tmp_path, "b.sh", "exit 9")))
    adapters.add(conn, "hangs", str(script(tmp_path, "c.sh", "sleep 30")))

    boards = [
        doctor.diagnose(tmp_path / "nowhere" / "bevis.db"),          # no database
        doctor.diagnose(db_path),                                    # bad adapters
        doctor.diagnose(db_path, probe_name="crashes"),              # bad exit
        doctor.diagnose(db_path, probe_name="hangs", timeout=1),     # no answer
        doctor.diagnose(db_path, probe_name="never-registered"),     # bad name
    ]
    seen = 0
    for results in boards:
        for item in results:
            if item["status"] != doctor.FAIL:
                continue
            seen += 1
            assert item["fix"], "no fix offered for: %s" % item["detail"]
    assert seen >= 8, "only %d failure modes were exercised" % seen


def test_cli_doctor_runs_before_any_database_exists(tmp_path, monkeypatch, capsys):
    # Every other bevis command opens the board first and dies if it is not
    # there. doctor is the one that has to survive that and say what to type.
    from bevis.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BEVIS_DB", raising=False)
    assert main(["doctor"]) == 1
    assert "run `bevis init`" in capsys.readouterr().out
