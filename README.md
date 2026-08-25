# bevis

**A job cannot close without machine-checkable evidence.**

I kept being told a task was finished. Often it had not been touched, had been
tested against the wrong thing, or had not been tested at all — just asserted.
bevis is the smallest tool I could build that makes that structurally hard to
get away with: a job cannot move to `closed` unless a command actually ran, its
exit code was zero, and the output it printed is stored where the next person
can read it.

*bevis* is Swedish for "proof".

## The idea, in five lines

1. A job cannot be closed without a command, its exit code and its output —
   stored, not asserted.
2. The acceptance bar is written when the job is created, before anyone has a
   reason to make it easy to pass. A job with no bar cannot be created.
3. Closing a job and verifying it are two different acts, and bevis will not let
   the same actor perform both.
4. A dispatcher can claim jobs and hand them to any agent or script, and records
   what came back — it never decides whether that counts as success. Only the
   job's own checks do.
5. Nothing in that loop calls a language model, because the thing under test is
   whether a claim of success is true.

## Sixty seconds

Every transcript in this file is executed by `tools/readme_check.py` in a scratch
directory and diffed against these exact bytes on every push — which is why the
paths below say `/tmp/bevis-readme-check`. Nothing here is typed by hand.

```console
$ export BEVIS_ACTOR=alice
$ bevis init
initialised bevis database at /tmp/bevis-readme-check/.bevis/bevis.db
$ bevis add "Import the inventory CSV" --acceptance "the importer reads inventory.csv and reports the row count"
created job 1: Import the inventory CSV
$ bevis close 1
bevis: refusing to close job 1 without evidence:
  - verify_cmd is missing or empty (what command proves this?)
  - verify_exit is missing (what did that command exit with?)
  - verify_output is missing or empty (a command that printed nothing proved nothing)
A job closes on a command that exited 0 and printed something. Use `bevis close 1 --run "<command>"` to have bevis run it for you.
$ echo $?
1
$ printf 'name,qty\nbolt,4\nnut,9\n' > inventory.csv
$ printf 'import csv, sys\nrows = list(csv.DictReader(open(sys.argv[1])))\nprint(len(rows), "rows imported")\n' > import_inventory.py
$ bevis close 1 --run "python import_inventory.py inventory.csv"
closed job 1 with evidence (exit 0 from: python import_inventory.py inventory.csv)
```

The evidence is stored on the job, not printed and forgotten:

```console
$ bevis show 1 | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z/<timestamp>/'
job        1 (internal id 1)
title      Import the inventory CSV
status     closed
acceptance the importer reads inventory.csv and reports the row count
ready      no — status is closed, not open
created    <timestamp>
evidence:
  closed_by   alice at <timestamp>
  verify_cmd  python import_inventory.py inventory.csv
  verify_exit 0
  verify_output:
    | 2 rows imported
```

`closed` means the evidence exists. `verified` means somebody else looked at it:

```console
$ bevis verify 1 --actor alice
bevis: refusing to verify job 1: 'alice' closed it, so 'alice' cannot verify it. Verification means a DIFFERENT actor read the evidence.
$ bevis verify 1 --actor bo
job 1 verified by bo
```

## Checks are gates, not warnings

A **check** is a command attached to a job whose outcome is a durable row. Mark
it `--blocking` and a failure has teeth: the job cannot be claimed, cannot be
closed, and drops out of `bevis ready` along with everything downstream of it —
its children, and any job created `--after` it, transitively.

```console
$ bevis add "Ship the release notes" --acceptance "NOTES.md lists every user-visible change"
created job 2: Ship the release notes
$ bevis check add 2 --name notes-exist --cmd "test -s NOTES.md" --blocking
added blocking check 'notes-exist' to job 2
$ bevis check run 2
notes-exist  exit=1
$ bevis ready
(nothing ready)
```

A blocking check that has *never been run* does not block readiness — checks
usually run after the work — but it does block the close. Unproven is not the
same as passing.

## The dispatcher never decides success

`bevis run` claims ready jobs and runs an adapter. The adapter is any command;
bevis does not care whether it is a build script or an AI agent, and holds no
dependency that could find out.

```console
$ bevis add "Write the changelog" --acceptance "CHANGELOG.md names every release"
created job 3: Write the changelog
$ bevis check add 3 --name changelog --cmd "test -s CHANGELOG.md" --blocking
added blocking check 'changelog' to job 3
$ bevis run --adapter "sh -c 'echo 0.1.0 - first release > CHANGELOG.md'"
job 3      closed   1 check(s) passed
```

Now the same thing without a check to ask:

```console
$ bevis add "Tidy the docs" --acceptance "the docs read cleanly end to end"
created job 4: Tidy the docs
$ bevis run --adapter true
job 4      blocked  no checks defined, so nothing can prove this job is done — add one with `bevis check add 4 --name <name> --cmd <cmd>`, then `bevis status 4 open` to requeue
```

The adapter exited 0 and the dispatcher still refused to close the job, because
an adapter exiting 0 proves that it did not crash and nothing else. Only the
job's checks can say whether the bar was met, and a job with no checks was never
verifiable to begin with. bevis says so instead of quietly calling it done.

`blocked` and `failed` are sticky: neither is `open`, so the next drain will not
pick the job up again. A job that could not be proved is a job a human should
look at, and a queue that silently retries it forever is how one broken step
burns a night of compute. Every blocked reason ends with the command that
requeues it.

## Install

