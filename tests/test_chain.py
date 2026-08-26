"""The event log's hash chain, tested in both directions.

A tamper detector that has only ever been run against a clean log is the
"check that cannot fail" this project exists to refuse, so every test below is
one of a pair: something is planted and must be caught AND located, or nothing
is planted and the chain must come back clean. The planted edits are made with
raw `sqlite3` against the file, which is the actual threat — somebody with the
board and no interest in going through bevis to change it.

The chain is tamper-EVIDENT, not tamper-proof, and the tests say so by what they
do not claim: nothing here asserts that an edit is prevented. It is not.
"""
from __future__ import annotations

import hashlib
import sqlite3

import pytest

from bevis import chain, core
from bevis.db import connect, init_db
from bevis.dispatch import dispatch


# ── Helpers ──────────────────────────────────────────────────────────────────
def edit_the_file(db_path, sql, params=()):
    """Reach past bevis and change the database, the way an attacker would."""
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        raw.execute(sql, params)
    finally:
        raw.close()


def busy_board(conn):
    """A few jobs, a close and a verify: a log with something in it."""
    core.create_job(conn, "first", "the first bar", actor="alice")
    core.close_job(conn, 1, "pytest -q", 0, "3 passed", actor="alice")
    core.verify_job(conn, 1, "bo")
    core.create_job(conn, "second", "the second bar", actor="alice")
    return conn


#: The event table exactly as a bevis that predates the chain declared it. Used
#: to build a board to adopt, because "an old board" has to be the literal old
#: shape and not a current one with the columns blanked out.
OLD_EVENT_TABLE = """
CREATE TABLE event (
  id      INTEGER PRIMARY KEY,
  ts      TEXT    NOT NULL,
  job_id  INTEGER,
  actor   TEXT    NOT NULL DEFAULT '',
  kind    TEXT    NOT NULL,
  detail  TEXT    NOT NULL DEFAULT ''
);
"""


def unchain_the_board(db_path):
    """Turn a current board back into one written before the chain existed.

    Faithful, not approximate: the old column list, no chain rows in `meta`, and
    no `chain_started` event — a board from bevis 0.2.x has never heard of any
    of them. Testing adoption against a current board with the columns blanked
    out would be testing a reconstruction instead of the artifact.
    """
    raw = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        rows = raw.execute(
            "SELECT ts, job_id, actor, kind, detail FROM event "
            "WHERE kind != ? ORDER BY id", (chain.GENESIS_KIND,)).fetchall()
        raw.execute("DROP TABLE event")
        raw.executescript(OLD_EVENT_TABLE)
        raw.executemany("INSERT INTO event (ts, job_id, actor, kind, detail) "
                        "VALUES (?,?,?,?,?)", rows)
        raw.execute("DELETE FROM meta WHERE key LIKE 'chain_%'")
    finally:
        raw.close()


@pytest.fixture()
def old_board(db_path):
    """A board with jobs and events on it, and no chain anywhere."""
    conn = connect(db_path)
    busy_board(conn)
    conn.close()
    unchain_the_board(db_path)
    return db_path


# ── An untouched log verifies clean ──────────────────────────────────────────
def test_an_untouched_log_verifies_clean(conn):
    busy_board(conn)
    report = chain.verify(conn)
    assert report["ok"] and report["problem"] is None
    assert report["verified"] == report["events"] > 4
    assert "chain ok" in chain.render(report)


