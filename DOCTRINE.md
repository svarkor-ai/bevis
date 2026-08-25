# DOCTRINE

*The rules behind [bevis](README.md). Every incident below is real and deliberately de-identified: no system, host, person, employer or ticket is named, and nothing here is reconstructible back to one.*

bevis's shape is not a design exercise. It comes from a specific, repeated failure: agents — and the
people directing them — reporting work as done when the artifact said otherwise. What follows is the
subset of operating rules, collected while running a small fleet of autonomous coding and
infrastructure agents against real systems for several months, that generalise past that one setup.
They are about verification and evidence, not about any particular framework, model, or vendor, and
most of them were already written down, in some form, before they were violated again.

Each entry gives the rule, one incident that produced or re-proved it — identifying details removed;
no system, host, or person is named below — and what to do instead. Where it applies, a closing note
says what bevis itself enforces because of the rule, and where it does not, because bevis cannot see
far enough into your process to enforce everything on this list.

## 1. Verify the literal artifact, not an equivalent of it

**What happened.** A synchronisation job was cleared to start writing to production after two
separate people each verified it by running the underlying script by hand, using the environment
they believed the real scheduled trigger used, and got a clean "no changes" result both times. The
first time the literal scheduled trigger actually fired, it produced a materially different,
regressive result — its working directory was set up in a way neither hand-built check had
reproduced. Both verifications had tested a stand-in for the job, not the job. A smaller version of
the same mistake, from the same project: a scheduled command was proven to work by running the
script directly in a clean shell. The installed schedule line also piped the script's output into a
log file in a directory its account could not write to, and a shell opens that redirect before it
runs anything at all — the installed job had never once actually started, silently, forever, and the
direct test could not have caught it.

**Do instead.** Run the exact thing that will run for real — the installed schedule line, the actual
deployment trigger, the literal command a job records — never a reconstruction of it that seems
equivalent. If you cannot run the literal artifact, say so and mark the result unproven rather than
substitute something adjacent for it.

**In bevis.** `close --run "<cmd>"` executes the named command itself and records its literal exit
code and output. It does not accept a report that the command was run somewhere else, or that
something equivalent passed.

## 2. Self-test every checker in both directions

**What happened.** A verification script's single most important check searched a document for a
retracted claim using a pattern that began with a dash. The pattern was read as an option rather than
a search string, the underlying tool exited with an error, and the wrapper around it treated any
non-zero exit as "not found" — so the one assertion that mattered most in the entire file never
actually ran, unnoticed for a full review cycle. The same week, a different check called a function
under a name that did not exist; the call silently did nothing, so the check kept re-measuring the
same starting screen and reported nothing wrong, nineteen times in a row.

**Do instead.** Before trusting a green result from any checker, prove the checker works: give it a
case you know should fail — a planted defect, a needle you put there yourself — and confirm it
fails. Give it a case you know should pass and confirm that too. A checker that has never been shown
to catch anything is not a checker.

**In bevis.** This one bevis cannot enforce. It runs whatever command a job names and trusts the exit
code completely — which is exactly why this rule matters most for anyone using it. A bevis job is
only as honest as the check attached to it.

## 3. "Green" is not evidence — assert the domain state

**What happened.** A deployment restarted three services cleanly and every health endpoint returned
200. Underneath, a database migration step had overwritten the live database with a stale copy
bundled in the deployment itself, silently deleting every record the system existed to serve. It was
caught only because the deploy script also asserted a fact about the domain — a specific row count —
and refused to report success at zero. The same environment, the same week, separately produced an
endpoint that answered HTTP 200 with a body saying a record was "not found," for a record that
provably existed, and a review step whose verdict was hardcoded to "approved" regardless of what the
text above it said — including text that itself said, in capital letters, that the work had failed.

**Do instead.** A process starting, a port answering, an exit code of zero: these are claims about
the wrapper, not about the work. Assert something about the actual domain — a row count, a record
that resolves, one real end-to-end read — before calling anything done.

**In bevis.** The evidence a job closes with is a command's exit code *and* its captured output,
both stored. Output is kept specifically so a later reader can check what it actually said, not just
that something returned zero.

## 4. A fallback or failover surface is a second publishing surface

**What happened.** A public site had a maintenance page that had been serving quietly on its own for
a long time, alongside a newer main application it had never been retired from. When several factual
claims on the main application were corrected, every check exercised the new application only. The
maintenance page kept serving the old, already-retracted claims for days, because it lived at a
different path, had been authored separately, and nothing had ever pointed a checker at it — it
surfaces exactly when nobody is looking, when the primary is down. The same shape recurs with a
cached copy at a content-delivery edge: a visitor can keep being served a stale asset for hours after
the origin is already fixed, because the cache is a separate publishing surface with its own
staleness window that nobody checked either.

**Do instead.** Enumerate every place served content can actually come from — error pages, cached
edges, a static fallback, a previous release left reachable — and run the same checks against all of
them, not only the one you were just editing. A fix applied to the surface you remembered is a fix
to one of the surfaces.

**In bevis.** This is a fact about your deployment topology that bevis cannot see into on its own.
What it can do is make "check every surface" a job with its own acceptance bar and its own required
evidence, instead of a step someone is trusted to remember.

## 5. An armed capability can be a log stub

**What happened.** A synchronisation tool was switched from dry-run into write mode after two
separate reviewers confirmed its trial runs showed zero difference from production, and reported the
capability live. Read line by line, the code path meant to perform the write consisted of a single
log statement saying what it would do, followed by nothing — the write itself had never been
implemented. Both reviews had verified the absence of change, which is exactly what a stub that does
nothing also produces. Nobody had forced a case that required a real write and then checked for the
artifact that only a real write could leave behind.

