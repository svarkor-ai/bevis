"""The adapter registry stores a name and a command. That is the product.

bevis's position is that it holds no credential, no endpoint, no model name and
no address: an adapter is a command it executes, and the command owns its own
configuration. A registry is where that position would quietly die — it is
exactly the place a "just one field for the API key" would go — so the rules
that keep it from happening are tested here rather than promised in the docs.
"""
from __future__ import annotations

import pytest

from bevis import adapters, core
from bevis.db import connect
from bevis.dispatch import dispatch
from bevis.errors import NotFound, Refusal, UsageError


def test_the_registry_stores_only_a_name_and_a_command(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(adapter)")}
    assert columns == {"name", "cmd", "note", "created_at"}, (
        "the adapter table grew a column. There is no field here for an "
        "endpoint, a model, a host or a key, and adding one would make bevis "
        "hold configuration it exists in order not to hold.")


def test_a_registered_adapter_name_resolves_to_its_command(conn, db_path):
    adapters.add(conn, "myagent", "bash -c 'echo agent ran'")
    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)

    [result] = dispatch(db_path, "myagent")

    assert result["outcome"] == "closed"
    [run] = core.job_runs(conn, 1)
    # the RESOLVED command is what the run records: evidence names what ran,
    # not the alias it was reached by
    assert run["adapter_cmd"] == "bash -c 'echo agent ran'"
    assert "agent ran" in run["stdout"]


def test_an_unregistered_bare_word_is_still_a_command(conn, db_path):
    # `bevis run --adapter true` has always meant the command `true`, and
    # registering adapters must not quietly change what an unregistered word means.
    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    [result] = dispatch(db_path, "true")
    assert result["outcome"] == "closed"
    assert core.job_runs(conn, 1)[0]["adapter_cmd"] == "true"


def test_registering_an_inline_credential_is_refused(conn):
    with pytest.raises(Refusal) as excinfo:
        adapters.add(conn, "leaky",
                     "curl -H 'Authorization: Bearer 9f2b1c4d8e0a' http://x/v1")
    message = str(excinfo.value)
    assert "Authorization header" in message
    assert "not a secret store" in message
    assert "$MY_API_KEY" in message          # the refusal names the fix
    assert adapters.list_all(conn) == []


PASTED_SECRETS = [
    "agent --api-key sk-EXAMPLE-not-a-real-key",
    "agent --token=hunter2",
    "OPENAI_API_KEY=sk-EXAMPLE-not-a-real-key ./agent.sh",
    "MYSERVICE_TOKEN=abc123 ./agent.sh",
    # more than one underscore-separated segment before the KEY
    "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIabcdEFGHijkl ./agent.sh",
    "curl -H 'x-api-key: abc123def456' http://host/v1",
    "agent --password hunter2",
    "agent --secret=abc123",
    # ...and the same values with quotes around them. Two quote characters used
    # to be enough to walk every one of these past the rule.
    'agent --token="hunter2"',
    'OPENAI_API_KEY="hunter2" ./agent.sh',
    '''curl -H 'x-api-key: "abc123def456"' http://host/v1''',
    'agent --api-key "EXAMPLE-not-a-real-key"',
    # a password in front of the @, and a lowercase leading assignment
    "curl https://user:hunter2@host/v1",
    "api_key=abc123 ./agent.sh",
]

REFERENCES_TO_THE_ENVIRONMENT = [
    "./my-agent.sh",
    "bash -c 'echo hello'",
    "python3 examples/adapter-local-model.py",
    "MODEL_URL=http://127.0.0.1:8080/v1 ./run.sh",
    'agent --api-key "$OPENAI_API_KEY"',
    "curl -H \"Authorization: Bearer $TOKEN\" http://host/v1",
    "agent --api-key $(pass show model/key)",
    "agent --api-key `cat /run/secrets/key`",
    "curl https://user:$TOKEN@host/v1",
    # near misses that are not credentials and must not be treated as any:
    # ordinary build commands that name a KEY, and a port that is not a password
    "docker run --rm -e OPENAI_API_KEY myimage",
    "agent --keyfile /etc/keys/id --token-budget 5000",
    "make KEY=value MONKEY=3",
    "helm install x --set TOKEN=managed",
    "myagent --set IDEMPOTENCY_KEY=job-42",
    "sed -e 's/KEY=old/KEY=new/' notes.txt",
    "curl http://127.0.0.1:8080/v1/models",
]


def test_the_shapes_a_pasted_secret_takes_are_refused(conn):
    for cmd in PASTED_SECRETS:
        with pytest.raises(Refusal, match="not a secret store"):
            adapters.add(conn, "leaky", cmd)
        assert adapters.get(conn, "leaky") is None, "stored: %s" % cmd


