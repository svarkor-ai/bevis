# Prior art

bevis is a small tool in a crowded field. This page names the neighbours, says plainly
what each of them does better, and tries to talk you out of bevis where something else
already fits. If you read one section, read
[Use something else instead of bevis if…](#use-something-else-instead-of-bevis-if).

**How this page was made.** Every claim below was read off a page listed in
[Sources](#sources), fetched on 2026-08-25, and is quoted rather than paraphrased where
the wording matters. Anything I could not confirm on a primary page is marked
**UNVERIFIED** instead of guessed. Absence statements ("does not do X") are scoped to the
pages I read on that date — they mean *"not documented on the page cited"*, not *"provably
impossible"*. Projects move; several of these move fast. If a row is wrong or unfair,
say so and it will be corrected — being accurate about the neighbours matters more
here than looking good next to them.

**What is checked, and by what.** Everything in *[What bevis claims](#what-bevis-claims)*
was re-read against the built code on 2026-08-25, and each claim there names the test
that fails if the claim is false. Claims about *other* projects are quotes from their own
pages, read on the date above and not re-verified since; they are the weakest material on
this page and are marked where they are thin.

A note on names: two of the projects in this space have namesakes. The `assay` in the table
below is [`Rul1an/assay`](https://github.com/Rul1an/assay),
which is **not** [`metahub-ai/assay`](https://github.com/metahub-ai/assay), a different
Apache-2.0 project ("An open, reproducible trust layer for AI artifacts"). Check the org
before you compare.

---

## The landscape in one sentence

The durable board exists (beads, Backlog.md, GitHub Issues, Muster). The machine gate
exists (GitHub Actions + required status checks, Dagster asset checks, Assay, Evidence
Gate). What is uncommon — **not** unheard of, see
[Where bevis's claim is weakest](#where-beviss-claim-is-weakest) — is one small tool where
the gate is a property of the job record itself, so the record cannot reach a closed state
without the evidence attached.

---

## The neighbours

| Project | What it is | What it does better than bevis | What bevis does that it does not | Link |
|---|---|---|---|---|
| **GitHub Issues + Actions** | The default: issues as the record, workflows as the check, branch protection as the gate. | Everything about being everywhere — permissions, notifications, mobile, search, integrations, an enormous runner ecosystem, and people already know it. Its gate is battle-tested at a scale bevis will never see. | Its gate protects **branches**, not issues. Closing an issue is one click and needs no evidence: "Click **Close issue**." Required status checks stop a *merge* ("all required status checks must pass before collaborators can merge changes into the protected branch"), and admins can bypass by default. bevis puts the gate on the work record so a job with no passing evidence has no closed state to reach. | [Issues docs](https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/closing-an-issue) · [Protected branches](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches) |
| **beads** (`gastownhall/beads`, MIT) | "Distributed graph issue tracker for AI agents, powered by Dolt". "Beads provides a persistent, structured memory for coding agents." | A far richer issue graph: dependency types, a computed ready frontier (`bd ready` = "tasks with no open blockers"), atomic claiming (`bd update <id> --claim`, "Atomically claim a task"), Dolt-backed versioning and sync, and a real gate object that blocks like any other blocker. Much bigger, much more used, and it already has acceptance criteria as a field (`bd update <id> --acceptance "acceptance criteria"`). | Its documented gate types are `human`, `timer`, `gh:run`, `gh:pr` — a wait on a person, a clock, or a GitHub run; none is documented as running a local command and reading its exit code. And close is a free-text act: `bd close bd-40 --reason "Completed"`. In bevis the acceptance bar is required at creation and the reason for closing has to be a command, an exit code and its output. | [repo](https://github.com/gastownhall/beads) · [gates doc](https://beads.gascity.com/workflows/gates) |
| **Backlog.md** (`MrLesk/Backlog.md`, MIT) | "Markdown‑native Task Manager & Kanban visualizer for any Git repository" — tasks as `.md` files in the repo, plus an MCP server and a web kanban. | Lives in git with zero infrastructure, diffs and reviews like code, works offline, has a UI, and ships acceptance criteria *and* a reusable Definition of Done: "Acceptance criteria & Definition of Done — verifiable scope per task, plus a reusable DoD checklist for every new task". | Those criteria are checklist items a human or an agent ticks. I found no documented mechanism that runs them or blocks a task from reaching Done — **UNVERIFIED beyond the README**: I did not read the source. bevis's bar is a required field the readiness rule reads and a human grades against; the close itself is gated on a command's exit code, not on a ticked box. | [repo](https://github.com/MrLesk/Backlog.md) |
| **Muster** (`AnalogElk/muster`, MIT) | "The operating system for agentic software teams" — "your back office, wired into Claude Code": projects, clients, invoices, knowledge, assets and delivery records agents update while they work. | It is a whole business back office, not a task table: invoices, clients, a portal UI, a RAG layer, Directus behind it, and humans and agents as first-class writers of the same row — "The row a human drags on a board is the row an agent picks up over MCP and writes back when it's done." Its worktree-isolated fan-out is more than bevis attempts. | Its documented gate is a permission and review gate, not a machine-checkable close: "The first MCP tool call requires approval in Claude Code. There is no zero-click connection." Its "3-reviewer adversarial risk gate" is review by agents; bevis's check is a command, an exit code and captured output. | [repo](https://github.com/AnalogElk/muster) · [musterr.dev](https://musterr.dev) |
| **Dagster asset checks** (`dagster-io/dagster`, Apache-2.0) | "An orchestration platform for the development, production, and observation of data assets." Asset checks are "tests that verify specific properties of your data assets". | The strongest blocking-check design in this list, and mature: "set the `blocking` argument to `True`" and "if the `orders_id_has_no_nulls` check fails, the downstream `augmented_orders` asset won't be materialized." Scheduling, partitions, backfills, observability, a UI — a real platform. | It gates **data assets in a pipeline**, not a job a person or an agent works on. There is no task record with an owner, no dispatcher to an agent, no two-actor rule. bevis borrows the blocking-check idea and applies it to work items. | [asset checks](https://docs.dagster.io/guides/test/asset-checks) · [repo](https://github.com/dagster-io/dagster) |
| **Temporal** (`temporalio/temporal`, MIT) | "Temporal is a durable execution platform that enables developers to build scalable applications without sacrificing productivity or reliability." | Durability of a kind bevis does not attempt: "executes units of application logic called Workflows in a resilient manner that automatically handles intermittent failures, and retries failed operations". Distributed, replayable, production-hardened for years. | Durable execution answers "did this step survive the crash", not "is the result good enough to close". Temporal has no acceptance bar, no evidence-required close, no second-actor verification. | [repo](https://github.com/temporalio/temporal) |
| **Prefect** (`PrefectHQ/prefect`, Apache-2.0) | "a workflow orchestration framework for building resilient data pipelines in Python", with "scheduling, caching, retries, and event-based automations". | Scheduling, caching and retries out of the box, plus a UI and a large data ecosystem. | Same gap as Temporal: pipeline reliability, not a work record with a required acceptance bar and an evidence-gated close. | [repo](https://github.com/PrefectHQ/prefect) |
| **Assay** (`Rul1an/assay` + `Rul1an/assay-action`, MIT) | "Policy-as-code for MCP agents: deny risky tool calls before they run, prove what ran with verifiable evidence, and enforce egress in the kernel (eBPF/LSM, Linux)." The Action verifies and lints the evidence bundle it produces. | Much stronger evidence than bevis records: tamper-evident, offline-verifiable bundles, a "deterministic, fail-closed gate [that] decides every `tools/call` before it runs", kernel-level egress enforcement, SARIF into the GitHub Security tab. If you need evidence an auditor can check without trusting you, this is far ahead. | It gates tool calls and PRs, not work items. Its README describes it as "CI-native, no backend" — there is no durable job record, no acceptance bar per task, no dispatcher. It is the evidence half without the board half. | [CLI](https://github.com/Rul1an/assay) · [Action](https://github.com/marketplace/actions/assay-ai-agent-security) |
| **Evidence Gate Action** (`evidence-gate/evidence-gate-action`, Apache-2.0) | "Fail-closed quality gates for GitHub Actions with verifiable evidence chains" — 25 gate types, four trust levels from "L1 Declaration" to "L4 Hash Chain — SHA-256 chain that any auditor can independently verify". | A much more developed theory of evidence *quality* than bevis has: trust levels, hash chains, and "Blind Gate" — keeping criteria "outside the pipeline" so "the AI that generated the code cannot see or game the thresholds". Worth reading before you design any gate. | It gates CI runs and merges. The durable cross-run state ("Quality State tracking", L4 chains) is described as a Pro/Enterprise hosted service; bevis's record is a local SQLite file under Apache-2.0 with nothing behind a tier. Also very new — 0 stars at the time I looked. | [Marketplace](https://github.com/marketplace/actions/evidence-gate-action) · [repo](https://github.com/evidence-gate/evidence-gate-action) |
| **Issue-Orchestrator** (`BruceBGordon/issue-orchestrator`, Apache-2.0) | "Orchestrate AI agents working on GitHub issues with guardrails" — a control plane that runs agents on GitHub issues in isolated worktrees behind validation and review gates. | Philosophically the closest thing here, and more complete on execution: "Treats agent completion as untrusted input, then validates the exact commit produced"; validation can be "tests, linting, type checks, architecture checks, and repo-specific policy scans"; worktree isolation, crash recovery, session replay, and "Agents cannot merge PRs. Humans merge." | It is GitHub-shaped by design — "Uses GitHub labels and observed worktree state as crash-safe external truth" — so it wants a repo, issues and PRs. bevis is a local record with no VCS or forge assumption, and gates the job's own close rather than the merge. Early beta, 3 stars when I looked. | [repo](https://github.com/BruceBGordon/issue-orchestrator) |
| **agentic-os** (`KbWen/agentic-os`, MIT) | "Governance framework for AI coding agents. It runs them through a five-step workflow (plan, build, review, test, ship) where no step counts as done without evidence." | The same invariant, delivered where most teams actually stand: drop-in `AGENTS.md`/`CLAUDE.md` rules plus git hooks and CI that enforce them — "adds a layer the agent doesn't control", with a ship gate that BLOCKS on "no review/test evidence". No new datastore to adopt. | Its record is the work log and its gate is the hook/CI boundary; the fixed five phases are the workflow. bevis has no opinion about your phases — a job graph of your own shape (acyclic by construction, since a blocker must exist before the job that waits on it), an explicit bar per job, and a `verified` state a second actor must set. | [repo](https://github.com/KbWen/agentic-os) |
| **pi-tasks** (published by `nczz`, MIT) | "Pi-native execution contracts for AI agents — evidence-gated completion, ordered plans, and compaction-safe resume". | Ships bevis's core invariant already, inside the Pi runtime: "Agents cannot mark work done without traceable, reproducible proof", and `task_complete` rejects when "No evidence exists" or "All evidence is only `not_verified`". It also solves something bevis ignores — surviving context compaction, with resume on `session_start`. | It is bound to the Pi agent runtime and its evidence is artifact references; whether a command and exit code are captured automatically is **UNVERIFIED** (the page does not say). bevis is runtime-agnostic, calls no model, and shells out to any command. | [pi.dev/packages/pi-tasks](https://pi.dev/packages/pi-tasks) |
| **Task Master** (`eyaltoledano/claude-task-master`) | "A task management system for AI-driven development with Claude" — parses a PRD into a decomposed task tree. | Decomposition is its whole craft, and it is enormously more popular. If your problem is "turn this spec into a sensible ordered task list", this is the tool. | Licence is **MIT with Commons Clause**, which is not OSI-open: "Not Allowed: Sell Task Master itself, Offer Task Master as a hosted service, Create competing products based on Task Master". No acceptance-bar-on-close mechanism appears in the README I read. bevis is plain Apache-2.0. | [repo](https://github.com/eyaltoledano/claude-task-master) |
| **OpenHands** (`All-Hands-AI/OpenHands`, MIT) | "The self-hosted developer control center for coding agents and automations" — "Run OpenHands, Claude Code, Codex, Gemini, or any ACP-compatible agent across local, remote, and cloud backends." | It is the thing that does the work. Sandboxes, runtimes, a UI, integrations, and years of engineering. bevis is not in this category and should not be compared on it. | It is a worker, not a ledger: no durable acceptance-gated job record documented in the README I read. bevis is a sane thing to put *in front of* OpenHands — it can dispatch to it and refuse to close on its say-so. | [repo](https://github.com/All-Hands-AI/OpenHands) |
| **Proof-or-Stop** (arXiv 2607.14890) | A paper, not a product: "Proof-or-Stop Lifecycle Control, a method that permits lifecycle transitions only when fresh, tracked-source-state-bound, mechanically verifiable evidence satisfies the relevant gate." | It has thought this through properly, with a formal admissibility predicate and an evaluation. Its conditions include `ExecutionAttested` — "checks command/exit-code/output" — and `ProducerAuthorized` — "checks actor authorization". If you want the theory behind what bevis does, read this, not bevis. | It is a method plus an evaluated implementation; I could not locate the public repository, so what it ships is **UNVERIFIED**. bevis is a stdlib CLI over one SQLite file that you can run from a checkout today (it is not published on any index), with a much thinner notion of evidence (no freshness binding, no signatures, no tamper-evidence). | [arXiv](https://arxiv.org/abs/2607.14890) |

---

## Use something else instead of bevis if…

**…your work already lives in GitHub Issues and your gate is CI.** Use those. This is the
right answer for most teams and it is not a consolation prize: put required status checks
on the protected branch, close issues from PRs, and you have a durable record and a machine
gate maintained by people whose full-time job it is. You are giving up one thing — the gate
sits on the merge, not on the issue, and "Click **Close issue**" needs no evidence — and if
that gap has never hurt you, it will not start hurting you because you installed bevis.
Adding a second board next to your issues is a real cost; pay it only for a real problem.

**…you need durable retries across infrastructure failures.** Use
[Temporal](https://github.com/temporalio/temporal). bevis is a single-node SQLite tool with
no retry semantics, no replay and no distributed guarantees. Temporal "automatically handles
intermittent failures, and retries failed operations" and has been doing it in production for
years. A job board that records a failure is not a substitute for a runtime that survives one.
[Prefect](https://github.com/PrefectHQ/prefect) is the same answer with a Python-pipeline
shape and built-in scheduling and caching.

**…you want a business back office your agents update.** Use
[Muster](https://github.com/AnalogElk/muster). Clients, invoices, delivery records, a portal,
humans and agents writing the same rows over MCP. bevis has no concept of a client or an
invoice and never will; it is a job table with a gate on it.

**…you want a rich, agent-native issue graph.** Use [beads](https://github.com/gastownhall/beads).
Dependency types, a computed ready frontier, atomic claims, Dolt-backed history and sync, an
acceptance-criteria field, and a real gate object — plus a community. bevis's data model is
deliberately smaller. If what you need is a good graph and you can live with a free-text close,
beads is more tool for less work. Prefer plain markdown files in your repo over a database?
[Backlog.md](https://github.com/MrLesk/Backlog.md) is the same answer with no server at all.

**…your gates are data-quality checks in a pipeline.** Use
[Dagster asset checks](https://docs.dagster.io/guides/test/asset-checks). `blocking=True` on a
check already stops downstream materialization, inside a mature orchestrator with scheduling,
partitions and a UI. bevis's blocking check is the same idea with far less behind it; do not
reimplement a data platform on top of a job table.

**…you want the discipline without adopting a datastore.** Use
[agentic-os](https://github.com/KbWen/agentic-os) — rules files plus hooks and CI that enforce
"no step counts as done without evidence" against the repo you already have.

**…you want evidence an auditor can verify without trusting you.** Use
[Assay](https://github.com/Rul1an/assay) or
[Evidence Gate](https://github.com/evidence-gate/evidence-gate-action). Tamper-evident bundles,
hash chains, trust levels, SARIF into GitHub's Security tab. bevis records a command, an exit
code and its output in a local SQLite file. That is enough to stop a job closing on a claim; it
is **not** cryptographic proof and it is not a compliance artifact.

**…you already run one of these and it works.** Then you have solved the problem bevis exists
for. Nothing here justifies a second system.

Where bevis is worth a look is narrower than any of the above: you dispatch work to agents or
scripts, you keep hitting jobs that were reported done and were not, and you want the smallest
possible thing — a stdlib CLI over a SQLite file, no model, no daemon required — that makes
"done" mean "a command said so, and here is which command and what it printed".

---

## What bevis claims

Six claims, and not one more. Each is a rule the code applies rather than advice it
offers, and each one names the test that fails if it is false. Run `python -m pytest -q`
to watch them pass, and `python tools/mutation_check.py` to watch each of those tests go
**red** when the rule it guards is deleted from the source — a test that has never been
seen to fail is not evidence that a rule holds. A claim with no test behind it does not
belong on this page; that is the same standard bevis holds a job to.

1. **A job cannot be created without an acceptance bar.** The bar is a required field,
   refused when it is empty or only whitespace, and no later update can empty it.
2. **A job cannot be closed without machine-checkable evidence.** Closing records a
   command, its exit code and its output, stored durably in SQLite. All three must be
   present, the exit code must be `0`, and the output must not be empty. There is no
   second door: the generic status setter refuses to write `closed` at all, so every
   close in the system goes through the one function that can refuse.
3. **A failing blocking check stops the job and its dependents.** The check outcome is a
   stored row, so the block survives the process that produced it and applies to whoever
   asks next — and it propagates to the job's children and to anything queued behind it,
   transitively. The same idea as a Dagster blocking check, applied to a work item.
4. **The dispatcher never decides success.** It claims ready jobs atomically and shells
   out to an agent or a command, then asks the job's *checks* whether the bar was met. An
   adapter that exits `0` with no checks defined gets the job `blocked`, never closed —
   and when the close rule refuses the dispatcher, the dispatcher records the refusal
   instead of overruling it.
5. **`verified` requires a second actor.** The actor who closed a job may not be the actor
   who verifies it, compared case-insensitively. (GitHub has long had the same principle
   on pushes — "require that the most recent reviewable push must be approved by someone
   other than the person who pushed it" — bevis applies it to a job record rather than a
   branch. What bevis compares is a name from `$BEVIS_ACTOR`, not an authenticated
   identity; see [Limitations](README.md#limitations).)
6. **Model-agnostic and boring by construction.** The CLI core is stdlib-only over SQLite
   and imports nothing that could reach a network; no module in the package imports a
   model-provider SDK; the FastAPI server is an optional extra. bevis calls no language
   model, so it behaves the same behind a frontier model, a local one, or a shell script.

| # | Claim | Enforced in | Test that fails if the claim is false | Mutant that proves that test can fail |
|---|---|---|---|---|
| 1 | Acceptance bar required at create | `core.create_job` | `test_acceptance_is_required`, `test_acceptance_of_only_whitespace_is_not_a_bar`, `test_cli_refuses_add_without_acceptance`, `test_acceptance_cannot_be_emptied_by_update` | `acceptance-bar-optional` |
| 2 | No close without command + exit 0 + output | `core.close_job` | `test_close_with_no_evidence_at_all_is_refused`, `test_close_with_nonzero_exit_is_refused`, `test_close_with_empty_output_is_refused`, `test_close_run_with_a_failing_command_is_refused`, `test_there_is_no_status_command_that_writes_closed` | `evidence-missing-exit-accepted`, `evidence-nonzero-exit-accepted`, `evidence-empty-output-accepted`, `evidence-empty-command-accepted`, `closed-status-writable-directly` |
| 3 | A failing blocking check stops the job and its dependents | `core.readiness`, `core.close_job` | `test_failing_blocking_check_makes_the_job_unready`, `test_failing_blocking_check_makes_the_job_unclosable`, `test_failing_blocking_check_makes_the_job_unclaimable`, `test_check_failure_propagates_transitively`, `test_check_outcome_is_a_durable_row` | `failing-blocking-check-still-ready`, `failing-blocking-check-does-not-stop-close`, `upstream-check-failure-not-propagated` |
| 4 | The dispatcher never decides success | `dispatch.process_job` | `test_adapter_exit_zero_alone_does_not_close_a_job`, `test_the_dispatcher_cannot_overrule_a_refusal` | `dispatcher-decides-success-itself` |
| 5 | `verified` needs a different actor | `core.verify_job` | `test_verify_by_the_actor_who_closed_it_is_refused`, `test_verify_ignores_case_when_comparing_actors`, `test_cli_verify_by_the_closer_exits_1` | `self-verification-allowed` |
| 6 | Stdlib-only core, no network, no model SDK anywhere | package imports, `pyproject.toml` | `test_the_core_imports_only_the_standard_library`, `test_the_core_cannot_reach_a_network`, `test_no_module_imports_a_model_provider_sdk`, `test_the_package_declares_no_runtime_dependencies` | none — these read the package's own imports with `ast`, so there is no rule in the code to delete. They fail the moment an import appears. |

Two things the table deliberately does not launder. Claim 4's atomic claim — one job per
slot, ever — has a test that compares recorded run intervals, but **no mutant**: remove
the atomicity and the suite usually still passes, because the race rarely loses. And the
`test_docs_claims.py` suite asserts that every test named in this file exists, so this
table cannot quietly rot into a list of names that no longer resolve; it cannot tell you
whether the test still *means* what the row says.

## What bevis does not claim

- **Not novel storage.** It is SQLite. Everything durable here is a table.
- **Not a scheduler.** No cron, no calendar, no distributed queue, no retries, no backoff, no
  replay. If a machine dies mid-job, bevis knows the job did not close; it does not resume it.
  For that, use Temporal or Prefect.
- **Not an agent framework.** No prompts, no tools, no memory, no planner, no model calls of any
  kind. It shells out and reads exit codes. If you want an agent, bring OpenHands, Claude Code,
  Codex, a Makefile, or anything else that runs.
- **Not distributed.** Single node, one SQLite file. No clustering, no HA, no multi-region.
  Concurrency is bounded by what one machine and one SQLite file can do.
- **Not cryptographic proof.** Evidence is a recorded command, exit code and output in a local
  database. It is not signed, not content-addressed, not tamper-evident, and not a compliance
  artifact. Anyone with write access to the file can edit it. Assay and Evidence Gate take that
  problem seriously; bevis does not.
- **Not a claim to have invented the idea.** See below.

---

## Where bevis's claim is weakest

This is the honest part, and it belongs in the same file as the pitch.

- **The idea is not new and is currently being formalised.** *Proof-or-Stop* (arXiv 2607.14890,
  July 2026) states the principle better than bevis does — "permits lifecycle transitions only
  when fresh, tracked-source-state-bound, mechanically verifiable evidence satisfies the relevant
  gate" — and its admissibility conditions are strictly stronger than bevis's, including
  freshness, integrity and producer authorisation alongside `ExecutionAttested`
  ("checks command/exit-code/output"). bevis implements a weaker version of a published idea.
- **At least one shipped tool already enforces the exact invariant.** pi-tasks: "Agents cannot
  mark work done without traceable, reproducible proof", with `task_complete` rejecting when
  "No evidence exists". It is bound to the Pi runtime; the invariant is the same one.
- **agentic-os enforces it too**, at the hook-and-CI boundary rather than in a job table, and it
  does so without asking you to adopt a new datastore — which for many teams is the better trade.
- **Issue-Orchestrator holds the same philosophy** — "agent output is a claim, not authority" —
  with a more complete execution story (worktrees, crash recovery, replay, human merge authority).
- **Each individual mechanism has well-established prior art**: blocking checks (Dagster),
  exit-code gating (GitHub Actions: "Any other exit code indicates the action failed"),
  separation of duties on approval (GitHub protected branches), acceptance criteria as a field
  (beads, Backlog.md), evidence bundles (Assay, Evidence Gate).

So the accurate claim is not "nobody does this". It is narrower: **bevis couples an
acceptance-bar-carrying job record to an evidence-required close, a blocking check that
propagates to dependents, a dispatcher with no authority over success, and a two-actor
verification — in one small model-agnostic tool with no runtime, forge or framework
dependency.** That combination is uncommon in what I could find. It is not unprecedented,
and if you know of a project that does it, it belongs on this page.

---

## Sources

All fetched 2026-08-25 unless noted. Quotes above come from these pages.

- beads — https://github.com/gastownhall/beads · https://github.com/gastownhall/beads/blob/main/AGENT_INSTRUCTIONS.md · https://beads.gascity.com/workflows/gates
- Backlog.md — https://github.com/MrLesk/Backlog.md
- Muster — https://github.com/AnalogElk/muster · https://musterr.dev
- Dagster — https://github.com/dagster-io/dagster · https://docs.dagster.io/guides/test/asset-checks
- Temporal — https://github.com/temporalio/temporal
- Prefect — https://github.com/PrefectHQ/prefect
- Assay — https://github.com/Rul1an/assay · https://github.com/Rul1an/assay-action · https://github.com/marketplace/actions/assay-ai-agent-security
- Evidence Gate — https://github.com/evidence-gate/evidence-gate-action · https://github.com/marketplace/actions/evidence-gate-action
- Issue-Orchestrator — https://github.com/BruceBGordon/issue-orchestrator · https://dev.to/brucebgordon/issue-orchestrator-a-software-engineering-control-plane-for-coding-agents-11ii
- agentic-os — https://github.com/KbWen/agentic-os
- pi-tasks — https://pi.dev/packages/pi-tasks
- Task Master — https://github.com/eyaltoledano/claude-task-master
- OpenHands — https://github.com/All-Hands-AI/OpenHands
- Proof-or-Stop — https://arxiv.org/abs/2607.14890 · https://arxiv.org/html/2607.14890
- GitHub Issues — https://docs.github.com/en/issues/tracking-your-work-with-issues/administering-issues/closing-an-issue
- GitHub protected branches — https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches
- GitHub Actions exit codes — https://docs.github.com/en/actions/how-tos/create-and-publish-actions/set-exit-codes

### Marked UNVERIFIED

- Whether Backlog.md enforces acceptance criteria or the Definition of Done in code. The README
  describes them as a checklist; I did not read the source.
- Whether pi-tasks automatically captures a command and its exit code as evidence. The package
  page describes evidence as "artifact references" and does not say.
- The public repository for the *Proof-or-Stop* implementation. The paper says the method "was
  instantiated in the open-source *Proof-or-Stop* implementation"; I could not find the repo.
- Star counts and beta status are point-in-time readings from 2026-08-25 and will be stale.
- Muster's, beads' and OpenHands' internals beyond their public docs — README-level reading only.

---

## Further reading

* [README.md](README.md) — what bevis is, and a transcript of it refusing a close.
* [DOCTRINE.md](DOCTRINE.md) — the operating rules its shape comes from, and the
  incident behind each one.
* [docs/DESIGN.md](docs/DESIGN.md) — the rules in full, the arguments behind them,
  and the known holes.
