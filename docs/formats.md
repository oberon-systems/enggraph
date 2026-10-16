---
layout: default
title: Supported Formats
nav_order: 3
---

## Supported Formats

The system uses both an upstream parser (`graphifyy`) and custom Tree-sitter
parsers to extract structural information.

- **Languages** (via this repo's own code extractor): Python, TypeScript
  (`.ts`, `.tsx`), JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`), Go, PHP, Rust,
  Java, Kotlin and C#. A changed file
  is re-read alone; imports, calls and base classes are resolved from what the
  database holds, for the changed files and the files importing them. An
  import resolves to any indexed file, whichever extractor reads it.
  TypeScript interfaces are nodes, and a capitalised JSX element counts as a
  call of its component. `paths` aliases of `tsconfig.json` are not resolved,
  and CommonJS exports are no symbols. Go (`.go`): a package is a directory,
  and its files see each other's symbols without an import; an import resolves
  through the `go.mod` files above the tree's Go files to every non-test file
  of the package, and a call qualified by an import alias is looked up in that
  package alone. A method declared away from its receiver type still gets the
  `method` edge from it, and an embedded type is a base. `vendor/`, `go.work`
  and `replace` directives are not read. PHP (`.php`): class and function
  names are qualified by the file's `namespace` and `use` clauses, and resolve
  through what every PHP file of the tree declares; of several declarations
  the one in an included or imported file wins, then the one nearest by path.
  An unqualified function falls back to the global one. An include resolves
  relative to the file, then by its path tail nearest to the includer; after
  a variable or a constant only the literal tail is read. Calls made at file
  level belong to the file. Traits and enums are classes, and `use` of a
  trait is a base. `.inc` files, the configured `include_path`, `use function`
  and calls on a variable typed elsewhere are not resolved. Rust (`.rs`):
  structs, enums, unions and type aliases are classes, traits are interfaces,
  and a method hangs off the type its `impl` names, in whatever file that type
  is declared; trait methods belong to the trait. Functions of an inline `mod`
  belong to the file. `use` and `mod` paths resolve through the module tree
  (`crate`, `self`, `super`, `a.rs` beside `a/`, `mod.rs`) and through the
  crates the `Cargo.toml` files above the Rust files declare; a re-export is
  followed up to four steps. `Type::f()` reaches the type's method wherever
  its `impl` is, and a method called on a value is matched by name among the
  methods of the file and of what it imports. A trait implemented for a type
  of the same file is a base. Macros, `#[path]`, `cfg` and renamed
  dependencies are not read. Java (`.java`): enums and records are classes,
  annotation types are interfaces, and a nested type belongs to the type
  around it; members of anonymous and local classes are no symbols. A type
  name resolves through the file, its single-type imports, its package and
  its on-demand imports, else as written in full; `pkg.*` imports every file
  of the package. A call on a variable, a field or a record component is
  looked up in the type it was declared with and that type's bases; a bare
  call in the caller's class, its bases and the classes around it, then in
  what a static import brings in. Overloads of one type are one target, the
  first declared. Generic and lambda types, the return type of a chained
  call and fields inherited from a base are not inferred. Kotlin (`.kt`):
  objects, enums, data and sealed classes are classes, interfaces are
  interfaces, a nested type belongs to the type around it, and the members
  of a companion object are methods of its class; members of object
  literals and local functions are no symbols. Kotlin and Java share one
  package space, so either resolves the other. Names resolve as in Java,
  an import alias included; an import may also name a top-level function
  or a member. A bare call is looked up in the caller's class, its bases
  and the classes around it, then as a class (a constructor call), then as
  a top-level function of the file, its imports and its package. A call on
  a parameter, a property or a value created by `Type()` is looked up in
  that type; one no type answers, or one on a value of unknown type, may
  reach an extension function. `.kts` scripts, smart casts, scope functions
  and the type of `it` are not read. C# (`.cs`): structs, records and enums
  are classes, a nested type belongs to the type around it, and constructors
  are methods; properties, operators, local functions and lambdas are no
  symbols, their calls belong to the type or method holding them, and
  top-level statements' to the file. Namespaces are no nodes. A type name
  resolves through the file, `using` aliases, the file's namespaces and every
  namespace enclosing them, then `using` directives, else as written in full;
  a `using` of a namespace imports every file of it. The declarations of a
  partial type in one directory are one type. Calls resolve as in Java, with
  `using static` in place of a static import; a call no type answers may
  reach an extension method. `.csx`, `.cshtml` and `.razor` files, project
  files and `ProjectReference` are not read.
- **Languages** (via `graphifyy`, until each moves to the own extractor): Ruby,
  C, C++, Scala.
- **Infrastructure and markup** (via this repo's own parsers): Ansible, Docker
  Compose, Puppet, Terraform/HCL, Dockerfiles, Makefiles, shell scripts, YAML,
  JSON, HTML, PHP templates, RPM specs, pip requirements, `setup.cfg`, systemd
  units.

## Controlling what gets indexed

Every file some parser or the graphifyy extractor reads is indexed, plus
`.sql` as an unparsed file node. A script without an extension is indexed
when its shebang names an interpreter a parser reads (`sh`, `bash`, `zsh`,
`python`, `node`, `ruby`), and it is parsed as that language. Files above 1 MB
are always skipped.

The formats an index run finds are recorded on the project and listed on its
_settings_ tab: extensions (`.py`), names a parser answers to (`makefile`,
`dockerfile.*`) and scripts by interpreter (`#!bash`). Every run adds what it
found and never removes anything; the _Update formats_ button does the same
without indexing.

### Ignore

What is left out is one gitignore-style document per level, and the levels
are summed:

| Level        | Scope                                             |
| ------------ | ------------------------------------------------- |
| global       | every project, edited under _Settings_ in the nav |
| organization | every project the organization holds              |
| project      | one project, edited on its _settings_ tab         |

```text
fixtures/
*.generated.go
```

The global default ships with four groups: secrets (`.env`, `.env.*`, keys
and certificates, `*.jwt`, `*.tfvars`, `*.tfstate`, sops and Ansible vault
files, `.ssh/`, `.aws/`, `.kube/` and the like), agent state (`.claude/`,
`.gemini/`), lock and generated files, and build output the built-in skip list
(`.git`, `.venv`, `node_modules`, `dist`, `target`, ...) does not cover. The
secrets group is also enforced in code, so removing a line of it here changes
nothing. The project's settings tab shows the lines it inherits above its own.

The indexing schedule uses the same three levels - see
[Indexing on a schedule](usage.md#indexing-on-a-schedule). It is stored in
the `settings` column of the same rows, so a knob added later is a key and
not a migration.

## Ansible

YAML that looks like Ansible is read as Ansible: plays, tasks, handlers and
role variables become nodes, and the references between them become edges.

| Edge            | Source                                          |
| --------------- | ----------------------------------------------- |
| `includes`      | `include_tasks`, `import_tasks`                 |
| `uses_role`     | `roles:`, `include_role`, `import_role`         |
| `depends_on`    | `dependencies:` in `meta/main.yml`              |
| `reads_vars`    | `include_vars`, `vars_files:`                   |
| `uses_template` | `template: src:`                                |
| `uses_file`     | `copy: src:`                                    |
| `notifies`      | `notify:`, resolved to the handler that answers |

Targets resolve inside the owning role, so `template: src: sshd_config.j2`
finds `roles/sshd/templates/sshd_config.j2`. A `notify:` that no handler
answers stays in the graph as an unresolved external node - usually a typo
worth seeing. YAML that is neither Ansible nor compose (CI configs, plain
data) falls back to plain top-level keys.

## Docker Compose

`compose.yaml`, `docker-compose.yaml` and the suffixed variants
(`docker-compose.prod.yml`, `compose.override.yaml`) are read as an
architecture. Any other YAML holding a top-level `services:` mapping of
service definitions is recognised by its shape, so a `stack.yml` is read the
same way.

Each declaration becomes a node named by its kind, the way an HCL block is -
a service and a volume may share a name, and the kind is what keeps them
apart:

| Compose             | Node               |
| ------------------- | ------------------ |
| `name: enggraph`    | `stack.enggraph`   |
| `services.postgres` | `service.postgres` |
| `volumes.graph-out` | `volume.graph-out` |
| `networks.base`     | `network.base`     |
| `configs.site`      | `config.site`      |
| `secrets.token`     | `secret.token`     |

`x-` extension fields declare nothing and get no node.

Every edge below leaves the **service** rather than the file, so
`get_code_graph_neighbors` on one service answers what it runs, waits for and
reads:

| Edge           | Source                                                 |
| -------------- | ------------------------------------------------------ |
| `depends_on`   | `depends_on:`, in list and `condition:` form; `links:` |
| `uses_image`   | `image:`, as `image:<ref>`                             |
| `builds`       | `build:`, resolved to the Dockerfile it names          |
| `uses_volume`  | a named volume in `volumes:`                           |
| `mounts`       | a bind mount in `volumes:`                             |
| `uses_network` | `networks:`                                            |
| `uses_config`  | `configs:`                                             |
| `uses_secret`  | `secrets:`                                             |
| `reads_vars`   | `env_file:`                                            |
| `extends`      | `extends: service:` within the same file               |

Top-level `include:`, `extends: file:` and the `file:` of a config or a secret
leave the compose file itself, as `includes` and `uses_file`.

Three things are deliberately dropped rather than turned into a node pointing
at nothing: any value still holding `${...}` (only compose can expand it), a
bind mount whose host side is absolute or names a directory rather than a
file, and a `build:` context that is a git URL. An image, by contrast, always
gets its external node - `image:` prefixed so `postgres` can never bind to an
unrelated symbol of that name.

## Puppet

Classes, defined types, node definitions and functions become nodes under
their full name (`class profile::web` is one node regardless of which file
declares it). Resource declarations become nodes too, named by type and
title:

| Manifest               | Node            |
| ---------------------- | --------------- |
| `package { 'nginx': }` | `package.nginx` |
| `service { 'nginx': }` | `service.nginx` |

| Edge            | Source                                         |
| --------------- | ---------------------------------------------- |
| `inherits`      | `inherits` on a class                          |
| `includes`      | `include`                                      |
| `requires`      | `require`, both the statement and `require =>` |
| `requires`      | the `->` and `~>` ordering chains              |
| `notifies`      | `notify =>`                                    |
| `uses_template` | `template(...)` and `epp(...)`                 |

Templates are read too: the code inside `<% %>` is reassembled in source
order and parsed as Ruby (`.erb`) or Puppet (`.epp`), so `<%= @port %>` puts
`@port` in the graph next to the manifest that renders the file. Ruby files
themselves go through the upstream extractor (classes, methods, call graph),
but report no `require` edges between files.

## HTML

`.html` and `.htm` are read as a page: what it declares, and what it pulls in.

| Markup                          | Node                  |
| ------------------------------- | --------------------- |
| `<title>Dashboard</title>`      | `title.Dashboard`     |
| `<div id="root">`               | `anchor.root`         |
| `<my-widget>`                   | `element.my-widget`   |
| `.card { }` in `<style>`        | `style..card`         |
| `function boot()` in `<script>` | `boot`, as a function |

A standard tag gets no node - only a hyphenated one, which is what tells a
custom element from the structure around it. The `<script>` and `<style>`
bodies are joined in source order and handed to the JavaScript and CSS
grammars, so an inline script contributes the same kind of nodes a `.js` file
would.

| Reference                                   | Edge          |
| ------------------------------------------- | ------------- |
| `<script src>`                              | `uses_script` |
| `<link href>`                               | `uses_style`  |
| `<img src>`, `<source src>`, `<iframe src>` | `uses_file`   |
| `<form action>`                             | `uses_file`   |

A reference is tried next to the page first and then from the project root, so
both `js/app.js` and `/js/app.js` reach the same file, and a `?v=2` or `#top`
tail is trimmed before the lookup. Anything naming something other than a file
of this project is dropped rather than turned into an external node: another
origin (`https://cdn...`, `//cdn...`), a scheme of its own (`data:`,
`mailto:`), a bare `#fragment`, and a value still holding `{{ }}` or `${ }`.

A call an inline script makes becomes an edge only when the same page declares
what it calls. A call into a `.js` file cannot: those files belong to the
upstream extractor, whose symbols this pass never sees, so the edge would be a
placeholder rather than a link.

## PHP

`.php` goes to the upstream extractor, which reads classes, functions,
methods, `use` declarations and the call graph between them.

`.phtml` templates cannot: the extractor routes on the `.php` suffix alone. So
they get a parser here instead, reading the `<?php ?>` islands for classes,
interfaces, traits, functions and methods, with the same rule as an inline
script - a call or an `extends` becomes an edge only when the template declares
the target itself. What does reach another file is `include` and `require`,
which become `includes` edges resolved the way a page's assets are. The markup
around the islands is opaque to this parser.

## JSON

Every top-level key becomes a node; nothing below it does, on purpose - a
second level taken indiscriminately turns one data file into hundreds of
nodes named `type` or `url`.

Manifests that carry known structure are read one level deeper by file name:
`scripts` in `package.json` becomes `script` nodes, `mcpServers` in
`.mcp.json` becomes `mcp_server` nodes, and dependency sections become
`depends_on` edges (`npm:express`, `composer:monolog/monolog` - namespaced by
ecosystem so they can't collide with an unrelated key of the same name).

Lock files (`package-lock.json`, `yarn.lock`, `composer.lock`,
`npm-shrinkwrap.json`) are skipped by default - they're generated and say
nothing the manifest next to them doesn't.

## RPM specs

A `.spec` file is read line by line, since no Tree-sitter grammar for it is
published. `%define` and `%global` macros, `Name`, `Version` and `Release`
are expanded; a value still holding an unknown macro is dropped.

| Spec               | Node                  |
| ------------------ | --------------------- |
| `Name: alpha`      | `package.alpha`       |
| `%package devel`   | `package.alpha-devel` |
| `%package -n beta` | `package.beta`        |

| Edge               | Source                                               |
| ------------------ | ---------------------------------------------------- |
| `depends_on`       | `Requires:`, as `package:<name>`                     |
| `build_depends_on` | `BuildRequires:`, as `package:<name>`                |
| `uses_file`        | a `Source` or `Patch` that is not a URL, by its name |

File dependencies (`/usr/bin/python3`) and virtual ones
(`python3dist(alpha)`) name no package and are skipped.

## Python manifests

pip requirements files are read through the
[tree-sitter-requirements](https://github.com/tree-sitter-grammars/tree-sitter-requirements)
grammar: `requirements*.txt`, `constraints*.txt`, the same names with `.in`,
and any `.txt` or `.in` under a `requirements/` directory. `setup.cfg` is read
with configparser.

| Edge         | Source                                                      |
| ------------ | ----------------------------------------------------------- |
| `depends_on` | a requirement line; `install_requires`, `setup_requires`,   |
|              | `tests_require` and `options.extras_require` in `setup.cfg` |
| `includes`   | `-r`, `-c` and their long forms; a `file:` directive        |

A dependency is a `pypi:` placeholder under its PEP 503 name, so `Alpha_Lib`
and `alpha-lib` are one node. `setup.cfg` also gives each section a node
(`section.metadata`) and each console or GUI script one
(`entry_point.alpha`).

## systemd units

`.service`, `.socket`, `.timer`, `.target`, `.path`, `.mount`,
`.automount`, `.slice` and `.swap` files are read line by line. A unit file is
one node, `unit.<file name>`, and every edge leaves it for `unit.<name>`: a
unit the tree holds resolves to its file, a stock one such as
`multi-user.target` stays external.

| Edge                       | Source                                        |
| -------------------------- | --------------------------------------------- |
| `requires`                 | `Requires=`, `BindsTo=`, `Requisite=`,        |
|                            | `PartOf=`                                     |
| `wants`                    | `Wants=`, `Upholds=`                          |
| `after`, `before`          | `After=`, `Before=`                           |
| `conflicts`                | `Conflicts=`                                  |
| `wanted_by`, `required_by` | `WantedBy=`, `RequiredBy=` in `[Install]`     |
| `activates`                | `Unit=` of a timer, socket or path unit, else |
|                            | the service of the same name                  |
| `reads_vars`               | a relative `EnvironmentFile=`                 |

An instance names its template, so `alpha@one.service` is
`unit.alpha@.service`. A name still holding a specifier (`%i.target`) and an
absolute `EnvironmentFile=`, which lies on the host rather than in the tree,
are dropped.

## Nuances

- Files larger than 1 MB are always skipped.
- Entity and import edges are only extracted for languages a parser
  understands. Indexing a Markdown file never mines its prose for the word
  "import".
