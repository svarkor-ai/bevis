"""The optional HTTP API enforces the same rules — because it calls the same code.

These tests drive a real uvicorn server over a real socket using nothing but
stdlib urllib, so they also prove the API is usable from a client that has no
special library.

The whole module skips when the [api] extra is not installed, which is the
supported state for a stdlib-only install.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("uvicorn")

from bevis import core  # noqa: E402
from bevis.api import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_api(db_path):
    import uvicorn

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(create_app(db_path), host="127.0.0.1",
                                           port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % port
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            urllib.request.urlopen(base + "/healthz", timeout=0.5).read()
            break
        except Exception:
            time.sleep(0.05)
    else:  # pragma: no cover
        server.should_exit = True
        raise RuntimeError("API did not start")
    try:
        yield base
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call(base, method, path, body=None, token=None):
    """Returns (status, parsed_json). Never raises on a 4xx — the refusals ARE
    the thing under test."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer %s" % token)
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


@pytest.fixture()
def api(db_path):
    with running_api(db_path) as base:
        yield base


def test_create_and_read_a_job(api):
    status, job = call(api, "POST", "/jobs",
                       {"title": "api job", "acceptance": "it works"})
    assert status == 201 and job["status"] == "open"
    status, fetched = call(api, "GET", "/jobs/%d" % job["id"])
    assert status == 200 and fetched["title"] == "api job"


def test_create_without_a_bar_is_422(api):
    status, body = call(api, "POST", "/jobs", {"title": "barless"})
    assert status == 422
    assert "acceptance is required" in body["error"]


def test_close_without_evidence_is_409(api):
    _, job = call(api, "POST", "/jobs", {"title": "j", "acceptance": "bar"})
    status, body = call(api, "POST", "/jobs/%d/close" % job["id"], {})
    assert status == 409
    assert "refusing to close" in body["refused"]
    _, fetched = call(api, "GET", "/jobs/%d" % job["id"])
    assert fetched["status"] == "open"


def test_close_with_evidence_works_over_http(api):
    _, job = call(api, "POST", "/jobs", {"title": "j", "acceptance": "bar"})
    status, closed = call(api, "POST", "/jobs/%d/close" % job["id"],
                          {"verify_cmd": "make test", "verify_exit": 0,
                           "verify_output": "ok", "actor": "ci"})
    assert status == 200 and closed["status"] == "closed"


def test_nonzero_exit_is_still_refused_over_http(api):
    _, job = call(api, "POST", "/jobs", {"title": "j", "acceptance": "bar"})
    status, body = call(api, "POST", "/jobs/%d/close" % job["id"],
                        {"verify_cmd": "make test", "verify_exit": 1,
                         "verify_output": "boom"})
    assert status == 409 and "not 0" in body["refused"]


def test_verify_by_the_closer_is_409_over_http(api):
    _, job = call(api, "POST", "/jobs", {"title": "j", "acceptance": "bar"})
    call(api, "POST", "/jobs/%d/close" % job["id"],
         {"verify_cmd": "t", "verify_exit": 0, "verify_output": "ok", "actor": "ci"})
    status, body = call(api, "POST", "/jobs/%d/verify" % job["id"], {"actor": "ci"})
    assert status == 409 and "cannot verify it" in body["refused"]
    status, _ = call(api, "POST", "/jobs/%d/verify" % job["id"], {"actor": "human"})
    assert status == 200


def test_reparenting_is_409_over_http(api):
    _, parent = call(api, "POST", "/jobs", {"title": "epic", "acceptance": "bar"})
    _, flat = call(api, "POST", "/jobs", {"title": "flat", "acceptance": "bar"})
    status, body = call(api, "PATCH", "/jobs/%d" % flat["id"],
                        {"parent_id": parent["id"]})
    assert status == 409 and "create time" in body["refused"]


def test_unknown_status_is_422_over_http(api):
    _, job = call(api, "POST", "/jobs", {"title": "j", "acceptance": "bar"})
    status, body = call(api, "POST", "/jobs/%d/status" % job["id"],
                        {"status": "done"})
    assert status == 422 and "unknown status" in body["error"]


def test_an_unknown_status_filter_is_422_not_an_empty_list(api):
    status, body = call(api, "GET", "/jobs?status=done")
    assert status == 422 and "unknown status" in body["error"]


def test_unknown_job_is_404_over_http(api):
    status, _ = call(api, "GET", "/jobs/999")
    assert status == 404


def test_ready_endpoint_mirrors_the_cli(api, db_path):
    from bevis.db import connect

    conn = connect(db_path)
    blocker = core.create_job(conn, "blocker", "bar")
    core.create_job(conn, "dependent", "bar", after=[blocker["id"]])
    conn.close()
    status, ready = call(api, "GET", "/ready")
    assert status == 200
    assert [j["title"] for j in ready] == ["blocker"]


def test_bearer_token_is_enforced_when_configured(db_path, monkeypatch):
    monkeypatch.setenv("BEVIS_TOKEN", "s3cret")
    with running_api(db_path) as base:
        assert call(base, "GET", "/jobs")[0] == 401
        assert call(base, "GET", "/jobs", token="wrong")[0] == 401
        assert call(base, "GET", "/jobs", token="s3cret")[0] == 200
        # health is deliberately open, so a load balancer can use it
        assert call(base, "GET", "/healthz")[0] == 200
