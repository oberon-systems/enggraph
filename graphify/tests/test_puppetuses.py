"""The images a Puppet manifest runs and the packages it installs."""

from __future__ import annotations

from enggraph.parsers.languages import PuppetParser
from enggraph.parsers.puppetuses import puppet_uses

MANIFEST = """class alpha_app (
  String $version,
  String $registry = 'example.com',
){
  $image = 'example.com/tools/alpha-app'
  $proxy = "${registry}/default/alpha-proxy"

  docker::run { 'alpha-app':
    image     => $image,
    image_tag => $version,
  }

  docker::run { 'alpha-proxy':
    image => $proxy,
  }

  $environment = {
    'IMAGE'   => $image,
    'VERSION' => $version,
  }

  docker::run { 'dynamic':
    image => "${unknown}/x",
  }

  package { 'alpha-tools':
    ensure => installed,
  }

  package { ['htop', 'mc']: ensure => present }

  package { 'requests':
    ensure   => installed,
    provider => 'pip3',
  }

  package { 'bundler': provider => gem }

  ensure_packages(['jq'])
}
"""


def test_images_resolve_through_the_variables_of_the_manifest() -> None:
    """A variable assigned a literal, or interpolated from one, is followed."""
    found = puppet_uses(MANIFEST)
    assert ("image:example.com/tools/alpha-app", "uses_image") in found
    assert ("image:example.com/default/alpha-proxy", "uses_image") in found
    assert not any("unknown" in target or "/x" == target[-2:] for target, _ in found)


def test_packages_are_taken_by_their_provider() -> None:
    """An OS package, a pip package, and nothing for a gem."""
    found = {target for target, _ in puppet_uses(MANIFEST)}
    assert {
        "package:alpha-tools",
        "package:htop",
        "package:mc",
        "package:jq",
        "pypi:requests",
    } <= found
    assert "package:bundler" not in found


def test_the_parser_hands_them_on_as_file_relations() -> None:
    """They leave the manifest file, for the indexer to turn into placeholders."""
    relations = PuppetParser().get_relations(
        MANIFEST, "modules/alpha_app/manifests/init.pp"
    )
    assert {
        "target": "image:example.com/tools/alpha-app",
        "type": "uses_image",
        "scope": "file",
    } in relations


INSTALL = """class alpha_app::install {
  $package_name = $::alpha_app::package_name
  $agent = 'alpha-agent'
  package { $package_name: ensure => present }
  package { $agent: ensure => present }
  ensure_resource('package', 'alpha-ssl', { ensure => '1.1' })
  $base = 'http://repo.example.com/pip/alpha-bridge'
  $file = "alpha-bridge-${version}.tar.gz"
  file { '/opt/req.txt': content => "${base}/${file}" }
  $rpm = 'http://repo.example.com/alpha-tools-1.2-3.el9.x86_64.rpm'
}
"""


def test_variables_functions_and_artifact_urls_are_followed() -> None:
    """A manifest variable, another class's parameter, ensure_resource, URLs."""
    found = {
        target
        for target, _ in puppet_uses(
            INSTALL,
            lambda key: ["alpha-app"] if key == "alpha_app::package_name" else [],
        )
    }
    assert {
        "package:alpha-app",
        "package:alpha-agent",
        "package:alpha-ssl",
        "pypi:alpha-bridge",
        "package:alpha-tools",
    } <= found


def test_an_image_handed_to_a_compose_environment_is_taken() -> None:
    """A hash key IMAGE is the image a templated compose file runs."""
    manifest = """class alpha_mail {
  $image = 'example.com/alpha-mail'
  $env = { 'IMAGE' => $image, 'VERSION' => '1.0' }
}
"""
    assert puppet_uses(manifest) == [("image:example.com/alpha-mail", "uses_image")]
