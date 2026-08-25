"""Shared fixtures.

Every test runs against a real SQLite file in a tmp directory and, where it
matters, through the real CLI entry point. Testing the rules by calling core.py
directly would leave the question "does the command line actually enforce this?"
unanswered, which is the question users care about.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bevis import core  # noqa: E402
from bevis.cli import main  # noqa: E402
from bevis.db import connect, init_db  # noqa: E402


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    path = init_db(tmp_path / ".bevis" / "bevis.db")
    monkeypatch.setenv("BEVIS_DB", str(path))
    monkeypatch.setenv("BEVIS_ACTOR", "tester")
    monkeypatch.chdir(tmp_path)
    return path


@pytest.fixture()
def conn(db_path):
    connection = connect(db_path)
    yield connection
    connection.close()


@pytest.fixture()
def cli(db_path, capsys):
    """Run the CLI in-process; return (exit_code, stdout, stderr)."""

    def run(*argv):
        code = main([str(a) for a in argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


@pytest.fixture()
def job(conn):
    """A plain open job with a bar."""
    return core.create_job(conn, "a job", "the thing works", actor="alice")
