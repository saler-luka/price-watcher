# Documentation

**This repository is the only source of truth.** No Jira, no Confluence, no
issue tracker, no wiki. If it is not in git, it is not a decision.

## Read in this order

| # | File | What it is |
|---|---|---|
| 1 | `../HANDOFF.md` | Current state, what is verified, what to do next |
| 2 | `../README.md` | How to operate the working CLI today |
| 3 | `specs/` | Approved design decisions and their rationale |
| 4 | `plans/` | Implementation plans, task by task. Execute in phase order (0, then 1), not date order |
| 5 | `notes/` | Findings that are not a design decision |

## The one rule that prevents drift

**`HANDOFF.md` is the only file guaranteed to be current.** Everything else is
a dated snapshot of what was true and decided on that date.

This is deliberate. Keeping five documents in sync is a promise nobody keeps;
keeping one current is achievable. So when state changes, update `HANDOFF.md`.
When a *decision* changes, add a dated spec that supersedes the old one.

## Conventions

- **Dated filenames** (`YYYY-MM-DD-topic.md`) so there is never a question of
  which version you are reading.
- **Every doc opens with a status header** — Date, Status, and Supersedes where
  it replaces something.
- **Approved specs are append-only.** Correct factual errors in place, but a
  changed *decision* goes into a new dated spec that names what it supersedes.
  Editing an approved spec silently destroys the record of what was decided and
  when, which is the only thing making the document worth more than the code.
- **Executed plans move to `plans/done/`.** The checkbox state in the file is
  the record of what was built.

## Task tracking

The checkboxes in the current plan are the task tracker, and `HANDOFF.md`
carries the next action. There is deliberately no second system: splitting
status from rationale is how the two stop agreeing.

## Not a source of truth

An architecture page was published as a Claude artifact on 2026-09-09. It is an
archived snapshot for showing someone, it is **not maintained**, and it is
already out of date on two points (see `HANDOFF.md`). Do not consult it and do
not update it. The repo is authoritative.
