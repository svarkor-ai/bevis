# bevis — design notes

Read `README.md` first. This file is the argument behind the rules, including
the parts that are uncomfortable.

## 1. The one invariant

> A job cannot reach `closed` without a `verify_cmd`, a `verify_exit` of 0, and
> a non-empty `verify_output`.

Everything else in the project is scaffolding for that sentence. It is enforced
in exactly one function — `core.close_job()` — and both the CLI and the HTTP API
reach `closed` only through it. There is no second implementation to drift, and
no privileged caller.

Two honest ways to close:

| form | who ran the command | when to use it |
|---|---|---|
| `bevis close 3 --run "pytest -q"` | **bevis** | the strong form: the exit code is observed, not reported |
| `bevis close 3 --verify-cmd "make test" --verify-exit 0 --verify-output-file ci.log` | somebody else | evidence from CI, another host, a colleague |

The second form is weaker on purpose and openly so: bevis is trusting your
transcription. What it still guarantees is that a transcription *exists*, that
it names a command, and that the command exited zero. That is a much lower bar
than proof — and a much higher one than a checkbox.

### What the invariant does not do

It does not check that the command is *relevant*. `bevis close 3 --run "echo
done"` will close job 3, and the evidence will say `echo done`. bevis makes the
lie small, specific, and permanently attached to the job — it does not make it
impossible. Relevance is what the `acceptance` bar and a human reading `bevis
show` are for; blocking checks are how you mechanise it.

## 2. Why `acceptance` is required at create

A job with no stated bar cannot be verified by anyone, which means it can only
be closed by assertion. Making the bar optional would put the tool's central
claim behind a flag people forget. So the CLI's `--acceptance` is mandatory,
empty and whitespace-only values are refused, and it cannot be emptied by a
later update.

The bar is prose, not a command, deliberately. The command version is a check
(§4). Most bars are not fully mechanisable when the job is written, and
pretending otherwise produces either fake checks or no jobs.

## 3. Why `parent_id` is create-only

Re-parenting rewrites the shape of a plan after the fact, usually to make a
report tidier. A finished child moved under a different epic changes what that
epic's completion means retroactively.

`core.update_job()` therefore refuses `parent_id` **by name** rather than
ignoring it. Silently dropping a field the caller believed was applied is how a
board starts lying to the people reading it.

Consequence: the parent/child structure of a plan is a decision made when the
plan is written. That is the intended pressure.

Because `--after` blockers must already exist, and a job is always created after
its blockers, every dependency edge points at a lower id — the graph is acyclic
by construction, not by a cycle check.

## 4. Checks — the borrowed idea

The concept is taken from data-pipeline **asset checks** (Dagster's are the best
known): a check is a first-class object attached to a unit of work, its result
is a durable record, and a failing check marked *blocking* prevents dependent
work from running. Nothing in the agent-task-board world ships that, and it is
the piece that turns a job board into a gate.

In bevis:

* a check is `(job, name, cmd, blocking)` and stores `last_exit`, `last_output`,
  `last_run_at`;
* a **failing blocking** check makes its job unready, unclaimable and
  unclosable, and propagates downstream — to children, and to jobs created
  `--after` it — transitively;
* a **never-run** blocking check does not affect readiness (checks normally run
  after the work) but does block the close. Unproven ≠ passing;
* an advisory (non-blocking) check reports and gates nothing.

Two directions of propagation, for different reasons: a broken gate on the epic
means there is no point doing its steps; a broken gate on a blocker means the
thing you are waiting for is not coming.

## 5. Readiness

`bevis ready` lists jobs that are `open`, have a bar, carry no failing blocking
check on themselves or anywhere upstream, and whose `--after` blockers are all
`closed` or `verified`.

Note what is *not* there: a parent does not block its own children. Decomposition
is not sequencing — the steps of an open epic are exactly the work that should
be available. Use `--after` when you mean ordering.

## 6. The dispatcher, and the thing it may not do

`bevis run --adapter '<template-or-registered-name>' [--slots N]`:

1. atomically claims a ready job (`BEGIN IMMEDIATE` + a status-guarded `UPDATE`;
   two slots cannot both get `rowcount == 1`);
2. runs the adapter as a subprocess, recording stdout, stderr and exit code as a
   row in `job_run`;
3. **asks the checks**, never itself, whether the work is done;
4. closes the job only through `core.close_job()`, with the checks' commands and
   output as the evidence — and if `close_job` refuses even then, records the
   refusal rather than overruling it.

Outcomes: adapter exits non-zero → `failed`. Adapter exits zero with no checks
defined → `blocked` ("nothing can prove this job is done"). A failing check →
`blocked` with the check named. All checks pass → `closed`, with evidence.

That third case is the one people argue with, and it is the point of the whole
project: an adapter exiting 0 has proved that it did not crash.

`blocked` and `failed` are sticky: neither is `open`, so the next drain will not
pick the job up again. A job that could not be proved is a job a human should
look at, and a queue that silently retries it forever is how one broken step
burns a night of compute. Every blocked reason ends with the command that
requeues it (`bevis status <id> open`).

