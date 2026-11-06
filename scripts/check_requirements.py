#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Fail when requirements/ holds anything besides one requirements.md per feature.

Spec Kit writes a feature's working file as specs/NNN-slug/spec.md, with its
plan, task list, research notes, data model, quickstart, contracts and
checklists beside it. `.gitignore` keeps all of that untracked, and the
working file is rewritten into requirements/NNN-slug/requirements.md before
the base pull request is opened (requirements/.kb/requirements.md).

This check reports what the ignore rules cannot prevent: a file added with
`git add -f`, a feature directory with no requirements.md, a requirements.md
with no `## Requirements` section, and a spec.md or a file under specs/ that
git tracks. It reads what git tracks, so a Spec Kit run in a working tree does
not fail it. Outside a git work tree it reads the files present instead.

It uses only the standard library.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ALLOWED_IN_FEATURE = {"requirements.md"}
KB_DOCUMENT = re.compile(r"^\.kb/[a-z0-9][a-z0-9-]*\.md$")
FEATURE_DIR = re.compile(r"^\d{3}-[a-z0-9][a-z0-9-]*$")
REQUIREMENTS_HEADING = re.compile(r"^##\s+Requirements\b", re.MULTILINE)
UNTRACKED_DIRS = {".git", ".venv", ".tox", "build", "node_modules", "__pycache__"}


class GitError(Exception):
    """git could not list the tracked files of a work tree."""


def tracked_files(directory: Path) -> list[str] | None:
    """Paths git tracks under directory, relative to it, or None outside a work tree.

    Any other git failure raises GitError rather than falling back to the
    files present, which include gitignored Spec Kit output.
    """
    try:
        listed = subprocess.run(
            ["git", "-C", str(directory), "ls-files", "-z", "--", "."],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        return None
    except subprocess.CalledProcessError as error:
        if "not a git repository" in error.stderr:
            return None
        raise GitError(error.stderr.strip() or f"git ls-files failed in {directory}") from error
    return [path for path in listed.stdout.split("\0") if path]


def present_files(directory: Path) -> list[str]:
    """Every file under directory, relative to it, skipping directories git never tracks."""
    return sorted(
        p.relative_to(directory).as_posix()
        for p in directory.rglob("*")
        if p.is_file() and not UNTRACKED_DIRS.intersection(p.relative_to(directory).parts)
    )


def check_requirements_dir(requirements_dir: Path) -> list[str]:
    """Problems in requirements/. An empty list means it is compliant."""
    if not requirements_dir.is_dir():
        return [f"{requirements_dir}/ does not exist"]

    prefix = requirements_dir.name
    files = tracked_files(requirements_dir)
    if files is None:
        files = present_files(requirements_dir)

    problems: list[str] = []
    features: set[str] = set()
    reported: set[str] = set()
    for rel in sorted(files):
        head, _, within = rel.partition("/")
        if not within:
            problems.append(f"{prefix}/{rel}: unexpected file directly under {prefix}/")
            continue
        if head == ".kb":
            if not KB_DOCUMENT.match(rel):
                problems.append(f"{prefix}/{rel}: not a knowledge-base document")
            continue
        if not FEATURE_DIR.match(head):
            if head not in reported:
                reported.add(head)
                problems.append(f"{prefix}/{head}/: not a feature directory, expected NNN-slug")
            continue
        features.add(head)
        if within not in ALLOWED_IN_FEATURE:
            problems.append(
                f"{prefix}/{rel}: Spec Kit working file. Only requirements.md is committed"
            )

    for feature in sorted(features):
        document = requirements_dir / feature / "requirements.md"
        if not document.is_file():
            problems.append(f"{prefix}/{feature}/requirements.md: missing")
        elif not REQUIREMENTS_HEADING.search(document.read_text(encoding="utf-8")):
            problems.append(f"{prefix}/{feature}/requirements.md: no '## Requirements' section")
    return problems


def check_no_spec_md(repo_root: Path) -> list[str]:
    """A tracked spec.md anywhere, or anything tracked under specs/."""
    files = tracked_files(repo_root)
    if files is None:
        files = present_files(repo_root)

    problems: list[str] = []
    for rel in sorted(files):
        parts = Path(rel).parts
        if parts[0] == "specs":
            problems.append(
                f"{rel}: tracked under specs/. Feature requirements are in requirements/"
            )
        elif parts[-1] == "spec.md":
            problems.append(
                f"{rel}: tracked spec.md. Rewrite it into requirements/NNN-slug/requirements.md"
            )
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--requirements-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "requirements",
        help="the directory of feature requirements (default: <repository>/requirements)",
    )
    args = parser.parse_args(argv)

    try:
        problems = check_requirements_dir(args.requirements_dir)
        problems += check_no_spec_md(args.requirements_dir.parent)
    except GitError as error:
        print(f"git could not list the tracked files: {error}", file=sys.stderr)
        return 2
    if problems:
        print(
            "requirements/ must hold one requirements.md per feature "
            "(requirements/.kb/requirements.md):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(f"{args.requirements_dir}: one requirements.md per feature")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
