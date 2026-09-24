"""Choosing a MicroPython build from npm -- resolved and cached, tested against an in-memory registry."""

import base64
import hashlib
import io
import os
import tarfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import mpwasm
from mpwasm import _npm

VERSIONS = [
    "1.26.0",
    "1.27.0-preview-256",
    "1.27.0",
    "1.28.0-preview-233",
    "1.28.0-6",
    "1.29.0-6",
]


def _tarball() -> bytes:
    """A package tarball holding the bundled loader and wasm under `package/`, like npm's."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in ("micropython.mjs", "micropython.wasm", "micropython-ulab.wasm"):
            data = Path(mpwasm.asset_path(name)).read_bytes()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        readme = b"docs"
        info = tarfile.TarInfo("package/README.md")
        info.size = len(readme)
        tar.addfile(info, io.BytesIO(readme))
    return buf.getvalue()


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """An offline stand-in for the npm registry, and an empty cache in tmp_path."""
    monkeypatch.setenv("MPWASM_CACHE", str(tmp_path / "cache"))
    monkeypatch.delenv("MPWASM_NPM", raising=False)
    tarball = _tarball()
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    state: dict[str, Any] = {"downloads": 0, "lists": 0, "tarball": tarball, "integrity": integrity}
    packument = {
        "dist-tags": {"latest": "1.29.0-6"},
        "versions": {
            v: {"dist": {"tarball": f"https://registry.test/{v}.tgz", "integrity": integrity}} for v in VERSIONS
        },
    }

    def fake_get(url: str) -> bytes:
        if url.endswith(".tgz"):
            state["downloads"] += 1
            return state["tarball"]
        state["lists"] += 1
        import json

        return json.dumps(packument).encode()

    monkeypatch.setattr(_npm, "_get", fake_get)
    yield state


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("latest", "1.29.0-6"),
        ("1.29", "1.29.0-6"),
        ("v1.28", "1.28.0-6"),  # the release, not the preview
        ("1.28.0-6", "1.28.0-6"),
        ("1.27", "1.27.0"),
        ("1.26.0", "1.26.0"),
        ("1", "1.29.0-6"),  # a bare major: the newest of them
        ("1.28.0-preview-233", "1.28.0-preview-233"),  # previews only when named
    ],
)
def test_resolve(registry: dict[str, Any], spec: str, expected: str) -> None:
    assert _npm.resolve(spec) == expected


@pytest.mark.parametrize("spec", ["", "9.9", "1.2", "nope", "1.27.0-preview"])
def test_resolve_rejects_unknown(registry: dict[str, Any], spec: str) -> None:
    with pytest.raises(ValueError):
        _npm.resolve(spec)


def test_short_prefix_is_by_components_not_by_characters(registry: dict[str, Any]) -> None:
    # "1.2" must not match 1.26 / 1.27 / 1.28 / 1.29 just because they start with the same characters.
    with pytest.raises(ValueError):
        _npm.resolve("1.2")


def test_versions_oldest_first(registry: dict[str, Any]) -> None:
    assert _npm.versions() == VERSIONS


def test_download_verifies_extracts_and_caches(registry: dict[str, Any], tmp_path: Path) -> None:
    d = _npm.asset_dir("1.28")
    assert sorted(os.listdir(d)) == [".complete", "micropython-ulab.wasm", "micropython.mjs", "micropython.wasm"]
    assert Path(d) == tmp_path / "cache" / "1.28.0-6"
    assert registry["downloads"] == 1
    assert _npm.asset_dir("1.28.0-6") == d  # cached: no second download ...
    assert registry["downloads"] == 1
    lists = registry["lists"]
    assert _npm.asset_dir("1.28.0-6") == d  # ... and an exact cached version needs no registry list either
    assert registry["lists"] == lists


def test_corrupt_download_is_rejected_and_leaves_no_cache(registry: dict[str, Any], tmp_path: Path) -> None:
    registry["tarball"] = registry["tarball"] + b"tampered"
    with pytest.raises(ValueError, match="checksum"):
        _npm.asset_dir("1.28")
    assert not (tmp_path / "cache" / "1.28.0-6").exists()
    assert [p for p in (tmp_path / "cache").iterdir() if p.name.startswith(".1.28")] == []


def test_micropython_from_npm(registry: dict[str, Any]) -> None:
    with mpwasm.MicroPython(npm="1.28") as mp:
        assert mp.run("print(6 * 7)") == "42\n"
    assert registry["downloads"] == 1


def test_bundled_version_needs_no_network(registry: dict[str, Any]) -> None:
    bundled = mpwasm.bundled_version()
    assert bundled is not None
    with mpwasm.MicroPython(npm=bundled) as mp:
        assert mp.run("print(1)") == "1\n"
    assert registry["downloads"] == 0 and registry["lists"] == 0


def test_env_default(registry: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPWASM_NPM", "1.27")
    with mpwasm.MicroPython() as mp:
        assert mp.run("print(2)") == "2\n"
    assert registry["downloads"] == 1


def test_variant_missing_from_a_build(registry: dict[str, Any]) -> None:
    with pytest.raises(FileNotFoundError, match="variant"):
        mpwasm.MicroPython(npm="1.28", variant="settrace")


def test_local_files_override_npm(registry: dict[str, Any]) -> None:
    with mpwasm.MicroPython(
        mjs_path=mpwasm.asset_path("micropython.mjs"), wasm_path=mpwasm.asset_path("micropython.wasm"), npm="1.28"
    ) as mp:
        assert mp.run("print(3)") == "3\n"
    assert registry["downloads"] == 0


def test_unwritable_cache_falls_back_to_temp(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setenv("MPWASM_CACHE", str(blocker / "sub"))  # a path below a regular file can't be created
    assert Path(_npm.cache_dir()).name == "mpwasm-cache"
