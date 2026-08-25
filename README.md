# bevis

**A job cannot close without machine-checkable evidence.**

That is the entire idea. In bevis there is no code path — no flag, no
environment variable, no `--force` — that moves a job to `closed` because
somebody said the work was done. Closing requires a command, an exit code of
zero, and the output that command produced. If any of the three is missing, the
close is refused and the refusal names what was missing.

*bevis* is Swedish for "proof".

```console
$ bevis close 3
bevis: refusing to close job 3 without evidence:
  - verify_cmd is missing or empty (what command proves this?)
  - verify_exit is missing (what did that command exit with?)
  - verify_output is missing or empty (a command that printed nothing proved nothing)
A job closes on a command that exited 0 and printed something. Use `bevis close 3 --run "<command>"` to have bevis run it for you.
$ echo $?
1
```

## Why

Task trackers record what people *say*. That is fine when a person reads every
update and remembers the context. It stops being fine when the thing reporting
"done" is a script, a pipeline, or an AI agent running unattended at 3am — at
which point a board full of confident green rows is not a record of work, it is
a record of claims.

bevis takes the position that a claim of completion is worthless without an
artefact, and enforces it at the only place it can be enforced: the write path
into `closed`.

## Install

```console
$ pip install bevis            # core: pure stdlib, zero dependencies
$ pip install 'bevis[api]'     # optional HTTP API
```

The core deliberately depends on nothing. Verification has to happen where the
work happens — a build box, a container, someone's laptop — and a gate that is
expensive to install is a gate that does not get installed.

## Sixty seconds

```console
$ bevis init
initialised bevis database at /work/.bevis/bevis.db

$ bevis add "Ship the CSV importer" --acceptance "importer handles 10k rows without error"
created job 1: Ship the CSV importer

$ bevis add "T1: parse the header" --parent 1 --acceptance "header parser has tests and they pass"
created job 1.1: T1: parse the header

$ bevis check add 1.1 --name unit --cmd "pytest tests/test_header.py -q" --blocking
added blocking check 'unit' to job 1.1

$ bevis ready
* 1      open      Ship the CSV importer
* 1.1    open      T1: parse the header

$ bevis close 1.1 --run "pytest tests/test_header.py -q"
closed job 1.1 with evidence (exit 0 from: pytest tests/test_header.py -q)
```

The evidence is stored on the job, not printed and forgotten:

```console
$ bevis show 1.1
...
evidence:
  closed_by   alice at 2026-01-04T09:12:44.881204Z
  verify_cmd  pytest tests/test_header.py -q
  verify_exit 0
  verify_output:
    | 7 passed in 0.42s
```

## The model

A job is one row:

| field | meaning |
|---|---|
| `title`, `description` | what it is |
| `acceptance` | **the bar** — what must be true for this to be done, in prose. Required at create. A job with no bar cannot exist. |
| `status` | one of `open claimed running blocked failed closed verified` — validated, never free text |
| `parent_id` | decomposition. **Settable only at create.** There is no re-parenting. |
| `verify_cmd`, `verify_exit`, `verify_output` | the evidence. All three, or the job is not closed. |

Plus three tables that make the board auditable rather than anecdotal: `checks`
(gates), `runs` (what the dispatcher executed), and `events` (who did what).

## Checks: gates, not warnings

A **check** is a command attached to a job whose outcome is a durable row. Mark
it `--blocking` and a failure has teeth:

* the job cannot be claimed,
* the job cannot be closed,
* everything downstream — its children, and any job created `--after` it —
  drops out of `bevis ready`, transitively.

```console
$ bevis check add 4 --name migrations --cmd "alembic check" --blocking
$ bevis check run 4
migrations   exit=1
$ bevis ready          # job 4 and everything behind it is gone from the list
```

A blocking check that has *never been run* does not block readiness — checks
usually run after the work — but it does block the close. Unproven is not the
same as passing.

## The dispatcher never decides success

`bevis run` claims ready jobs and runs an adapter — any command; bevis does not
care whether it is a build script or an AI agent:

```console
$ bevis run --adapter 'my-agent --job {id} --goal {acceptance}' --slots 3
job 12    closed   2 check(s) passed
job 13    blocked  check 'lint' failed (exit 1)
job 14    blocked  no checks defined, so nothing can prove this job is done
job 15    failed   adapter exited 3
```

Note job 14. The adapter exited 0 and the dispatcher still refused to close it,
because an adapter exiting 0 proves it did not crash and nothing else. Only the
job's checks can say whether the bar was met, and a job with no checks was never
verifiable to begin with. bevis says so instead of quietly calling it done.

* One job per slot, ever. Claims are atomic.
* A crash leaves the job `claimed`, not lost: `bevis reclaim --stale 30m`.
* Placeholders (`{id} {title} {acceptance}` …) are shell-quoted, and a
  placeholder written inside quotes is refused rather than silently made
  injectable.

## `verified` needs a second pair of eyes

`closed` means the evidence exists. `verified` means somebody else looked at it:

```console
$ bevis verify 12 --actor alice
bevis: refusing to verify job 12: 'alice' closed it, so 'alice' cannot verify it.
Verification means a DIFFERENT actor read the evidence.
```

A worker cannot grade its own work. `verified` is terminal.

## HTTP API (optional)

```console
$ pip install 'bevis[api]'
$ export BEVIS_TOKEN="$(openssl rand -hex 32)"
$ bevis serve --port 8420
```

Every endpoint calls the same functions the CLI calls, so the rules cannot drift
apart. Refusals are `409`, malformed input is `422`, unknown ids are `404`. The
bearer token is enforced only when `BEVIS_TOKEN` is set — which is honest for a
localhost board, and something you must set if you expose the port.

## No model calls

bevis never talks to a language model and has no LLM dependency. It is
model-agnostic infrastructure: the thing doing the work can be a human, a shell
script, a CI runner, or an agent, and bevis treats them identically — it
believes the exit code, never the narrator.

## Prove the tests can fail

A green suite proves the suite ran, not that the rules hold. `tools/mutation_check.py`
plants one realistic defect at a time in a copy of the source and asserts the
test that guards that rule goes red:

```console
$ python tools/mutation_check.py
baseline: green

CAUGHT   evidence-nonzero-exit-accepted    by: test_close_with_nonzero_exit_is_refused
CAUGHT   dispatcher-decides-success-itself by: test_adapter_exit_zero_alone_does_not_close_a_job
...
all 20 mutants were caught: every rule above has a test that fails when the rule is removed.
```

A surviving mutant means that rule is untested, however many assertions
surround it.

## What bevis is not

* Not a workflow engine. It does not do retries, schedules, fan-out or DAG
  execution. If you need durable execution, keep using Temporal or Prefect and
  point them at bevis for the gate.
* Not an agent framework. It launches a subprocess and reads an exit code.
* Not an observability tool. It stores outcomes, not traces.
* Not a replacement for CI. Your checks are usually *invocations* of CI.

The board exists elsewhere (issue trackers, agent-native task boards). The gate
exists elsewhere (data-pipeline asset checks, CI evidence bundles). bevis is the
coupling: one durable job row that cannot reach a terminal state without one
machine-checkable artefact.

## Documentation

* [`docs/DESIGN.md`](docs/DESIGN.md) — the rules in full, the decisions behind
  them, and the known holes.

## Licence

Apache-2.0.
