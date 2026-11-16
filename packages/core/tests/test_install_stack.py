"""The stack's installer, driven against fakes on PATH.

Docker, the network and sha256sum are fakes that log what they were asked;
the release files are this checkout's own.
"""

from __future__ import annotations

import os
import re
import socket
import stat
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "scripts" / "install-stack.sh"
VERSION = "9.9.0"
DIGESTED = "services:\n  postgres:\n    image: pgvector/pgvector:pg16@sha256:00\n"

FAKE_DOCKER = r"""#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
case "$*" in
    info | "compose version") exit 0 ;;
    ps\ *) [ -z "${FAKE_RUNNING_FROM:-}" ] || echo "$FAKE_RUNNING_FROM" ;;
    *" exec -T web "*) echo "egt_operator" ;;
    *" exec -T postgres pg_dump "*) echo "dump" ;;
esac
"""

FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift ;;
        http*) url="$1" ;;
    esac
    shift
done
if [ -n "${FAKE_FAIL:-}" ] && [[ "$url" == *"$FAKE_FAIL"* ]]; then exit 22; fi
case "$url" in
    *releases/latest) echo "{\"tag_name\": \"v$FAKE_VERSION\"}" ;;
    *releases/download/*) printf '%b' "$FAKE_COMPOSE" > "$out" ;;
    *raw.githubusercontent.com*)
        cp "$FAKE_ROOT/$(sed 's#.*/v[0-9.]*/##' <<< "$url")" "$out" ;;
    *huggingface.co*) echo weights > "$out" ;;