**Do instead.** To prove a capability that is supposed to change something actually works, force a
change that must happen and verify the artifact it should produce actually changed. A clean no-op,
an empty diff, "nothing to do" — none of these are evidence that the doing part exists. Before
trusting an armed switch, read its own code for words like "would," "not yet," "TODO."

**In bevis.** The same discipline, structurally: closing a job is not "the capability is switched
on," it is one specific command that ran, at a specific time, with a specific exit code, whose
output is kept. A capability that has never actually run under bevis has produced no evidence and
cannot close anything.

## 6. An agent asked to grade its own work will pass itself

**What happened.** A process that both wrote a task's own success bar and later closed that task
against that same bar was treated as two independent steps because each ran under a different
internal name in the log. They were the same authority, twice, wearing two names. Separately, a
strict evidence requirement was added to the one function that finally marked a task complete — but
a second, older function that merely updated a task's status to the same finished-looking state
carried no such check, so the very effort of installing the new requirement kept closing its own
work through the door it had forgotten to lock.

**Do instead.** Whoever, or whatever, produces a piece of work must not be the sole judge of whether
it meets its own bar. Verification has to be attributable to a distinct principal — a different
account, credential, or process that did not write the bar and did not do the work — never just a
different function called by the same hands.

**In bevis.** `verified` cannot be set by the actor that performed `close`. Of everything in this
document, this is the one rule bevis makes structurally impossible to skip rather than merely
advising against.

## 7. A rule nobody can perform is not a rule

**What happened.** A team of automated workers looked, for weeks, like they were ignoring their own
standing instructions. The written procedure told each of them to check a work log for anything
similar before starting, to mark a task as started, and to close it themselves when finished. A pile
of stray, half-filed tasks read as carelessness. It was not, read literally: step one required an
action that was only possible before a worker had been assigned to a task, but by the time any
worker was ever invoked it already had been. Step two was refused by the system for that class of
worker, every time, for a permission reason that had nothing to do with the work. Step three wrote
its evidence to a location a later checking process could not actually read, so a successful write
reported total success while leaving nothing behind that the next check could see. Three of five
steps in the procedure had never once succeeded, for any worker, and the failures looked exactly
like the workers not bothering to comply.

**Do instead.** Before treating repeated non-compliance as a discipline problem, run the instruction
literally, as the least-privileged version of whoever must follow it, and confirm each step is even
possible. A missing capability looks identical to disobedience from the outside, and costs far
longer to find if you assume the latter.

**In bevis.** There is exactly one way to close a job: attach a command, its exit code, and its
output. There is no multi-step procedure to fail at performing, because the rule and the only
available action are the same thing.

## 8. Measure compliance before adding a fourth copy of an instruction

**What happened.** A rule — ask a clarifying question before starting a new, unscoped piece of work
— already existed in three separate places an operator was supposed to read, and a task confirming
that exact behaviour had already been marked verified once before. When someone finally pulled the
actual record of the six most recent cases where the rule should have fired, it had fired in zero of
them. Writing a fourth copy of the same sentence, in a fourth document, was the plan on the table
before anyone checked.

**Do instead.** Before writing another instruction, or trusting that an existing one works because
it is written down and a report once said so, go and count. Pull a handful of the most recent real
cases where it should have applied and read what actually happened. A rule sitting at zero measured
compliance needs a mechanism that removes the alternative, not a more emphatic restatement.

**In bevis.** Not directly enforced — this rule is about instructions, and about the habit of
trusting a closed ticket over a fresh count. bevis's own stored job evidence is exactly the kind of
artifact that measurement needs: a job marked closed is not, by itself, proof that the behaviour
behind it happens again next time. Go and look.

## 9. Name the artifact a figure reads from, or do not publish the figure

**What happened.** A cost-reduction percentage was marked "true, evidenced" in an internal document,
in the same sentence that named its only source as a slide in a presentation deck. A search of the
entire project later found that number nowhere except that deck and the notes written to prepare it
— the evidence for the figure was the figure. It had already been published, under a badge stating
it had been checked, before the person who had supplied it withdrew it, unprompted, once asked
plainly where it came from. It also silently contradicted a second claim two lines away in the same
document: if the thing being measured were fully true, the percentage next to it could not have been
anything other than complete.

**Do instead.** Before a number goes anywhere public, name the specific artifact it can be read back
out of — a log, a query, a commit, a file — not the fact that someone confident said it. If nobody
can point at where it comes from, or the person who supplied it cannot stand behind it once asked,
the number does not ship, however plausible it sounds. Check adjacent figures against each other
while you are at it: two numbers in the same paragraph that cannot both be true is a check that costs
nothing.

**In bevis.** `verify_output` is stored, not only `verify_exit`. A number that only ever existed as
someone's assertion has no command that could have produced it, and bevis has nothing to attach it
to.

---

This is not a complete account of everything that went wrong over those months. A scheduler's
surprising default, one queue's particular API shape, a locking primitive that fails instantly
instead of waiting, and others, were real and cost real time — but did not survive being generalised
past the one system they happened on. What is above did.

---

## Further reading

* [README.md](README.md) — what bevis is, and a transcript of it refusing a close.
* [PRIOR-ART.md](PRIOR-ART.md) — the neighbouring projects, and every claim bevis
  makes mapped to the test that would fail if it were false.
* [docs/DESIGN.md](docs/DESIGN.md) — the rules in full, and the known holes.
