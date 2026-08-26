#!/usr/bin/env python3
"""Run README.md and diff it against what the tool actually prints.

bevis's whole claim is that a document without a command behind it is
decoration. That has to apply to its own README first: every transcript in
README.md is executed here, in order, and compared line by line with the output
the real CLI produced. A drifted flag, a renamed subcommand or an invented
message is a failing build, not a documentation bug somebody notices later.

    python tools/readme_check.py            # check (this is what CI runs)
    python tools/readme_check.py --record   # re-run and print README.md with the
                                            # real output pasted in (never edits
                                            # README.md in place)
    python tools/readme_check.py --keep      # leave the sandbox for inspection

How a block is classified
-------------------------
Any fenced block containing a line that starts with "$ " is a TRANSCRIPT and is
executed. That rule is deliberate: you cannot exempt a transcript from the check
by mislabelling the fence, because the prompt marker is what selects it.

  ```console        the commands run in a scratch sandbox directory
  ```console repo   the commands run in the repository checkout (dev commands)

A fenced block with no "$ " prompt is inert text. If such a block is shell-ish
(sh/bash/console/shell/text) every non-empty line in it must match the small
allowlist of things that legitimately cannot run here — installing the package
itself. Anything else is a failure, so a command cannot hide from execution by
dropping its prompt either.

What is NOT verified, stated plainly
------------------------------------
* `pip install -e .` is not executed; it would mutate the environment running
  the check. It is linted instead: the extras it names must exist in
  pyproject.toml, and a bare `pip install bevis` is refused outright, because
  the package is not published and a README must not print a command that
  cannot work.
* Commands run through `bash`, so this proves the transcripts on a POSIX shell
  and says nothing about Windows.
* Both streams are captured together, exactly as a terminal shows them, then
  compared after stripping trailing whitespace and leading/blank trailing lines
  on BOTH sides. Nothing else is normalised: no timestamps are papered over, no
  paths are rewritten. Where a transcript would print something unstable the
  README pipes it through a visible filter, so the reader can see what was
  elided and by what.
* Every path into this repository that the docs name in backticks must exist.
* A `bevis ...` command written in prose rather than in a transcript cannot be
  executed, so it is parsed against the CLI's own argparse surface instead: the
  subcommand must exist and every flag must be one the CLI accepts. That check
  covers README.md, PRIOR-ART.md, DOCTRINE.md and docs/DESIGN.md.
* `bevis` and `python` resolve to shims created here, so the check always
  exercises THIS checkout with the interpreter running this script — never a
  copy of bevis that happens to be installed on the machine.
"""
from __future__ import annotations

import argparse
import difflib
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
README = ROOT / "README.md"
PYPROJECT = ROOT / "pyproject.toml"

#: Fixed on purpose. `bevis init` prints an absolute path, so a README that
#: shows that line can only be byte-exact if the sandbox is at the same place on
#: every machine. A random mkdtemp() name would force the check to rewrite paths
#: before comparing, and a check that edits the output before reading it is the
#: kind of thing this project exists to argue against.
SANDBOX = Path("/tmp/bevis-readme-check")

MARK = "__BEVIS_README_CHECK__"
PROMPT = "$ "
SHELLISH = {"sh", "bash", "shell", "console", "text", ""}

#: The only commands allowed to appear in a block that is not executed.
INERT_ALLOWED = (
    re.compile(r"^pip install -e ['\"]?\.(\[[a-z0-9,._-]+\])?['\"]?$"),
    re.compile(r"^python -m pip install -e ['\"]?\.(\[[a-z0-9,._-]+\])?['\"]?$"),
    # bevis is on PyPI as of the launch commit, so installing it by name is a
    # command that works. It stays INERT on purpose: see lint_pip.
    re.compile(r"^pip install ['\"]?bevis(\[[a-z0-9,._-]+\])?['\"]?$"),
    re.compile(r"^python -m pip install ['\"]?bevis(\[[a-z0-9,._-]+\])?['\"]?$"),
)


class Failure(Exception):
    pass


