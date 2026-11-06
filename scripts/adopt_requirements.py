#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Move Spec Kit's specs/NNN-slug/spec.md into requirements/ under a free feature number.

Spec Kit numbers a feature from specs/, which is empty in a fresh checkout, so
its number can already belong to a feature under requirements/. This moves the
working file to requirements/MMM-slug/requirements.md, where MMM is the
working file's number when it is free and the next free number otherwise. The
rewrite into the format of requirements/.kb/requirements.md and the `git add`
are left to the author.

It uses only the standard library.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

FEATURE_DIR = re.compile(r"^(\d{3})-([a-z0-9][a-z0-9-]*)$")
REPOSITORY = Path(__file__).resolve().parents[1]


class AdoptError(Exception):
    """The working file cannot be moved, with the reason in plain words."""


def working_files(specs_dir: Path) -> list[str]:
    """The feature directories under specs/ that hold a spec.md."""
    if not specs_dir.is_dir():
        return []
    return sorted(
        d.name
        for d in specs_dir.iterdir()
        if FEATURE_DIR.match(d.name) and (d / "spec.md").is_file()
    )


def destination(feature: str, requirements_dir: Path) -> str:
    """The feature's directory name under requirements/, renumbered when its number is taken."""
    number, slug = FEATURE_DIR.match(feature).groups()
    existing = (
        [d.name for d in requirements_dir.iterdir() if FEATURE_DIR.match(d.name)]
        if requirements_dir.is_dir()
        else []
    )
    if any(FEATURE_DIR.match(name).group(2) == slug for name in existing):
        raise AdoptError(f"requirements/ already has a feature named {slug}")
    taken = {int(name[:3]) for name in existing}
    if int(number) not in taken:
        return feature
    return f"{max(taken) + 1:03d}-{slug}"


def adopt(feature: str | None, repo_root: Path) -> Path:
    """Move the working file and return the path of its requirements.md."""
    specs_dir = repo_root / "specs"
    requirements_dir = repo_root / "requirements"
    found = working_files(specs_dir)
    if feature is None:
        if len(found) != 1:
            listed = ", ".join(found) if found else "none"
            raise AdoptError(
                f"name the feature to adopt. Spec Kit features under specs/: {listed}"
            )
        feature = found[0]
    feature = feature.removeprefix("specs/").rstrip("/")
    if not FEATURE_DIR.match(feature):
        raise AdoptError(f"{feature} is not a feature directory, expected NNN-slug")
    if feature not in found:
        raise AdoptError(f"specs/{feature}/spec.md does not exist")

    target = requirements_dir / destination(feature, requirements_dir) / "requirements.md"
    target.parent.mkdir(parents=True)
    (specs_dir / feature / "spec.md").rename(target)
    return target


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "feature", nargs="?", help="the NNN-slug directory under specs/ (default: the only one)"
    )
    parser.add_argument("--repo", type=Path, default=REPOSITORY, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        target = adopt(args.feature, args.repo)
    except AdoptError as error:
        print(f"adopt-requirements: {error}", file=sys.stderr)
        return 1
    rel = target.relative_to(args.repo).as_posix()
    print(f"Moved to {rel}.")
    print("Rewrite it into the format requirements/.kb/requirements.md defines, then run:")
    print(f"  git add {rel}")
    print("  tox -e check-requirements")
    return 0


if __name__ == "__main__":
    sys.exit(main())
