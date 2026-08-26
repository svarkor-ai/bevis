# bevis

**A job cannot close without machine-checkable evidence.**

I kept being told a task was finished. Often it had not been touched, had been
tested against the wrong thing, or had not been tested at all — just asserted.
bevis is the smallest tool I could build that makes that structurally hard to
get away with: a job cannot move to `closed` unless a command actually ran, its
exit code was zero, and the output it printed is stored where the next person
can read it.

*bevis* is Swedish for "proof".

## The idea, in six lines

1. A job cannot be closed without a command, its exit code and its output —
   stored, not asserted.
2. A command that reports success whether or not the work was done is not
   evidence. bevis refuses a close whose output says it measured nothing, and
   will run a negative control beside your verification if you give it one.
3. The acceptance bar is written when the job is created, before anyone has a
   reason to make it easy to pass. A job with no bar cannot be created.
4. Closing a job and verifying it are two different acts, and bevis will not let
   the same actor perform both.
5. A dispatcher can claim jobs and hand them to any agent or script, and records
   what came back — it never decides whether that counts as success. Only the
   job's own checks do.
6. Nothing in that loop calls a language model, because the thing under test is
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

Job 1 is verified and job 2 was the only other candidate, so a gate that came
back non-zero emptied the queue. A blocking check that has *never been run* does
not block readiness — checks usually run after the work — but it does block the
close. Unproven is not the same as passing.

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

`examples/adapter-echo.sh` is the smallest adapter that does anything: it prints
the job it was handed, exits 0, and says in its own comments why exiting 0 is not
a claim to have finished.

`blocked` and `failed` are sticky: neither is `open`, so the next drain will not
pick the job up again. A job that could not be proved is a job a human should
look at, and a queue that silently retries it forever is how one broken step
burns a night of compute. Every blocked reason ends with the command that
requeues it.

## A check that cannot fail

`--cmd true` always passes. So does a test runner that found no tests, a
scanner whose pattern never matches, and a checker whose failure path prints
`FAIL` and forgets to set the exit code. All three exit 0. All three would have
exited 0 whether the work was done or not, which is the one thing an exit code
cannot tell you about itself.

bevis refuses two shapes of that. The first costs nothing and is on by default:
**evidence whose own output says it measured nothing.**

```console
$ bevis add "Run the unit tests" --acceptance "the unit test suite passes"
created job 5: Run the unit tests
$ mkdir suite
$ printf '#!/bin/sh\necho "Ran $(ls suite | wc -l | tr -d " ") tests in 0.001s"\n' > run-tests
$ chmod +x run-tests
$ bevis close 5 --run "./run-tests"
bevis: refusing to close job 5 on evidence that measured nothing:
  - the output says 'Ran 0 tests', and nothing else in it reports a non-zero count
A run that examined no tests, no files and no rows exited 0 because there was nothing there to disagree with. That is a constant, not a check. Point the command at the work and run it again.
$ echo $?
1
```

Nothing was wrong with that command. It ran, it exited 0, it printed something —
it just had no tests to run, and a runner with nothing to run cannot disagree
with you. Give it one and the same command closes the job:

```console
$ touch suite/test_arithmetic.py
$ bevis close 5 --run "./run-tests"
closed job 5 with evidence (exit 0 from: ./run-tests)
```

That example uses a stand-in runner rather than a real one on purpose: a real
runner's behaviour here is not stable across versions. Python 3.12 changed
`unittest` to print `NO TESTS RAN` and exit 5 where 3.10 printed `OK` and exited
0 — which is to say CPython now enforces this same rule itself. bevis still has
to, because a runner that exits 0 having measured nothing is the general case,
and `unittest` is one runner that got fixed.

The second shape needs a phrase list to be lucky, and a negative control to be
sure. Here is a secret scanner with the defect above — its failure path prints
and returns 0 — and here is bevis closing a job on it, because every rule so far
is satisfied:

```console
$ export BEVIS_ACTOR=alice
$ printf '%s\n' '#!/bin/sh' 'grep -q SECRET "$1" && echo "FAIL: secret in $1"' 'echo "scanned $1"' > leakcheck.sh && chmod +x leakcheck.sh
$ printf 'nothing to see here\n' > clean.txt
$ printf 'SECRET=hunter2\n' > planted.txt
$ bevis add "Scan the bundle for secrets" --acceptance "leakcheck.sh finds no secret in the bundle"
created job 6: Scan the bundle for secrets
$ bevis close 6 --run "./leakcheck.sh clean.txt"
closed job 6 with evidence (exit 0 from: ./leakcheck.sh clean.txt)
```

