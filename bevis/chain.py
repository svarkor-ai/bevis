"""The event log's hash chain: what is hashed, in what order, and what breaks it.

Every row in `event` carries the hash of the row before it. Change one byte of a
historical row and its own hash stops matching; delete a row and the next row
stops following the one before it. Neither is prevented — bevis cannot stop you
editing a SQLite file it does not own — but neither can happen quietly any more,
and `bevis check --chain` names the first row where the arithmetic fails.

Be precise about what that is worth. This is tamper-EVIDENT, not tamper-proof.
The chain lives in the same file as the log it protects, so somebody who holds
the file and knows this format can rewrite every row, recompute every hash, and
move the recorded head to match. What the chain removes is the cheap version:
one UPDATE on one row, invisible afterwards. What closes the rest of the gap is
not more code here — it is copying the head hash somewhere else, which is why
`bevis check --chain` prints it.

There is no key, no signature and no clock authority in this file. Adding one
would mean bevis holding a secret, and a tool whose selling point is that it has
no field for your credentials does not get to grow one for its own.


THE CANONICAL FORM
------------------
The bytes that are hashed for one event are, in this exact order, with no
separators other than the ones shown and no encoding other than UTF-8:

    bevis-event-v1\n
    prev=<n>:<the previous event's hash, or 64 zeros for the first>\n
    id=<n>:<the event's integer id, in decimal>\n
    ts=<n>:<the event's timestamp, exactly as stored>\n
    job_id=<n>:<the job's integer id, or empty when the event has no job>\n
    actor=<n>:<the actor, exactly as stored>\n
    kind=<n>:<the kind, exactly as stored>\n
    detail=<n>:<the detail, exactly as stored>\n

`<n>` is the LENGTH IN BYTES of the value that follows the colon, in decimal.
That length prefix is the only reason the form is unambiguous: `detail` is free
text written by whoever closed the job, it can and does contain newlines and
`=` signs, and without a length a detail could be written that reproduces the
lines of some other event exactly. The test named
`test_a_field_containing_a_newline_cannot_impersonate_another_field` builds that
collision and asserts the two hashes differ, and the mutant
`chain-canonical-form-drops-the-length-prefix` proves the test can fail.

The hash is `sha256` of those bytes, lowercase hex. Nothing is normalised on the
way in: no trimming, no case folding, no JSON. A canonicalisation with choices
in it is a canonicalisation two implementations can disagree about.

To recompute one by hand, with no bevis code in the loop:

    bevis check --chain --bytes 4 | sha256sum

That prints the bytes above for event 4 and hands them to a hasher that has
never heard of this project. The README does exactly that, and diffs the answer
against the hash stored on the row, on every push.
"""
from __future__ import annotations

import hashlib
import sqlite3
from typing import List, Optional

#: The first line of every canonical payload. It is inside the hash on purpose:
#: if the field list or the encoding ever changes, this string changes with it,
#: and an old row cannot be silently reinterpreted under new rules.
CHAIN_FORMAT = "bevis-event-v1"

#: The digest. Named here rather than spelled `hashlib.sha256(...)` inline so
#: there is one place to read the answer to "which hash is this".
HASH_NAME = "sha256"

#: What the first event links to. Sixty-four zeros is not a hash of anything —
#: it is a value no sha256 will realistically produce, so "this row claims to be
#: first" is distinguishable from "this row links to a real predecessor".
GENESIS_PREV = "0" * 64

#: Rows in `meta`. The first three answer "when did this board start hashing,
#: and was there anything here before that" — the question a chain that begins
#: at an unrecorded point cannot answer. The last two are the recorded head,
#: which is what makes cutting events off the END detectable at all: a chain on
#: its own says nothing about a row that is no longer there.
META_STARTED_AT = "chain_started_at"
META_MODE = "chain_mode"
META_SEALED_THROUGH = "chain_sealed_through"
META_HEAD_ID = "chain_head_id"
META_HEAD_HASH = "chain_head_hash"

