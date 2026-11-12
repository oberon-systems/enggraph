# enggraph-agent

The host agent of [enggraph](../../README.md). It runs on the machine that holds
a codebase and connects out to the stack, so the stack never reaches into the
host. It enrolls, keeps its credential fresh, announces its projects, holds one
signed socket and answers read-only file operations over it. A project is read
through its agent once an operator bound it on the Agents page.

- [How it works](#how-it-works)
- [Build](#build)
- [Config](#config)
- [Commands](#commands)
- [Example](#example)

## How it works

1. The agent registers and gets a one-time claim code. It logs the first 8 hex
   digits of the code's sha256 and never the code itself.
2. An operator compares that fingerprint on the dashboard's Agents page and
   approves the agent. The agent polls its claim every 10 s and gets a credential
   signed under the code.
3. Every later request is signed. A credential lasts an hour and is replaced 15
   minutes before it ends.
4. The agent announces its projects and opens the socket. It pings every 20 s and
   dials again after a lost socket, waiting 1 s at first and up to 60 s.

| The stack answers | What the agent does                             |
| ----------------- | ----------------------------------------------- |
| `401`             | Drops the credential and registers under its id |
| `429`, `503`      | Keeps the credential and retries                |
| Close on a socket | Announces again, which says if it was refused   |

The stack disables an agent that signs with a generation already replaced, and
only an operator undoes that. So one task makes every signed call, in turn, and
a second agent on the same `state_dir` refuses to start. Never restore an old
`state.json` into a running agent's directory.

A credential that lapses, because the stack was down past its hour or the host
was off, is refused. The agent then registers again and waits for an operator.

## What it serves

The stack asks for `walk`, `stat`, `read`, `head`, `grep` and `fetch`, each scoped
to one project, and hears `changed` and `resync` from the watch. There is no
operation that writes. Anything else is refused, as is a project it does not
offer.

| Guard        | What it does                                                                          |
| ------------ | ------------------------------------------------------------------------------------- |
| Confinement  | Opens with `openat2(RESOLVE_BENEATH)`, so `..`, absolute paths and links leaving fail |
| Denied names | `.env`, keys and the like are never read, through a link included, nor ever listed    |
| Size limit   | The stack's `MAX_FILE_BYTES`, sent in its hello; past it a file is refused            |
| Concurrency  | At most 64 operations at once and 120000 a minute; past either it answers `busy`      |

Without `openat2`, on an old kernel or in a sandbox that filters it, a path is
walked one component at a time, each opened below the one before and never
through a link out, so a directory swapped for a link is not followed.

The watch holds one inotify watch per directory. A directory it cannot read is
skipped and logged, and the rest of the tree stays watched. It asks for a full
pass with `resync` after each connect and after an overflow.

## Build

You need [Rust](https://rustup.rs/) 1.98.1 with rustfmt and clippy, the
`<arch>-unknown-linux-musl` target and `musl-gcc`. The binary is static and lands
in `packages/agent/target/<arch>-unknown-linux-musl/release/enggraph-agent`.

```bash
make agent
```

Run the crate's tests and checks the same way:

```bash
make agent-test
make agent-fmt
make agent-clippy
```

The agent is versioned apart from the stack. `cz bump` in `packages/agent`
writes its `CHANGELOG.md` from the commits touching it and tags
`enggraph-agent-<version>`. The tag publishes static `x86_64` and `aarch64`
Linux binaries with a `SHA256SUMS` to a GitHub release, and the crate to
crates.io.

## Config

The tool writes the file, so you do not have to:

```bash
enggraph-agent init --stack-url https://stack.example.com
enggraph-agent project add gamma /srv/gamma
```

To install the binary and a systemd unit as well, use
[`scripts/install-agent.sh`](../../docs/agent.md).

`init` takes the host name as the agent's name and picks the state directory:
`enggraph` under `$XDG_STATE_HOME` or `~/.local/state`, and
`/var/lib/enggraph` for the system config. `--name` and `--state-dir`
replace them, and `--labels` adds words the operator sees. It never replaces a
config that exists.

The file is [TOML](https://toml.io/). `--config` or `ENGGRAPH_AGENT_CONFIG` names it. Otherwise it
is `enggraph/config.toml` under `$XDG_CONFIG_HOME` or `~/.config`. Without
that file it is `/etc/enggraph/config.toml`, so `project add` finds the
config of a system unit with no flag.

| Key           | Required | What it is                                                 |
| ------------- | -------- | ---------------------------------------------------------- |
| `stack_url`   | yes      | The gateway, with no path: `https://stack.example.com`     |
| `name`        | yes      | What the operator sees                                     |
| `labels`      | no       | Up to 16 short words                                       |
| `state_dir`   | yes      | Absolute; holds the credential in a file only you can read |
| `[[project]]` | no       | `name` and `path`, an absolute directory; up to 256        |

Use `https` for any stack beyond this machine: a claim answer carries the
credential's key. Plain `http` to another machine is refused unless the file
says `allow_plain_http = true` (`init --allow-plain-http` writes it), and the
agent then warns at every start. Paths are made canonical when the file loads.

A running agent reads the file every 5 s and announces changed projects. Changes
to anything else wait for a restart. A file that stops parsing is reported once,
and the agent keeps the projects it had.

## Commands

| Command                                | What it does                                       |
| -------------------------------------- | -------------------------------------------------- |
| `enggraph-agent init --stack-url URL`  | Writes the config file                             |
| `enggraph-agent run`                   | Enrolls and holds the socket until stopped         |
| `enggraph-agent project add NAME PATH` | Offers a directory                                 |
| `enggraph-agent project remove NAME`   | Stops offering a directory                         |
| `enggraph-agent project list`          | Shows the projects and where they stand            |
| `enggraph-agent project import`        | Offers the projects the stack reads from this host |
| `enggraph-agent status`                | Shows the enrollment, and any waiting fingerprint  |

`project add` and `project remove` rewrite the config file in one step, keeping its
owner and mode, and the rewrite drops any comments in it. A running agent picks
the change up within 5 s, with no restart, and the project then waits for an
operator on the Agents page. `status` and `project list` read local files
and show approval and binding as the stack last reported them. That is at most
about 15 seconds old while a project waits for a decision, and about 5 minutes
once every project is bound or refused.

`project import` asks for an operator token, or reads `ENGGRAPH_OPERATOR_TOKEN`,
and `--under PATH` narrows it: a listed directory outside it is skipped. With
no `--under` it asks first, unless `--yes` is given. The root of the host, a
home directory itself and a directory holding the agent's state or config are
never offered. It says of each project whether it was added or why it was
skipped.

The agent logs to stderr in a format journald can store. `RUST_LOG` sets how
much, `info` by default.

## Example

Write the config and offer a directory:

```bash
enggraph-agent init --stack-url https://stack.example.com --name alpha
enggraph-agent project add gamma /srv/gamma
```

Start the agent:

```bash
enggraph-agent run
```

It logs what to compare on the Agents page:

```text
INFO enggraph_agent::libs::enroll: waiting for an operator fingerprint=4e6a9904 approve_at=https://stack.example.com/agents
```

Once approved, approve the project too and check it from the host:

```bash
enggraph-agent status
```

```text
agent alpha 11111111-2222-4333-8444-555555555555
stack https://stack.example.com
state enrolled, credential good for 59 min
gamma /srv/gamma approved
```