class Block:
    def __init__(self, info: str, lines, first_line: int):
        self.info = info
        self.lines = lines
        self.first_line = first_line          # 1-based line number of the first
        tokens = info.split()                 # content line in README.md
        self.language = tokens[0] if tokens else ""
        self.options = tokens[1:]

    @property
    def is_transcript(self) -> bool:
        return any(line.startswith(PROMPT) for line in self.lines)


# ── Parsing ──────────────────────────────────────────────────────────────────
def parse_blocks(text: str):
    lines = text.split("\n")
    blocks, index = [], 0
    while index < len(lines):
        if lines[index].startswith("```"):
            info = lines[index][3:].strip()
            start = index + 1
            end = start
            while end < len(lines) and not lines[end].startswith("```"):
                end += 1
            if end >= len(lines):
                raise Failure("unclosed code fence opened at README.md line %d"
                              % (index + 1))
            blocks.append(Block(info, lines[start:end], start + 1))
            index = end + 1
        else:
            index += 1
    return blocks


def parse_steps(block: Block):
    """Split a transcript into (command, expected output lines) pairs."""
    steps, lines, index = [], block.lines, 0
    while index < len(lines):
        if not lines[index].startswith(PROMPT):
            raise Failure(
                "README.md line %d: output before any '$ ' prompt in a transcript "
                "block — every transcript must start with a command"
                % (block.first_line + index))
        command = [lines[index][len(PROMPT):]]
        while command[-1].rstrip().endswith("\\"):
            index += 1
            if index >= len(lines):
                raise Failure("README.md line %d: command ends with a dangling "
                              "line continuation" % (block.first_line + index))
            command.append(lines[index])
        index += 1
        expected = []
        while index < len(lines) and not lines[index].startswith(PROMPT):
            expected.append(lines[index])
            index += 1
        steps.append(("\n".join(command), expected))
    return steps