def test_a_secret_referenced_from_the_environment_is_not_a_secret(conn):
    # The other direction of the same gate, and the more important one: a rule
    # that refused `$OPENAI_API_KEY` would teach people to obfuscate the literal
    # rather than move it out of bevis. Calibration is two-sided or it is noise.
    for index, cmd in enumerate(REFERENCES_TO_THE_ENVIRONMENT):
        assert adapters.credential_problem(cmd) is None, cmd
        name = "fine%d" % index
        adapters.add(conn, name, cmd)
        assert adapters.get(conn, name)["cmd"] == cmd


def test_a_duplicate_name_is_refused_rather_than_overwritten(conn):
    adapters.add(conn, "myagent", "./a.sh")
    with pytest.raises(UsageError) as excinfo:
        adapters.add(conn, "myagent", "./b.sh")
    assert "already registered" in str(excinfo.value)
    assert adapters.get(conn, "myagent")["cmd"] == "./a.sh"


@pytest.mark.parametrize("name", ["", "my agent", "-agent", "./agent", "a;b"])
def test_a_name_that_is_not_an_identifier_is_refused(conn, name):
    with pytest.raises(UsageError):
        adapters.add(conn, name, "./a.sh")


def test_a_name_that_is_also_a_program_is_refused(conn, db_path):
    """Registering `true` would change what `--adapter true` means, silently.

    Not for the person registering it — for everyone else already using the
    board, whose working command line quietly starts running something else.
    """
    with pytest.raises(Refusal) as excinfo:
        adapters.add(conn, "true", "bash -c 'touch SHADOW-RAN'")
    assert "also a program on this PATH" in str(excinfo.value)

    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    dispatch(db_path, "true")
    assert core.job_runs(conn, 1)[0]["adapter_cmd"] == "true"


def test_a_template_that_can_never_render_is_refused_at_registration(conn):
    # Otherwise it registers cleanly and blows up at `bevis run`, after a job
    # has been claimed for it.
    with pytest.raises(UsageError) as excinfo:
        adapters.add(conn, "quoted", "bash -c 'echo {title}'")
    assert "inside quotes" in str(excinfo.value)
    assert adapters.get(conn, "quoted") is None


def test_removing_an_adapter_that_was_never_registered_is_an_error(conn):
    with pytest.raises(NotFound):
        adapters.remove(conn, "ghost")


def test_the_registry_round_trips_through_the_cli(cli):
    code, out, _ = cli("adapter", "add", "myagent", "--cmd", "./my-agent.sh",
                       "--note", "the one on this laptop")
    assert code == 0 and "registered adapter myagent = ./my-agent.sh" in out

    code, out, _ = cli("adapter", "list")
    assert code == 0
    assert "myagent" in out and "./my-agent.sh" in out and "laptop" in out

    code, out, _ = cli("adapter", "remove", "myagent")
    assert code == 0 and "removed adapter myagent" in out

    code, out, _ = cli("adapter", "list")
    assert "(no adapters registered)" in out


def test_the_cli_refuses_to_store_a_credential(cli):
    code, _, err = cli("adapter", "add", "leaky", "--cmd",
                       "agent --api-key sk-EXAMPLE-not-a-real-key")
    assert code == 1
    assert "not a secret store" in err


def test_cli_run_accepts_a_registered_name(cli, conn):
    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    cli("adapter", "add", "myagent", "--cmd", "bash -c 'echo agent ran'")
    code, out, _ = cli("run", "--adapter", "myagent")
    assert code == 0 and "closed" in out


def test_registering_an_adapter_is_written_to_the_event_log(conn):
    adapters.add(conn, "myagent", "./my-agent.sh", actor="alice")
    kinds = [(r["kind"], r["actor"], r["detail"]) for r in
             conn.execute("SELECT * FROM event ORDER BY id")]
    assert ("adapter_added", "alice", "myagent = ./my-agent.sh") in kinds


# ── Boards written by an older bevis ─────────────────────────────────────────
def _drop_the_registry(db_path):
    conn = connect(db_path)
    conn.execute("DROP TABLE adapter")
    conn.close()


def test_a_board_that_predates_the_registry_names_the_one_command_that_fixes_it(
        db_path):
    _drop_the_registry(db_path)
    conn = connect(db_path)
    with pytest.raises(UsageError) as excinfo:
        adapters.add(conn, "myagent", "./my-agent.sh")
    assert "`bevis init` again" in str(excinfo.value)
    conn.close()


def test_a_board_that_predates_the_registry_still_dispatches(conn, db_path):
    # Adding the registry must not break a board that has not got one: a
    # command template never needed the table and still must not.
    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    core.create_job(conn, "another", "the bar")
    core.add_check(conn, 2, "unit", "echo ok", blocking=True)
    conn.close()
    _drop_the_registry(db_path)
    # A bare word is the path that actually reaches the registry lookup; a
    # command template short-circuits before it and proves nothing.
    assert dispatch(db_path, "true", max_jobs=1)[0]["outcome"] == "closed"
    assert dispatch(db_path, "bash -c 'echo worked'")[0]["outcome"] == "closed"
