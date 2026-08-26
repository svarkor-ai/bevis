"""The example adapters are illustrations, so they have to actually run.

An example that has never been executed is pseudocode with a shebang, and the
first person to copy it discovers that. Each adapter in `examples/` is driven
here the way `bevis run` drives it — same environment, same subprocess — and
`examples/adapter-local-model.py` is pointed at a real HTTP server started in
this process, so the network path is exercised rather than described.

That server is also the clearest statement of the architecture in the whole
suite: the HTTP call lives in the EXAMPLE, which is yours. The bevis package
imports no HTTP library at all (`tests/test_no_dependencies.py`), so the only
thing it knows about the model server below is that a command exited 0.
"""
from __future__ import annotations

import json
import re
import shlex
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from bevis import adapters, core, doctor
from bevis.dispatch import dispatch

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
ECHO = EXAMPLES / "adapter-echo.sh"
AGENT = EXAMPLES / "adapter-agent.sh"
LOCAL_MODEL = EXAMPLES / "adapter-local-model.py"


@pytest.mark.parametrize("path", sorted(EXAMPLES.iterdir()), ids=lambda p: p.name)
def test_every_example_adapter_is_executable_and_documents_itself(path):
    assert path.stat().st_mode & 0o111, "%s is not executable" % path.name
    head = path.read_text(encoding="utf-8")[:1200]
    assert head.startswith("#!"), "%s has no shebang" % path.name
    assert "bevis" in head, "%s never says how it is used" % path.name


# ── The shell-command agent ──────────────────────────────────────────────────
def test_the_agent_example_hands_the_job_to_the_command_you_name(
        conn, db_path, tmp_path, monkeypatch):
    # $MY_AGENT_CMD is the adapter's own configuration. bevis never reads it,
    # never stores it, and could not tell you what it points at.
    monkeypatch.setenv("MY_AGENT_CMD", "tr a-z A-Z")
    core.create_job(conn, "write the changelog", "CHANGELOG.md names the release",
                    description="one line is enough")
    core.add_check(conn, 1, "transcript", "test -s bevis-job-1.log", blocking=True)

    [result] = dispatch(db_path, str(AGENT))

    assert result["outcome"] == "closed"
    transcript = (tmp_path / "bevis-job-1.log").read_text()
    assert "WRITE THE CHANGELOG" in transcript          # the agent saw the title
    assert "THIS IS DONE WHEN: CHANGELOG.MD NAMES THE RELEASE" in transcript


def test_the_agent_example_fails_the_job_when_the_agent_crashes(
        conn, db_path, monkeypatch):
    monkeypatch.setenv("MY_AGENT_CMD", "false")
    core.create_job(conn, "doomed", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    [result] = dispatch(db_path, str(AGENT))
    assert result["outcome"] == "failed"
    assert core.get_job(conn, 1)["status"] == "failed"


def test_the_agent_example_answers_a_doctor_probe_without_running_the_agent(
        db_path, conn, tmp_path, monkeypatch):
    marker = tmp_path / "the-agent-ran"
    monkeypatch.setenv("MY_AGENT_CMD", "touch %s" % marker)
    adapters.add(conn, "myagent", str(AGENT))
    results = doctor.diagnose(db_path, probe_name="myagent")
    assert doctor.exit_code(results) == 0
    assert not marker.exists(), "a doctor probe cost a whole agent run"


def test_the_echo_example_still_prints_the_job_it_was_handed(conn, db_path):
    core.create_job(conn, "a job", "the bar is here")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    dispatch(db_path, str(ECHO))
    assert "the bar is here" in core.job_runs(conn, 1)[0]["stdout"]


# ── The local HTTP model server ──────────────────────────────────────────────
class _Chat(BaseHTTPRequestHandler):
    """The smallest thing that answers like an OpenAI-compatible server."""

    def do_POST(self):                                   # noqa: N802 - stdlib API
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        prompt = body["messages"][0]["content"]
        reply = "OK" if "single word OK" in prompt else "# Notes\n\n%s\n" % prompt
        payload = json.dumps({"choices": [{"message": {"content": reply}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):                       # keep the test output clean
        pass


@pytest.fixture()
def model_server(monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _Chat)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("MODEL_URL", "http://127.0.0.1:%d/v1/chat/completions"
                       % server.server_address[1])
    monkeypatch.setenv("MODEL_NAME", "a-model-bevis-never-hears-about")
    yield server
    server.shutdown()


def local_model_adapter() -> str:
    return "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(LOCAL_MODEL)))


def test_the_local_model_example_closes_a_job_against_a_real_server(
        conn, db_path, tmp_path, model_server):
    core.create_job(conn, "summarise the release", "bevis-job-1.md exists and "
                    "mentions the release")
    core.add_check(conn, 1, "wrote-notes", "grep -q release bevis-job-1.md",
                   blocking=True)

    [result] = dispatch(db_path, local_model_adapter())

    assert result["outcome"] == "closed", result["detail"]
    assert "summarise the release" in (tmp_path / "bevis-job-1.md").read_text()
    # the evidence on the job is the CHECK's command and output, never the
    # model's account of itself
    row = core.get_job(conn, 1)
    assert row["verify_cmd"] == "grep -q release bevis-job-1.md"


def test_the_local_model_example_answers_a_doctor_probe(
        db_path, conn, model_server):
    adapters.add(conn, "localmodel", local_model_adapter())
    results = doctor.diagnose(db_path, probe_name="localmodel")
    assert doctor.exit_code(results) == 0
    reported = [r["detail"] for r in results if r["section"] == "adapters"]
    assert any("answered as a-model-bevis-never-hears-about" in d for d in reported)


def test_the_local_model_example_fails_the_job_when_the_server_is_down(
        conn, db_path, monkeypatch):
    # A closed port is the single most common way this adapter breaks, and the
    # job must end `failed` rather than closed-on-nothing.
    monkeypatch.setenv("MODEL_URL", "http://127.0.0.1:1/v1/chat/completions")
    core.create_job(conn, "a job", "the bar")
    core.add_check(conn, 1, "unit", "echo ok", blocking=True)
    [result] = dispatch(db_path, local_model_adapter())
    assert result["outcome"] == "failed"
    assert "Connection refused" in core.job_runs(conn, 1)[0]["stderr"]


#: A literal that is unmistakably somebody's key, wherever it appears.
TOKEN_LITERAL = re.compile(r"sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}"
                           r"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}")


def test_no_example_shows_a_secret_on_a_command_line():
    # The examples are the thing people copy. If one of them pasted a key onto a
    # command line, the registry's refusal would be advice nobody follows.
    #
    # Two different questions, asked of the two different artifacts.
    # credential_problem() judges what you would REGISTER, so it is applied to
    # the `bevis ...` lines in each header, not to the file as prose — an
    # `export MY_KEY=<yours>` in a comment is the instruction to keep the key
    # out of bevis, not an example of putting one in. A key-shaped literal is
    # refused anywhere at all.
    for path in sorted(EXAMPLES.iterdir()):
        text = path.read_text(encoding="utf-8")
        assert not TOKEN_LITERAL.search(text), "%s contains a key" % path.name
        for number, line in enumerate(text.splitlines(), 1):
            command = line.lstrip("#").strip()
            if not command.startswith("bevis "):
                continue
            problem = adapters.credential_problem(command)
            assert problem is None, ("%s line %d shows %s"
                                     % (path.name, number, problem))
