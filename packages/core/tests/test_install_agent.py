"""The agent's installer and `make install`, driven against fakes on PATH.

Nothing here reaches systemd, sudo, the network or a stack: a fake answers for
each, and each writes what it was asked into a log the tests read.
"""

from __future__ import annotations

import hashlib
import json
import os
import pty
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
INSTALLER = ROOT / "scripts" / "install-agent.sh"
ONBOARD = ROOT / "scripts" / "install.sh"
STACK = "http://stack.example.com:3000"
UNIT = "enggraph-agent.service"

# What a release holds. `state` and the binding come from files a test writes.
FAKE_AGENT = r"""#!/usr/bin/env bash
echo "agent $*" >> "$FAKE_LOG"
config="$HOME/.config/enggraph/config.toml"
if [ "${1:-}" = --config ]; then config="$2"; shift 2; fi
case "$1" in
    --version) echo "enggraph-agent 1.2.3" ;;
    init)
        mkdir -p "$(dirname "$config")"
        printf 'stack_url = "%s"\n' "$3" > "$config"
        ;;
    project)
        [ -f "$config" ] || { echo "no config" >&2; exit 1; }
        case "$2" in
            add)
                standing="${FAKE_STANDING:-approved bound to $3}"
                printf '%s %s %s\n' "$3" "$4" "$standing" >> "$config.projects"
                ;;
            list) cat "$config.projects" 2> /dev/null || true ;;
        esac
        ;;
    status)
        [ -f "$config" ] || { echo "no config" >&2; exit 1; }
        sed -n 's/^stack_url = "\(.*\)"$/stack \1/p' "$config"
        echo "state ${FAKE_STATE:-waiting for an operator, fingerprint 0a1b2c3d}"
        ;;
esac
"""

# Answers a download with the fake binary and its checksum, and any other URL
# with what FAKE_ANSWER holds, or a failure when FAKE_FAIL names it.
FAKE_CURL = r"""#!/usr/bin/env bash
echo "curl $*" >> "$FAKE_LOG"
out=""
url=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out="$2"; shift ;;
        -K) cat >> "$FAKE_LOG" ;;
        http*) url="$1" ;;
    esac
    shift
done
if [ -n "${FAKE_FAIL:-}" ] && [[ "$url" == *"$FAKE_FAIL"* ]]; then exit 22; fi
case "$url" in
    */SHA256SUMS)
        version="$(sed 's/.*agent-v\([^/]*\)\/.*/\1/' <<< "$url")"
        name="enggraph-agent-$version-$(uname -m)-linux-musl"
        printf '%s  %s\n' "$FAKE_SUM" "$name" > "$out"
        ;;
    *linux-musl) cp "$FAKE_BINARY" "$out" ;;
    *) [ -z "$out" ] && printf '%s' "${FAKE_ANSWER:-}" ;;
esac
exit 0
"""

FAKE_RECORDER = """#!/usr/bin/env bash
echo "$(basename "$0") $*" >> "$FAKE_LOG"
"""

FAKE_SYSTEMCTL = """#!/usr/bin/env bash
echo "systemctl $*" >> "$FAKE_LOG"
case "$*" in
    *is-enabled* | *is-active*) [ -z "${FAKE_UNIT_OFF:-}" ] ;;
esac
"""

FAKE_LOGINCTL = """#!/usr/bin/env bash
echo "loginctl $*" >> "$FAKE_LOG"
if [ "$1" = show-user ] && [ -n "${FAKE_LINGER:-}" ]; then echo Linger=yes; fi
"""

# Onboarding must never reach it, which the log says.
FAKE_DOCKER = """#!/usr/bin/env bash
echo "docker $*" >> "$FAKE_LOG"
"""

