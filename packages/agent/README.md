# enggraph-agent

The host agent of [enggraph](../../README.md). It runs on the machine that holds
a codebase and connects out to the stack, so the stack never reaches into the
host. For now it enrolls, keeps its credential fresh, announces its exports and
holds one signed socket. Serving files over that socket comes later: until then
projects are still read from their mounts.

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
4. The agent announces its exports and opens the socket. It pings every 20 s and
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

Every `agent-v<version>` tag publishes static `x86_64` and `aarch64` Linux
binaries with a `SHA256SUMS` to a GitHub release.

## Config

The file is [TOML](https://toml.io/). `--config` or `ENGGRAPH_AGENT_CONFIG` names it. Otherwise it
is `enggraph-agent/config.toml` under `$XDG_CONFIG_HOME` or `~/.config`, and
`/etc/enggraph-agent/config.toml` when neither is set.

| Key          | Required | What it is                                                 |
| ------------ | -------- | ---------------------------------------------------------- |
| `stack_url`  | yes      | The gateway, with no path: `https://stack.example.com`     |
| `name`       | yes      | What the operator sees                                     |
| `labels`     | no       | Up to 16 short words                                       |
| `state_dir`  | yes      | Absolute; holds the credential in a file only you can read |
| `[[export]]` | no       | `name` and `path`, an absolute directory; up to 256        |

Use `https` for any stack beyond this machine: a claim answer carries the
credential's key, and the agent warns about plain `http`. Paths are made
canonical when the file loads.

A running agent reads the file every 5 s and announces changed exports. Changes
to anything else wait for a restart. A file that stops parsing is reported once,
and the agent keeps the exports it had.

## Commands

| Command                               | What it does                                      |
| ------------------------------------- | ------------------------------------------------- |
| `enggraph-agent run`                  | Enrolls and holds the socket until stopped        |
| `enggraph-agent export add NAME PATH` | Offers a directory                                |
| `enggraph-agent export remove NAME`   | Stops offering a directory                        |
| `enggraph-agent export list`          | Shows the exports and where they stand            |
| `enggraph-agent status`               | Shows the enrollment, and any waiting fingerprint |

`export add` and `export remove` rewrite the config file in one step, and the
rewrite drops any comments in it. `status` and `export list` read local files
and show approval as the stack last reported it, at most about 5 minutes old
while the agent runs.

The agent logs to stderr in a format journald can store. `RUST_LOG` sets how
much, `info` by default.

## Example

Write the config:

```toml
stack_url = "https://stack.example.com"
name = "alpha"
labels = ["beta"]
state_dir = "/var/lib/enggraph-agent"

[[export]]
name = "gamma"
path = "/srv/gamma"
```

Start the agent:

```bash
enggraph-agent --config /etc/enggraph-agent/config.toml run
```

It logs what to compare on the Agents page:

```text
INFO enggraph_agent::libs::enroll: waiting for an operator fingerprint=4e6a9904 approve_at=https://stack.example.com/agents
```

Once approved, approve the export too and check it from the host:

```bash
enggraph-agent --config /etc/enggraph-agent/config.toml status
```

```text
agent alpha 11111111-2222-4333-8444-555555555555
stack https://stack.example.com
state enrolled, credential good for 59 min
gamma /srv/gamma approved
```
