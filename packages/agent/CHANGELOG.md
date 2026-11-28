## enggraph-agent-0.1.1 (2026-11-28)

### Build

- **deps**: Bump the agent group across 1 directory with 2 updates

## enggraph-agent-0.1.0 (2026-11-16)

### Breaking Changes

- **trees**: make mounts, scripts/mounts.sh, INSTALL_MODE=mounts and the /code and /project mounts are removed, along with CODE_ROOT and SCAN_PATH. A project with no agent bound is no longer read or indexed. The worker API replies to POST /projects and to a rename no longer carry mounts, and its settings answer says readable instead of mounted. Before upgrading, install the agent on every host holding a tree, run enggraph-agent project import, and press Approve the import on the Agents page. Then delete the generated docker-compose.override.yaml, which make up refuses to start with, and run make build and make up. See docs/breaking-changes.md.

### Features

- **agent**: project import offers what the stack reads from this host
- **agent**: a bound project is read through its host agent
- **agent**: a static host agent enrolls, holds a signed socket and announces its exports

### Bug Fixes

- **agent**: a home, the state directory and a second name for a file are never served
- **agent**: a swapped path reads nothing outside and a denied file is not even named
- **agent**: hold what the stack sends to what an agent can take
- **agent**: refuse plain http to another machine and hold an import to --under
- **agent**: a credential refresh keeps the watch, so it owes no resync

### Refactor

- **trees**: trees are read through host agents, the mounts are gone

### Build

- **agent**: the crate names its repository
- **deps**: Bump the agent group in /packages/agent with 3 updates
- **agent**: the agent is released on its own, from cz bump in packages/agent
