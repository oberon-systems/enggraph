"""What a tree provides to other projects and takes from them."""

from __future__ import annotations

import pytest

from enggraph.indexer.projectlinks import (
    Export,
    Import,
    collect,
    configured_inventories,
    embedded_compose_images,
    host_definitions,
    image_exports,
    inventory_hosts,
    inventory_probes,
    manifest_candidates,
    normalize,
    placeholder_imports,
    puppet_links,
    read_manifest,
    role_exports,
    unique_exports,
)

PACKAGE_JSON = '{"name": "alpha-api", "dependencies": {"beta-sdk": "^1.0.0"}}'
VCPKG_JSON = '{"name": "alpha-core", "dependencies": ["fmt", {"name": "beta-net"}]}'
PYPROJECT = """[project]
name = "Alpha_Worker"
dependencies = ["beta-client>=1.2", "requests[socks] ; python_version > '3.8'"]

[project.optional-dependencies]
dev = ["pytest"]
"""
POETRY = """[tool.poetry]
name = "alpha-tool"

[tool.poetry.dependencies]
python = "^3.11"
beta_client = "^1.0"
"""
SETUP_CFG = """[metadata]
name = alpha.legacy

[options]
install_requires =
    beta-client
    six>=1.0
"""
REQUIREMENTS = """# pinned
beta-client==1.2.0
-r other.txt
git+https://example.com/alpha/gamma.git
Gamma.Lib  # trailing
"""
CARGO = """[package]
name = "alpha-engine"

[dependencies]
serde = "1"
beta_net = { version = "0.3", package = "beta-net" }

[dev-dependencies]
criterion = "0.5"
"""
GO_MOD = """module example.com/alpha/service

go 1.22

require example.com/beta/client v1.4.0

require (
	example.com/gamma/log v0.2.1
	example.com/delta/trace v1.0.0 // indirect
)
"""
CMAKE = """cmake_minimum_required(VERSION 3.20)
project(AlphaCore VERSION 1.0 LANGUAGES CXX)
find_package(BetaNet REQUIRED)
  FIND_PACKAGE(fmt CONFIG)
"""


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("image", "alpha-worker:latest", "alpha-worker"),
        ("image", "example.com:5000/alpha/worker:1.2", "example.com:5000/alpha/worker"),
        ("image", "docker.io/library/postgres:16@sha256:abc", "postgres"),
        ("image", "$WORKER_IMAGE", ""),
        ("pypi", "Alpha_Worker>=1.0", "alpha-worker"),
        ("pypi", "beta.client[extra] ; python_version > '3'", "beta-client"),
        ("role", "../roles/alpha", "alpha"),
        ("npm", "@alpha/api", "@alpha/api"),
        ("go", "  ", ""),
    ],
)
def test_normalize(kind: str, raw: str, expected: str) -> None:
    """Every kind is matched on the name other projects write."""
    assert normalize(kind, raw) == expected


def test_manifest_candidates_cover_the_root_and_every_directory() -> None:
    """The root and each directory holding a file are probed, nothing else."""
    candidates = manifest_candidates(["src/app/main.py", "README.md"])
    assert "go.mod" in candidates
    assert "src/app/Cargo.toml" in candidates
    assert "src/Cargo.toml" not in candidates


def test_json_manifest_names_the_directory_it_sits_in() -> None:
    """A package is the directory its manifest sits in."""
    exports, imports = read_manifest("web/package.json", PACKAGE_JSON, set())
    assert exports == [Export("npm", "alpha-api", "web/")]
    # The parser already wrote the dependencies as placeholder edges.
    assert imports == []


def test_vcpkg_reads_both_dependency_forms() -> None:
    """A dependency is a bare name or an object naming one."""
    exports, imports = read_manifest("vcpkg.json", VCPKG_JSON, {"vcpkg.json"})
    assert exports == [Export("vcpkg", "alpha-core", "./")]
    assert imports == [
        Import("vcpkg", "fmt", "vcpkg.json", "depends_on"),
        Import("vcpkg", "beta-net", "vcpkg.json", "depends_on"),
    ]


