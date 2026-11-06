# Preface

Language rules for everything written in and about this repository: `README.md`, `CONTRIBUTING.md`, `AGENTS.md` and `.kb/` files, requirements, code comments, commit messages, pull request titles and descriptions, review comments, and an agent's replies. How a pull request is divided and described is in `.kb/pull-requests.md`.

IMPORTANT: Read the `.kb/agents.md` file.


# Laws

## §1 - Plain words

Write the word that states the fact, not a figure of speech or jargon. Use an upstream system's term exactly as it defines it: Juju's `unit`, `relation`, `secret` and `action`, Pebble's `layer` and `check`, Hindsight's `bank`, `retain`, `recall` and `reflect`.

## §2 - Finding first

Open with the result or the change. Do not narrate the work that led to it, and do not frame it with a slogan.

## §3 - Once and finished

State a fact once, in a complete, grammatical sentence, in the present tense for present behavior. An intention is written as one.

## §4 - No semicolons or em dashes

Do not use semicolons, em dashes, or `--` standing in for one, in prose. Write two sentences, or use a colon, a comma or a list.

## §5 - Verified claims

Check a claim about behavior against the code before writing it. Report what was run, where, and what it showed, and state plainly what was not verified. Never write "all tests pass" without naming the suite and where it ran.

## §6 - No AI attribution

The human author owns every change. No commit trailer, pull request footer, heading or comment attributes the work to an AI assistant or a tool.


# Concepts

## Sentences and lists

- Delete words that add emphasis without a fact: "simply", "just", "really", "actually", "exactly", "entirely".
- Delete clauses that comment on a fact ("which is deliberate", "and that matters"). When the reason matters, write the reason.
- Use a list for steps, files, requirements, findings, and what was and was not verified.
- Write American English. An upstream term keeps its own spelling.

## Titles and commit subjects

- Name the change, then stop. The reason and the old behavior go in the body.
- One idea per title. "Instead of" and "rather than" phrases go in the body.
- Keep the summary after the Conventional Commits type under about 60 characters.

## Headings and links

- A heading is a noun phrase naming its content. Never a question or an instruction.
- A pull request description does not open with a "Summary" or "Description" heading.
- A link names its destination, never "here" or "this".

## Backticks

Put identifiers, commands, paths, config options, environment variables and values in backticks when the exact spelling matters.
