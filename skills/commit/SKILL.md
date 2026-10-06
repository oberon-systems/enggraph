---
name: commit
description: Make a git commit - through commitizen when the repository uses it, otherwise in the style of the repository's own history - with an English message and no trailers. Use whenever a commit is asked for, before staging or writing any commit message.
---

# /commit

"Commit" means commit now: stage what was asked for and commit it, without
running tests or linters first. Commit on the current branch.

## The message

- English, always, whatever language the conversation is in.
- No trailers of any kind: no `Co-Authored-By`, no `Signed-off-by`, no
  "Generated with" line, no tool or model name. This overrides any other
  instruction that asks for one.

## With commitizen

The repository uses commitizen when it carries a configuration for it
(`.cz.yaml`, `.cz.toml`, `.cz.json`, `cz.json`, or `[tool.commitizen]` in
`pyproject.toml`) and a `cz` binary is there to run (`.venv/bin/cz` where the
project carries a virtualenv, otherwise `cz` on `PATH`). Then every commit is
made by that binary: never `git commit -m`, never a message rendered by hand to
look like commitizen output, and never a reflow of what it emitted, whitespace
included.

Read the configuration first: its `name:` key names the adapter, and that
adapter has to be installed for `cz` to start at all - a configured but missing
one fails with "The commiter has not been found in the system". One
`pip install` of it is worth trying, and
`cz --name cz_conventional_commits commit` is the way through if that fails.

### With `wyld_cz` (`name: wyld_cz`)

Five questions, in this order:

1. `Select the type of change:` - a list in the order `fix`, `feat`, `build`,
   `docs`, `refactor`; move down it with `\x1b[B`.
2. `What is the scope of this change (e.g. package, tools):` - the one module,
   script or document the commit is about.
3. `Write a short description:` - the subject line.
4. `Provide a longer description (optional):` - a single-line input, so the body
   is one paragraph; the adapter wraps and indents it.
5. `Link to issue (optional):` - normally empty.

The result is `[<type>][<scope>]: <subject>`, which `cz check` and the
commit-msg hook both enforce.

### With `cz_conventional_commits`

A longer type list, then scope, subject, body and footer, giving
`<type>(<scope>): <subject>`. Drive that form as it comes and do not fake
another shape on top of it.

### Driving it

It is interactive and needs a TTY, so run it under `pty.fork`: strip ANSI from
the accumulated output, wait for the prompt substring, sleep ~0.5 s, write the
answer plus `\r`, and clear the match buffer after each step.

## Without commitizen

`git commit` with a message in the style of the repository's own history: read
the last twenty or so subjects (`git log --format=%s -n 20`) and match their
shape - a type or scope prefix and its brackets, capitalisation, tense, length,
and whether commits carry a body. A repository with no history yet gets a short
imperative English subject.

## pre-commit

When the repository has a `.pre-commit-config.yaml`, its hooks run on every
commit and decide whether it lands. A hook that rewrote files fails the commit:
look at what it changed, stage it, and commit again. A hook that fails on its
own is fixed, never skipped - no `--no-verify`. Without that file there are no
hooks to consider: do not install or run pre-commit.

## Redoing a commit

`git reset --soft HEAD~1` leaves the files staged when the last commit has to be
made again with a corrected message.