FAKE_SUDO = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
exec "$@"
"""


def executable(path: Path, text: str) -> None:
    """Write a script a shell can run."""
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class Host:
    """A temporary home and PATH that stand for a machine."""

    def __init__(self, base: Path) -> None:
        """Lay the fakes out under base."""
        self.home = base / "home"
        self.bin = base / "bin"
        self.root = base / "root"
        self.log = base / "log"
        self.tree = base / "srv" / "alpha"
        for directory in (self.home, self.bin, self.root, self.tree):
            directory.mkdir(parents=True)
        self.log.write_text("", encoding="utf-8")
        self.binary = base / "release"
        executable(self.binary, FAKE_AGENT)
        executable(self.bin / "curl", FAKE_CURL)
        executable(self.bin / "sudo", FAKE_SUDO)
        executable(self.bin / "docker", FAKE_DOCKER)
        executable(self.bin / "systemd-run", FAKE_RECORDER)
        executable(self.bin / "systemctl", FAKE_SYSTEMCTL)
        executable(self.bin / "loginctl", FAKE_LOGINCTL)
        self.env = {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": str(self.home),
            "DESTDIR": str(self.root),
            "FAKE_LOG": str(self.log),
            "FAKE_HOME": str(self.home),
            "FAKE_BINARY": str(self.binary),
            "FAKE_SUM": hashlib.sha256(self.binary.read_bytes()).hexdigest(),
        }

    def run(
        self, script: Path, *args: str, **env: str
    ) -> subprocess.CompletedProcess[str]:
        """Run a script on this host, with no terminal to ask at."""
        return subprocess.run(
            ["bash", str(script), *args],
            env={**self.env, **env},
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            check=False,
        )

    def install(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        """Run the installer with what every dry run passes."""
        base = ("--stack-url", STACK, "--name", "alpha", "--release", "1.2.3", "--yes")
        return self.run(INSTALLER, *base, *args, **env)

    def asked(self, answers: str, *args: str) -> str:
        """Run the installer on a terminal; end-of-file takes every other default."""
        pid, terminal = pty.fork()
        if pid == 0:
            try:
                os.execve("/bin/bash", ["bash", str(INSTALLER), *args], self.env)
            finally:
                os._exit(127)
        os.write(terminal, (answers + "\x04" * 8).encode())
        said = b""
        try:
            while chunk := os.read(terminal, 4096):
                said += chunk
        except OSError:
            pass
        os.waitpid(pid, 0)
        os.close(terminal)
        return said.decode()

    def release(self, version: str) -> None:
        """Publish another binary, which the next download fetches."""
        executable(self.binary, FAKE_AGENT.replace("1.2.3", version))
        self.env["FAKE_SUM"] = hashlib.sha256(self.binary.read_bytes()).hexdigest()

    def logged(self, prefix: str = "") -> list[str]:
        """Return the calls logged, those starting with prefix when one is given."""
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [line for line in lines if line.startswith(prefix)]


@pytest.fixture
def host(tmp_path: Path) -> Host:
    """Return a machine with nothing installed."""
    return Host(tmp_path)


def test_the_xdg_layout_lands_under_the_home(host: Host) -> None:
    """The binary, config, state and a user unit, with systemd left alone."""
    done = host.install(
        "--layout", "xdg", "--no-systemd", "--project", f"alpha={host.tree}"
    )

    assert done.returncode == 0, done.stderr
    binary = host.home / ".local/share/enggraph/bin/enggraph-agent"
    config = host.home / ".config/enggraph/config.toml"
    state = host.home / ".local/state/enggraph"
    assert os.access(binary, os.X_OK)
    assert (host.home / ".local/bin/enggraph-agent").resolve() == binary
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    init = host.logged("agent ")[0]
    assert f"--config {config} init" in init
    assert f"--stack-url {STACK} --name alpha --state-dir {state}" in init
    assert f"--config {config} project add alpha {host.tree}" in "\n".join(
        host.logged("agent ")
    )
    unit = (host.home / ".config/systemd/user" / UNIT).read_text(encoding="utf-8")
    assert f'ExecStart="{binary}" --config "{config}" run' in unit
    assert f'ReadWritePaths="{state}"' in unit
    assert "WantedBy=default.target" in unit
    assert "User=" not in unit
    assert host.logged("systemctl") == []
    assert host.logged("sudo") == []


def test_the_system_layout_lands_where_the_binary_looks(host: Host) -> None:
    """Under /opt, /etc and /var/lib, owned by whoever installs, a system unit."""
    done = host.install("--layout", "system", "--no-systemd")

    assert done.returncode == 0, done.stderr
    binary = host.root / "opt/enggraph/agent/bin/enggraph-agent"
    config = host.root / "etc/enggraph/config.toml"
    state = host.root / "var/lib/enggraph"
    assert os.access(binary, os.X_OK)
    assert (host.root / "usr/local/bin/enggraph-agent").resolve() == binary
    assert config.parent.stat().st_uid == os.getuid()
    assert state.stat().st_uid == os.getuid()
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    unit = (host.root / "etc/systemd/system" / UNIT).read_text(encoding="utf-8")
    assert f"User={subprocess.getoutput('id -un')}" in unit
    assert "WantedBy=multi-user.target" in unit
    assert host.logged("sudo")


@pytest.mark.parametrize(
    "line",
    [
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "PrivateTmp=yes",
        "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "Restart=on-failure",
    ],
)
def test_the_unit_is_hardened(host: Host, line: str) -> None:
    """Every line the plan asks for is in the unit."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0
    unit = (host.home / ".config/systemd/user" / UNIT).read_text(encoding="utf-8")
    assert line in unit


