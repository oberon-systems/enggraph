# Security Policy

enggraph holds the code graph, the plans and the memory of the codebases it
indexes, and its host agent reads those codebases on their machines. A flaw in
either can expose source code or let someone write records they should not.
This page says which releases get fixes and how to report a problem privately.

## Supported versions

The stack and the host agent are released apart, and each gets fixes on its
newest release only. A fix ships as a new release, never as a patch to an old
one.

| Component      | Tags                   | Supported                |
| -------------- | ---------------------- | ------------------------ |
| Stack (images) | `vX.Y.Z`               | The newest release       |
| Host agent     | `enggraph-agent-X.Y.Z` | The newest release       |
| Either of them | Any older tag          | No: update to the newest |

The [deployment guide](docs/deployment.md#upgrading) says how to update the
stack, and the [host agent guide](docs/agent.md#update) how to update the agent.

## Reporting a vulnerability

Report it privately through GitHub, never in a public issue, pull request or
discussion:

1. Open the repository's **Security** tab.
2. Press **Report a vulnerability**.
3. Describe the problem, the version or commit it was found on, and the steps
   that reproduce it. Say what an attacker gains: what they read, write or run.

The report stays visible to you and the maintainers alone. The fix is worked
out in that advisory, and the advisory is published once a fixed release is
out. Say in the report how you want to be credited, if at all.

## Scope

In scope is anything that breaks one of the guarantees the stack makes:

- A request through the gateway is served without the token, session or scope
  [authentication](docs/deployment.md#authentication) requires for it, or a
  token reads a project outside the projects it names.
- A host agent serves a path outside the directories it offers, writes to the
  tree it serves, or is used before an operator approved it.
- A container reaches a codebase other than through its host agent, or a
  secret, token or file text is stored or logged where it should not be.
- `scripts/install-agent.sh` installs a binary its release `SHA256SUMS` does
  not match.

Out of scope are a stack run with `AUTH_ENFORCE=off` on a network others reach,
an attacker who already controls the host or the database, and the disposable
stacks of `dev/` and `docker-compose.eval.yaml`.