def test_every_event_bevis_writes_is_hashed(conn):
    """No second writer. If some path inserted an event around log_event, the
    chain would be a habit of the callers rather than a property of the table."""
    core.create_job(conn, "first", "the bar", actor="alice")
    core.close_job(conn, 1, "pytest -q", 0, "3 passed", actor="alice")
    core.reopen_job(conn, 1, "on second thoughts", actor="alice")
    core.claim(conn, 1, actor="bo")
    core.add_check(conn, 1, "unit", "echo 1 passed", blocking=True)
    core.run_checks(conn, 1)
    core.set_status(conn, 1, "blocked", reason="waiting", actor="alice")
    core.create_job(conn, "second", "the bar", actor="alice")
    rows = list(conn.execute("SELECT id, kind, hash FROM event ORDER BY id"))
    assert len(rows) >= 9
    unhashed = [r["kind"] for r in rows if not r["hash"]]
    assert not unhashed, "these event kinds were written without a hash: %s" % unhashed
    assert chain.verify(conn)["ok"]


def test_the_chain_survives_four_slots_appending_at_once(conn, db_path):
    """Two workers appending at the same instant must not fork the chain.

    A fork looks exactly like a tamper, so a chain that only holds when one
    process is writing would fail on a real board and be blamed on an attacker.
    """
    for index in range(8):
        job = core.create_job(conn, "job %d" % index, "the bar")
        core.add_check(conn, job["id"], "unit", "echo 1 passed", blocking=True)
    results = dispatch(db_path, "bash -c 'echo worked'", slots=4)
    assert len(results) == 8 and all(r["outcome"] == "closed" for r in results)
    report = chain.verify(connect(db_path))
    assert report["ok"], chain.render(report)
    assert report["verified"] == report["events"]


# ── A planted edit is detected AND located ───────────────────────────────────
def test_a_planted_edit_to_a_historical_row_is_detected_and_named(conn, db_path):
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "UPDATE event SET detail='nothing happened' WHERE id=2")

    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "content-changed"
    assert report["problem"]["event_id"] == 2
    rendered = chain.render(report)
    assert "chain BROKEN at event 2" in rendered
    assert "event 1 verifies clean" in rendered


@pytest.mark.parametrize("column, value", [
    ("actor", "somebody-else"),
    ("kind", "verified"),
    ("ts", "2020-01-01T00:00:00.000000Z"),
    ("job_id", "None"),
])
def test_an_edit_to_any_hashed_column_is_detected(conn, db_path, column, value):
    """Every field in the canonical form is load-bearing, not just `detail`."""
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "UPDATE event SET %s=? WHERE id=3" % column,
                  (None if value == "None" else value,))
    report = chain.verify(connect(db_path))
    assert not report["ok"] and report["problem"]["event_id"] == 3


def test_an_edit_is_named_at_the_first_broken_row_not_the_last(conn, db_path):
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "UPDATE event SET detail='a' WHERE id=2")
    edit_the_file(db_path, "UPDATE event SET detail='b' WHERE id=4")
    report = chain.verify(connect(db_path))
    assert report["problem"]["event_id"] == 2


def test_a_deleted_event_is_detected_at_the_next_link(conn, db_path):
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "DELETE FROM event WHERE id=2")
    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "broken-link"
    assert report["problem"]["event_id"] == 3
    assert "does not follow the event before it" in chain.render(report)


def test_rehashing_the_edited_row_still_breaks_the_next_link(conn, db_path):
    """The interesting attacker: one who knows the format and fixes one row.

    Every hash after it is now wrong, so the break moves rather than vanishing —
    which is the entire reason each event hashes the one before it instead of
    just hashing itself.
    """
    busy_board(conn)
    conn.close()
    raw = connect(db_path)
    row = raw.execute("SELECT * FROM event WHERE id=2").fetchone()
    forged = chain.event_hash(row["prev_hash"], 2, row["ts"], row["job_id"],
                              row["actor"], row["kind"], "a nicer story")
    raw.close()
    edit_the_file(db_path, "UPDATE event SET detail='a nicer story', hash=? WHERE id=2",
                  (forged,))

    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "broken-link"
    assert report["problem"]["event_id"] == 3