@pytest.mark.parametrize(
    ("layout", "unit_dir"),
    [("xdg", "home/.config/systemd/user"), ("system", "root/etc/systemd/system")],
)
def test_no_unit_sets_capabilities(
    host: Host, tmp_path: Path, layout: str, unit_dir: str
) -> None:
    """A user manager may not drop them: such a unit exits 218 at every start."""
    assert host.install("--layout", layout, "--no-systemd").returncode == 0
    unit = (tmp_path / unit_dir / UNIT).read_text(encoding="utf-8")
    assert "Capabilit" not in unit


def test_an_agent_that_never_registers_says_how_to_read_a_user_unit(host: Host) -> None:
    """Both tools need --user for a user unit, or they answer that there is none."""
    executable(host.bin / "sleep", "#!/usr/bin/env bash\n")

    done = host.install(
        "--layout", "xdg", "--systemd", "--no-linger", FAKE_STATE="not registered"
    )

    assert done.returncode == 0, done.stderr
    assert f"systemctl --user status {UNIT}" in done.stdout
    assert f"journalctl --user -u {UNIT}" in done.stdout


def test_a_started_unit_says_the_fingerprint_and_where_to_approve(host: Host) -> None:
    """What the operator has to compare, printed once the unit runs."""
    done = host.install("--layout", "xdg", "--systemd", "--no-linger")

    assert done.returncode == 0, done.stderr
    assert "systemctl --user daemon-reload" in host.logged("systemctl")
    assert f"systemctl --user restart {UNIT}" in host.logged("systemctl")
    assert "0a1b2c3d" in done.stdout
    assert f"{STACK}/agents" in done.stdout
    assert f"systemctl --user status {UNIT}" in done.stdout
    assert f"journalctl --user -u {UNIT}" in done.stdout


def test_labels_reach_init(host: Host) -> None:
    """Passed on as they were given."""
    assert (
        host.install(
            "--layout", "xdg", "--no-systemd", "--labels", "beta,gamma"
        ).returncode
        == 0
    )
    assert "--labels beta,gamma" in host.logged("agent ")[0]


def test_a_binary_that_fails_its_checksum_is_not_installed(host: Host) -> None:
    """Nothing is placed, and the reason names the file."""
    done = host.install("--layout", "xdg", "--no-systemd", FAKE_SUM="0" * 64)

    assert done.returncode == 1
    assert "does not match its SHA256SUMS" in done.stderr
    assert not (host.home / ".local/share/enggraph").exists()


def test_the_doctor_stops_a_project_that_cannot_be_read(host: Host) -> None:
    """Checked before anything is written."""
    missing = host.tree.parent / "missing"
    done = host.install(
        "--layout", "xdg", "--no-systemd", "--project", f"alpha={missing}"
    )

    assert done.returncode == 1
    assert str(missing) in done.stderr
    assert not (host.home / ".local/share/enggraph").exists()


def test_the_doctor_stops_a_stack_that_does_not_answer(host: Host) -> None:
    """The health address is the one the agent would dial."""
    done = host.install("--layout", "xdg", "--no-systemd", FAKE_FAIL="/health")

    assert done.returncode == 1
    assert f"{STACK}/health" in done.stderr


