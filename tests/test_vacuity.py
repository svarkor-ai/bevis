"""The vacuity lexicon, calibrated in both directions.

A command that exits 0 having examined NOTHING has not verified anything. It
returned success because there was nothing there to disagree with. This file is
the corpus that decides where that line falls, and it has two halves on purpose:
a gate tested only on the cases it should catch is half-tested (DOCTRINE 2).

  VACUOUS    real output from a real tool that measured nothing. Every one of
             these must be refused, or the rule is decoration.
  LEGITIMATE real output from a real tool that measured something and found it
             clean. Every one of these must pass, or the rule is worse than
             useless: it refuses honest evidence and teaches people to route
             around bevis.

The distinction the whole lexicon turns on is SUBJECTS versus DEFECTS. "Ran 0
tests" is zero subjects. "0 tests failed" is every subject, all fine. Several
entries below sit deliberately close to the line, and `test_which_legitimate_
outputs_the_lexicon_nearly_caught` prints the ones that a naive lexicon would
have refused.
"""
from __future__ import annotations

import pytest

from bevis import core
from bevis.dispatch import dispatch
from bevis.errors import Refusal
from bevis.model import EXIT_REFUSED

#: Real output from a real tool that measured nothing. MUST be refused.
VACUOUS = [
    ("unittest discover over an empty tree",
     "\n------------------------------------------------\nRan 0 tests in 0.000s\n\nOK"),
    ("pytest with everything deselected", "no tests ran in 0.01s"),
    ("pytest collecting nothing", "collected 0 items\n\nno tests ran in 0.02s"),
    ("jest with no test files", "No tests found, exiting with code 0"),
    ("rspec on an empty suite", "0 examples, 0 failures\n\nFinished in 0.0002 seconds"),
    ("pytest where everything skipped", "0 passed, 3 skipped in 0.11s"),
    ("mocha on an empty suite", "  0 passing (2ms)"),
    ("go test on a package with no tests", "?   example.com/pkg  [no test files]\nok"),
    ("a scanner handed no files", "scanned 0 files for secrets\nclean"),
    ("the same, in the other word order", "secret scan complete: 0 files checked"),
    ("shellcheck invoked with no arguments",
     "No files specified.\nUsage: shellcheck [OPTIONS...] FILES..."),
    ("a linter whose glob matched nothing", "no python files to check"),
    ("a home-made gate with an empty worklist", "nothing to verify\ndone"),
    ("a runner that asserted nothing", "0 assertions, 0 failures\nBuild OK"),
]

#: Real output from a real tool that measured something. MUST NOT be refused.
#: Several of these are near misses on purpose — see the test at the bottom.
LEGITIMATE = [
    ("pytest, an ordinary pass", "12 passed in 0.34s"),
    ("pytest, a big run with skips", "100 passed, 2 skipped in 12.01s"),
    ("unittest, ten tests",
     "..........\n------------------------------\nRan 10 tests in 0.012s\n\nOK"),
    ("unittest, exactly one test (singular)",
     ".\n------------------------------\nRan 1 test in 0.000s\n\nOK"),
    ("unittest with skips", "OK (skipped=3)"),
    ("mocha, an ordinary pass", "  12 passing (340ms)"),
    ("go test, an ordinary pass", "ok      example.com/pkg  0.012s"),
    ("mypy on a clean tree", "Success: no issues found in 42 source files"),
    ("black on a formatted tree", "All done!\n1 file left unchanged."),
    ("a secret scan that read files and found nothing",
     "gitleaks: no leaks found\nscanned 143 files in 0.8s"),
    ("zero DEFECTS — the answer you wanted", "0 errors, 0 warnings"),
    ("a summary saying nothing broke", "40 tests, 0 tests failed"),
    ("npm audit on a clean lockfile", "found 0 vulnerabilities"),
    ("a query asserting there are no orphan rows", " id \n----\n(0 rows)"),
    ("a grep-based scan reporting a clean artefact",
     "no matches found in build/release.tar"),
    ("an assertion that no temp files were left behind", "no files matched *.tmp"),
    ("git, proving a re-apply changed nothing", "nothing to commit, working tree clean"),
    ("git diff --stat on an idempotent apply",
     "0 files changed, 0 insertions(+), 0 deletions(-)"),
    ("a deploy assertion", "Deployed. 4 services healthy, 0 failed."),
    ("the README's own worked example", "2 rows imported"),
    ("a coverage summary", "TOTAL   210    0   100%"),
    ("a row-count assertion", "row_count=1841"),
    ("a checker naming what it inspected", "checked 23 files, 0 problems"),
    ("a migration with nothing left to apply",
     "alembic: no new migrations, head is 8f3c2a1"),
    ("a lint pass over real files", "ruff: checked 88 files, all clean"),
    ("a note about test files, in prose", "regenerated 4 test files"),
    ("a four-thousand-line build log with one empty sub-run",
     "building 40 packages\ntests/legacy: no tests ran\ntests/core: ok\n"
     "312 passed in 41.20s"),
    ("a multi-package run where one package has no tests",
     "pkg a: 18 tests passed\npkg b: collected 0 items\npkg c: ok"),
]


@pytest.mark.parametrize("name,output", VACUOUS, ids=[n for n, _ in VACUOUS])
def test_output_that_measured_nothing_is_vacuous(name, output):
    assert core.vacuity_problem(output), (
        "%r is a tool reporting that it measured nothing, and the lexicon let it "
        "through: %r" % (name, output))


@pytest.mark.parametrize("name,output", LEGITIMATE, ids=[n for n, _ in LEGITIMATE])
def test_real_passing_output_is_never_called_vacuous(name, output):
    problem = core.vacuity_problem(output)
    assert problem is None, (
        "%r is real evidence from a run that measured something, and the lexicon "
        "refused it on %r. A rule that refuses honest output is worse than no "
        "rule: it teaches people to route around bevis." % (name, problem))


