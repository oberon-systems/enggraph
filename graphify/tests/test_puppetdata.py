"""A Puppet class followed out into the data applying and configuring it."""

from __future__ import annotations

from enggraph.projectlinks import Import, puppet_links
from enggraph.puppetdata import DataEdge, follow

APP = """class alpha_app (
  String $version,
  String $image_name,
  Array[String] $tools = [],
){
  docker::run { 'alpha-app':
    image     => $image_name,
    image_tag => $version,
  }
  package { $tools: ensure => present }
}
"""
CONTENTS = {
    "site/modules/alpha_app/manifests/init.pp": APP,
    "data/role/app.yaml": (
        "classes:\n  - alpha_app\n  - unknown_class\n"
        "alpha_app::image_name: example.com/tools/alpha-app\n"
        "alpha_app::tools: [alpha-cli]\n"
    ),
    "data/node/web-01.example.com.yaml": "role: app\n",
    "data/node/web-02.example.com.yaml": "role: other\n",
    "data/node/db-01.example.com.yaml": "alpha_app::version: '2'\n",
    "data/common.yaml": "ntp::servers: [192.0.2.1]\n",
}
MANIFEST = "site/modules/alpha_app/manifests/init.pp"


def test_the_class_is_followed_into_the_data_wherever_it_lives() -> None:
    """Classes applied, parameters set and the node selecting them are edges."""
    edges, _ = follow(CONTENTS)
    assert set(edges) == {
        DataEdge("data/role/app.yaml", MANIFEST, "includes_class"),
        DataEdge("data/role/app.yaml", MANIFEST, "configures"),
        DataEdge("data/node/web-01.example.com.yaml", "data/role/app.yaml", "selects"),
        DataEdge("data/node/db-01.example.com.yaml", MANIFEST, "configures"),
    }


def test_parameters_take_what_the_data_gives_them() -> None:
    """The image and packages come from the data, the host from the node."""
    _, imports = puppet_links(CONTENTS)
    assert set(imports) == {
        Import("image", "example.com/tools/alpha-app", MANIFEST, "uses_image"),
        Import("package", "alpha-cli", MANIFEST, "installs"),
        Import(
            "host",
            "web-01.example.com",
            "data/node/web-01.example.com.yaml",
            "deploys_to",
        ),
        Import(
            "host",
            "db-01.example.com",
            "data/node/db-01.example.com.yaml",
            "deploys_to",
        ),
    }


def test_a_tree_without_classes_reads_no_data() -> None:
    """Without a manifest nothing is followed."""
    assert follow({"data/node/a.example.com.yaml": "role: app\n"}) == ([], [])


def test_a_module_reading_a_key_takes_what_the_data_puts_there() -> None:
    """hiera_hash('packages') in a module, packages: in a role file."""
    contents = {
        "shared/packages/manifests/init.pp": (
            "class packages {\n"
            "  create_resources_virt('@package', hiera_hash('packages', {}))\n"
            "}\n"
        ),
        "data/role/sso.yaml": "packages:\n  alpha-lua:\n    ensure: 1.0\n",
    }
    edges, imports = puppet_links(contents)
    assert (
        DataEdge(
            "data/role/sso.yaml", "shared/packages/manifests/init.pp", "configures"
        )
        in edges
    )
    assert (
        Import("package", "alpha-lua", "shared/packages/manifests/init.pp", "installs")
        in imports
    )