def test_a_hand_inserted_event_is_detected_as_unchained(conn, db_path):
    busy_board(conn)
    conn.close()
    edit_the_file(
        db_path,
        "INSERT INTO event (ts, job_id, actor, kind, detail) "
        "VALUES ('2026-01-01T00:00:00.000000Z', 1, 'nobody', 'verified', 'ok')")
    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "unchained"
    assert "carries no hash" in report["problem"]["detail"]


def test_events_cut_off_the_end_are_detected_by_the_recorded_head(conn, db_path):
    """A chain says nothing about a row that is no longer there: lop the last
    three events off and what remains is a perfectly valid chain. The head
    written down beside it is the only thing that notices."""
    busy_board(conn)
    last = conn.execute("SELECT MAX(id) FROM event").fetchone()[0]
    conn.close()
    edit_the_file(db_path, "DELETE FROM event WHERE id > ?", (last - 2,))

    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "truncated"
    assert report["problem"]["event_id"] == last
    assert "what followed them is gone" in chain.render(report)


def test_an_emptied_log_is_reported_as_emptied_not_as_clean(conn, db_path):
    """Deleting every event leaves a chain with nothing in it, which is
    arithmetically unbroken and obviously wrong. The recorded head is what
    tells the difference."""
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "DELETE FROM event")
    report = chain.verify(connect(db_path))
    assert not report["ok"] and report["problem"]["kind"] == "truncated"
    assert "the log has no events left" in report["problem"]["detail"]
    assert "no event verified before the break" in chain.render(report)


def test_a_chain_with_no_recorded_start_is_refused(conn, db_path):
    """A chain that begins at an unrecorded point is a provenance claim with a
    hole in it, so the checker refuses one rather than reporting it clean."""
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "DELETE FROM meta WHERE key='chain_started_at'")
    report = chain.verify(connect(db_path))
    assert not report["ok"]
    assert report["problem"]["kind"] == "no-recorded-start"


# ── The canonical form ───────────────────────────────────────────────────────
def test_the_printed_canonical_bytes_hash_to_the_stored_hash(conn, db_path, cli):
    """`bevis check --chain --bytes N | sha256sum` must reproduce the stored hash.

    This is the anti-log-stub test for the documentation: if the bytes bevis
    SAYS it hashed are not the bytes it did hash, the published canonicalisation
    is decoration and nobody could ever recompute one by hand.
    """
    busy_board(conn)
    stored = conn.execute("SELECT hash FROM event WHERE id=3").fetchone()[0]
    code, out, _ = cli("check", "--chain", "--bytes", "3")
    assert code == 0
    assert hashlib.sha256(out.encode("utf-8")).hexdigest() == stored


def test_the_canonical_bytes_are_the_documented_shape(conn):
    busy_board(conn)
    row = conn.execute("SELECT * FROM event WHERE id=2").fetchone()
    text = chain.row_bytes(row, row["prev_hash"]).decode("utf-8")
    assert text.startswith("bevis-event-v1\n")
    for field in ("prev", "id", "ts", "job_id", "actor", "kind", "detail"):
        assert "\n%s=" % field in "\n" + text
    assert "actor=%d:%s\n" % (len(row["actor"].encode("utf-8")), row["actor"]) in text


def test_the_first_event_links_to_the_genesis_value(conn):
    first = conn.execute("SELECT * FROM event ORDER BY id LIMIT 1").fetchone()
    assert first["prev_hash"] == chain.GENESIS_PREV == "0" * 64


def test_a_field_containing_a_newline_cannot_impersonate_another_field():
    """The length prefix is what makes the form unambiguous, and here is the
    collision it prevents — built here so the rule is tested against a real
    ambiguity rather than against the assertion that there is one.

    `detail` is free text somebody else writes. Without the length prefix these
    two different events render to the same bytes, so one of them could be
    swapped for the other and the hash would not notice.
    """
    common = dict(prev_hash=chain.GENESIS_PREV, event_id=7,
                  ts="2026-01-01T00:00:00.000000Z", job_id=1)
    one = dict(actor="x\nkind=y\ndetail=z", kind="w", detail="v")
    two = dict(actor="x", kind="y", detail="z\nkind=w\ndetail=v")
    assert one != two

    def without_lengths(fields):
        return "".join("%s=%s\n" % (name, fields[name])
                       for name in ("actor", "kind", "detail"))

    # The premise: dropped lengths really would collide. If this ever stops
    # being true the test below is passing for the wrong reason.
    assert without_lengths(one) == without_lengths(two)
    assert chain.event_hash(**common, **one) != chain.event_hash(**common, **two)


