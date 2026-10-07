"""A deployment workspace, laid out by the hierarchy its data declares."""

from __future__ import annotations

from enggraph.indexer.parsers.ansible import AnsibleParser
from enggraph.indexer.parsers.workspace import workspace_candidates, workspace_relations
from enggraph.indexer.projectlinks import Export, Import, workspace_links
from enggraph.indexer.puppetdata import DataEdge
from enggraph.indexer.resolution import placeholder_id, resolve_file_target
from enggraph.indexer.workspacedata import load_data, workspace_files, workspaces

HIERARCHY = """---
hierarchy:
  - os/{family}/{release}.yaml
  - modules/{module}.yaml
  - roles/{role}.yaml
  - nodes/{node}.yaml
"""
NODE = """---
role: web
agent:
  version: 2.0.0
docker:
  stacks:
    panel:
      content: |
        services:
          panel:
            image: "example.com/alpha/panel:1.2"
"""
ROLE = """---
modules:
  - agent
  - backup
nftables: !replace
  rules: {}
"""
REQUIRES = """---
requires:
  - repos
"""
AGENT_CONFIG = """from pydantic import BaseModel


class Config(BaseModel):
    version: str = '1.0.0'
    package: str = 'alpha-agent'
    artifact: str
    base: str = 'https://example.com/releases'

    @property
    def artifact_name(self) -> str:
        return self.artifact.format(version=self.version)

    @property
    def url(self) -> str:
        return f'{self.base}/{self.artifact_name}'
"""
AGENT_MAIN = """from pyinfra.operations import dnf, files

from .config import Config


def _names(config: Config) -> list[str]:
    return sorted(config.extra)


def deploy(config: Config) -> None:
    files.download(src=config.url, dest='/tmp/agent.rpm')
    dnf.packages(packages=[config.package])
    if wanted := _names(config):
        dnf.packages(packages=wanted)
"""
AGENT_DEFAULTS = """---
agent:
  artifact: alpha-agent-{version}-1.x86_64.rpm
  extra:
    htop: {}
    alpha-tools: {}
"""
TREE = {
    "deploy/data/common.yaml": HIERARCHY,
    "deploy/data/nodes/web-01.example.com.yaml": NODE,
    "deploy/data/roles/web.yaml": ROLE,
    "deploy/data/modules/repos.yaml": (
        "---\nrepos:\n  stores:\n    alpha:\n      bucket: repo\n"
    ),
    "deploy/modules/agent/requires.yaml": REQUIRES,
    "deploy/modules/agent/data/defaults.yaml": AGENT_DEFAULTS,
    "deploy/modules/agent/code/config.py": AGENT_CONFIG,
    "deploy/modules/agent/code/main.py": AGENT_MAIN,
    "deploy/modules/agent/tests/test_agent.py": "from pyinfra.operations import dnf\n",
    "deploy/modules/docker/code/main.py": "",
    "deploy/modules/repos/README.md": "# repos\n",
    "deploy/modules/nftables/README.md": "# nftables\n",
}
NODE_PATH = "deploy/data/nodes/web-01.example.com.yaml"
ROLE_PATH = "deploy/data/roles/web.yaml"
AGENT = "deploy/modules/agent/requires.yaml"


def test_the_layout_is_what_the_hierarchy_declares() -> None:
    """Data under the declaring directory, modules beside it, tests left out."""
    (space,) = workspaces(TREE)
    assert (space.root, space.data_dir) == ("deploy/", "deploy/data/")
    assert space.variables == {"family", "release", "module", "role", "node"}
    files = workspace_files(space, list(TREE))
    assert "deploy/modules/agent/data/defaults.yaml" in files
    assert "deploy/modules/agent/code/main.py" in files
    assert "deploy/modules/agent/tests/test_agent.py" not in files


def test_without_a_hierarchy_there_is_no_workspace() -> None:
    """A Puppet hiera.yaml lists mappings, not patterns."""
    hiera = "hierarchy:\n  - name: common\n    path: common.yaml\n"
    assert workspaces({"hiera.yaml": hiera, "data/nodes/a.yaml": "role: web\n"}) == []


def test_merge_tags_keep_their_value() -> None:
    """`!replace {...}` is still the mapping it marks."""
    assert load_data(ROLE)["nftables"] == {"rules": {}}


def test_data_selects_runs_and_configures_from_the_module() -> None:
    """A layer key selects its file, `modules:` runs, a module key configures."""
    edges, _, _ = workspace_links(list(TREE), TREE)
    assert set(edges) >= {
        DataEdge(NODE_PATH, ROLE_PATH, "selects"),
        DataEdge(NODE_PATH, AGENT, "configures"),
        DataEdge(NODE_PATH, "deploy/modules/docker/code/main.py", "configures"),
        DataEdge(ROLE_PATH, AGENT, "includes_module"),
        DataEdge(ROLE_PATH, "deploy/modules/nftables/README.md", "configures"),
        DataEdge(
            "deploy/data/modules/repos.yaml",
            "deploy/modules/repos/README.md",
            "configures",
        ),
    }
    assert not any(edge.source_id.startswith("deploy/modules/") for edge in edges)


def test_what_a_workspace_provides_and_takes() -> None:
    """Modules and selected roles are provided; hosts, artifacts and images taken."""
    _, exports, imports = workspace_links(list(TREE), TREE)
    assert Export("deploy-role", "web", ROLE_PATH) in exports
    assert Export("deploy-module", "agent", "deploy/modules/agent/") in exports
    assert set(imports) == {
        Import("host", "web-01.example.com", NODE_PATH, "deploys_to"),
        Import("deploy-module", "backup", ROLE_PATH, "includes_module"),
        Import("package", "alpha-agent", AGENT, "installs"),
        Import("package", "htop", AGENT, "installs"),
        Import("package", "alpha-tools", AGENT, "installs"),
        Import("image", "example.com/alpha/panel", NODE_PATH, "uses_image"),
        Import("bucket", "repo", "deploy/data/modules/repos.yaml", "uses_bucket"),
    }


def test_the_parser_reads_only_what_a_module_requires() -> None:
    """Node and role files are left to the hierarchy."""
    relations = AnsibleParser().get_relations(REQUIRES, AGENT)
    assert [(one["target"], one["type"]) for one in relations] == [
        ("repos", "requires_module")
    ]
    assert workspace_relations(ROLE_PATH, [{"modules": ["agent"]}]) is None
    known = {"deploy/modules/repos/README.md"}
    assert workspace_candidates("requires_module", "repos", AGENT)[-1] in known
    assert resolve_file_target("requires_module", "repos", AGENT, known) == (
        "deploy/modules/repos/README.md"
    )
    assert placeholder_id("requires_module", "nginx") == "deploy-module:nginx"
