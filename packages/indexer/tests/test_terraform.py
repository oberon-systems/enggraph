"""What a Terraform, OpenTofu or Terragrunt file refers to."""

from __future__ import annotations

from enggraph.indexer.parsers.languages import HCLParser
from enggraph.indexer.resolution import has_placeholder, resolve_file_target

ROOT_MODULE = """
module "compute" {
  source = "../../modules/compute"
}

module "network" {
  source  = "git::https://example.com/alpha/infra.git//modules/network?ref=v1.2"
}

locals {
  config   = yamldecode(file("${path.module}/config.yaml"))
  userdata = templatefile("templates/cloud-init.tpl", { name = "a" })
  dynamic  = file("${var.root}/other.yaml")
}
"""
TERRAGRUNT = """
include "root" {
  path = find_in_parent_folders("root.hcl")
}

terraform {
  source = "${get_repo_root()}/modules//compute"
}

dependency "network" {
  config_path = "../network"
}

locals {
  env = read_terragrunt_config(find_in_parent_folders("env.hcl"))
}
"""


def _relations(rel_path: str, content: str) -> list[tuple[str, str]]:
    return [
        (one["target"], one["type"])
        for one in HCLParser().get_relations(content, rel_path)
    ]


def test_a_root_module_names_its_modules_and_files() -> None:
    """Local sources and static file paths resolve, templated ones are skipped."""
    found = _relations("circuits/live/web/main.tf", ROOT_MODULE)
    assert ("circuits/modules/compute", "uses_module") in found
    assert (
        "tfmodule:git::https://example.com/alpha/infra.git//modules/network?ref=v1.2",
        "uses_module",
    ) in found
    assert ("circuits/live/web/config.yaml", "reads_file") in found
    assert ("circuits/live/web/templates/cloud-init.tpl", "reads_file") in found
    assert len(found) == 4


def test_a_terragrunt_unit_names_its_parents_source_and_dependencies() -> None:
    """Terragrunt functions resolve to the files they would find."""
    found = _relations("live/eu/compute/terragrunt.hcl", TERRAGRUNT)
    assert sorted(found) == [
        ("live/eu/network", "depends_on"),
        ("modules/compute", "uses_module"),
        ("parents:env.hcl", "reads_vars"),
        ("parents:root.hcl", "includes"),
    ]


def test_a_path_climbing_out_of_the_tree_is_dropped() -> None:
    """A source above the tree root names nothing in this project."""
    assert _relations("main.tf", 'module "x" { source = "../shared" }') == []


def test_targets_resolve_to_the_files_of_the_tree() -> None:
    """A module is its main file, a parent lookup the nearest file above."""
    known = {
        "modules/compute/main.tf",
        "live/root.hcl",
        "live/eu/network/terragrunt.hcl",
    }
    unit = "live/eu/compute/terragrunt.hcl"
    assert resolve_file_target("uses_module", "modules/compute", unit, known) == (
        "modules/compute/main.tf"
    )
    assert resolve_file_target("includes", "parents:root.hcl", unit, known) == (
        "live/root.hcl"
    )
    assert resolve_file_target("depends_on", "live/eu/network", unit, known) == (
        "live/eu/network/terragrunt.hcl"
    )


def test_a_module_dir_without_main_resolves_to_its_first_file() -> None:
    """A module split into other files still resolves."""
    known = {"modules/r2/outputs.tf", "modules/r2/buckets.tf"}
    assert resolve_file_target("uses_module", "modules/r2", "main.tf", known) == (
        "modules/r2/buckets.tf"
    )


def test_only_a_remote_module_keeps_a_placeholder() -> None:
    """A missing file gets no node; a module from elsewhere does."""
    assert has_placeholder("tfmodule:example.com/alpha/infra", "main.tf")
    assert not has_placeholder("config.yaml", "main.tf")
    assert not has_placeholder("parents:root.hcl", "live/terragrunt.hcl")
