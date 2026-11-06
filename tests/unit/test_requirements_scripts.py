# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.
"""Tests for scripts/check_requirements.py and scripts/adopt_requirements.py."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import adopt_requirements
import check_requirements

REQUIREMENTS = (
    "# Requirements: Example\n\n## Requirements\n\n- **FR-001**: THE SYSTEM SHALL work.\n"
)


def git_repo(root: Path, files: dict[str, str], *, force: bool = False) -> Path:
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    add = ["git", "-C", str(root), "add"] + (["-f"] if force else []) + list(files)
    subprocess.run(add, check=True)
    return root


def problems(root: Path) -> list[str]:
    requirements_dir = root / "requirements"
    return check_requirements.check_requirements_dir(
        requirements_dir
    ) + check_requirements.check_no_spec_md(root)


def test_compliant_tree_passes(tmp_path: Path):
    root = git_repo(
        tmp_path,
        {
            "requirements/.kb/requirements.md": "# Preface\n",
            "requirements/001-example/requirements.md": REQUIREMENTS,
        },
    )
    assert problems(root) == []


def test_untracked_spec_kit_output_is_ignored(tmp_path: Path):
    root = git_repo(tmp_path, {"requirements/001-example/requirements.md": REQUIREMENTS})
    (root / "specs/002-next").mkdir(parents=True)
    (root / "specs/002-next/spec.md").write_text("draft")
    (root / "requirements/001-example/plan.md").write_text("plan")
    assert problems(root) == []


@pytest.mark.parametrize(
    "files, expected",
    [
        ({"requirements/001-example/plan.md": "x"}, "Spec Kit working file"),
        ({"requirements/README.md": "x"}, "unexpected file directly under"),
        ({"requirements/example/requirements.md": REQUIREMENTS}, "not a feature directory"),
        ({"requirements/001-example/requirements.md": "# Requirements\n"}, "no '## Requirements'"),
        ({"requirements/.kb/Notes.txt": "x"}, "not a knowledge-base document"),
        ({"specs/001-example/plan.md": "x"}, "tracked under specs/"),
        ({"docs/spec.md": "x"}, "tracked spec.md"),
    ],
)
def test_violations_are_reported(tmp_path: Path, files: dict[str, str], expected: str):
    files = {"requirements/.kb/requirements.md": "# Preface\n", **files}
    root = git_repo(tmp_path, files, force=True)
    found = problems(root)
    assert any(expected in problem for problem in found), found


def test_feature_without_requirements_md_is_reported(tmp_path: Path):
    root = git_repo(tmp_path, {"requirements/001-example/plan.md": "x"}, force=True)
    assert "requirements/001-example/requirements.md: missing" in problems(root)


def test_adopt_moves_spec_md(tmp_path: Path):
    (tmp_path / "specs/003-new-thing").mkdir(parents=True)
    (tmp_path / "specs/003-new-thing/spec.md").write_text("draft")
    target = adopt_requirements.adopt(None, tmp_path)
    assert target == tmp_path / "requirements/003-new-thing/requirements.md"
    assert target.read_text() == "draft"
    assert not (tmp_path / "specs/003-new-thing/spec.md").exists()


def test_adopt_renumbers_a_taken_number(tmp_path: Path):
    (tmp_path / "requirements/001-first").mkdir(parents=True)
    (tmp_path / "requirements/002-second").mkdir(parents=True)
    (tmp_path / "specs/001-third").mkdir(parents=True)
    (tmp_path / "specs/001-third/spec.md").write_text("draft")
    target = adopt_requirements.adopt("specs/001-third/", tmp_path)
    assert target.parent.name == "003-third"


def test_adopt_refuses_a_taken_slug(tmp_path: Path):
    (tmp_path / "requirements/001-thing").mkdir(parents=True)
    (tmp_path / "specs/002-thing").mkdir(parents=True)
    (tmp_path / "specs/002-thing/spec.md").write_text("draft")
    with pytest.raises(adopt_requirements.AdoptError, match="already has a feature"):
        adopt_requirements.adopt("002-thing", tmp_path)


def test_adopt_needs_a_name_when_ambiguous(tmp_path: Path):
    for name in ("001-a", "002-b"):
        (tmp_path / "specs" / name).mkdir(parents=True)
        (tmp_path / "specs" / name / "spec.md").write_text("draft")
    with pytest.raises(adopt_requirements.AdoptError, match="001-a, 002-b"):
        adopt_requirements.adopt(None, tmp_path)