def test_pyproject_reads_pep621_and_poetry() -> None:
    """Both pyproject layouts declare a name and dependencies."""
    exports, imports = read_manifest("worker/pyproject.toml", PYPROJECT, set())
    assert exports == [Export("pypi", "alpha-worker", "worker/")]
    assert [one.name for one in imports] == ["beta-client", "requests", "pytest"]
    # Not indexed, so the directory takes the dependency.
    assert {one.source_id for one in imports} == {"worker/"}

    exports, imports = read_manifest("pyproject.toml", POETRY, set())
    assert exports == [Export("pypi", "alpha-tool", "./")]
    assert [one.name for one in imports] == ["beta-client"]


def test_setup_cfg_and_requirements() -> None:
    """The older Python manifests are read too."""
    exports, imports = read_manifest("setup.cfg", SETUP_CFG, set())
    assert exports == [Export("pypi", "alpha-legacy", "./")]
    assert [one.name for one in imports] == ["beta-client", "six"]

    exports, imports = read_manifest("requirements.txt", REQUIREMENTS, set())
    assert exports == []
    assert [one.name for one in imports] == ["beta-client", "gamma-lib"]


def test_cargo_follows_a_renamed_dependency() -> None:
    """A renamed crate is taken under the name it is published as."""
    exports, imports = read_manifest("Cargo.toml", CARGO, set())
    assert exports == [Export("cargo", "alpha-engine", "./")]
    assert [one.name for one in imports] == ["serde", "beta-net", "criterion"]


def test_go_mod_skips_indirect_requirements() -> None:
    """Only what the module requires itself is taken."""
    exports, imports = read_manifest("go.mod", GO_MOD, set())
    assert exports == [Export("go", "example.com/alpha/service", "./")]
    assert [one.name for one in imports] == [
        "example.com/beta/client",
        "example.com/gamma/log",
    ]


def test_cmake_reads_project_and_find_package() -> None:
    """CMake provides a project and takes a package."""
    exports, imports = read_manifest("core/CMakeLists.txt", CMAKE, set())
    assert exports == [Export("cmake", "AlphaCore", "core/")]
    assert [one.name for one in imports] == ["BetaNet", "fmt"]


def test_a_broken_manifest_declares_nothing() -> None:
    """A file that does not parse yields nothing rather than failing."""
    assert read_manifest("package.json", "{not json", set()) == ([], [])
    assert read_manifest("Cargo.toml", "[package", set()) == ([], [])
    assert read_manifest("README.md", "# alpha", set()) == ([], [])


def test_roles_are_found_under_a_roles_directory_only() -> None:
    """A role is a directory under one named roles."""
    exports = role_exports(
        [
            "roles/alpha/tasks/main.yml",
            "roles/alpha/meta/main.yml",
            "deploy/roles/beta/meta/main.yaml",
            "tasks/main.yml",
            "playbooks/gamma/tasks/main.yml",
        ]
    )
    assert set(exports) == {
        Export("role", "alpha", "roles/alpha/"),
        Export("role", "beta", "deploy/roles/beta/"),
    }


def test_a_built_image_is_provided_by_its_build_directory() -> None:
    """An image is provided by the directory it is built from."""
    rows = [
        (
            "image:alpha-worker:latest",
            "worker/Dockerfile",
            "compose.yml::service.worker",
        ),
        ("image:alpha-api:1.0", "Dockerfile", "deploy/compose.yml::service.api"),
    ]
    assert image_exports(rows, {"worker/Dockerfile"}) == [
        Export("image", "alpha-worker", "worker/"),
        Export("image", "alpha-api", "deploy/"),
    ]


def test_placeholder_edges_become_imports() -> None:
    """Placeholders of a known kind are taken, the rest are not."""
    rows = [
        ("compose.yml::service.api", "image:beta-worker:2", "uses_image"),
        ("site.yml", "role:beta", "uses_role"),
        ("package.json", "npm:beta-sdk", "depends_on"),
        ("src/main.py", "os.path", "imports"),
        ("compose.yml::service.db", "image:${DB_IMAGE}", "uses_image"),
    ]
    assert placeholder_imports(rows) == [
        Import("image", "beta-worker", "compose.yml::service.api", "uses_image"),
        Import("role", "beta", "site.yml", "uses_role"),
        Import("npm", "beta-sdk", "package.json", "depends_on"),
    ]