def test_the_hashed_bytes_name_their_own_format_version():
    """An old row must not be silently reinterpretable under new rules."""
    payload = chain.canonical_bytes(chain.GENESIS_PREV, 1, "t", None, "a", "k", "d")
    assert payload.startswith(b"bevis-event-v1\n")
    assert chain.HASH_NAME == "sha256"


# ── Starting the chain, and saying which way it started ──────────────────────
def test_a_fresh_board_records_that_its_chain_started_from_nothing(conn):
    report = chain.verify(conn)
    assert report["mode"] == chain.MODE_FRESH
    assert report["sealed_through"] == 0
    genesis = conn.execute(
        "SELECT * FROM event WHERE kind='chain_started'").fetchone()
    assert genesis is not None and genesis["hash"]
    assert "hashed as it was written" in genesis["detail"]
    assert "every event on this board was hashed" in chain.render(report)


def test_adopting_a_board_that_already_had_events_seals_them_and_says_so(old_board):
    before = connect(old_board)
    existing = before.execute("SELECT COUNT(*) FROM event").fetchone()[0]
    before.close()
    assert existing >= 4

    init_db(old_board)                       # the one command the refusal names

    conn = connect(old_board)
    report = chain.verify(conn)
    assert report["ok"], chain.render(report)
    assert report["mode"] == chain.MODE_ADOPTED
    assert report["sealed_through"] == existing
    genesis = conn.execute(
        "SELECT * FROM event WHERE kind='chain_started' ORDER BY id").fetchone()
    assert genesis["id"] == existing + 1
    # The honest half: sealed as they stood, not as they were written.
    assert "existed already and were sealed as they stood" in genesis["detail"]
    assert "not when they were written" in chain.render(report)


def test_an_adopted_board_keeps_verifying_as_new_events_arrive(old_board):
    init_db(old_board)
    conn = connect(old_board)
    core.create_job(conn, "after the migration", "the bar", actor="alice")
    core.close_job(conn, 3, "pytest -q", 0, "9 passed", actor="alice")
    assert chain.verify(conn)["ok"]


def test_an_edit_to_a_row_sealed_at_adoption_is_still_detected(old_board):
    """Sealing is worth something: it cannot prove what the row said before the
    chain existed, and it does prove nothing has touched it since."""
    init_db(old_board)
    edit_the_file(old_board, "UPDATE event SET detail='rewritten' WHERE id=1")
    report = chain.verify(connect(old_board))
    assert not report["ok"] and report["problem"]["event_id"] == 1


def test_running_init_twice_does_not_restart_the_chain(db_path, conn):
    busy_board(conn)
    started = chain.get_meta(conn, chain.META_STARTED_AT)
    conn.close()

    init_db(db_path)

    conn = connect(db_path)
    assert chain.get_meta(conn, chain.META_STARTED_AT) == started
    assert conn.execute(
        "SELECT COUNT(*) FROM event WHERE kind='chain_started'").fetchone()[0] == 1
    assert chain.verify(conn)["ok"]


def test_init_does_not_launder_a_tamper(conn, db_path):
    """`bevis init` must never re-seal a chain that is already started.

    Re-hashing whatever is in the file today would turn every planted edit into
    a clean chain and call it a migration — the tool quietly destroying the
    evidence it exists to keep. So a started chain is never restarted, and a
    broken one stays broken until a human looks at it.
    """
    busy_board(conn)
    conn.close()
    edit_the_file(db_path, "UPDATE event SET detail='rewritten' WHERE id=2")

    init_db(db_path)

    report = chain.verify(connect(db_path))
    assert not report["ok"], "bevis init re-sealed a tampered log"
    assert report["problem"]["event_id"] == 2


