# Preface

How changes are divided into pull requests, how titles, descriptions and commits are written, and how review feedback is handled. Read this before opening, updating or reviewing a pull request. The language rules are in `.kb/writing-style.md`.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Stack shape

A Spec Kit feature is a linear stack. The bottom layer targets `main` and holds the requirements and documentation only (spec-kit.md§2). Each higher layer targets the layer below it and delivers one set of `FR-*` and `SC-*` with its code and tests.

## §2 - One change per pull request

A pull request makes one reviewable change. Unrelated cleanup goes in its own pull request.

## §3 - Conventional titles

Pull request titles and commit subjects use Conventional Commits: `feat:`, `fix:`, `ci:`, `docs:`, `test:`, `refactor:`, `chore:` and `chore(deps):`. The summary after the type is lowercase and imperative.

## §4 - Every finding answered

Every actionable review finding gets a fix with a reply naming the commit, or a reply that explains why the code is correct or the change is out of scope. Do not resolve a thread without one.

## §5 - Rebase, never merge, a stack

Update a stack by rebasing. Do not merge `main` or a parent branch into a layer.

## §6 - Requirements stay current

A pull request that changes behavior described by a `requirements.md` updates that document in the same pull request (requirements.md§3).


# Concepts

## Stacked pull requests

Use GitHub's `gh stack` extension (`gh extension install github/gh-stack`):

```bash
gh stack init feat/NNN-slug-requirements
# commit requirements/NNN-slug/requirements.md and the documentation
gh stack add feat/NNN-slug-<deliverable>
# implement and commit the first deliverable with its tests
gh stack push
gh stack submit
gh stack view
```

After a lower layer changes, `gh stack rebase` cascades the rebase and `gh stack push` force-pushes each branch with a lease. When review changes the requirements, amend the base layer and rebase the stack before continuing.

Do not create a layer that is only scaffolding, and do not defer tests to a later layer. Each layer is reviewable and verifiable on its own.

## Description

Start with one paragraph, without a heading, that says what changes for someone deploying or operating the charm and why. Then fill the sections of `.github/pull_request_template.md` that apply, and delete the rest:

- **Upgrade**: what an existing deployment has to do once.
- **Configuration**: config options, actions, secrets keys, relations and storage that are new, changed or removed.
- **Requirements**: each `requirements/*/requirements.md` added or changed.
- **Documentation**: each `README.md`, `CONTRIBUTING.md` and `.kb/` file added or changed.
- **Verification**: what was run, where, and what it showed, then what was not run and why.

Do not hard-wrap paragraphs in a pull request, issue or review body.

## Review feedback

Read every review, conversation comment and inline comment before changing a pull request:

```bash
gh pr view <number> --json reviews,comments
gh api --paginate repos/<owner>/<repo>/pulls/<number>/comments
```

In a stack, fix a defect in the lowest layer that introduced it, then rebase the layers above.

## Before requesting review

- `tox -e lint,unit` passes.
- `tox -e check-requirements` passes.
- Integration tests that cover the change were run on a local controller (testing.md§1), or the description says why not.