def test_collect_keeps_the_shallowest_export_of_a_name() -> None:
    """A name declared twice in one tree is provided once."""
    manifests = {
        "packages/api/package.json": '{"name": "alpha-api"}',
        "package.json": '{"name": "alpha-api"}',
    }
    exports, imports = collect(
        ["package.json", "packages/api/package.json"],
        manifests,
        [("package.json", "npm:beta-sdk", "depends_on")] * 2,
        [],
    )
    assert exports == [Export("npm", "alpha-api", "./")]
    assert imports == [Import("npm", "beta-sdk", "package.json", "depends_on")]


CIRCUIT = """instances:
  Web-01.Example.com.:
    flavor: small
  db-01.example.com: {}
"""


@pytest.mark.parametrize(
    ("kind", "raw", "expected"),
    [
        ("host", "Web-01.Example.com.", "web-01.example.com"),
        (
            "tfmodule",
            "git::https://example.com/alpha/infra.git//modules/vpc?ref=v1",
            "example.com/alpha/infra//modules/vpc",
        ),
        ("tfmodule", "git@example.com:alpha/infra.git", "example.com/alpha/infra"),
        ("tfmodule", "alpha/network/openstack", "alpha/network/openstack"),
    ],
)
def test_normalize_hosts_and_module_sources(kind: str, raw: str, expected: str) -> None:
    """A host and a module source are matched on their bare form."""
    assert normalize(kind, raw) == expected


def test_a_circuit_config_provides_its_instances() -> None:
    """Only a config beside a Terraform file is read, for its instance keys."""
    paths = ["live/web/main.tf", "live/web/config.yaml", "other/config.yaml"]
    assert "live/web/config.yaml" in manifest_candidates(paths)
    assert "other/config.yaml" not in manifest_candidates(paths)
    exports, imports = read_manifest("live/web/config.yaml", CIRCUIT, set())
    assert exports == [
        Export("host", "web-01.example.com", "live/web/config.yaml"),
        Export("host", "db-01.example.com", "live/web/config.yaml"),
    ]
    assert imports == []


def test_workspace_and_module_placeholders_become_imports() -> None:
    """A role, module or remote module the tree lacks is taken from outside."""
    rows = [
        ("data/nodes/a.yaml", "deploy-role:web", "has_role"),
        ("data/roles/web.yaml", "deploy-module:nginx", "includes_module"),
        ("main.tf", "tfmodule:git::https://example.com/alpha/infra.git", "uses_module"),
    ]
    assert placeholder_imports(rows) == [
        Import("deploy-role", "web", "data/nodes/a.yaml", "has_role"),
        Import("deploy-module", "nginx", "data/roles/web.yaml", "includes_module"),
        Import("tfmodule", "example.com/alpha/infra", "main.tf", "uses_module"),
    ]


SPEC = """Name:           alpha-tools
Version:        1.0
Requires:       beta-lib >= 2.1, iproute
Requires(post): /usr/bin/systemctl
Requires:       kmod(alpha.ko) python3

%package devel
Summary: headers

%package -n alpha-dkms
Summary: module
"""
MAKEFILE = """R2_BUCKET   ?= Repo
OTHER := $(R2_BUCKET)/rpm
"""


def test_a_spec_provides_its_packages_and_takes_its_requirements() -> None:
    """Name and subpackages are provided; plain Requires are taken."""
    exports, imports = read_manifest("packages/rpm/alpha/alpha.spec", SPEC, set())
    assert [one.name for one in exports] == [
        "alpha-tools",
        "alpha-tools-devel",
        "alpha-dkms",
    ]
    assert {one.node_id for one in exports} == {"packages/rpm/alpha/"}
    assert [one.name for one in imports] == ["beta-lib", "iproute", "python3"]


def test_a_makefile_names_the_bucket_it_writes_to() -> None:
    """A variable named for a bucket is taken as one."""
    exports, imports = read_manifest("Makefile", MAKEFILE, {"Makefile"})
    assert exports == []
    assert imports == [Import("bucket", "repo", "Makefile", "uses_bucket")]


def test_a_circuit_config_provides_its_buckets() -> None:
    """A circuit creates buckets the way it creates instances."""
    exports, _ = read_manifest(
        "storage/config.yaml", "buckets:\n  repo:\n    location: WEUR\n", set()
    )
    assert exports == [Export("bucket", "repo", "storage/config.yaml")]