def test_an_old_board_names_the_one_command_that_fixes_it(old_board):
    report = chain.verify(connect(old_board))
    assert not report["ok"] and report["problem"]["kind"] == "no-chain"
    assert "`bevis init` again" in report["problem"]["fix"]
    assert "chain unavailable" in chain.render(report)


def test_an_old_board_still_records_events(old_board):
    """Losing the audit trail to protect a chain that does not exist yet would
    be the wrong trade, so an un-migrated board keeps working."""
    conn = connect(old_board)
    core.create_job(conn, "still working", "the bar", actor="alice")
    kinds = [r["kind"] for r in conn.execute("SELECT kind FROM event ORDER BY id")]
    assert kinds[-1] == "created"


# ── Through the CLI ──────────────────────────────────────────────────────────
def test_cli_check_chain_exits_0_on_an_untouched_log(conn, cli):
    busy_board(conn)
    code, out, _ = cli("check", "--chain")
    assert code == 0
    assert out.startswith("chain ok")
    assert "head       " in out


def test_cli_check_chain_exits_1_and_names_the_row_on_a_tampered_log(
        conn, db_path, cli):
    busy_board(conn)
    edit_the_file(db_path, "UPDATE event SET actor='mallory' WHERE id=2")
    code, out, _ = cli("check", "--chain")
    assert code == 1
    assert "chain BROKEN at event 2" in out


def test_cli_check_chain_json_carries_the_head_and_the_problem(conn, db_path, cli):
    import json

    busy_board(conn)
    edit_the_file(db_path, "UPDATE event SET detail='x' WHERE id=2")
    code, out, _ = cli("--json", "check", "--chain")
    report = json.loads(out)
    assert code == 1
    assert report["ok"] is False and report["problem"]["event_id"] == 2
    assert len(report["head_hash"]) == 64


def test_cli_check_without_a_subcommand_says_what_it_needs(cli):
    """Exit 2 — the code argparse gave when the subcommand was mandatory. A new
    flag must not change what an existing script sees for an old mistake."""
    code, _, err = cli("check")
    assert code == 2
    assert "add, list, run or rm" in err and "--chain" in err


def test_cli_check_chain_bytes_refuses_an_id_that_is_not_one(conn, cli):
    busy_board(conn)
    code, _, err = cli("check", "--chain", "--bytes", "nonsense")
    assert code == 2 and "takes an event id" in err
    code, _, err = cli("check", "--chain", "--bytes", "9999")
    assert code == 3 and "no event 9999" in err


def test_cli_bytes_without_chain_is_refused(conn, cli):
    busy_board(conn)
    code, _, err = cli("check", "--bytes", "1")
    assert code == 2 and "--chain" in err


# ── doctor ───────────────────────────────────────────────────────────────────
def test_doctor_fails_when_the_event_log_has_been_edited(conn, db_path, cli):
    from bevis import doctor

    busy_board(conn)
    edit_the_file(db_path, "UPDATE event SET detail='x' WHERE id=2")
    results = doctor.diagnose(db_path)
    chain_results = [r for r in results if r["section"] == "chain"]
    assert [r["status"] for r in chain_results] == ["FAIL"]
    assert "event 2" in chain_results[0]["detail"]
    assert doctor.exit_code(results) == 1


def test_doctor_reports_the_chain_it_actually_verified(conn, db_path):
    from bevis import doctor

    busy_board(conn)
    results = doctor.diagnose(db_path)
    chain_results = [r for r in results if r["section"] == "chain"]
    assert [r["status"] for r in chain_results] == ["ok"]
    assert "hash chain intact" in chain_results[0]["detail"]
