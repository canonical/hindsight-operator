# Preface

How a feature is defined with Spec Kit and delivered as a stack of pull requests, and which Spec Kit files are generated. Read this before starting, planning or reviewing feature work.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Requirements only

The only Spec Kit output that is committed is a feature's requirements, rewritten into `requirements/NNN-slug/requirements.md`. Everything Spec Kit writes under `specs/` (`spec.md`, plans, research, data models, contracts, quickstarts, task lists, checklists) stays untracked. No pull request contains a `spec.md` or a file under `specs/`.

## §2 - Base pull request first

A feature starts with a base pull request against `main` that holds its `requirements.md` and the knowledge-base and user documentation for the whole feature, and no implementation. Implementation follows in dependent pull requests stacked on it (pull-requests.md§1).

## §3 - Review gates before code

Run specify, clarify, plan and tasks, and resolve every open question, before any implementation branch exists.

## §4 - No single-pass implement

`speckit-implement` executes every task on the current branch and is not stack-aware. Do not run it unchanged for a stacked feature. Run it per implementation layer, scoped to that layer's task IDs.

## §5 - Generated files are not edited

`.specify/` and `.claude/skills/speckit-*/` are Spec Kit output. Change them by re-running the Specify CLI, and commit the regenerated output on its own.

## §6 - When Spec Kit is optional

A bug fix or maintenance change with no new behavior needs no Spec Kit run. It still updates any `requirements.md` it contradicts (requirements.md§3).


# Concepts

## Installation

The repository was initialized with Specify CLI 0.12.15 using the `claude` integration, as recorded in `.specify/init-options.json` and `.specify/integration.json`. The commands are installed as Claude Code skills, invoked as `/speckit-specify`, `/speckit-clarify`, `/speckit-plan`, `/speckit-tasks`, `/speckit-analyze`, `/speckit-checklist`, `/speckit-implement`, `/speckit-converge`, `/speckit-constitution` and `/speckit-taskstoissues`.

Another harness follows the same workflow. Add its integration with `specify integration install <key>`, never by copying the Claude skills. Check the result with `specify integration status`: an integration that is not declared multi-install safe, such as `opencode` in 0.12.15, puts the project in an error state when installed alongside another one.

`.specify/memory/constitution.md` is the unfilled upstream template. The knowledge base states this project's rules. Fill the constitution only through `/speckit-constitution`, and only if the project decides to adopt one.

## Delivering a feature

1. Read the knowledge base and the existing `requirements/` that the feature touches.
2. Run `/speckit-specify` with the next free number under `requirements/`. Spec Kit numbers from `specs/`, which is empty in a fresh checkout.
3. Run `/speckit-clarify`, `/speckit-plan` and `/speckit-tasks`. `/speckit-analyze` checks the three for consistency.
4. Adopt the requirements and rewrite them into the format of `requirements/.kb/requirements.md`:

   ```bash
   tox -e adopt-requirements -- NNN-slug
   # rewrite requirements/NNN-slug/requirements.md
   git add requirements/NNN-slug/requirements.md
   tox -e check-requirements
   ```

5. Open the base pull request with the requirements, the knowledge-base changes, and the `README.md` and `CONTRIBUTING.md` changes for the whole feature.
6. Group the tasks into deliverables, each one an independently verifiable set of `FR-*` and `SC-*`. Map every `FR-*` and `SC-*` to exactly one deliverable.
7. Stack one branch per deliverable on the base, in dependency order, each with its code and tests (pull-requests.md§1).

A Spec Kit task is not a pull request boundary. Several tasks usually make one deliverable.

## Durable outcomes of a Spec Kit run

The plan and research notes are discarded with `specs/`. A constraint they establish that later work must respect goes into the relevant `.kb/*.md` file in the base pull request.