That close is worthless and nothing so far can tell. **`--negative-control` is
how you ask:** a second command that must FAIL. bevis runs both, and the close
needs the verification to pass *and* the control not to.

```console
$ export BEVIS_ACTOR=alice
$ bevis reopen 6 --reason "prove the scanner can fail before trusting it"
job 6 reopened (evidence discarded, see `bevis events`)
$ bevis close 6 --run "./leakcheck.sh clean.txt" --negative-control "./leakcheck.sh planted.txt"
bevis: refusing to close job 6 on a check that cannot fail:
  - verify           exited 0: ./leakcheck.sh clean.txt
  - negative control exited 0: ./leakcheck.sh planted.txt
The control was supposed to fail and it passed, so this command reports success whether the work was done or not. That is not evidence, it is a constant. Fix the check, or point --negative-control at a case that must fail.
$ echo $?
1
```

Fix the scanner — one `exit 1` — and the identical command line closes the job:

```console
$ export BEVIS_ACTOR=alice
$ printf '%s\n' '#!/bin/sh' 'if grep -q SECRET "$1"; then echo "FAIL: secret in $1"; exit 1; fi' 'echo "scanned $1: clean"' > leakcheck.sh
$ bevis close 6 --run "./leakcheck.sh clean.txt" --negative-control "./leakcheck.sh planted.txt"
closed job 6 with evidence (exit 0 from: ./leakcheck.sh clean.txt)
negative control exited 1, as a control must: ./leakcheck.sh planted.txt
$ bevis show 6 | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z/<timestamp>/'
job        6 (internal id 6)
title      Scan the bundle for secrets
status     closed
acceptance leakcheck.sh finds no secret in the bundle
ready      no — status is closed, not open
created    <timestamp>
evidence:
  closed_by   alice at <timestamp>
  verify_cmd  ./leakcheck.sh clean.txt
  verify_exit 0
  verify_output:
    | scanned clean.txt: clean
  negative control:
    cmd    ./leakcheck.sh planted.txt
    exit   1
    output:
      | FAIL: secret in planted.txt
```

The control is stored beside the evidence, not folded into it, because it is a
different claim: the verification says the work is done, and the control says
the command asking would have noticed if it were not.

Three things about that gate, because a gate is only worth what it is honest
about:

* **bevis runs the control itself**, in the same environment as the
  verification, and sets no variable telling it which of the two runs it is. A
  command that could see it was the control could pass this gate by failing on
  sight of the flag. For the same reason there is no transcribed form:
  `--negative-control` needs `--run`.
* **A control that exits 126 or 127 is refused**, not accepted. Those are the
  shell saying it could not run the command at all, and a control that failed to
  start has been shown not to exist rather than shown to fail.
