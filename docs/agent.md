---
layout: default
title: Host agent
nav_order: 11
---

## Host agent

The host agent serves directories of one machine to the stack, read-only, over
a socket it opens itself. Every indexed tree is read this way: it needs no
compose file and no service recreated, and it can live on another host than the
stack. This page covers installing the agent, offering projects and moving
existing projects onto it.

- [Install](#install)
- [Update](#update)
- [Layouts](#layouts)
- [Onboard a project](#onboard-a-project)
- [Offer a project by hand](#offer-a-project-by-hand)
- [Move the existing projects](#move-the-existing-projects)
- [Unbind a project](#unbind-a-project)
- [Uninstall](#uninstall)

## Install

One command installs the binary, writes the config and starts the unit:

```bash
curl -fsSL https://raw.githubusercontent.com/oberon-systems/enggraph/main/scripts/install-agent.sh | bash -s -- --stack-url https://stack.example.com
```

From a checkout, run the script itself:

```bash
scripts/install-agent.sh --stack-url https://stack.example.com --from-source
```

The script asks for whatever a flag did not give, and `--help` lists its commands
and flags.

| Flag                        | What it does                                          |
| --------------------------- | ----------------------------------------------------- |
| `--stack-url URL`           | The gateway of the stack, with no path                |
| `--name NAME`               | What the operator sees, the host name by default      |
| `--labels A,B`              | Words the operator sees beside the name               |
| `--project NAME=PATH`       | A directory to offer; repeatable                      |
| `--layout xdg\|system`      | Where everything lands, see [Layouts](#layouts)       |
| `--systemd`, `--no-systemd` | Start the unit, or only write it                      |
| `--linger`, `--no-linger`   | Keep a user unit running without a login              |
| `--release X.Y.Z`           | The agent release to install, the newest by default   |
| `--from-source`             | Build the binary with `make agent` from this checkout |
| `--yes`                     | Never ask; a required value no flag gave is an error  |

Before it changes anything, the script checks that the host is Linux on `x86_64`
or `aarch64`, that systemd can run the unit, that the stack answers `/health`
and that every project path is a directory you can read. A release binary is
checked against the `SHA256SUMS` of its release. To run those checks alone, with
nothing changed:

```bash
scripts/install-agent.sh doctor --stack-url https://stack.example.com
```

After the start it prints the fingerprint and the address of the Agents page:

```text
install-agent: compare fingerprint 4e6a9904 on https://stack.example.com/agents and approve the agent there
```

While an agent waits, operators see a red `agents` lamp in the dashboard header.
It links to the Agents page, and its tooltip names the agent and the fingerprint.

A credential lasts 60 minutes and the agent renews it before that. One past
its end still renews itself for seven days (`AGENT_RENEWAL_GRACE_SECONDS` on
the stack), and opens nothing else meanwhile, so a host that slept overnight
or a stack that was down for a day needs no operator. Past that the agent
waits on the Agents page again, and its projects are not read until you
approve it.

## Update

```bash
scripts/install-agent.sh update
```

This installs the newest release. `--release X.Y.Z` names another one, and
`--from-source` builds it from this checkout. It asks nothing and keeps the
layout, the config, the credential and the linger. It restarts a unit in use and
leaves alone a unit you never started. It says what changed:

```text
install-agent: updated the agent: 0.1.0 -> 0.2.0
```

Without a checkout, pipe the script as you did to install it, ending in
`bash -s -- update`. `make install` does not update an agent it finds running.

## Layouts

| Layout   | Binary                        | Config               | State                     | Unit                               |
| -------- | ----------------------------- | -------------------- | ------------------------- | ---------------------------------- |
| `xdg`    | `~/.local/share/enggraph/bin` | `~/.config/enggraph` | `~/.local/state/enggraph` | User unit                          |
| `system` | `/opt/enggraph/agent/bin`     | `/etc/enggraph`      | `/var/lib/enggraph`       | System unit, runs as the installer |

`enggraph-agent` lands on your `PATH` through a link in `~/.local/bin` or
`/usr/local/bin`, and finds the config of either layout with no flag. The `xdg`
layout needs no `sudo`; the `system` layout uses it only for what lands outside
your home. The unit reads the config and writes only its state directory.

A user unit is read with `systemctl --user status enggraph-agent` and
`journalctl --user -u enggraph-agent`; a system unit takes the same commands
without `--user`.

A user unit sandboxes itself through an unprivileged user namespace. Where the
host forbids that, the script says so before it installs anything: use
`--layout system`.

## Onboard a project

Run `make install` from this repository, and it does the rest:

```bash
make install AGENT_ROOT=/srv/alpha
```

The run installs the agent when it is missing, offers the directory and prints
the Agents page. It then waits for an operator, 15 minutes by default:

1. Approve the agent.
2. Approve the project.
3. Bind it, with **Bind to** an existing project of that path or **Bind as a new
   project**.

Once the project is bound, the run prints what the tree holds and writes the
agent files. Compose is not touched. A project still not bound in time stops the
run with nothing lost: bind it and run the same command again.

| Variable            | Default                  | Purpose                                      |
| ------------------- | ------------------------ | -------------------------------------------- |
| `STACK_URL`         | `http://localhost:$PORT` | The stack, when it is on another host        |
| `AGENT_LAYOUT`      | `xdg`                    | The layout of an agent installed by the run  |
| `AGENT_FROM_SOURCE` | empty                    | `1` builds the binary instead of downloading |
| `AGENT_WAIT`        | `900`                    | Seconds an operator gets to bind the project |
| `ALLOW_PLAIN_HTTP`  | empty                    | `1` allows plain http to a remote stack      |

The report and a `TYPE=` or `SOURCE=none` run call the stack with
`ENGGRAPH_TOKEN`, taken from the environment or from the `env` block of Claude's
`settings.json`. Storing a project needs the `write-records` scope. A report the
stack could not produce is said and does not stop the run.

## Offer a project by hand

```bash
enggraph-agent project add gamma /srv/gamma
```

A running agent announces it within 5 seconds, with no restart. It then waits on
the Agents page like any other. `enggraph-agent project list` shows where each
project stands, and what it is bound to:

```text
gamma /srv/gamma approved bound to gamma
```

## Move the existing projects

One command offers every project the stack reads from a directory this host
holds, each under its own name:

```bash
enggraph-agent project import
```

It asks for an operator token, or reads `ENGGRAPH_OPERATOR_TOKEN`. `--under PATH`
narrows it to the projects under a directory, and the agent holds the answer
to that directory too, whatever the stack lists. With no `--under` it asks
before taking every directory listed; `--yes` answers for it. The root of the
host, a home directory itself and anything holding the agent's state or
config are never offered. It says what became of each one:

```text
added alpha /srv/alpha
skipped beta /srv/beta: no such directory on this host
Approve the import at https://stack.example.com/agents
```

On the Agents page, press **Approve the import** beside the agent. It approves
every project on the path of an unbound project and binds it there. A project an
operator refused stays refused. A bound project is read through its agent from
that moment, and nothing is reindexed unless a file really changed.

## Unbind a project

**Unbind** on the Agents page leaves the project with nothing to read it. It
keeps its graph and its records and gets no index run until it is bound again.

## Uninstall

```bash
scripts/install-agent.sh uninstall
```

This removes the unit, the binary, the config and the state, the credential
included. It asks first, unless you pass `--yes`. It never turns linger off, and
mentions it only when the installer turned it on. The stack keeps the agent until
you press **Delete** on the Agents page, and the projects stay either way.
