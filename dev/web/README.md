# Dashboard design tool

A disposable [Penpot](https://penpot.app) stack for drawing dashboard mockups.
The stack itself is
[penpot-local-stack](https://github.com/oberon-systems/penpot-local-stack),
pinned in the root `Makefile` and installed by `make init`, so this directory
holds only the mockups and the settings. Mockups live in `templates/` as
unpacked Penpot exports, JSON plus SVG, so git shows readable diffs and an
agent can read them directly.

- [Install](#install)
- [Design loop](#design-loop)
- [Commands](#commands)
- [Settings](#settings)
- [Templates](#templates)
- [Connect Penpot MCP](#connect-penpot-mcp)

## Install

`make init` installs the package into `.venv`. A virtualenv created before
the pin was added gets it with:

```bash
.venv/bin/pip install 'penpot-local-stack==0.1.0'
```

The package needs Python 3.12 or newer and Docker with the compose plugin.

## Design loop

Start the stack and pick a template or `(empty)`:

```bash
make -C dev/web up
```

`up` creates a throwaway profile and opens the browser already logged in
through the printed `/autologin` link. If that session is lost, log in as
`designer@example.com` with password `enggraph-design`.

Draw in the browser at `http://localhost:9001`. When you are done, pick the
file to export and the template to write it to, then let the stack go down:

```bash
make -C dev/web down
```

`down` writes the export into `templates/<name>/` and runs
`docker compose down -v`, which drops the database and the assets. Pick
`(skip export)` to throw the work away; the wipe is confirmed first whenever
the stack still holds a file.

## Commands

| Target                    | What it does                                |
| ------------------------- | ------------------------------------------- |
| `make -C dev/web up`      | Start the stack, import a template, open it |
| `make -C dev/web down`    | Offer the export, confirm the wipe, stop it |
| `make -C dev/web import`  | Import a template into the running stack    |
| `make -C dev/web export`  | Export a file into `templates/`             |
| `make -C dev/web extract` | Unpack a `.penpot` file into `templates/`   |
| `make -C dev/web convert` | Pack a template into a `.penpot` file       |

`import` and `export` work against a running stack, so a template can be
swapped in or a file saved off without ending the session. Both ask before
they touch anything.

`extract` and `convert` are the offline pair and need no stack at all.
`extract` reads a `.penpot` file from `dev/web/` under the same rules as
`export`; `convert` goes the other way and writes `<name>.penpot`, which is
what you hand to a Penpot that is not this one. Packed `.penpot` files are
ignored by git.

## Settings

`.penpot.yaml` sits next to the `Makefile` and every command reads it:

```yaml
templates: templates
port: 9001
project: enggraph-design
version: 2.17.2
password: enggraph-design
```

Drop a key to fall back to the package default. Every key also answers to an
environment variable with a `PENPOT_` prefix, and the variable wins over the
file:

```bash
PENPOT_PORT=9100 make -C dev/web up
```

## Templates

- One directory per template: `manifest.json`, `files/` and `objects/`.
- Images and icons must be SVG. An export that holds raster images is refused
  and the offending files are listed, so you can replace them and export
  again.
- Frame thumbnails are raster renders Penpot rebuilds on its own, so the
  export leaves them out.
- Import keeps the Penpot file id, so a round trip changes only what you
  drew.
- Text in a mockup must be ASCII: the `ascii-only` hook refuses the commit
  otherwise.

## Connect Penpot MCP

`up` enables MCP for the throwaway profile, issues its key and serves it
behind the fixed `/mcp/claude` URL. The repository `.mcp.json` already names
it as the `penpot` server, so Claude picks it up on the next start. After
later `up` runs, reconnect `penpot` from `/mcp` instead of restarting. While
the stack is down, the server just shows as failed.

The MCP plugin runs inside the Penpot browser tab. Keep a file open in the
workspace while the agent works; a hidden or unloaded tab stops MCP.
