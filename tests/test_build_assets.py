"""The bundled MicroPython build is whatever package-lock.json pins, so a Dependabot npm bump changes it."""

import json
import sys
from pathlib import Path

import pytest

import mpwasm

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import build_assets


def test_pinned_reads_version_url_and_integrity_from_the_lock() -> None:
    version, url, integrity = build_assets.pinned()
    assert url.endswith(f"micropython-webassembly-pyscript-{version}.tgz")
    assert integrity.startswith("sha512-")


def test_the_lock_agrees_with_package_json() -> None:
    package = json.loads((ROOT / "package.json").read_text())
    assert build_assets.PACKAGE in package["dependencies"]


def test_bundled_build_is_the_pinned_one() -> None:
    assert mpwasm.bundled_version() == build_assets.pinned()[0]


def test_a_lock_without_the_package_is_an_error(tmp_path: Path) -> None:
    lock = tmp_path / "package-lock.json"
    lock.write_text(json.dumps({"packages": {}}))
    with pytest.raises(KeyError):
        build_assets.pinned(str(lock))