def test_a_second_run_keeps_the_config_and_offers_nothing_twice(host: Host) -> None:
    """An upgrade replaces the binary and nothing the agent wrote."""
    flags = ("--layout", "xdg", "--no-systemd", "--project", f"alpha={host.tree}")
    assert host.install(*flags).returncode == 0
    assert host.install(*flags).returncode == 0

    calls = host.logged("agent ")
    assert len([call for call in calls if " init " in call]) == 1
    assert len([call for call in calls if " project add " in call]) == 1


def test_another_stack_is_refused_over_a_config_that_exists(host: Host) -> None:
    """A flag never silently loses to the file, nor rewrites it."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(
        INSTALLER,
        "--stack-url",
        "https://other.example.com",
        "--name",
        "alpha",
        "--release",
        "1.2.3",
        "--no-systemd",
        "--yes",
    )

    assert done.returncode == 1
    assert "https://other.example.com" in done.stderr


def test_a_missing_stack_url_is_an_error_without_a_terminal(host: Host) -> None:
    """The flag to pass is named."""
    done = host.run(INSTALLER, "--layout", "xdg", "--no-systemd", "--yes")

    assert done.returncode == 1
    assert "--stack-url" in done.stderr


def test_from_source_uses_the_binary_make_builds(host: Host, tmp_path: Path) -> None:
    """No download: the checkout's own target is what lands."""
    checkout = tmp_path / "checkout"
    (checkout / "scripts").mkdir(parents=True)
    (checkout / "packages/agent").mkdir(parents=True)
    (checkout / "packages/agent/Cargo.toml").write_text("", encoding="utf-8")
    script = checkout / "scripts" / INSTALLER.name
    shutil.copy(INSTALLER, script)
    target = "packages/agent/target/$(uname -m)-unknown-linux-musl/release"
    executable(
        host.bin / "make",
        '#!/usr/bin/env bash\necho "make $*" >> "$FAKE_LOG"\n'
        f'mkdir -p "{checkout}/{target}"\n'
        f'cp "$FAKE_BINARY" "{checkout}/{target}/enggraph-agent"\n',
    )

    done = host.run(
        script,
        "--stack-url",
        STACK,
        "--name",
        "alpha",
        "--layout",
        "xdg",
        "--no-systemd",
        "--from-source",
        "--yes",
    )

    assert done.returncode == 0, done.stderr
    assert host.logged("make ") == [f"make --no-print-directory -C {checkout} agent"]
    assert not [call for call in host.logged("curl ") if "releases" in call]
    assert (host.home / ".local/share/enggraph/bin/enggraph-agent").exists()


def test_uninstall_takes_everything_away(host: Host) -> None:
    """The unit, the link, the binary, the config and the credential."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0
    state = host.home / ".local/state/enggraph/state.json"
    state.write_text("{}", encoding="utf-8")

    done = host.run(INSTALLER, "uninstall", "--no-systemd", "--yes")

    assert done.returncode == 0, done.stderr
    assert not (host.home / ".local/share/enggraph").exists()
    assert not (host.home / ".local/bin/enggraph-agent").is_symlink()
    assert not (host.home / ".config/enggraph").exists()
    assert not state.parent.exists()
    assert not (host.home / ".config/systemd/user" / UNIT).exists()


def test_uninstall_asks_when_it_may_not_assume(host: Host) -> None:
    """With no terminal and no --yes nothing is removed."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(INSTALLER, "uninstall", "--no-systemd")

    assert done.returncode == 1
    assert (host.home / ".config/enggraph/config.toml").exists()


def test_uninstall_is_silent_about_linger_it_did_not_turn_on(host: Host) -> None:
    """Linger found on is someone else's, so it is neither advised on nor touched."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(INSTALLER, "uninstall", "--no-systemd", "--yes", FAKE_LINGER="1")

    assert done.returncode == 0, done.stderr
    assert "linger" not in done.stdout
    assert not [call for call in host.logged("loginctl") if "linger " in call]


def test_uninstall_names_linger_it_turned_on_and_leaves_it(host: Host) -> None:
    """The installer enabled it, so it says so, and turning it off stays a choice."""
    assert host.install("--layout", "xdg", "--systemd", "--linger").returncode == 0
    assert [call for call in host.logged("loginctl") if "enable-linger" in call]

    done = host.run(INSTALLER, "uninstall", "--yes", FAKE_LINGER="1")

    assert done.returncode == 0, done.stderr
    assert "loginctl disable-linger" in done.stdout
    assert not [call for call in host.logged("loginctl") if "disable-linger" in call]


def test_the_old_uninstall_flag_is_gone(host: Host) -> None:
    """It is a command now, so the flag is an error and removes nothing."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(INSTALLER, "--uninstall", "--yes")

    assert done.returncode == 1
    assert "unknown argument" in done.stderr
    assert (host.home / ".config/enggraph/config.toml").exists()


