# Preface

The format and lifecycle of `requirements/NNN-slug/requirements.md`, the committed requirements of each feature: the `FR-*` and `SC-*` statements that code and tests cite, and the user stories that motivate them. Read this before writing or changing a requirements document, or before opening the base pull request of a feature.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - One file per feature

`requirements/` holds one `NNN-slug/` directory per feature, and each holds `requirements.md` and nothing else. `NNN` is three digits and `slug` is lowercase kebab case. The only other tracked files are this `.kb/` directory's documents.

## §2 - Rewritten before review

Spec Kit's `specs/NNN-slug/spec.md` is moved to `requirements/NNN-slug/requirements.md` and rewritten into the format below before the base pull request is opened.

## §3 - Current, not historical

A requirements document states what the charm does now. A pull request that changes behavior, a term or a name that a `requirements.md` describes updates it in the same pull request, even when the change needed no requirements of its own. Search `requirements/` for the old wording before submitting.

## §4 - Stable identifiers

Never renumber or reuse an `FR-*`, `SC-*` or feature number. Code and tests cite them. Retire a requirement by marking it withdrawn in place. A requirement whose behavior was removed may be deleted with it. Git history is the archive.

## §5 - What, not how

Requirements state behavior and measurable outcomes. How the charm implements them belongs in a `.kb/*.md` file.

## §6 - CI checks the tree

`scripts/check_requirements.py` fails the `conventions` workflow when a feature directory tracks anything besides `requirements.md`, a feature has no `requirements.md`, a `requirements.md` has no `## Requirements` section, or a `spec.md` or any file under `specs/` is tracked. Run it with `tox -e check-requirements`. To allow another file, change §1 first.


# Concepts

## Naming

The directory is `requirements/` and the file `requirements.md` because at Canonical a "spec" is an RFC-like document reviewed within and between teams. What Spec Kit writes is requirements and user stories.

## Format

One compact document per feature, opening with `# Requirements: <feature name>`:

- `## Why`: optional. A few sentences on the problem the feature solves.
- `## Stories`: one line per user story, `- **US-1 (P1)**: <who> needs <what>, so <why>. Motivates FR-001, FR-002.` Name the `FR-*` a story motivates when the mapping is unambiguous. No narrative journeys and no acceptance scripts.
- `## Requirements`: one EARS sentence per line, `- **FR-0NN**: WHEN <trigger> THE SYSTEM SHALL <response>.` The other EARS forms are `WHILE <state>`, `IF <condition> THEN`, `WHERE <feature is included>`, and the unconditioned `THE SYSTEM SHALL <response>`. A list of values a requirement needs may follow it as indented bullets.
- `## Key Entities`: optional. One line per entity the feature introduces.
- `## Success Criteria`: one measurable outcome per line, `- **SC-0NN**: ...`, with a number and a unit.
- `## Edge Cases`: optional. One line per boundary condition that qualifies a requirement.
- `## Assumptions`: optional. One line per observed fact about Juju, Kubernetes, PostgreSQL or Hindsight that the requirements depend on.
- `## Out of Scope`: optional. What the feature leaves for later, with what that later feature would need.

"The system" is the charm and the workload it manages. The writing style in `.kb/writing-style.md` applies.

## Adoption

`scripts/adopt_requirements.py` moves `specs/NNN-slug/spec.md` to `requirements/NNN-slug/requirements.md`. Spec Kit numbers from `specs/`, which is empty in a fresh checkout, so when `NNN` is taken the script renumbers the feature to the next free number. The rewrite and the `git add` are left to the author.

```bash
tox -e adopt-requirements -- NNN-slug
git add requirements/NNN-slug/requirements.md
tox -e check-requirements
```