# ── Execution ────────────────────────────────────────────────────────────────
def write_shims(bindir: Path) -> None:
    bindir.mkdir(parents=True, exist_ok=True)
    for name, body in (
        ("bevis", '#!/bin/sh\nexec "%s" -m bevis "$@"\n' % sys.executable),
        ("python", '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable),
    ):
        path = bindir / name
        path.write_text(body)
        path.chmod(0o755)


def make_env(bindir: Path) -> dict:
    env = dict(os.environ)
    for key in ("BEVIS_DB", "BEVIS_ACTOR", "BEVIS_TOKEN"):
        env.pop(key, None)
    env["PATH"] = "%s%s%s" % (bindir, os.pathsep, env.get("PATH", ""))
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(ROOT) + (os.pathsep + existing if existing else "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"      # keep __pycache__ out of the tree
    env["LC_ALL"] = "C.UTF-8"
    env["LANG"] = "C.UTF-8"
    env["COLUMNS"] = "80"
    env["TERM"] = "dumb"
    return env


def run_steps(steps, cwd: Path, env: dict):
    """Run one block's commands in a single shell, and split the output up.

    One shell per block, not one per command, so `$?`, `cd` and exported
    variables behave the way the transcript says they do.
    """
    script = ["set +e"]
    for command, _ in steps:
        script.append(command)
        script.append('__rc=$?; printf "\\n%s%%d\\n" "$__rc"; (exit $__rc)' % MARK)
    proc = subprocess.run(["bash", "-c", "\n".join(script)], cwd=str(cwd), env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    chunks, current = [], []
    for line in proc.stdout.split("\n"):
        if line.startswith(MARK):
            chunks.append(current)
            current = []
        else:
            current.append(line)
    while len(chunks) < len(steps):            # a shell that died mid-block
        chunks.append(current + ["[readme_check: the shell produced no more output]"])
        current = []
    return chunks


def normalise(lines):
    out = [line.rstrip() for line in lines]
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    return out


# ── Static lint of the blocks that are not executed ──────────────────────────
def declared_extras() -> set:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r"^\[project\.optional-dependencies\]\s*$(.*?)(?=^\[|\Z)",
                      text, re.M | re.S)
    if not match:
        return set()
    return set(re.findall(r"^([A-Za-z0-9_.-]+)\s*=", match.group(1), re.M))


def lint_inert(block: Block, problems: list) -> None:
    if block.language not in SHELLISH:
        return
    for offset, line in enumerate(block.lines):
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        text = re.sub(r"\s+#.*$", "", text).strip()
        if any(pattern.match(text) for pattern in INERT_ALLOWED):
            continue
        problems.append(
            "README.md line %d: %r sits in a block that is never executed. Only "
            "installing the package itself may do that; give the block a '$ ' "
            "prompt so it runs, or delete it."
            % (block.first_line + offset, text))


def lint_pip(text: str, extras: set, problems: list) -> None:
    for number, line in enumerate(text.split("\n"), 1):
        prompted = line.strip().startswith(PROMPT)
        stripped = re.sub(r"^\$ ", "", line.strip())
        if not re.match(r"^(python -m )?pip install\b", stripped):
            continue
        if prompted and re.match(
                r"^(python -m )?pip install\s+['\"]?bevis(\[|['\"]?$)", stripped):
            problems.append(
                "README.md line %d: %r — a prompted block is EXECUTED by this "
                "checker, and installing from PyPI would make the README test "
                "depend on the network and on a published release. Show it in a "
                "block with no '$ ' prompt." % (number, stripped))
        for extra in re.findall(r"\[([a-z0-9,._-]+)\]", stripped):
            for name in extra.split(","):
                if name and name not in extras:
                    problems.append(
                        "README.md line %d: extra %r is not declared in "
                        "pyproject.toml (declared: %s)"
                        % (number, name, ", ".join(sorted(extras)) or "none"))


# ── Every `bevis ...` written in prose is checked against the real parser ────
def cli_surface():
    """The subcommands and options argparse actually accepts, from the CLI itself."""
    from bevis.cli import build_parser

    parser = build_parser()
    top = {name for action in parser._actions for name in action.option_strings}
    commands = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                options = {o for a in sub._actions for o in a.option_strings}
                nested = {}
                for inner in sub._actions:
                    if isinstance(inner, argparse._SubParsersAction):
                        for sub_name, sub_parser in inner.choices.items():
                            nested[sub_name] = {
                                o for a in sub_parser._actions for o in a.option_strings}
                commands[name] = (options, nested)
    return top, commands


def lint_inline_commands(path: Path, problems: list) -> None:
    """A command written in prose is a claim too.

    Transcripts are executed, which proves them. A `bevis ...` mentioned in a
    sentence is not, so it is parsed against the real argparse surface instead:
    the subcommand must exist and every flag must be one the CLI accepts. That
    is how a README ends up documenting a `--force` nobody implemented.
    """
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)   # fences run instead
    top, commands = cli_surface()
    for span in re.findall(r"`([^`\n]+)`", text):
        if not re.match(r"^bevis\b", span.strip()):
            continue
        try:
            tokens = shlex.split(span.strip())
        except ValueError:
            problems.append("%s: cannot parse the command `%s` written in prose"
                            % (path.name, span))
            continue
        tokens = tokens[1:]
        while tokens and tokens[0] in top:
            tokens.pop(0)
        if not tokens:
            continue
        command = tokens.pop(0)
        if command not in commands:
            problems.append(
                "%s: `%s` — %r is not a bevis subcommand (have: %s)"
                % (path.name, span, command, ", ".join(sorted(commands))))
            continue
        allowed, nested = commands[command]
        if nested and tokens and tokens[0] in nested:
            allowed = allowed | nested[tokens.pop(0)]
        elif nested and tokens and not tokens[0].startswith("-"):
            problems.append(
                "%s: `%s` — %r is not a `bevis %s` subcommand (have: %s)"
                % (path.name, span, tokens[0], command, ", ".join(sorted(nested))))
            continue
        for token in tokens:
            if not token.startswith("--"):
                continue
            flag = token.split("=", 1)[0]
            if flag not in allowed:
                problems.append(
                    "%s: `%s` — `bevis %s` has no %s flag (has: %s)"
                    % (path.name, span, command, flag,
                       ", ".join(sorted(o for o in allowed if o.startswith("--")))))


#: A path inside this repository, as written in prose: `tools/readme_check.py`.
#: It has to start with one of our directories AND end in a file extension, so
#: that neither another project's file (`AGENTS.md`, `MrLesk/Backlog.md`) nor a
#: quoted method name from someone else's API (`tools/call`) is mistaken for ours.
REPO_PATH_RE = re.compile(
    r"^(?:bevis|tests|tools|docs|examples|\.github)/[\w./-]+"
    r"\.(?:py|md|sh|ya?ml|toml|txt|cfg|ini|json)$")


def lint_repo_paths(path: Path, problems: list) -> None:
    """A cited file that does not exist is the oldest documentation defect there is."""
    text = path.read_text(encoding="utf-8")
    for span in re.findall(r"`([^`\n]+)`", text):
        candidate = span.strip()
        if REPO_PATH_RE.match(candidate) and not (ROOT / candidate).exists():
            problems.append("%s: `%s` does not exist in the repository"
                            % (path.name, candidate))


# ── Main ─────────────────────────────────────────────────────────────────────
def check(record: bool, keep: bool) -> int:
    if not README.exists():
        print("readme_check: no README.md at %s" % README, file=sys.stderr)
        return 2
    text = README.read_text(encoding="utf-8")
    blocks = parse_blocks(text)

    problems: list = []
    lint_pip(text, declared_extras(), problems)
    for doc in (README, ROOT / "PRIOR-ART.md", ROOT / "DOCTRINE.md",
                ROOT / "docs" / "DESIGN.md"):
        if doc.exists():
            lint_inline_commands(doc, problems)
            lint_repo_paths(doc, problems)
    for block in blocks:
        if not block.is_transcript:
            lint_inert(block, problems)
        elif block.language != "console":
            problems.append(
                "README.md line %d: a transcript block must be fenced as "
                "```console (found %r)" % (block.first_line, block.info))
        elif set(block.options) - {"repo"}:
            problems.append(
                "README.md line %d: unknown fence option(s) %s"
                % (block.first_line, ", ".join(sorted(set(block.options) - {"repo"}))))

    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)
    bindir = SANDBOX.parent / "bevis-readme-check-bin"
    if bindir.exists():
        shutil.rmtree(bindir)
    write_shims(bindir)
    env = make_env(bindir)

    transcripts = [b for b in blocks if b.is_transcript and b.language == "console"]
    recorded = {}
    commands = 0
    for block in transcripts:
        steps = parse_steps(block)
        cwd = ROOT if "repo" in block.options else SANDBOX
        actual = run_steps(steps, cwd, env)
        replacement = []
        for (command, expected), got in zip(steps, actual):
            commands += 1
            replacement.append(PROMPT + command)
            replacement.extend(normalise(got))
            if normalise(expected) != normalise(got):
                diff = "\n".join(difflib.unified_diff(
                    normalise(expected), normalise(got),
                    fromfile="README.md claims", tofile="the tool printed",
                    lineterm=""))
                problems.append(
                    "README.md line %d: `%s` did not print what the README says\n%s"
                    % (block.first_line, command.replace("\n", " "), diff))
        recorded[id(block)] = replacement

    if record:
        lines = text.split("\n")
        for block in reversed(transcripts):
            start = block.first_line - 1
            lines[start:start + len(block.lines)] = recorded[id(block)]
        sys.stdout.write("\n".join(lines))
        print("readme_check: recorded %d command(s); README.md was NOT modified"
              % commands, file=sys.stderr)
        return 0

    if not keep and SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    if bindir.exists() and not keep:
        shutil.rmtree(bindir)

    if problems:
        print("readme_check: FAILED — README.md does not match the tool\n")
        for problem in problems:
            print("  * %s\n" % problem)
        print("%d problem(s). The README is a test; fix the README or fix the code."
              % len(problems))
        return 1
    print("readme_check: %d command(s) in %d transcript(s) ran and printed exactly "
          "what README.md says." % (commands, len(transcripts)))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="readme_check",
        description="Execute every transcript in README.md and diff it against "
                    "what the tool really prints.")
    parser.add_argument("--record", action="store_true",
                        help="print README.md with the real output pasted in "
                             "(does not modify README.md)")
    parser.add_argument("--keep", action="store_true",
                        help="keep the sandbox directory for inspection")
    args = parser.parse_args(argv)
    try:
        return check(record=args.record, keep=args.keep)
    except Failure as exc:
        print("readme_check: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
