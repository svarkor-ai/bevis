#!/usr/bin/env bash
# A bevis adapter that hands the job to YOUR agent — whatever it is.
#
# The shape: a command-line agent that reads a prompt on stdin and writes its
# work to the repository. Point $MY_AGENT_CMD at it and register this file:
#
#   export MY_AGENT_CMD="my-coding-agent --yes"      # yours, not bevis's
#   bevis adapter add myagent --cmd "$PWD/examples/adapter-agent.sh"
#   bevis doctor --adapter myagent
#   bevis run --adapter myagent
#
# Note where the configuration lives: in THIS script's environment, on the
# machine where the agent runs. bevis stores the name `myagent` and the path to
# this file. It never learns what your agent is, where it runs, or what it
# authenticates with — and it has nowhere to put those even if you wanted it to.
set -euo pipefail

AGENT=${MY_AGENT_CMD:?set MY_AGENT_CMD to the agent command bevis should run}

# `bevis doctor --adapter <name>` sets this. Answer cheaply: a diagnostic must
# not cost an agent run.
[ "${BEVIS_DOCTOR_PROBE:-0}" = 1 ] && { echo "ready to run: $AGENT"; exit 0; }

transcript="bevis-job-${BEVIS_JOB_DISPLAY_ID}.log"
printf '%s\n\n%s\n\nThis is done when: %s\n' \
    "$BEVIS_JOB_TITLE" "$BEVIS_JOB_DESCRIPTION" "$BEVIS_JOB_ACCEPTANCE" \
  | $AGENT 2>&1 | tee "$transcript"

# `set -o pipefail` means a crashing agent exits non-zero here, and bevis marks
# the job `failed` instead of asking the checks. Exiting 0 only claims that the
# attempt finished — the job's checks decide whether it worked.
echo "transcript: $transcript"
