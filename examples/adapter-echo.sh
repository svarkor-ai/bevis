#!/usr/bin/env bash
# The smallest possible bevis adapter.
#
# `bevis run` calls an adapter once per claimed job and hands it the job through
# the environment (and, if you use placeholders, on the command line). What the
# adapter does is entirely up to you: run a build, drive a coding agent, page a
# human. bevis does not care and does not ask.
#
# What matters is what happens AFTER: bevis ignores this script's opinion of its
# own success. It runs the job's checks and closes the job only if they pass.
#
#   bevis run --adapter examples/adapter-echo.sh
#
set -euo pipefail

echo "job:        ${BEVIS_JOB_DISPLAY_ID}"
echo "title:      ${BEVIS_JOB_TITLE}"
echo "the bar:    ${BEVIS_JOB_ACCEPTANCE}"
echo "slot:       ${BEVIS_SLOT}"

# ... do the work here ...

# Exiting 0 says "the attempt completed". It does not say "the job is done" —
# only the checks can say that, and they are about to run.
exit 0
