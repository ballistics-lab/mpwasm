"""Other MicroPython builds from npm: resolve a version or tag, download it once, keep it in a cache.

The bundled build (fetched by build_assets.py when mpwasm is built) is what you get by default and works
offline. `MicroPython(npm="1.28")` -- or `--npm 1.28` on the command line -- asks for another one:
`@micropython/micropython-webassembly-pyscript` on the npm registry publishes every release (and
previews) of MicroPython's WebAssembly port, each as a tarball with the loader and all its variants.

How a spec is resolved (same idea as rp2040py's `--image`): a leading "v" is dropped; an npm dist-tag
("latest") or an exact version wins; otherwise the spec is a prefix of dotted components -- "1.28"
means 1.28.x, "1" every 1.x -- and the highest release that matches is used. Previews
("1.28.0-preview-233") are only used when named exactly.

Downloads are checked against the `integrity` (sha512) the registry publishes for that tarball, and
kept in `$MPWASM_CACHE` or `~/.cache/mpwasm` (falling back to a temp directory when neither can be
created), one directory per version. A version already there is used without any network access.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import re
import tarfile
import tempfile
import time
import urllib.request
from typing import Any, Final

__all__ = ("PACKAGE", "asset_dir", "cache_dir", "resolve", "versions")

PACKAGE: Final = "@micropython/micropython-webassembly-pyscript"
REGISTRY: Final = "https://registry.npmjs.org"
_LIST_TTL: Final = 3600  # seconds a downloaded version list is reused
_TIMEOUT: Final = 120

_log = logging.getLogger("mpwasm")


def cache_dir() -> str:
    """Where downloaded builds are kept across runs and projects."""
    root = os.environ.get("MPWASM_CACHE") or os.path.join(os.path.expanduser("~"), ".cache", "mpwasm")
    try:
        os.makedirs(root, exist_ok=True)
    except OSError as exc:
        fallback = os.path.join(tempfile.gettempdir(), "mpwasm-cache")
        _log.warning("cannot create cache directory %s (%s); using %s", root, exc, fallback)
        os.makedirs(fallback, exist_ok=True)
        return fallback
    return root


def _get(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:
        return resp.read()


def _packument() -> dict[str, Any]:
    """The registry's record of the package (all versions and dist-tags), cached for an hour."""
    path = os.path.join(cache_dir(), "packument.json")
    try:
        if time.time() - os.path.getmtime(path) < _LIST_TTL:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    try:
        data = _get(f"{REGISTRY}/{PACKAGE}")
    except OSError as exc:
        # Offline: a stale list still resolves tags well enough, and cached versions never need it.
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            raise exc from None
    packument = json.loads(data)
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass
    return packument


def _key(version: str) -> tuple[int, ...]:
    """Sort key: (major, minor, patch, is_release, build) with previews below releases."""
    m = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-(.+))?", version)
    if not m:
        return (-1,)
    major, minor, patch = (int(g) if g else 0 for g in m.groups()[:3])
    suffix = m.group(4) or ""
    build = re.search(r"(\d+)$", suffix)
    return (major, minor, patch, 0 if "preview" in suffix else 1, int(build.group(1)) if build else 0)


def versions() -> list[str]:
    """Every published version, oldest first (previews included)."""
    return sorted(_packument()["versions"], key=_key)


def _cached(version: str) -> bool:
    return os.path.isfile(os.path.join(cache_dir(), version, ".complete"))


def resolve(spec: str) -> str:
    """The exact npm version a spec ("latest", "1.28", "v1.29.0-6", ...) stands for."""
    spec = spec.strip().removeprefix("v")
    if not spec:
        raise ValueError("empty MicroPython version")
    if _cached(spec):  # exact and already downloaded: no network needed
        return spec
    pk = _packument()
    if spec in pk.get("dist-tags", {}):
        return str(pk["dist-tags"][spec])
    if spec in pk["versions"]:
        return spec
    m = re.fullmatch(r"\d+(?:\.\d+)*", spec)
    if m:
        precision = spec.count(".") + 1
        want = _key(spec)[:precision]
        matches = [v for v in pk["versions"] if _key(v)[:precision] == want and _key(v)[3] == 1]
        if matches:
            return max(matches, key=_key)
    raise ValueError(f"no MicroPython build {spec!r} on npm; try one of: {', '.join(versions()[-8:])}, ...")


def _verify(data: bytes, integrity: str) -> None:
    algo, _, expected = integrity.partition("-")
    if algo != "sha512":
        raise ValueError(f"unsupported integrity {integrity[:20]!r} (expected sha512)")
    got = base64.b64encode(hashlib.sha512(data).digest()).decode()
    if got != expected:
        raise ValueError(f"checksum mismatch: registry says {integrity[:24]}..., downloaded {algo}-{got[:16]}...")


def asset_dir(spec: str) -> str:
    """Directory holding the `micropython.mjs` and `micropython[-variant].wasm` files of a build.

    Resolves `spec`, downloads and unpacks it on first use, and returns the cached directory.
    """
    version = resolve(spec)
    out = os.path.join(cache_dir(), version)
    if os.path.isfile(os.path.join(out, ".complete")):
        return out
    info = _packument()["versions"][version]["dist"]
    _log.info("downloading MicroPython %s from %s", version, info["tarball"])
    data = _get(info["tarball"])
    _verify(data, info["integrity"])
    tmp = tempfile.mkdtemp(prefix=f".{version}-", dir=cache_dir())
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar.getmembers():
                name = os.path.basename(member.name)
                if member.isfile() and name.endswith((".mjs", ".wasm")):
                    src = tar.extractfile(member)
                    if src is not None:
                        with open(os.path.join(tmp, name), "wb") as f:
                            f.write(src.read())
        if not os.path.isfile(os.path.join(tmp, "micropython.mjs")):
            raise ValueError(f"MicroPython {version} has no micropython.mjs")
        with open(os.path.join(tmp, ".complete"), "w") as f:
            f.write(info["integrity"] + "\n")
        if os.path.isdir(out):  # a previous attempt or a racing process left one behind
            for name in os.listdir(out):
                os.remove(os.path.join(out, name))
            os.rmdir(out)
        os.replace(tmp, out)
    except BaseException:
        for name in os.listdir(tmp):
            os.remove(os.path.join(tmp, name))
        os.rmdir(tmp)
        raise
    return out