Crash behaviour is deliberate. If a worker dies mid-job, the job stays `claimed`
with a `claimed_at`, and the `job_run` row keeps `finished_at = NULL`. bevis
cannot distinguish a dead worker from a slow one, so it invents no outcome;
`bevis reclaim --stale 30m` hands the job back and says so in the event log.

### Adapter templates and quoting

Placeholders (`{id} {display_id} {title} {description} {acceptance} {assignee}`)
are substituted with shell-quoted values, and only those tokens are touched —
`awk '{print $1}'` survives unharmed. A placeholder written *inside* quotes is
refused, because the quoting would nest wrongly and a job title containing a
semicolon could break out into a command. The same values are always available
as `$BEVIS_JOB_*` environment variables, which is the safe way to use them
inside a quoted string.

## 7. Named adapters, and what the registry may not hold

`--adapter` takes a command template. `bevis adapter add myagent --cmd
'./my-agent.sh'` gives that template a name so you stop retyping it, and `bevis
run --adapter myagent` resolves it.

The registry has four columns: `name`, `cmd`, `note`, `created_at`. There is no
column for an endpoint, a model, a host or a key, and that absence is the whole
product. bevis never asks for any of it: an adapter is a command it executes,
the command owns its own configuration, and bevis learns nothing about the far
end except an exit code. A registry is exactly where that would quietly stop
being true — it is where the "just one field for the API key" field would go —
so `tests/test_adapters.py` asserts the column set directly and fails if it
grows.

Be precise about what that does and does not mean. `cmd` is free text: it holds
whatever command line you gave it, so `MODEL_URL=… ./agent.sh` puts a URL on
your board, and so does the same string passed straight to `bevis run`, which
lands in `job_run.adapter_cmd` with no lint in front of it. The guarantee is
that bevis has no field that *asks* for your configuration and no code that
reads one — not that a command line you wrote is somehow not stored.

A name-only schema does not by itself stop somebody pasting a key into the
command, so there is a second half: `bevis adapter add` refuses a command
carrying a credential and names the two places it belongs instead — the
adapter's own environment, or a wrapper script. That rule is calibrated in both
directions on purpose. `--api-key $OPENAI_API_KEY` is a *reference*, not a
secret, and is allowed; a rule that refused it would teach people to obfuscate
the literal rather than move it out of bevis. Both directions have a mutant
(`registry-stores-a-pasted-credential`,
`credential-lint-refuses-an-environment-reference`), because a gate tested only
on the cases it should catch is half-tested.

Two calibration decisions worth stating, because both were bugs first:

* **A quote is not a reference.** The rule reads one opening quote and then asks
  what is behind it, so `--token="hunter2"` is refused and `--api-key "$MY_KEY"`
  is not. An earlier version treated any adjacent quote as a reference, which
  meant two characters walked every pasted secret past it.
* **Only a *leading* `NAME=value` is an environment variable.** `make KEY=value`,
  `helm --set TOKEN=managed` and `sed 's/KEY=old/KEY=new/'` are arguments, not
  secrets, and an earlier version refused all three — with no override, which
  would have left a user simply unable to register a working command.

The schema is structural; the refusal is a lint, and the README lists what it
knows and what walks past, where a user will read it rather than here where they
will not.

Resolution is deliberately narrow. Only a bare identifier is looked up, so
`--adapter true` still means the command `true` — and it keeps meaning that,
because a name that resolves on your `PATH` is refused at registration. Adding
an adapter must not change what an existing command line means for the next
person to run it. The RESOLVED command is what the `job_run` row records:
evidence names what actually ran, never the alias it was reached by.

Registration also renders the template once, with an empty job, so a template
bevis can never render (`bash -c 'echo {title}'` — see §6) is refused now rather
than at `bevis run`, after a job has already been claimed for it.

## 8. `bevis doctor`

A diagnosis, not a status page. Three rules:

* **Every problem is a FAIL that names the command which fixes it**, and the
  process exits non-zero so it can gate a script. "Everything is green" is the
  failure mode DOCTRINE §3 exists for, and a doctor that prints a column of
  ticks is that failure mode in a nicer font.
* **It never reports an adapter as working that it did not call.** Whether
  an adapter is
  executable can be answered by looking at the file system. Whether it
  *responds* cannot — and calling every registered adapter to find out would
  spend an agent run per diagnosis on somebody else's machine. So doctor calls
  only the adapter named with `--adapter`, and reports the rest `unproven`.
  That is the same rule as a blocking check that has never been run: unproven is
  not passing, and it is not failing either.
* **The probe is the literal artifact**, not a reconstruction of it: the same
  rendering, the same `BEVIS_JOB_*` environment and the same subprocess call
  `bevis run` makes, plus `BEVIS_DOCTOR_PROBE=1` so an adapter that drives a
  real agent can answer cheaply. What that proves is that the adapter starts and
  exits 0 on a probe. It does not prove the adapter can do a job, and doctor
  does not claim it does. Being the literal call has a cost worth knowing: the
  probe hands the adapter `$BEVIS_DB` pointing at your real board, because a
  real run does, so an adapter that writes to the board will write to it during
  a diagnostic.

