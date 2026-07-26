@AGENTS.md

# Trivela backend — Claude-specific notes

`AGENTS.md` above is the shared contract and binds every agent. This file
holds only what is specific to *running as Claude Code in this repo* and
appears nowhere in that file. Do not restate the invariants here.

## 1. You are the implementer

Per `AGENTS.md` §7 you own implementation; the reviewer owns the audit.
Two consequences that are easy to get wrong in practice:

- A review finding is **evidence to investigate, not a patch to apply**.
  Reproduce it yourself before changing anything, and say how you
  reproduced it.
- When you and the reviewer disagree, **put the disagreement in front of
  Son**. Do not quietly pick a side — the reviewer's independence is the
  only thing making this loop worth running.

## 2. Interpreter and environment

There is a committed virtualenv at `.venv/`. Use it explicitly; the
system `python3` does not have the dependencies.

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m alembic ...
```

Migrations live in `live_migrations/`, not `alembic/` — `alembic.ini`
points there. `alembic/` does not exist; looking for it and concluding
the project has no migrations is a mistake that has been made before.

## 3. Never pipe a gated command

```bash
.venv/bin/python -m pytest tests/ -q | tail -5     # WRONG before a gate
```

The pipe reports the exit status of `tail`, so a failing suite reads as
success and a gated push proceeds on a red build. When the result gates
an action, run the command unpiped and read its exit code. Use
`${pipestatus[1]}` (zsh) if you must pipe.

## 4. CI does not run on this branch

Both repos' `.github/workflows/ci.yml` trigger on `push` to `main` and on
`pull_request`. Pushing an implementation branch with no PR open runs
**no CI at all**. Do not report a green feature-branch push as validated
— local runs are the only signal until a PR exists.

## 5. What "verify" means here

`AGENTS.md` §9 lists expected counts. They are a claim with a date on
them, not a constant. Run the suite and report the number you actually
saw; if it differs from the documented figure, correct the document
rather than the report.

Anything you could not run gets an explicit status
(`SKIPPED_ENVIRONMENT`, `NOT_RUN`) with the reason. A silent omission
reads as a pass.

## 6. Frontend work happens in the other repo

`~/dev/TRIVELA/frontend` is a **separate repository** with its own
`AGENTS.md` and its own base commit. It also serves Son's personal site.
Changes there need their own branch, their own commit and their own
handoff block — never fold them into a backend commit.
