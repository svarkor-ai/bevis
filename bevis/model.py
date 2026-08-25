"""The vocabulary and the state machine — the single place both the CLI and the
HTTP API validate against, so the two cannot drift apart.

An unknown status is never stored. It is refused at the edge, loudly, with the
valid set named in the message. A board whose status column can hold anything
is a board you cannot query, and a board you cannot query cannot gate anything.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .errors import UsageError

# ── Status vocabulary ────────────────────────────────────────────────────────
# Deliberately tiny. Every status answers one question: can this job be worked
# on, and if not, why not.
#
#   open      nobody holds it; may be claimed when it is ready
#   claimed   a worker holds it and has not started the adapter yet
#   running   the adapter process is executing
#   blocked   something machine-checkable says it must not proceed (reason stored)
#   failed    the attempt ran and did not succeed
#   closed    finished, WITH evidence attached — the only way in is close()
#   verified  a second actor confirmed the close — terminal, nothing follows it
STATUSES = ("open", "claimed", "running", "blocked", "failed", "closed", "verified")

#: Statuses that the generic status-setter must never write. `closed` is
#: reachable only through close() (which demands evidence) and `verified` only
#: through verify() (which demands a second actor). If a plain "set status to
#: closed" existed, the whole tool would be decorative.
GATED_STATUSES = ("closed", "verified")

#: Nothing follows `verified`. It cannot be reopened, edited or downgraded.
TERMINAL_STATUSES = ("verified",)

#: A blocker is satisfied only by these.
DONE_STATUSES = ("closed", "verified")

#: Legal moves. Anything not listed is refused by name, not silently written.
#: `closed -> open` is absent on purpose: undoing a close is not a status
#: change, it is the separate `reopen` command, which demands a reason and
#: files the discarded evidence in the event log.
TRANSITIONS = {
    "open": {"claimed", "running", "blocked", "failed", "closed"},
    "claimed": {"open", "running", "blocked", "failed", "closed"},
    "running": {"open", "blocked", "failed", "closed"},
    "blocked": {"open", "claimed", "failed"},
    "failed": {"open", "claimed"},
    "closed": {"verified"},
    "verified": set(),
}

# ── Process exit codes ───────────────────────────────────────────────────────
EXIT_OK = 0
EXIT_REFUSED = 1  # a rule said no
EXIT_USAGE = 2    # malformed input (argparse uses 2 as well)
EXIT_NOTFOUND = 3


def validate_status(status: str) -> str:
    """Return `status` if it is in the vocabulary, else refuse and name the set."""
    if status not in STATUSES:
        raise UsageError(
            "unknown status %r — valid statuses are: %s"
            % (status, ", ".join(STATUSES))
        )
    return status


def check_transition(current: str, new: str) -> None:
    """Refuse an illegal move, naming what would have been legal."""
    validate_status(new)
    allowed = TRANSITIONS.get(current, set())
    if new == current:
        raise UsageError("job is already %r" % current)
    if new not in allowed:
        legal = ", ".join(sorted(allowed)) or "nothing (terminal status)"
        raise UsageError(
            "illegal transition %s -> %s; from %s you may go to: %s"
            % (current, new, current, legal)
        )


# ── Time ─────────────────────────────────────────────────────────────────────
# Everything is UTC, ISO-8601, microsecond precision, 'Z'-suffixed. Microseconds
# are not decoration: the slot-exclusivity test compares run intervals, and
# second precision would make two runs look simultaneous when they were not.
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime(TS_FORMAT)


def parse_ts(value: str) -> datetime:
    return datetime.strptime(value, TS_FORMAT).replace(tzinfo=timezone.utc)


_DURATION_RE = re.compile(r"^(\d+)\s*([smhd])$")


def parse_duration(value: str) -> timedelta:
    """'30m' -> timedelta(minutes=30). Accepts s/m/h/d only, on purpose: a
    typo like '30' or '30min' is a refusal, not a silently different window."""
    match = _DURATION_RE.match((value or "").strip().lower())
    if not match:
        raise UsageError(
            "bad duration %r — use <number><unit> with unit s, m, h or d (e.g. 30m)"
            % value
        )
    amount, unit = int(match.group(1)), match.group(2)
    seconds = amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    return timedelta(seconds=seconds)
