"""bevis claims to be stdlib-only, model-free and unable to phone anywhere.

Those are the easiest claims in the project to make and the easiest to break by
accident, so they are asserted mechanically here rather than promised in prose.
Each test reads the source with `ast` — not a grep over comments — so an import
added in a refactor fails the build.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "bevis"

#: api.py is the one module allowed third-party imports: the HTTP API is an
#: optional extra (`pip install bevis[api]`) and the CLI must keep working on a
#: machine that has nothing but Python.
CORE_MODULES = sorted(p for p in PACKAGE.glob("*.py") if p.name != "api.py")

#: Anything that could carry a request off this machine. The core must not be
#: able to reach a network at all, which is a stronger and more checkable
#: property than "does not call a language model".
NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "urllib2", "ftplib", "smtplib",
    "telnetlib", "xmlrpc", "asyncio", "requests", "httpx", "aiohttp",
    "websockets", "grpc",
}

#: Model provider SDKs. No module in the package may import one, api.py
#: included: bevis decides nothing with a model, anywhere.
MODEL_SDKS = {
    "openai", "anthropic", "cohere", "mistralai", "litellm", "ollama",
    "transformers", "torch", "langchain", "llama_cpp", "huggingface_hub",
    "google", "vertexai", "boto3",
}


def imported_roots(path: Path) -> set:
    """Top-level module names imported by one file, relative imports excluded."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # `from .db import ...` — inside the package
                continue
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


@pytest.mark.parametrize("path", CORE_MODULES, ids=lambda p: p.name)
def test_the_core_imports_only_the_standard_library(path):
    third_party = {
        name for name in imported_roots(path)
        if name not in sys.stdlib_module_names and name != "bevis"
    }
    assert not third_party, (
        "%s imports %s; the CLI core must install and run with nothing but "
        "Python" % (path.name, ", ".join(sorted(third_party))))


@pytest.mark.parametrize("path", CORE_MODULES, ids=lambda p: p.name)
def test_the_core_cannot_reach_a_network(path):
    reachable = imported_roots(path) & NETWORK_MODULES
    assert not reachable, (
        "%s imports %s — the core decides everything from a local exit code and "
        "must not be able to talk to anything" % (path.name, ", ".join(sorted(reachable))))


@pytest.mark.parametrize("path", sorted(PACKAGE.glob("*.py")), ids=lambda p: p.name)
def test_no_module_imports_a_model_provider_sdk(path):
    sdks = imported_roots(path) & MODEL_SDKS
    assert not sdks, (
        "%s imports %s; bevis calls no language model, in any module"
        % (path.name, ", ".join(sorted(sdks))))


def test_the_package_declares_no_runtime_dependencies():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project = re.search(r"^\[project\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    assert project, "pyproject.toml has no [project] table"
    assert re.search(r"^dependencies\s*=\s*\[\s*\]\s*$", project.group(1), re.M), (
        "the [project] table must declare `dependencies = []`; anything the API "
        "needs belongs in the optional [api] extra")