def test_which_legitimate_outputs_the_lexicon_nearly_caught():
    """The calibration record: legitimate output that trips a needle and is
    saved only by the counter-evidence rule. Printed, not merely asserted, so a
    change to either half shows what it moved."""
    near = [(name, core.first_vacuity_needle(text), core.measured_something(text))
            for name, text in LEGITIMATE if core.first_vacuity_needle(text)]
    for name, needle, rescue in near:
        print("NEAR MISS  %-52s needle=%-20r rescued by %r" % (name, needle, rescue))
    assert near, (
        "no legitimate output in the corpus comes near the lexicon at all, which "
        "means the corpus is not testing where the line falls. Add a real log "
        "that mentions an empty sub-run.")
    assert all(rescue for _, _, rescue in near)


def test_a_log_that_measured_something_is_not_vacuous():
    """The counter-evidence rule, on its own.

    A build log that mentions one empty sub-run and also reports 312 passing
    tests measured plenty. Refusing it would be a false accusation, and this is
    the rule that stops the lexicon making one."""
    log = ("building 40 packages\ntests/legacy: no tests ran\n"
           "tests/core: ok\n312 passed in 41.20s")
    assert core.first_vacuity_needle(log) == "no tests ran"
    assert core.measured_something(log) == "312 passed"
    assert core.vacuity_problem(log) is None


def test_a_needle_does_not_match_a_bigger_number():
    """`0 passed` must not fire inside `100 passed`, and `0 tests` must not fire
    inside `40 tests`. Asserted against the lexicon directly, with no rescue in
    the way, because the counter-evidence rule would otherwise mask a pattern
    that matches the wrong digits."""
    for text in ("100 passed in 3.2s", "40 tests, all green", "20 passing (1s)",
                 "collected 130 items", "150 examples, 0 failures"):
        assert core.first_vacuity_needle(text) is None, text


def test_zero_defects_is_not_zero_subjects():
    """The single distinction the lexicon rests on, stated as a test."""
    assert core.first_vacuity_needle("0 tests ran") == "0 tests"
    assert core.first_vacuity_needle("0 tests failed") is None
    assert core.first_vacuity_needle("0 files checked") == "0 files checked"
    assert core.first_vacuity_needle("0 files changed") is None


def test_close_with_vacuous_output_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_job(conn, job["id"], "python -m unittest discover", 0,
                       "Ran 0 tests in 0.000s\n\nOK")
    message = str(excinfo.value)
    assert "measured nothing" in message
    assert "'Ran 0 tests'" in message
    assert core.get_job(conn, job["id"])["status"] == "open"


# A runner that reports "Ran 0 tests" and still exits 0.
#
# These two tests used `python3 -m unittest discover` over an empty directory.
# That is version-dependent: on 3.10 it prints "OK" and exits 0, but Python 3.12
# changed unittest to print "NO TESTS RAN" and exit 5. On 3.12 the close was then
# refused by the EXIT-CODE rule before the vacuity rule was ever consulted, so the
# tests failed on an assertion about which refusal fired — while bevis behaved
# correctly on both. The fixture, not the product, was the version-dependent part.
#
# (Python 3.12 adopting "ran no tests" as a failure is the same rule this module
# exists to enforce. We keep testing our own, which must hold when the runner
# under test exits 0.)
VACUOUS_RUNNER = (
    "printf '%s\\n' '' "
    "'----------------------------------------------------------------------' "
    "'Ran 0 tests in 0.000s' '' 'OK'"
)

def test_close_run_on_a_runner_that_found_no_tests_is_refused(conn, job):
    with pytest.raises(Refusal) as excinfo:
        core.close_by_running(conn, job["id"], VACUOUS_RUNNER)
    assert "measured nothing" in str(excinfo.value)
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_close_with_real_output_is_untouched_by_the_rule(conn, job):
    closed = core.close_job(conn, job["id"], "pytest -q", 0, "12 passed in 0.34s")
    assert closed["status"] == "closed"


def test_cli_close_on_a_vacuous_run_exits_1(cli, conn, job):
    code, _, err = cli("close", str(job["id"]), "--run", VACUOUS_RUNNER)
    assert code == EXIT_REFUSED
    assert "measured nothing" in err
    assert core.get_job(conn, job["id"])["status"] == "open"


def test_the_dispatcher_cannot_close_on_a_check_that_measured_nothing(conn, db_path):
    """The rule lives in close_job(), so the dispatcher inherits it rather than
    carrying a second copy that could drift."""
    core.create_job(conn, "run the suite", "the suite passes")
    core.add_check(conn, 1, "unit", "printf 'Ran 0 tests in 0.000s\\n\\nOK\\n'",
                   blocking=True)
    [result] = dispatch(db_path, "bash -c 'echo did the work'")
    assert result["outcome"] == "blocked"
    assert "measured nothing" in result["detail"]
    row = core.get_job(conn, 1)
    assert row["status"] == "blocked"
    assert row["verify_cmd"] is None


def test_the_refusal_survives_a_reopened_and_retried_close(conn, job):
    """A vacuous close is refused, so there is nothing to reopen: the job never
    left `open`. Proving that here rather than assuming it, because a rule that
    refuses AFTER writing the row would leave evidence on the board."""
    with pytest.raises(Refusal):
        core.close_by_running(conn, job["id"], "echo 'collected 0 items'")
    row = core.get_job(conn, job["id"])
    assert row["status"] == "open"
    assert row["verify_cmd"] is None
    assert row["closed_at"] is None