#: How the chain began on this board.
#:   fresh   — the board had no events, so every event it has was hashed as it
#:             was written.
#:   adopted — the board already had events. They were sealed as they stood at
#:             adoption, which proves nobody has touched them SINCE; it proves
#:             nothing about what they said before. That distinction is the
#:             whole reason this value is stored instead of assumed.
MODE_FRESH = "fresh"
MODE_ADOPTED = "adopted"

#: The event kind written when the chain starts. It is an ordinary event and is
#: itself chained, so the record of where the chain began is inside the chain.
GENESIS_KIND = "chain_started"


# ── The canonical form ───────────────────────────────────────────────────────
def _field(name: str, value) -> bytes:
    """One line: `name=<byte length>:<value>\\n`, UTF-8, nothing normalised."""
    raw = ("" if value is None else str(value)).encode("utf-8")
    return b"".join([name.encode("ascii"), b"=",
                     str(len(raw)).encode("ascii"), b":", raw, b"\n"])


def canonical_bytes(prev_hash, event_id, ts, job_id, actor, kind, detail) -> bytes:
    """The exact bytes hashed for one event. See this module's docstring.

    Every caller — appending, verifying, and `--bytes` — goes through here, so
    the documented form and the enforced form cannot drift apart.
    """
    return b"".join([
        CHAIN_FORMAT.encode("ascii"), b"\n",
        _field("prev", prev_hash or GENESIS_PREV),
        _field("id", int(event_id)),
        _field("ts", ts),
        _field("job_id", "" if job_id is None else int(job_id)),
        _field("actor", actor),
        _field("kind", kind),
        _field("detail", detail),
    ])


def event_hash(prev_hash, event_id, ts, job_id, actor, kind, detail) -> str:
    """sha256 of the canonical bytes, lowercase hex."""
    return hashlib.sha256(
        canonical_bytes(prev_hash, event_id, ts, job_id, actor, kind, detail)
    ).hexdigest()


def row_bytes(row: sqlite3.Row, prev_hash) -> bytes:
    return canonical_bytes(prev_hash, row["id"], row["ts"], row["job_id"],
                           row["actor"], row["kind"], row["detail"])


def row_hash(row: sqlite3.Row, prev_hash) -> str:
    return hashlib.sha256(row_bytes(row, prev_hash)).hexdigest()