bevis is not on PyPI. The name `bevis` was still unclaimed there when this was
written (checked 2026-08-25), and nothing has been published under it, so
installing by name will not get you this tool — treat any package that appears
under that name as somebody else's until this README says otherwise. Install
from a checkout:

```sh
pip install -e .          # the CLI: pure stdlib, zero dependencies
pip install -e '.[api]'   # optional: the HTTP API (FastAPI + uvicorn)
```

The core deliberately depends on nothing. Verification has to happen where the
work happens — a build box, a container, someone's laptop — and a gate that is
expensive to install is a gate that does not get installed.

## When to use it

* Work is handed to agents, scripts or people across more than one sitting, and
  "done" has to mean the same thing later that it meant at the time.
* A task should carry its pass/fail bar from the moment it is opened, before
  anyone has a reason to write a bar that is easy to clear.
* The record of what happened needs to survive the process that produced it,
  including a process that would rather it did not.

It is not a fit if one person is doing the work in a single sitting and watching
the output directly — bevis adds a step for no return there — or if "done"
cannot be reduced to a command and an exit code. bevis does not read prose and
does not judge intent.

If your work already lives in GitHub Issues and your gate is CI, use those; that
is the right answer for most teams, and [PRIOR-ART.md](PRIOR-ART.md) argues the
case for the neighbours honestly, including several projects that do parts of
this better.

## Prove the tests can fail

A green suite proves the suite ran, not that the rules hold.
`tools/mutation_check.py` plants one realistic defect at a time in a copy of the
source and asserts that the test which guards that rule goes red. Run it with a
filter to see one family of rules:

```console repo
$ python tools/mutation_check.py evidence
baseline: running the whole suite on unmutated source ...
baseline: green

CAUGHT   evidence-nonzero-exit-accepted             by: test_close_run_with_a_failing_command_is_refused, test_close_with_nonzero_exit_is_refused
CAUGHT   evidence-empty-output-accepted             by: test_close_run_with_a_silent_command_is_refused, test_close_with_empty_output_is_refused
CAUGHT   evidence-empty-command-accepted            by: test_close_with_empty_command_is_refused
CAUGHT   evidence-missing-exit-accepted             by: test_close_with_no_evidence_at_all_is_refused

all 4 mutants were caught: every rule above has a test that fails when the rule is removed.
```

Run it with no argument for all of them. A surviving mutant means that rule is
untested, however many assertions surround it.

`tools/readme_check.py` does the same job for this file: every transcript above
is executed and diffed against what the tool really printed, in CI, on every
push. A document without a command behind it is decoration.

## Limitations

Read this before adopting bevis. None of it is throat-clearing.

* **A close proves an artefact exists, not that it is relevant.** `bevis close 3
  --run "echo done"` closes job 3, and the evidence will say `echo done`. bevis
  makes the lie small, specific and permanently attached to the job; it does not
  make it impossible. Relevance is what the acceptance bar, a blocking check and
  a human reading `bevis show` are for.
* **The `--verify-*` form trusts your transcription.** `bevis close 3
  --verify-cmd "make test" --verify-exit 0 --verify-output-file ci.log` is how
  evidence from CI or another machine gets in, and bevis cannot tell whether you
  typed it faithfully. What it still guarantees is that a transcription exists,
  that it names a command, and that the command exited zero.
* **A check is only as good as the command you point at.** `--cmd true` is a
  valid blocking check that always passes. bevis records what you chose to
  measure and has no opinion about whether it measures the right thing.
* **The event log is not tamper-evident.** No hash chain, no signatures. Anyone
  with write access to the SQLite file can rewrite history, and the evidence a
  job carries is a recorded string, not a signed artefact. bevis raises the
  effort; it is not a compliance artefact.
* **Slot exclusivity is argued from the code, not proved by the tests.** The
  atomic claim is `BEGIN IMMEDIATE` plus a status-guarded `UPDATE`, and a test
  compares recorded run intervals — but removing the atomicity usually still
  passes, because the race rarely loses. Concurrency is the one property here
  that mutation testing does not cover.
* **Single machine only.** One SQLite file, WAL mode, no clustering and no
  replication. It is fine for slots on one host and a small team through the
  HTTP API; it is not a distributed queue, and pointing two hosts at one
  database over a network filesystem is not a supported configuration.
* **The bar can be revised after the fact.** `bevis update --acceptance` cannot
  empty a bar, but it can rewrite one, and the event log records only that the
  field was edited, not what it used to say.
* **No auth model beyond one optional token.** There are no users, roles or
  permissions. `$BEVIS_TOKEN` gates the HTTP API if it is set, and anyone
  holding it can create, close or verify any job. The actor on a job is whoever
  `$BEVIS_ACTOR` says it is, so two-actor verification is a discipline the tool
  supports, not an identity it can enforce.
* **Not a workflow engine.** No retries, schedules, fan-out or DAG execution. If
  you need durable execution, keep Temporal or Prefect and point them at bevis
  for the gate.
* **Not an agent framework, and not an observability tool.** It launches a
  subprocess, reads an exit code, and stores outcomes rather than traces.
* **No pagination and no full-text search.** A board of a few thousand jobs is
  fine. A hundred thousand is not the target.

## Further reading

* [DOCTRINE.md](DOCTRINE.md) — the operating rules this tool's shape comes from,
  and the incident behind each one.
* [PRIOR-ART.md](PRIOR-ART.md) — the neighbouring projects, what each of them
  does better, when to use one instead, and every claim bevis makes mapped to
  the test that would fail if it were false.
* [docs/DESIGN.md](docs/DESIGN.md) — the rules in full and the arguments behind
  them.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
