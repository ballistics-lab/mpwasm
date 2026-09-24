"""Download MicroPython's WebAssembly build from npm into the package.

The tarball is @micropython/micropython-webassembly-pyscript (MIT), the same build the MicroPython project
publishes for browsers. Which version is pinned in package.json / package-lock.json, so Dependabot's npm
updates change what gets bundled: the lock file records the exact version, its tarball URL and its sha512
`integrity`, which is checked against the download, so a tampered or swapped file fails the build.

Runs from setup.py on every build (wheel, sdist -> wheel, editable `uv sync`), and by hand as
`uv run python build_assets.py`. A download is skipped when the files are already there and were
fetched from the same pinned tarball.
"""

import base64
import hashlib
import io
import json
import os
import tarfile
import urllib.request

PACKAGE = "@micropython/micropython-webassembly-pyscript"
ROOT = os.path.dirname(os.path.abspath(__file__))
LOCK = os.path.join(ROOT, "package-lock.json")


def pinned(lock_path: str = LOCK) -> tuple[str, str, str]:
    """(version, tarball URL, sha512 integrity) of the package, from package-lock.json."""
    with open(lock_path, encoding="utf-8") as f:
        entry = json.load(f)["packages"][f"node_modules/{PACKAGE}"]
    return entry["version"], entry["resolved"], entry["integrity"]


VERSION, URL, INTEGRITY = pinned()

# The loader plus the plain build and the one with ulab (numpy-like arrays). The two settrace builds
# in the tarball are left out to keep the wheel small.
FILES = ("micropython.mjs", "micropython.wasm", "micropython-ulab.wasm")

OUT_DIR = os.path.join(ROOT, "src", "mpwasm")
STAMP = ".npm-integrity"


def _integrity(data: bytes) -> str:
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()


def up_to_date(out_dir: str = OUT_DIR) -> bool:
    try:
        with open(os.path.join(out_dir, STAMP)) as f:
            if f.read().splitlines()[:2] != [INTEGRITY, VERSION]:
                return False
    except OSError:
        return False
    return all(os.path.isfile(os.path.join(out_dir, name)) for name in FILES)


def fetch(out_dir: str = OUT_DIR, verbose: bool = True) -> list[str]:
    """Make sure the assets are in out_dir; return their paths."""
    paths = [os.path.join(out_dir, name) for name in FILES]
    if up_to_date(out_dir):
        return paths
    if verbose:
        print(f"Downloading {URL}")
    with urllib.request.urlopen(URL, timeout=120) as resp:
        data = resp.read()
    got = _integrity(data)
    if got != INTEGRITY:
        raise SystemExit(f"npm tarball checksum mismatch: expected {INTEGRITY}, got {got}")
    os.makedirs(out_dir, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for name, path in zip(FILES, paths, strict=True):
            member = tar.extractfile("package/" + name)
            if member is None:
                raise SystemExit(f"package/{name} is not in the npm tarball")
            with open(path, "wb") as f:
                f.write(member.read())
            if verbose:
                print(f"  {path} ({os.path.getsize(path)} bytes)")
    with open(os.path.join(out_dir, STAMP), "w") as f:
        f.write(f"{INTEGRITY}\n{VERSION}\n")
    return paths


if __name__ == "__main__":
    fetch()
