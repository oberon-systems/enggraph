"""The hashed requirement files hold what the pins they are made from say."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def lock() -> ModuleType:
    """Load the script `make lock` runs."""
    spec = importlib.util.spec_from_file_location("lock", ROOT / "scripts/lock.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_pin_is_in_its_file_with_a_hash(lock: ModuleType) -> None:
    """A pin moved without `make lock` would be installed from nowhere."""
    for path, (_, pins) in lock.targets().items():
        lines = (ROOT / path).read_text(encoding="utf-8").splitlines()
        held = {line.split(" ")[0].lower() for line in lines if "==" in line}
        for pin in pins:
            assert pin.lower().replace("_", "-") in held, f"{path}: run make lock"
        packages = [line for line in lines if line and not line.startswith((" ", "#"))]
        assert all(line.endswith("\\") for line in packages), path
        assert sum("--hash=sha256:" in line for line in lines) >= len(packages)


def test_an_image_installs_what_its_dockerfile_copies(lock: ModuleType) -> None:
    """The packages a lock is resolved for are the ones the image installs."""
    for image, packages in lock.IMAGES.items():
        text = (ROOT / "packages" / image / "Dockerfile").read_text(encoding="utf-8")
        assert f"COPY packages/{image}/requirements.txt" in text
        assert "--require-hashes" in text and "--no-build-isolation" in text
        installed = " ".join(f"./packages/{one}" for one in packages)
        assert installed in text, image


def test_the_build_backend_is_the_one_the_locks_hold(lock: ModuleType) -> None:
    """Without build isolation the pinned backend is the only one there is."""
    build = lock.groups()["build"]
    for package in lock.PACKAGES:
        path = ROOT / "packages" / package / "pyproject.toml"
        assert f"requires = {build!r}".replace("'", '"') in path.read_text("utf-8")
