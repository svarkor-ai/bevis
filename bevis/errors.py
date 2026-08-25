"""Error types. Every refusal in bevis is one of these, never a bare assert.

The split matters for scripting: a Refusal means "a rule said no" (exit 1) and
is the tool working correctly; a UsageError means the caller supplied something
malformed (exit 2); NotFound means the id does not resolve (exit 3).
"""


class BevisError(Exception):
    """Base class. Carries a process exit code so the CLI never guesses."""

    exit_code = 1


class Refusal(BevisError):
    """A rule refused the operation. This is bevis doing its job."""

    exit_code = 1


class UsageError(BevisError):
    """Malformed or missing input from the caller."""

    exit_code = 2


class NotFound(BevisError):
    """An id did not resolve to a row."""

    exit_code = 3