* **bevis cannot invent a control for you**, which is why this one is a flag and
  not a default. A control bevis chose would be exactly the fake check this tool
  exists to refuse. The vacuity rule is free, so it is on; this one costs a
  second run and a thought, so you ask for it. See
  [Limitations](#limitations) for how far each of them goes.

## Quickstart: plug in your own agent

**bevis never asks you for a credential, an endpoint, a model name or a GPU
address, and has no field to keep one in.** An adapter is a *command* bevis
executes; the command owns its own configuration, reads its own environment, and
talks to whatever it likes. bevis hands it a job and reads back an exit code.
That is why a local model server, a cloud API, a box with a GPU in it and a
coding agent all plug in the same way, and why there is no bevis-side
configuration of yours to keep in step with anything.

What bevis does store is the command line you hand it, verbatim. Write a URL on
it and that URL is on your board — which is why `bevis adapter add` refuses a
command with a credential written into it and points at your adapter's own
environment instead.

```sh
pip install bevis
```

Then, end to end, with a stand-in agent you can replace with yours:

```console
$ mkdir bevis-quickstart && cd bevis-quickstart && export BEVIS_ACTOR=you
$ export BEVIS_DB=$PWD/.bevis/bevis.db   # a board of its own, just for this walkthrough
$ bevis init
initialised bevis database at /tmp/bevis-readme-check/bevis-quickstart/.bevis/bevis.db
$ # 1. your agent. Any command. bevis never looks inside it.
$ printf '%s\n' '#!/bin/sh' \
    '[ "$BEVIS_DOCTOR_PROBE" = 1 ] && { echo "ready (a probe does no work)"; exit 0; }' \
    'echo "$BEVIS_JOB_TITLE" > NOTES.md' \
    'echo wrote NOTES.md' > my-agent.sh && chmod +x my-agent.sh
$ bevis adapter add myagent --cmd "$PWD/my-agent.sh"
registered adapter myagent = /tmp/bevis-readme-check/bevis-quickstart/my-agent.sh
$ # 2. before queueing anything: does this actually work here?
$ bevis doctor --adapter myagent | sed -E 's/bevis [0-9]+\S* on Python.*/bevis <version>, on this Python/'
bevis
  ok       bevis <version>, on this Python
database
  ok       /tmp/bevis-readme-check/bevis-quickstart/.bevis/bevis.db — 0 job(s), 0 ready
actor
  ok       $BEVIS_ACTOR=you — closes will be recorded under that name
adapters
  ok       myagent — ran and exited 0: ready (a probe does no work)

no problems found.
$ # 3. a job, and the gate that will decide whether it is done
$ bevis add "Write the release notes" --acceptance "NOTES.md names the release"
created job 1: Write the release notes
$ bevis check add 1 --name notes --cmd "grep -q release NOTES.md" --blocking
added blocking check 'notes' to job 1
$ # 4. hand it over. The check decides, not the agent and not bevis.
$ bevis run --adapter myagent
job 1      closed   1 check(s) passed
$ bevis show 1 | sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.]+Z/<timestamp>/'
job        1 (internal id 1)
title      Write the release notes
status     closed
acceptance NOTES.md names the release
ready      no — status is closed, not open
created    <timestamp>
checks:
  - notes        [blocking] PASS  $ grep -q release NOTES.md
evidence:
  closed_by   you#0 at <timestamp>
  verify_cmd  grep -q release NOTES.md
  verify_exit 0
  verify_output:
    | $ grep -q release NOTES.md
    | [check notes exit=0]
```

That job is closed and carries the command that closed it, its exit code and its
output. Point `--cmd` at your own agent instead and nothing else changes.

Three things in that transcript are worth a second look:

* **The registry stores a name and a command.** Not a URL, not a key, not a
  model. `bevis adapter add` refuses a command with a credential written into it
  and tells you to reference `$YOUR_KEY` from the adapter's own environment
  instead — see [Limitations](#limitations) for how far that goes.
* **`bevis doctor` fails rather than reassures.** Every problem it finds names
  the command that fixes it — asserted by `test_every_doctor_failure_names_a_fix`,
  not by this sentence — and it exits non-zero so you can put it in a script.
  It calls only the adapter you name with `--adapter`; every other one is
  reported `unproven`, never `ok`, because doctor will not vouch for something it
  did not run.
* **`BEVIS_DOCTOR_PROBE=1`** is set during that call, so an adapter that drives a
  real agent can answer a diagnostic without spending a job's worth of work on
  it. Ignoring it is fine; the probe just costs more.

Two worked examples, both runnable, both short enough to read before you
trust them:

| file | shape | its configuration lives in |
|---|---|---|
| [`examples/adapter-local-model.py`](examples/adapter-local-model.py) | HTTP to an OpenAI-compatible server — llama.cpp, vLLM, Ollama, LM Studio, a hosted API | `$MODEL_URL`, `$MODEL_NAME`, `$MODEL_API_KEY` in **its** environment |
| [`examples/adapter-agent.sh`](examples/adapter-agent.sh) | pipes the job into a command-line agent and keeps the transcript | `$MY_AGENT_CMD` in **its** environment |

Every line of network code in the first one is in that file, which is yours. The
bevis package imports no HTTP library at all and could not make the call if it
wanted to — `tests/test_no_dependencies.py` reads the imports with `ast` and
fails the build if that stops being true. `bevis adapter list` shows what is
registered; `bevis adapter remove myagent` forgets it.

## Install

```sh
pip install bevis          # the CLI: pure stdlib, zero dependencies
pip install 'bevis[api]'   # optional: the HTTP API (FastAPI + uvicorn)
```

Published from this repository by GitHub Actions using PyPI Trusted Publishing,
so no API token exists to be leaked or rotated. Or from a checkout:

```sh
pip install -e .
pip install -e '.[api]'
```

**Upgrading from 0.1.x.** Run `bevis init` once against an existing board: it is
idempotent, it adds the three columns a negative control is stored in, and it
touches no job. Until you do, ordinary closes work exactly as before and a close
carrying `--negative-control` is refused by name. One behaviour change is not
opt-in: a close whose output says it measured nothing is now refused, where
0.1.x accepted it — the phrases and the argument for that default are in
[docs/DESIGN.md](docs/DESIGN.md).

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
  --run "echo done"` still closes job 3, and the evidence will still say `echo
  done`. Two narrower things became structural rather than conceded: a close
  whose output *says* it measured nothing is refused by default, and a close
  carrying a `--negative-control` that also passed is refused. Neither of them
  makes relevance checkable. `echo done` measures nothing bevis has a phrase for
  and carries no control unless you supply one, so it goes through, and that is
  the honest limit: bevis makes the lie small, specific and permanently attached
  to the job; it does not make it impossible. Relevance is what the acceptance
  bar, a blocking check and a human reading `bevis show` are for.
* **The negative control is opt-in, and nothing turns it on for you.** bevis
  cannot invent a control: a control bevis chose would be exactly the fake check
  this tool exists to refuse. So `--negative-control` raises the evidence quality
  of one close, on the closes where somebody remembered — the default board is
  no better calibrated than it was before. That is a real gap and it is the
  reason this is a flag rather than a promise.
* **A negative control proves the command can fail, not that it fails for the
  right reason.** `./check.sh planted.txt` might be exiting non-zero because the
  check works, or because the file is missing, or because a path is wrong. bevis
  observes one bit — the control did not pass — and stores the command and its
  output next to the evidence so the next person can read *why* it failed. It
  cannot tell a control that failed for the intended reason from one that fell
  over on the way. That is the same class of limit as the one above it, one step
  further in.
* **The vacuity lexicon is a phrase list, not an understanding.** It knows about
  ten shapes that mean "this run examined nothing" — `Ran 0 tests`, `no tests
  ran`, `collected 0 items`, `[no test files]`, `0 passed`, `0 examples`,
  `0 files checked`, `no files to check`, `nothing to verify` — and it stands
  down when the same output reports a non-zero count anywhere, so a build log
  that mentions one empty sub-run beside `312 passed` is not refused. It is
  deliberately narrower than it could be: `no matches`, `no files matched`,
  `(0 rows)` and `nothing to commit` are absent because each of them is the
  *passing* output of some real check, and a rule that refused honest evidence
  would teach people to route around bevis. What it therefore cannot catch is a
  domain number that is wrong: `redacted_count: 0` printed by a redactor that
  redacted 178 times exits 0 and reads like any other count. A negative control
  catches that. A phrase list never will. Both halves of the calibration are a
  corpus in `tests/test_vacuity.py`, and both have a mutant.
* **The `--verify-*` form trusts your transcription.** `bevis close 3
  --verify-cmd "make test" --verify-exit 0 --verify-output-file ci.log` is how
  evidence from CI or another machine gets in, and bevis cannot tell whether you
  typed it faithfully. What it still guarantees is that a transcription exists,
  that it names a command, and that the command exited zero.
* **A check is only as good as the command you point at.** `--cmd true` is a
  valid blocking check that always passes, and a check has no negative control
  of its own — `--negative-control` is on `bevis close`, not on `bevis check
  add`, so the dispatcher's closes are covered by the vacuity rule and nothing
  else. bevis records what you chose to measure and has no opinion about whether
  it measures the right thing.
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
* **The credential refusal on `bevis adapter add` is a lint, not a boundary.**
  It knows five shapes: an `Authorization:` or `api-key:` header with a literal
  value, an `--api-key`/`--token`/`--password` flag with one, a *leading*
  `SOMETHING_KEY=value` assignment, a password in front of the `@` in a URL, and
  a literal shaped like a well-known token (`sk-…`, `ghp_…`, `AKIA…`). Everything
  else walks past — `curl -u user:pass`, an `X-Auth-Token:` header, a `?token=`
  in a query string, anything encoded or fetched from somewhere it cannot see.
  It deliberately allows `$YOUR_KEY`, `"$YOUR_KEY"`, `$(pass show …)` and
  backticks, because a rule that refused the reference would teach people to
  obfuscate the literal instead of moving it out of bevis; both directions are
  in the test suite, and both have a mutant. It also guards only `bevis adapter
  add`: a raw `bevis run --adapter '<command>'` is stored verbatim on the job's
  run record with no lint at all. What is structural is narrower and worth more:
  the registry has four columns — name, command, note, timestamp — so bevis
  never asks for a secret and has no field to keep one in. It does keep the
  command line you typed, and that is the thing the lint is guarding.
* **`bevis doctor --adapter` proves an adapter answers, not that it works.** A
  probe is one call, with a throwaway job and `BEVIS_DOCTOR_PROBE=1`; an adapter
  that short-circuits on that flag has told doctor almost nothing about what it
  does with a real job. Doctor reports what it ran and nothing else, which is
  why every adapter it did not call is reported `unproven` rather than `ok`.
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