def test_update_replaces_the_binary_and_keeps_the_rest(host: Host) -> None:
    """Nothing asked, nothing rewritten, the linger untouched, a used unit restarted."""
    assert host.install("--layout", "xdg", "--systemd", "--no-linger").returncode == 0
    host.release("1.2.4")
    host.log.write_text("", encoding="utf-8")

    done = host.run(INSTALLER, "update", "--release", "1.2.4")

    assert done.returncode == 0, done.stderr
    assert "updated the agent: 1.2.3 -> 1.2.4" in done.stdout
    assert f"systemctl --user restart {UNIT}" in host.logged("systemctl")
    assert f"systemctl --user enable {UNIT}" not in host.logged("systemctl")
    assert not [call for call in host.logged("agent ") if " init " in call]
    assert not [call for call in host.logged("loginctl") if "enable-linger" in call]


def test_update_leaves_a_unit_nobody_started_alone(host: Host) -> None:
    """An install with --no-systemd stays one."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(INSTALLER, "update", "--release", "1.2.3", FAKE_UNIT_OFF="1")

    assert done.returncode == 0, done.stderr
    assert f"systemctl --user restart {UNIT}" not in host.logged("systemctl")
    assert "systemctl --user daemon-reload" not in host.logged("systemctl")


def test_update_finds_the_system_layout_by_itself(host: Host) -> None:
    """The layout comes from what is installed, and no flag names it."""
    assert host.install("--layout", "system", "--no-systemd").returncode == 0
    host.release("1.2.4")

    done = host.run(INSTALLER, "update", "--release", "1.2.4", FAKE_UNIT_OFF="1")

    assert done.returncode == 0, done.stderr
    assert "1.2.3 -> 1.2.4" in done.stdout


def test_update_needs_an_agent_and_a_config_alone_is_not_one(host: Host) -> None:
    """It points at the install, and it installs nothing itself."""
    missing = host.run(INSTALLER, "update", "--release", "1.2.3")
    config = host.home / ".config/enggraph/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'stack_url = "{STACK}"\n', encoding="utf-8")
    leftover = host.run(INSTALLER, "update", "--release", "1.2.3")

    for done in (missing, leftover):
        assert done.returncode == 1
        assert "no agent is installed" in done.stderr
    assert not (host.home / ".local/share/enggraph").exists()


def test_update_refuses_a_flag_it_would_ignore(host: Host) -> None:
    """A flag never silently loses to what is installed."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    done = host.run(INSTALLER, "update", "--name", "beta")

    assert done.returncode == 1
    assert "--release and --from-source" in done.stderr


def test_doctor_checks_and_changes_nothing(host: Host) -> None:
    """Both answers, with nothing landing either way."""
    flags = ("doctor", "--stack-url", STACK, "--no-systemd")

    passed = host.run(INSTALLER, *flags)
    refused = host.run(INSTALLER, *flags, FAKE_FAIL="/health")

    assert passed.returncode == 0, passed.stderr
    assert "this host can run the agent" in passed.stdout
    assert refused.returncode == 1
    assert f"{STACK}/health" in refused.stderr
    assert not (host.home / ".local").exists()
    assert not (host.home / ".config").exists()