# ── Board state ──────────────────────────────────────────────────────────────
def has_chain(conn: sqlite3.Connection) -> bool:
    """Can this board store a chain at all? False on a board from bevis 0.2.x."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(event)")}
    return {"prev_hash", "hash"} <= columns


def get_meta(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row[0]


def set_meta(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def head_hash(conn: sqlite3.Connection) -> str:
    """What the next event links to: the last event's hash, or the genesis value.

    A last event with no hash is a row bevis did not write. It gets the genesis
    value rather than an exception, because refusing to append would turn a
    tampered board into an unusable one — and verify() names that row anyway.
    """
    row = conn.execute("SELECT hash FROM event ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or not row[0]:
        return GENESIS_PREV
    return row[0]


def set_head(conn: sqlite3.Connection, event_id: int, digest: str) -> None:
    set_meta(conn, META_HEAD_ID, int(event_id))
    set_meta(conn, META_HEAD_HASH, digest)


# ── Starting the chain, and saying which way it started ──────────────────────
def start_chain(conn: sqlite3.Connection) -> Optional[dict]:
    """Begin the chain on this board, once. Returns how it began, or None.

    None means it had already begun and nothing was touched — `bevis init` is
    idempotent, and re-sealing an existing chain would silently paper over a
    tamper that verify() would otherwise have caught.

    A board that already has events is the case worth being careful about. The
    rows there are hashed into the chain, so an edit to one of them from now on
    is detectable — but they are hashed as they stand TODAY, and bevis has no
    way to know what they said yesterday. So the mode, the timestamp and the
    last pre-existing id are written to `meta` and repeated in an event of their
    own. A chain that begins at an unrecorded point is a provenance claim with a
    hole in it; this is the record that fills it.
    """
    if get_meta(conn, META_STARTED_AT) is not None:
        return None

    from .model import now_ts

    rows = conn.execute(
        "SELECT id, ts, job_id, actor, kind, detail FROM event ORDER BY id"
    ).fetchall()
    prev = GENESIS_PREV
    for row in rows:
        digest = row_hash(row, prev)
        conn.execute("UPDATE event SET prev_hash=?, hash=? WHERE id=?",
                     (prev, digest, int(row["id"])))
        prev = digest

    mode = MODE_ADOPTED if rows else MODE_FRESH
    sealed_through = int(rows[-1]["id"]) if rows else 0
    started_at = now_ts()
    set_meta(conn, META_STARTED_AT, started_at)
    set_meta(conn, META_MODE, mode)
    set_meta(conn, META_SEALED_THROUGH, sealed_through)
    if rows:
        set_head(conn, sealed_through, prev)

    if mode == MODE_FRESH:
        detail = ("%s/%s; this board had no events, so every event in it was "
                  "hashed as it was written" % (CHAIN_FORMAT, HASH_NAME))
    else:
        detail = ("%s/%s; events 1-%d existed already and were sealed as they "
                  "stood at %s — unchanged since then is provable, unchanged "
                  "since they were written is not"
                  % (CHAIN_FORMAT, HASH_NAME, sealed_through, started_at))
    return {"mode": mode, "started_at": started_at,
            "sealed_through": sealed_through, "detail": detail}


# ── Verification ─────────────────────────────────────────────────────────────
def _problem(kind: str, event_id, detail: str, fix: str = "") -> dict:
    return {"kind": kind, "event_id": event_id, "detail": detail, "fix": fix}


def _row_problem(row: sqlite3.Row, expected_prev: str) -> Optional[dict]:
    """Why this row does not follow the one before it, or None.

    Order matters. A row with no hash was not written by bevis at all, and
    saying "it does not match its hash" about a row that has none would be a
    worse answer than the true one.
    """
    event_id = int(row["id"])
    if not row["hash"]:
        return _problem(
            "unchained", event_id,
            "event %d carries no hash: bevis did not write it" % event_id,
            "every event bevis appends is hashed, so this row was inserted by "
            "something else — a hand-written INSERT, or a restore from a board "
            "that predates the chain")
    stored_prev = row["prev_hash"] or GENESIS_PREV
    if stored_prev != expected_prev:
        return _problem(
            "broken-link", event_id,
            "event %d does not follow the event before it: it links to %s, and "
            "the previous event hashes to %s" % (event_id, stored_prev, expected_prev),
            "an event between them was deleted or re-ordered, or the event "
            "before this one was rewritten")
    recomputed = row_hash(row, expected_prev)
    if recomputed != row["hash"]:
        return _problem(
            "content-changed", event_id,
            "event %d no longer matches its own hash: recorded %s, recomputed %s"
            % (event_id, row["hash"], recomputed),
            "this row's ts, job_id, actor, kind or detail was edited after it "
            "was written; bevis can say it changed, not what it used to say")
    return None


def verify(conn: sqlite3.Connection) -> dict:
    """Walk the log and report the FIRST place the arithmetic fails.

    "Invalid" is not an answer anybody can act on. The report names the event,
    what kind of break it is, and how far the log verified before it — because
    the events before a break are still proved, and saying otherwise would throw
    away evidence that is perfectly good.
    """
    report = {
        "chained": False, "ok": False, "format": CHAIN_FORMAT,
        "algorithm": HASH_NAME, "events": 0, "verified": 0,
        "first_id": None, "last_id": None, "head_id": None, "head_hash": None,
        "started_at": None, "mode": None, "sealed_through": None,
        "problem": None,
    }
    if not has_chain(conn):
        report["problem"] = _problem(
            "no-chain", None,
            "this board's event log has no hash columns: it was created by a "
            "bevis older than the chain",
            "run `bevis init` again — it is idempotent, it seals the events "
            "already there, and it records that that is what it did")
        return report

    report["chained"] = True
    report["started_at"] = get_meta(conn, META_STARTED_AT)
    report["mode"] = get_meta(conn, META_MODE)
    sealed = get_meta(conn, META_SEALED_THROUGH)
    report["sealed_through"] = int(sealed) if sealed is not None else None
    head_id = get_meta(conn, META_HEAD_ID)
    report["head_id"] = int(head_id) if head_id is not None else None
    report["head_hash"] = get_meta(conn, META_HEAD_HASH)

    rows = conn.execute(
        "SELECT id, ts, job_id, actor, kind, detail, prev_hash, hash "
        "FROM event ORDER BY id").fetchall()
    report["events"] = len(rows)
    if rows:
        report["first_id"] = int(rows[0]["id"])

    problems: List[dict] = []
    prev = GENESIS_PREV
    for row in rows:
        trouble = _row_problem(row, prev)
        if trouble:
            problems.append(trouble)
            break                      # the FIRST break is the answer
        prev = row["hash"]
        report["verified"] += 1
        report["last_id"] = int(row["id"])

    if report["started_at"] is None and (rows or report["head_hash"]):
        # A chain whose start is not written down cannot say whether the rows
        # under it were hashed when they were written or long afterwards.
        problems.append(_problem(
            "no-recorded-start", None,
            "this board has a chain but no record of when it started or what "
            "was already in the log when it did",
            "the `%s` row in the `meta` table is missing; bevis writes it when "
            "the chain begins and never rewrites it" % META_STARTED_AT))

    if not problems and report["head_id"] is not None:
        # A chain proves nothing about a row that is no longer there: lop the
        # last three events off and what remains is a perfectly valid chain.
        # The recorded head is the only thing that notices.
        if report["last_id"] != report["head_id"] or prev != report["head_hash"]:
            ends = ("the log has no events left" if report["last_id"] is None
                    else "the log ends at event %s" % report["last_id"])
            problems.append(_problem(
                "truncated", report["head_id"],
                "%s, and this board records event %s as its head (%s)"
                % (ends, report["head_id"], report["head_hash"]),
                "events were removed from the end, or the last event was "
                "rewritten and the chain recomputed behind it"))

    report["problem"] = problems[0] if problems else None
    report["ok"] = not problems
    return report


def _verified_so_far(report: dict, problem: dict) -> str:
    """How much of the log is still proved. A break does not void what precedes
    it, and saying it did would throw away evidence that is perfectly good."""
    tail = ("what followed them is gone" if problem["kind"] == "truncated"
            else "nothing after that is proved")
    if report["verified"] == 0:
        return "no event verified before the break; %s." % tail
    if report["verified"] == 1:
        return "event %s verifies clean; %s." % (report["first_id"], tail)
    return "events %s-%s verify clean; %s." % (report["first_id"],
                                               report["last_id"], tail)


def render(report: dict) -> str:
    """The report as a human reads it. One screen, first broken link named."""
    problem = report["problem"]
    if not report["chained"]:
        return "chain unavailable — %s\n-> %s" % (problem["detail"], problem["fix"])

    lines = []
    if problem is None:
        lines.append("chain ok — %d event(s), unbroken from event %s to event %s"
                     % (report["verified"], report["first_id"], report["last_id"]))
    else:
        where = ("" if problem["event_id"] is None
                 else " at event %s" % problem["event_id"])
        lines.append("chain BROKEN%s — %s" % (where, problem["detail"]))
        lines.append("-> %s" % problem["fix"])
        lines.append(_verified_so_far(report, problem))
    lines.append("format     %s over %s, each event hashing the one before it"
                 % (report["algorithm"], report["format"]))
    lines.append("head       %s (event %s)" % (report["head_hash"], report["head_id"]))
    if report["mode"] == MODE_ADOPTED:
        lines.append("started    adopted %s — events 1-%s were already here and "
                     "were sealed as they stood then, not when they were written"
                     % (report["started_at"], report["sealed_through"]))
    elif report["mode"] == MODE_FRESH:
        lines.append("started    fresh %s — every event on this board was hashed "
                     "as it was written" % report["started_at"])
    lines.append("anchor     the chain sits in the same file as the log, so copy "
                 "that head hash somewhere else; a rewrite of everything is only "
                 "detectable against a copy kept outside")
    return "\n".join(lines)


def exit_code(report: dict) -> int:
    return 0 if report["ok"] else 1
