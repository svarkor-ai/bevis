"""The documentation may not name a test that does not exist.

PRIOR-ART.md maps every claim bevis makes to the test that fails if the claim is
false. A table like that is worth exactly as much as its names still resolving,
so the names are checked here rather than trusted: rename or delete a test and
the build tells you which document now lies.

It checks that the names resolve. It cannot check that a test still *means* what
the row says it means — that is a human's job, and the row is a pointer to the
place where the reading happens.
"""
from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
DOCS = [ROOT / "README.md", ROOT / "PRIOR-ART.md", ROOT / "DOCTRINE.md",
        ROOT / "docs" / "DESIGN.md"]

#: A test name mentioned in prose, but not a filename like test_docs_claims.py.
TEST_NAME_RE = re.compile(r"\btest_[a-z0-9_]+\b(?!\.py)")
MUTANT_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)+$")


def collected_test_names() -> set:
    names = set()
    for path in sorted(TESTS.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name.startswith("test_"):
                names.add(node.name)
    return names


def mutant_names() -> set:
    spec = importlib.util.spec_from_file_location(
        "_mutation_check", ROOT / "tools" / "mutation_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {name for name, *_ in module.MUTANTS}, module.MUTANTS


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: p.name)
def test_every_test_named_in_the_docs_exists(doc):
    known = collected_test_names()
    cited = set(TEST_NAME_RE.findall(doc.read_text(encoding="utf-8")))
    missing = sorted(cited - known)
    assert not missing, (
        "%s names %d test(s) that do not exist: %s"
        % (doc.name, len(missing), ", ".join(missing)))


def test_every_mutant_named_in_the_claims_table_exists():
    known, _ = mutant_names()
    text = (ROOT / "PRIOR-ART.md").read_text(encoding="utf-8").split("\n")
    header = next((i for i, line in enumerate(text)
                   if line.startswith("|") and "Mutant that proves" in line), None)
    assert header is not None, "PRIOR-ART.md has no claim -> test table"
    cited = set()
    for line in text[header + 2:]:
        if not line.startswith("|"):
            break
        cell = [c.strip() for c in line.strip().strip("|").split("|")][-1]
        cited.update(token for token in re.findall(r"`([^`]+)`", cell)
                     if MUTANT_NAME_RE.match(token))
    assert cited, "the claim table cites no mutants at all"
    missing = sorted(cited - known)
    assert not missing, (
        "PRIOR-ART.md names %d mutant(s) that tools/mutation_check.py does not "
        "plant: %s" % (len(missing), ", ".join(missing)))


def test_every_mutant_names_a_real_test():
    known = collected_test_names()
    _, mutants = mutant_names()
    broken = {name: sorted(set(expected) - known) for name, _, _, _, expected in mutants
              if set(expected) - known}
    assert not broken, (
        "tools/mutation_check.py expects tests that do not exist: %s" % broken)