DOCKER_MAKEFILE = """NAME := example.com/tools/alpha-keeper

VERSION ?= $(shell grep '^version = ' ../pyproject.toml | cut -d'"' -f2)
TAG ?= $(shell git rev-parse --short HEAD)

build:
\t@docker build -t $(NAME):$(TAG) \\
\t\t-f Dockerfile ../

push:
\t@docker push $(NAME):$(TAG)
"""


def test_a_makefile_provides_the_image_it_builds() -> None:
    """The tag is computed at run time, the repository is not."""
    exports, imports = read_manifest(
        "tools/keeper/docker/Makefile", DOCKER_MAKEFILE, set()
    )
    assert exports == [
        Export("image", "example.com/tools/alpha-keeper", "tools/keeper/")
    ]
    assert imports == []


def test_a_package_marker_provides_its_package() -> None:
    """A build marker or an nfpm config names the package a directory makes."""
    exports, _ = read_manifest(
        "tools/keeper/.package.yaml", "name: alpha-keeper\n", set()
    )
    assert exports == [Export("package", "alpha-keeper", "tools/keeper/")]
    exports, _ = read_manifest("nfpm.yaml", "name: alpha-agent\narch: amd64\n", set())
    assert exports == [Export("package", "alpha-agent", "./")]


def test_any_yaml_beside_a_circuit_provides_its_instances() -> None:
    """The file is named as the tree likes; the instances key is what counts."""
    paths = ["live/eu/ec2/terragrunt.hcl", "live/eu/ec2/compute.yaml"]
    manifests = {"live/eu/ec2/compute.yaml": CIRCUIT}
    exports, _ = collect(paths, manifests, [], [])
    assert {(one.kind, one.name) for one in exports} == {
        ("host", "web-01.example.com"),
        ("host", "db-01.example.com"),
    }


def test_a_fixture_provides_nothing() -> None:
    """A test tree naming a real package must not claim it; the shallowest wins."""
    exports = [
        Export("npm", "alpha-api", "tests/fixtures/app/"),
        Export("bucket", "repo", "eval/corpus/alpha/infra/"),
        Export("image", "example.com/alpha/web", "examples/web/"),
        Export("npm", "alpha-api", "packages/api/"),
        Export("npm", "alpha-api", "./"),
    ]
    assert unique_exports(exports) == [Export("npm", "alpha-api", "./")]


def test_a_file_keyed_by_its_own_host_name_defines_that_host() -> None:
    """A per-machine definition provides the host; a node's settings do not.

    An address or an object keyed by a dotted name that is no host name is no
    host either: a host is taken only by its host name.
    """
    paths = [
        "vm/web-01.example.com.yaml",
        "data/node/db-01.example.com.yaml",
        "hosts/192.0.2.10.yaml",
        "inventory/web-01.alpha_db.yaml",
    ]
    manifests = {
        "vm/web-01.example.com.yaml": "web-01.example.com:\n  cpu: 2\n",
        "data/node/db-01.example.com.yaml": "role: db\n",
        "hosts/192.0.2.10.yaml": "192.0.2.10:\n  name: web\n",
        "inventory/web-01.alpha_db.yaml": "web-01.alpha_db:\n  kind: db\n",
    }
    assert set(host_definitions(paths)) == set(paths)
    exports, _ = collect(paths, manifests, [], [])
    assert exports == [
        Export("host", "web-01.example.com", "vm/web-01.example.com.yaml")
    ]


SHELL_BUILD = """#!/usr/bin/env bash
set -e
LOCAL_IMAGE=alpha_tx
REPO_IMAGE="example.com/alpha/tx"
VERSION_TAG=$(git describe --tags)
CONTEXT="${CONTEXT_DIR:-..}"

docker buildx build \\
    --load \\
    --tag=$LOCAL_IMAGE \\
    -- $CONTEXT
docker tag -- $LOCAL_IMAGE "${REPO_IMAGE}:$VERSION_TAG"
docker image push -- "${REPO_IMAGE}:$VERSION_TAG"
"""


def test_a_shell_script_provides_the_image_it_retags_and_pushes() -> None:
    """The local name a retag starts from is dropped, the registry name kept."""
    exports, imports = read_manifest("scripts/tx/build", SHELL_BUILD, set())
    assert exports == [Export("image", "example.com/alpha/tx", "scripts/")]
    assert imports == []
    assert read_manifest("scripts/tx/notes", "docker build -t a/b .\n", set()) == (
        [],
        [],
    )