def test_doctor_reads_the_stack_of_an_installed_agent(host: Host) -> None:
    """No flag needed once a config names it."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0
    host.log.write_text("", encoding="utf-8")

    done = host.run(INSTALLER, "doctor", "--no-systemd")

    assert done.returncode == 0, done.stderr
    assert f"{STACK}/health" in "\n".join(host.logged("curl"))


def test_a_clean_host_is_asked_where_the_agent_lives(host: Host) -> None:
    """The layout question, whose answer decides where everything lands."""
    said = host.asked(
        "system\n", "--stack-url", STACK, "--release", "1.2.3", "--no-systemd"
    )

    assert "Where should the agent live?" in said
    assert "agent name" in said
    assert (host.root / "etc/enggraph/config.toml").exists()
    assert not (host.home / ".config/enggraph").exists()


def test_an_existing_config_answers_the_layout_and_the_name(host: Host) -> None:
    """Neither is asked, since neither answer would be used."""
    assert host.install("--layout", "xdg", "--no-systemd").returncode == 0

    said = host.asked("", "--release", "1.2.3", "--no-systemd")

    assert "Where should the agent live?" not in said
    assert "agent name" not in said
    assert "keeping" in said


class Onboarding:
    """A codebase, and the environment `make install` hands the script."""

    def __init__(self, host: Host, base: Path) -> None:
        """Put an installed agent on PATH and a tree to onboard beside it."""
        self.host = host
        self.tree = base / "alpha"
        self.tree.mkdir()
        self.tree = self.tree.resolve()
        executable(host.bin / "enggraph-agent", FAKE_AGENT)
        self.config = host.home / ".config/enggraph/config.toml"
        self.config.parent.mkdir(parents=True)
        self.config.write_text(f'stack_url = "{STACK}"\n', encoding="utf-8")
        self.offered = self.config.with_name("config.toml.projects")
        self.env = {
            "AGENT_ROOT": str(self.tree),
            "PYTHON_BIN": sys.executable,
            "PERMISSIONS": "0",
            "CLAUDE_CONFIG_DIR": str(base / "claude"),
            "SHELL_RC": str(base / "rc"),
            "AGENT_WAIT": "0",
            "ENGGRAPH_TOKEN": "egt_alpha_beta",
            "FAKE_ANSWER": json.dumps({"report": ["selected: 3  formats: 1"]}),
        }

    def run(self, **env: str) -> subprocess.CompletedProcess[str]:
        """Run the onboarding script."""
        return self.host.run(ONBOARD, **{**self.env, **env})

    def address(self) -> str:
        """Return the MCP address written for Claude."""
        document = json.loads((self.tree / ".mcp.json").read_text(encoding="utf-8"))
        return document["mcpServers"]["enggraph"]["url"]


@pytest.fixture
def onboarding(host: Host, tmp_path: Path) -> Onboarding:
    """Return a codebase ready to be onboarded through an installed agent."""
    return Onboarding(host, tmp_path)


def test_onboarding_leaves_compose_untouched(onboarding: Onboarding) -> None:
    """Offered, bound, scanned through the stack, and no docker at all."""
    done = onboarding.run()

    assert done.returncode == 0, done.stderr
    assert onboarding.host.logged("docker") == []
    assert f"agent project add alpha {onboarding.tree}" in onboarding.host.logged(
        "agent "
    )
    assert "selected: 3  formats: 1" in done.stdout
    assert f"{STACK}/worker/projects/alpha/scan" in "\n".join(
        onboarding.host.logged("curl ")
    )
    assert onboarding.address() == f"{STACK}/mcp/alpha"
    assert (onboarding.tree / "CLAUDE.local.md").exists()


def test_the_token_stays_out_of_the_process_list(onboarding: Onboarding) -> None:
    """Handed to curl on stdin, where an argument would show to every user."""
    assert onboarding.run().returncode == 0

    calls = onboarding.host.logged("curl ")
    assert calls
    assert not [call for call in calls if "egt_alpha_beta" in call]
    assert 'header = "Authorization: Bearer egt_alpha_beta"' in (
        onboarding.host.logged("header")
    )


def test_the_address_names_the_project_it_was_bound_to(onboarding: Onboarding) -> None:
    """An export bound to an existing project answers under that project's name."""
    done = onboarding.run(FAKE_STANDING="approved bound to delta")

    assert done.returncode == 0, done.stderr
    assert onboarding.address() == f"{STACK}/mcp/delta"


def test_a_project_not_bound_in_time_writes_nothing(onboarding: Onboarding) -> None:
    """A clear message, and a rerun picks up where this stopped."""
    done = onboarding.run(FAKE_STANDING="pending")

    assert done.returncode == 1
    assert "not bound yet" in done.stderr
    assert f"{STACK}/agents" in done.stderr
    assert not (onboarding.tree / ".mcp.json").exists()

    onboarding.offered.write_text(
        f"alpha {onboarding.tree} approved bound to alpha\n", encoding="utf-8"
    )
    assert onboarding.run().returncode == 0
    assert len(onboarding.host.logged("agent project add")) == 1