doctor is also the only command that has to work before the board exists — it is
what you type when bevis has just told you there is no database — so it opens
the file itself and reports a missing one as a finding, with the command that
creates it, instead of dying in the connection helper every other command goes
through.

## 9. `closed` vs `verified`

`closed` = the evidence exists. `verified` = a different actor looked at it.

`verify` refuses when the actor matches `closed_by` (case-insensitively), which
is why closing always records an actor — `$BEVIS_ACTOR` or the OS login name.
Without that, `verified` would just be `closed` with extra typing.

`verified` is terminal: it cannot be reopened, downgraded or edited. A close
that turns out to be wrong can be undone with `bevis reopen --reason ...`, which
clears the evidence *after* copying it into the event log. Losing the record of
a bad close would lose the most interesting thing that happened to that job.

## 10. Storage

One SQLite file, stdlib `sqlite3`, no ORM, WAL mode, six small tables you can
read in five minutes. Every gate lives in Python, not in a trigger, so nothing about the
schema is load-bearing magic and `sqlite3 .bevis/bevis.db` is a supported way to
look around.

bevis cannot stop you editing that file by hand. It does refuse to dispatch what
it finds there if the row is nonsense — a job whose bar was erased is never
ready.

## 11. Ids

Every job has an integer id. A child also has a dotted display id — `7.2` is the
second child of job 7 — and both resolve everywhere. An id that resolves to
nothing is a loud error, never a silent no-op: a reference nobody notices is
worse than a crash.

## 12. Testing the tests

`tools/mutation_check.py` plants one realistic defect at a time in a copy of the
tree and asserts that the guarding test fails. Every rule above has a mutant. A
surviving mutant means the rule is untested, whatever the assertions say.

Building it immediately paid: it caught `test_an_unknown_status_is_never_stored`
passing for the wrong reason — the transition table was rejecting the bad status
before the vocabulary check ever ran, so the vocabulary check itself was
unproven. Two tests were added that exercise it directly.

Known limit: race conditions are not mutation-testable this way. Removing the
atomic claim usually still passes, because the race rarely loses. Slot
exclusivity is asserted by comparing recorded run intervals, and argued from the
code — not proved by a mutant.

`tools/readme_check.py` holds the documentation to the same standard: every
transcript in `README.md` is executed and diffed against the bytes the tool
really printed, so a renamed flag or an invented message fails the build instead
of being discovered by the first person who copies a line out of it. A separate
suite (`tests/test_docs_claims.py`) asserts that every test name the documents
cite still exists, and `tests/test_no_dependencies.py` reads the package's own
imports with `ast` so "stdlib only, no network, no model" is checked rather than
promised. All of it runs on every push and pull request
(`.github/workflows/ci.yml`).

## 13. Deliberate non-goals

* **No retries, schedules or DAG execution.** Durable execution engines exist and
  are better at it. bevis is the gate they can call.
* **No agent framework.** It launches a subprocess and reads an exit code.
* **No tracing.** It records outcomes, not spans.
* **No model calls, anywhere, ever.** bevis has no LLM dependency and never
  will; a gate that needs a model to decide whether something passed is not a
  gate.

## 14. Known holes

Stated plainly, because a tool about honest evidence should be honest about
itself.

1. **`--run "echo done"` closes a job.** See §1. The lie is small, attached and
   auditable, not prevented.
2. **The `--verify-*` form trusts transcription.** By design; it is the only way
   to accept CI evidence at all.
3. **Nothing checks that a check is relevant.** `--cmd true` is a valid blocking
   check that always passes. bevis records what you chose to measure.
4. **The event log is not tamper-evident.** No hash chain, no signatures. Anyone
   with the file can rewrite history; bevis raises the effort, it does not make
   it impossible.
5. **Concurrency is single-machine.** SQLite + WAL is fine for slots on one host
   and for a small team through the HTTP API. It is not a distributed queue.
6. **No pagination, no full-text search.** Boards of a few thousand jobs are
   fine; a hundred thousand are not the target.
7. **The credential refusal on `bevis adapter add` is a lint.** It knows five
   shapes and the README lists them; `curl -u user:pass`, an `X-Auth-Token:`
   header, a `?token=` in a query string and anything encoded all walk past. It
   also guards only that one command: a raw `bevis run --adapter '<command>'`
   reaches `job_run.adapter_cmd` unlinted. What is structural is that bevis has
   no field that asks for a secret; what is heuristic is the refusal that stops
   you writing one into the field it does have.
8. **A doctor probe proves an adapter answers, not that it works.** One call,
   with a throwaway job and `BEVIS_DOCTOR_PROBE=1` — an adapter that
   short-circuits on that flag has told doctor almost nothing. Doctor reports
   what it ran and nothing else, which is why every adapter it did not call is
   `unproven` rather than `ok`.
