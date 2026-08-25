"""Creating jobs: the bar is mandatory, and the shape of a plan is fixed at
create time."""
from __future__ import annotations

import pytest

from bevis import core
from bevis.errors import NotFound, Refusal, UsageError
from bevis.model import EXIT_REFUSED, EXIT_USAGE


def test_acceptance_is_required(conn):
    with pytest.raises(UsageError) as excinfo:
        core.create_job(conn, "no bar here", "")
    assert "acceptance is required" in str(excinfo.value)


def test_acceptance_of_only_whitespace_is_not_a_bar(conn):
    with pytest.raises(UsageError):
        core.create_job(conn, "no bar here", "   \n\t ")


def test_cli_refuses_add_without_acceptance(cli):
    # argparse itself makes --acceptance mandatory: the bar is not something you
    # can forget, it is something the tool will not proceed without.
    with pytest.raises(SystemExit) as excinfo:
        cli("add", "titled but unbarred")
    assert excinfo.value.code == EXIT_USAGE


def test_cli_refuses_empty_acceptance(cli):
    code, _, err = cli("add", "titled", "--acceptance", "   ")
    assert code == EXIT_USAGE
    assert "acceptance is required" in err


def test_title_is_required(conn):
    with pytest.raises(UsageError):
        core.create_job(conn, "  ", "a bar")


def test_children_get_dotted_display_ids(conn):
    parent = core.create_job(conn, "epic", "all steps done")
    first = core.create_job(conn, "T1: step one", "step one works",
                            parent=parent["id"])
    second = core.create_job(conn, "T2: step two", "step two works",
                             parent=parent["id"])
    assert parent["display_id"] == "1"
    assert first["display_id"] == "1.1"
    assert second["display_id"] == "1.2"


def test_dotted_and_internal_ids_both_resolve(conn):
    parent = core.create_job(conn, "epic", "bar")
    child = core.create_job(conn, "T1: step", "bar", parent=parent["id"])
    by_dotted = core.job_dict(conn, core.get_job(conn, "1.1"))
    by_internal = core.job_dict(conn, core.get_job(conn, child["id"]))
    assert by_dotted["id"] == by_internal["id"] == child["id"]


def test_unknown_id_is_a_loud_404_not_a_silent_noop(conn):
    with pytest.raises(NotFound):
        core.get_job(conn, 999)
    with pytest.raises(NotFound):
        core.get_job(conn, "3.7")
    with pytest.raises(NotFound):
        core.get_job(conn, "not-an-id")


def test_dangling_parent_is_refused(conn):
    with pytest.raises(NotFound):
        core.create_job(conn, "orphan", "bar", parent=404)


def test_parent_id_cannot_be_set_on_update(conn):
    parent = core.create_job(conn, "epic", "bar")
    other = core.create_job(conn, "flat job", "bar")
    with pytest.raises(Refusal) as excinfo:
        core.update_job(conn, other["id"], parent_id=parent["id"])
    assert "create time" in str(excinfo.value)
    # and the row is untouched
    assert core.get_job(conn, other["id"])["parent_id"] is None


def test_cli_refuses_reparenting(cli, conn):
    core.create_job(conn, "epic", "bar")
    core.create_job(conn, "flat", "bar")
    code, _, err = cli("update", "2", "--parent", "1")
    assert code == EXIT_REFUSED
    assert "parent_id is settable only at create time" in err


def test_update_edits_prose_only(conn, job):
    updated = core.update_job(conn, job["id"], title="renamed",
                              acceptance="a sharper bar")
    assert updated["title"] == "renamed"
    assert updated["acceptance"] == "a sharper bar"
    with pytest.raises(UsageError):
        core.update_job(conn, job["id"], status="closed")


def test_acceptance_cannot_be_emptied_by_update(conn, job):
    with pytest.raises(UsageError):
        core.update_job(conn, job["id"], acceptance="")


def test_dependency_graph_is_acyclic_by_construction(conn):
    # A blocker must already exist, so every edge points backwards in id order.
    # There is no way to express a cycle through the API at all.
    first = core.create_job(conn, "one", "bar")
    second = core.create_job(conn, "two", "bar", after=[first["id"]])
    assert second["blockers"] == [first["id"]]
    with pytest.raises(NotFound):
        core.create_job(conn, "three", "bar", after=[999])