def test_a_makefile_may_name_its_container_tool_in_a_variable() -> None:
    """`$(DOCKER) build` is a docker build; a bare local name is not provided.

    The value of an option such as `--build-context` is not the build context.
    """
    makefile = (
        "DOCKER ?= docker\nIMAGE ?= example.com/alpha/panel\n"
        "build:\n\t$(DOCKER) build --build-context skills=../skills"
        " -t $(IMAGE):$(VERSION) -t local .\n"
    )
    exports, _ = read_manifest("web/Makefile", makefile, set())
    assert exports == [Export("image", "example.com/alpha/panel", "web/")]


TERRAFORM = """resource "hcloud_server" "web" {
  name        = "web-01.example.com"
  server_type = "cx22"
  labels = {
    name = "db-01.example.com"
  }
}

resource "aws_instance" "db" {
  ami  = "ami-1"
  tags = {
    Name = "db-02.example.com"
  }
}

resource "aws_route53_record" "www" {
  zone_id = "Z1"
  name    = "www.example.com"
  type    = "CNAME"
  records = ["web-01.example.com.", "${var.other}"]
  alias {
    name = "lb.example.com"
  }
}

resource "aws_route53_record" "bare" {
  name    = "api"
  records = ["192.0.2.10"]
}
"""


def test_terraform_creates_machines_and_names_hosts_in_dns() -> None:
    """Only literal fully qualified names count, at the resource's own level."""
    exports, imports = read_manifest("live/web/main.tf", TERRAFORM, set())
    assert exports == [
        Export("host", "web-01.example.com", "live/web/main.tf"),
        Export("host", "db-02.example.com", "live/web/main.tf"),
    ]
    assert imports == [
        Import("host", "www.example.com", "live/web/main.tf", "uses_host"),
        Import("host", "web-01.example.com", "live/web/main.tf", "uses_host"),
    ]


INI_INVENTORY = """# production
web-01.example.com
[db]
db[01:03].example.com ansible_port=2222
192.0.2.7
[db:vars]
backup.example.com=yes
[all:children]
db
"""
YAML_INVENTORY = """all:
  hosts:
    web-01.example.com:
  children:
    cache:
      hosts:
        cache-a.example.com: {ansible_host: 192.0.2.5}
      vars:
        proxy.example.com: 1
"""


def test_an_inventory_uses_its_hosts_in_either_format() -> None:
    """Ranges expand, variables, groups and addresses are not hosts."""
    names = [one.name for one in inventory_hosts("inventory/prod", INI_INVENTORY)]
    assert names == [
        "web-01.example.com",
        "db01.example.com",
        "db02.example.com",
        "db03.example.com",
    ]
    imports = inventory_hosts("hosts.yml", YAML_INVENTORY)
    assert {one.name for one in imports} == {
        "web-01.example.com",
        "cache-a.example.com",
    }
    assert {one.relation for one in imports} == {"uses_host"}


def test_inventories_are_looked_for_beside_every_directory() -> None:
    """Named files, inventory directories and what an ansible.cfg points at."""
    files, folders = inventory_probes(["ops/site.yml"])
    assert "hosts" in files and "ops/inventory.ini" in files
    assert "ops/inventories" in folders
    config = "[defaults]\ninventory = ./envs/prod, /etc/ansible/hosts\n"
    assert configured_inventories("ops/ansible.cfg", config) == ["ops/envs/prod"]


def test_a_compose_document_held_in_data_names_its_images() -> None:
    """A string that parses as a compose file is read as one."""
    data = (
        "app:\n  content: |\n    services:\n      web:\n"
        "        image: example.com/alpha/web:1\n  note: services are fine\n"
    )
    assert embedded_compose_images(data) == ["example.com/alpha/web:1"]
    manifest = "class alpha {\n}\n"
    _, imports = puppet_links(
        {
            "modules/alpha/manifests/init.pp": manifest,
            "data/role/app.yaml": "classes: [alpha]\n" + data,
        }
    )
    taken = Import("image", "example.com/alpha/web", "data/role/app.yaml", "uses_image")
    assert taken in imports
