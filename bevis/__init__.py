"""bevis — a job board where a job cannot close without machine-checkable evidence.

bevis is Swedish for "proof". That is the whole idea: the tool has no code path
that lets a job reach a closed state on the strength of somebody asserting it is
done. Closing requires a command, an exit code of 0, and the output that command
produced.

bevis never calls a language model and has no LLM dependency. It is deliberately
model-agnostic plumbing: the thing doing the work can be a human, a shell
script, a CI runner, or an AI agent, and bevis treats them all identically —
it believes the exit code, never the narrator.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]