def test_a_refused_project_stops_at_once(onboarding: Onboarding) -> None:
    """Waiting would change nothing."""
    done = onboarding.run(FAKE_STANDING="refused", AGENT_WAIT="900")

    assert done.returncode == 1
    assert "refused" in done.stderr


def test_a_directory_already_offered_keeps_its_name(onboarding: Onboarding) -> None:
    """A second name for the same path would never bind."""
    onboarding.offered.write_text(
        f"gamma {onboarding.tree} approved bound to gamma\n", encoding="utf-8"
    )

    assert onboarding.run().returncode == 0
    assert onboarding.host.logged("agent project add") == []
    assert onboarding.address() == f"{STACK}/mcp/gamma"


def test_a_failed_scan_does_not_stop_the_onboarding(onboarding: Onboarding) -> None:
    """The report is for reading: the project is bound whatever it says."""
    done = onboarding.run(FAKE_FAIL="/scan")

    assert done.returncode == 0, done.stderr
    assert "no report" in done.stdout
    assert onboarding.address() == f"{STACK}/mcp/alpha"


def test_a_missing_agent_is_installed_first(onboarding: Onboarding, host: Host) -> None:
    """At the version of this checkout, in the layout asked for."""
    (host.bin / "enggraph-agent").unlink()
    onboarding.config.unlink()

    done = onboarding.run(STACK_URL=STACK, AGENT_LAYOUT="xdg")

    assert done.returncode == 0, done.stderr
    calls = "\n".join(host.logged("curl "))
    assert "/releases/download/agent-v" in calls
    assert (host.home / ".local/share/enggraph/bin/enggraph-agent").exists()
    assert f"systemctl --user restart {UNIT}" in host.logged("systemctl")
    assert onboarding.address() == f"{STACK}/mcp/alpha"


def test_a_project_with_no_tree_needs_no_agent(
    onboarding: Onboarding, host: Host
) -> None:
    """An organization is only a row: stored through the stack."""
    (host.bin / "enggraph-agent").unlink()

    done = onboarding.run(SOURCE="none", STACK_URL=STACK)

    assert done.returncode == 0, done.stderr
    assert host.logged("agent ") == []
    assert host.logged("docker") == []
    post = [call for call in host.logged("curl ") if "-X POST" in call]
    assert f"{STACK}/worker/projects" in post[0]
    assert onboarding.address() == f"{STACK}/mcp/alpha"


def test_make_install_recreates_no_service(tmp_path: Path) -> None:
    """Read from what make would run, which runs nothing with -n."""
    done = subprocess.run(
        [
            "make",
            "-n",
            "--no-print-directory",
            "install",
            f"AGENT_ROOT={tmp_path}",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "up -d" not in done.stdout
    assert "Onboarding" not in done.stdout
    assert not list(tmp_path.iterdir())


def make_up(directory: Path, first_line: str) -> subprocess.CompletedProcess[str]:
    """Run `make up` where an override starts with this line, compose doing nothing."""
    (directory / ".env").write_text(f"WORKER_API_TOKEN={'a' * 16}\n", encoding="utf-8")
    override = directory / "docker-compose.override.yaml"
    override.write_text(f"{first_line}\n", encoding="utf-8")
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(ROOT / "Makefile"),
            "up",
            "COMPOSE=true",
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=False,
    )


def test_make_up_refuses_a_generated_override_and_leaves_it(tmp_path: Path) -> None:
    """Compose would go on loading it, and deleting it is the user's to do."""
    done = make_up(tmp_path, "# Generated by 'make mounts'. Do not edit:")

    assert done.returncode != 0
    assert "Delete the file" in done.stderr
    assert (tmp_path / "docker-compose.override.yaml").exists()


def test_make_up_keeps_an_override_written_by_hand(tmp_path: Path) -> None:
    """Only the generated one is refused."""
    done = make_up(tmp_path, "services:")

    assert done.returncode == 0, done.stderr