esac
"""

FAKE_SHA256SUM = """#!/usr/bin/env bash
cat > /dev/null
[ -z "${FAKE_SUM_FAIL:-}" ]
"""


def executable(path: Path, text: str) -> None:
    """Write a script a shell can run."""
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def free_port() -> int:
    """Return a port nothing listens on now."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Host:
    """A temporary home and PATH that stand for a machine."""

    def __init__(self, base: Path) -> None:
        """Lay the fakes out under base."""
        self.home = base / "home"
        self.bin = base / "bin"
        self.log = base / "log"
        for directory in (self.home, self.bin):
            directory.mkdir(parents=True)
        self.log.write_text("", encoding="utf-8")
        executable(self.bin / "docker", FAKE_DOCKER)
        executable(self.bin / "curl", FAKE_CURL)
        executable(self.bin / "sha256sum", FAKE_SHA256SUM)
        self.dir = self.home / ".local/share/enggraph/stack"
        self.port = str(free_port())
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "FAKE_LOG": str(self.log),
            "FAKE_ROOT": str(ROOT),
            "FAKE_VERSION": VERSION,
            "FAKE_COMPOSE": DIGESTED,
        }

    def run(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run the installer on this host, with no terminal to ask at."""
        return subprocess.run(
            ["bash", str(INSTALLER), *args],
            env={**self.env, **env},
            cwd=self.home,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            check=False,
        )

    def install(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Install on a port nothing listens on."""
        return self.run("--port", self.port, *args, **env)

    def logged(self, prefix: str = "") -> list[str]:
        """Return the calls logged, those starting with prefix when one is given."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if line.startswith(prefix)]

    def env_file(self) -> dict[str, str]:
        """Return the stack's .env as a mapping."""
        text = (self.dir / ".env").read_text(encoding="utf-8")
        return dict(
            line.split("=", 1) for line in text.splitlines() if re.match(r"^\w+=", line)
        )


@pytest.fixture
def host(tmp_path: Path) -> Iterator[Host]:
    """Return a machine with nothing installed."""
    if not sys.platform.startswith("linux"):
        pytest.skip("the installer runs on Linux")
    yield Host(tmp_path)


def test_an_install_lays_out_the_release_and_starts_it(host: Host) -> None:
    """The files, a .env of its own, the model, the services and a token."""
    done = host.install()

    assert done.returncode == 0, done.stderr
    for name in (
        "docker-compose.release.yaml",
        "nginx/default.conf",
        "nginx/templates/auth.conf.template",
    ):
        assert (host.dir / name).is_file()
    models = host.home / ".local/share/enggraph/models"
    assert (models / "nomic-embed-text-v1.5.f16.gguf").is_file()
    up = [call for call in host.logged("docker compose") if " up -d " in call]
    assert len(up) == 1
    assert up[0].endswith(
        "postgres valkey embedder worker-api embed summarize"
        " mcp-server viewer web nginx"
    )
    assert "egt_operator" in done.stdout


def test_the_env_holds_secrets_of_its_own(host: Host) -> None:
    """Nothing of .env.example's placeholders survives, and only its owner reads it."""
    assert host.install("--host", "stack.example.com").returncode == 0

    env = host.env_file()
    for key in ("POSTGRES_PASSWORD", "POSTGRES_APP_PASSWORD", "WORKER_API_TOKEN"):
        assert re.fullmatch(r"[0-9a-f]{48}", env[key])
    assert env["POSTGRES_PASSWORD"] != env["POSTGRES_APP_PASSWORD"]
    assert env["TAG"] == VERSION
    assert env["GATEWAY_BIND"] == "127.0.0.1"
    assert env["GATEWAY_PORT"] == host.port
    assert f"stack.example.com:{host.port}" in env["GATEWAY_HOSTS"].split(",")
    assert stat.S_IMODE((host.dir / ".env").stat().st_mode) == 0o600
    assert sorted(path.name for path in host.home.iterdir()) == [".local"]


def test_an_installed_stack_is_left_as_it_is(host: Host) -> None:
    """A second plain run downloads nothing and starts nothing."""
    assert host.install().returncode == 0
    calls = len(host.logged())

    done = host.run()

    assert done.returncode == 0, done.stderr
    assert f"the stack {VERSION} is installed" in done.stdout
    assert len(host.logged()) == calls


def test_an_installed_stack_refuses_a_flag_it_would_ignore(host: Host) -> None:
    """A flag never silently loses to what is installed."""
    assert host.install().returncode == 0

    done = host.run("--port", "3001")

    assert done.returncode == 1
    assert "run update or uninstall" in done.stderr


def test_a_release_with_an_image_by_tag_alone_is_refused(host: Host) -> None:
    """What runs is what the release pushed, named by digest."""
    done = host.install(FAKE_COMPOSE="services:\n  web:\n    image: web:latest\n")

    assert done.returncode == 1
    assert "not named by digest" in done.stderr
    assert not host.dir.exists()


def test_a_model_that_fails_its_digest_is_not_kept(host: Host) -> None:
    """The embedder never loads a file nobody checked."""
    done = host.install(FAKE_SUM_FAIL="1")

    assert done.returncode == 1
    models = host.home / ".local/share/enggraph/models"
    assert list(models.iterdir()) == []


def test_a_taken_port_is_found_before_anything_lands(host: Host) -> None:
    """The doctor runs before the first download."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = str(taken.getsockname()[1])

        done = host.run("--port", port)

    assert done.returncode == 1
    assert f"listens on port {port}" in done.stderr
    assert host.logged("curl ") == []


def test_a_stack_run_from_elsewhere_is_left_alone(host: Host) -> None:
    """One database directory, so one stack per machine."""
    done = host.install(FAKE_RUNNING_FROM="/srv/checkout")

    assert done.returncode == 1
    assert "already runs from /srv/checkout" in done.stderr


def test_update_backs_up_first_and_keeps_the_env(host: Host) -> None:
    """The migration that follows is not reversible; the dump is the way back."""
    assert host.install().returncode == 0
    before = host.env_file()

    done = host.run("update", FAKE_VERSION="9.10.0", FAKE_RUNNING_FROM=str(host.dir))

    assert done.returncode == 0, done.stderr
    assert len(list((host.dir / "backups").glob("enggraph-*.dump"))) == 1
    after = host.env_file()
    assert after["TAG"] == "9.10.0"
    assert after["POSTGRES_PASSWORD"] == before["POSTGRES_PASSWORD"]
    assert f"{VERSION} -> 9.10.0" in done.stdout


def test_update_needs_a_stack(host: Host) -> None:
    """Nothing to update is an error, not an install."""
    done = host.run("update")

    assert done.returncode == 1
    assert "no stack is installed" in done.stderr


def test_uninstall_asks_when_it_may_not_assume(host: Host) -> None:
    """No terminal and no --yes removes nothing."""
    assert host.install().returncode == 0

    done = host.run("uninstall")

    assert done.returncode == 1
    assert "--yes" in done.stderr
    assert host.dir.exists()


def test_uninstall_keeps_the_database_unless_purged(host: Host) -> None:
    """The data outlives the files that run it."""
    assert host.install().returncode == 0
    data = host.home / ".local/share/enggraph/db"

    kept = host.run("uninstall", "--yes")

    assert kept.returncode == 0, kept.stderr
    assert not host.dir.exists()
    assert data.exists()
    assert not [call for call in host.logged("docker compose") if "--volumes" in call]


def test_purge_deletes_the_database_through_its_own_image(host: Host) -> None:
    """The files belong to the container's postgres user."""
    assert host.install().returncode == 0

    done = host.run("uninstall", "--purge", "--yes")

    assert done.returncode == 0, done.stderr
    assert [call for call in host.logged("docker compose") if "--volumes" in call]
    assert any(
        "pgvector/pgvector:pg16@sha256:00" in call for call in host.logged("docker run")
    )


def installer_list(name: str) -> list[str]:
    """Return the words of one array assignment in the installer."""
    text = INSTALLER.read_text(encoding="utf-8")
    found = re.search(rf"^{name}=\(([^)]*)\)", text, re.MULTILINE)
    assert found, name
    return found.group(1).split()


def test_the_services_are_the_ones_make_up_starts() -> None:
    """Two lists of one stack would drift."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    pattern = r"\$\(STACK\) up -d (.*?)\$\(AUTH_SERVICES\)"
    started = re.search(pattern, makefile, re.DOTALL)
    assert started
    assert installer_list("services") == started.group(1).replace("\\", " ").split()


def test_every_file_the_gateway_mounts_is_downloaded() -> None:
    """A template added under nginx/ would be missing from an install."""
    files = (ROOT / "nginx").rglob("*")
    nginx = sorted(str(path.relative_to(ROOT)) for path in files if path.is_file())
    assert sorted(set(installer_list("release_files")) - {".env.example"}) == nginx


def test_the_model_is_the_one_the_catalogue_names() -> None:
    """The embedder loads what make llm-model-install would put there."""
    sys.path.insert(0, str(ROOT / "worker"))
    try:
        from enggraph_worker import catalogue
    finally:
        sys.path.remove(str(ROOT / "worker"))
    text = INSTALLER.read_text(encoding="utf-8")
    assert f"model_url={catalogue.url('nomic-embed')}" in text
    assert f"model_sha256={catalogue.sha256('nomic-embed')}" in text
    assert f"model_file={catalogue.file_name('nomic-embed')}" in text


def test_the_installer_is_executable() -> None:
    """It is run as a file from a checkout, not only through bash."""
    assert os.access(INSTALLER, os.X_OK)
