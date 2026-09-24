"""mpwasm -- MicroPython (its WebAssembly build) for CPython, PyPy and Pythonista.

MicroPython's WebAssembly port normally needs a browser or Node. This package runs it inside any
bare JavaScript engine driven from Python: JavaScriptCore's `JSContext` in Pythonista on iOS (via
objc_util), WebKitGTK's JavaScriptCore on Linux, or a Node `vm` context -- with the same JavaScript on
all of them (see `_hosts.py`, `_js.py`).

    from mpwasm import MicroPython

    mp = MicroPython()
    print(mp.run("print(sum(range(10)))"))          # -> 45
    mp.run("x = 6")                                 # state persists between calls
    print(mp.repl("x * 7"))                         # -> 42 (echoed like at the prompt)
    print(mp.run_async("import asyncio\\nawait asyncio.sleep(0.1)\\nprint('done')"))

Which MicroPython: the bundled build by default (from the MicroPython project's npm package, see
build_assets.py; works offline), `MicroPython(npm="1.28")` for another release from npm (downloaded once
and cached, see `_npm.py`), or `MicroPython(mjs_path=..., wasm_path=...)` for local files -- a firmware with
your own usermod compiled in, say. `variant="ulab"` picks the build with numpy-like arrays.

Threads: an interpreter can be shared between threads (each call holds the instance's lock, so one call's
output is never mixed with another's), and separate `MicroPython` objects run truly in parallel, including
on free-threaded Python. Each has its own JavaScript host.

Configuration (environment variables):
    MPWASM_HOST    jscontext | gi-jsc | node -- which JavaScript host (default: first that starts)
    MPWASM_NPM     default for `npm=`: a MicroPython version or tag to fetch from npm
    MPWASM_CACHE   where downloaded builds are kept (default ~/.cache/mpwasm)
"""

from __future__ import annotations

import codecs
import json
import os
import re
import threading
import time
from types import TracebackType
from typing import TYPE_CHECKING, Final

from . import _js, _npm
from ._hosts import AUTO_ORDER, HOSTS, JSHost, default_host

if TYPE_CHECKING:
    from typing_extensions import Self  # typing.Self is 3.11+; only needed by type checkers

__all__ = (
    "AUTO_ORDER",
    "HOSTS",
    "JSHost",
    "MicroPython",
    "MicroPythonError",
    "asset_path",
    "bundled_version",
    "default_host",
    "npm_versions",
)

_HERE: Final = os.path.dirname(os.path.abspath(__file__))


class MicroPythonError(RuntimeError):
    """MicroPython raised an exception (or the JS engine failed) while running code.

    `output` is what MicroPython printed to stdout before that, which `run()` would have returned.
    """

    def __init__(self, message: str, output: str = "") -> None:
        super().__init__(message)
        self.output = output


def bundled_version() -> str | None:
    """The npm version of the bundled build, or None if the assets are not there."""
    try:
        with open(os.path.join(_HERE, ".npm-integrity"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    return lines[1] if len(lines) > 1 else None


def npm_versions() -> list[str]:
    """Every MicroPython WebAssembly build published on npm, oldest first (queries the registry)."""
    return _npm.versions()


def _wasm_name(variant: str) -> str:
    return f"micropython-{variant}.wasm" if variant else "micropython.wasm"


def _build_files(npm: str | None, variant: str) -> tuple[str, str]:
    """(loader, wasm) paths of the bundled build, or of the npm build `npm` names."""
    if npm is not None and npm.strip().removeprefix("v") != bundled_version():
        directory = _npm.asset_dir(npm)
        mjs = os.path.join(directory, "micropython.mjs")
        wasm = os.path.join(directory, _wasm_name(variant))
        if not os.path.isfile(wasm):
            have = sorted(
                f[len("micropython") :].removesuffix(".wasm").lstrip("-")
                for f in os.listdir(directory)
                if f.endswith(".wasm")
            )
            raise FileNotFoundError(
                f"MicroPython {npm} has no variant {variant!r}; it has: {', '.join(map(repr, have))}"
            )
        return mjs, wasm
    return asset_path("micropython.mjs"), asset_path(_wasm_name(variant))


def asset_path(name: str) -> str:
    """Path of a bundled file: "micropython.mjs", "micropython.wasm" or "micropython-ulab.wasm"."""
    path = os.path.join(_HERE, name)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"{path} not found. The MicroPython assets are fetched when the package is built: run "
            "`uv sync` or `python build_assets.py` in the mpwasm repo, or pass mjs_path/wasm_path."
        )
    return path


class MicroPython:
    """One MicroPython interpreter (own heap, own globals) running in a JavaScript host."""

    def __init__(
        self,
        mjs_path: str | None = None,
        wasm_path: str | None = None,
        *,
        npm: str | None = None,
        variant: str = "",
        heapsize: int = 1024 * 1024,
        timeout: float = 10.0,
        host: str | JSHost | None = None,
    ) -> None:
        """Load the interpreter.

        Which build: `mjs_path` / `wasm_path` are local files (each falls back to the build below);
        `npm` is a version or tag to fetch from npm ("latest", "1.28", "1.29.0-6"; $MPWASM_NPM is the
        default), downloaded once and cached; with neither, the bundled build. `variant` picks the
        wasm within that build: "" (plain), "ulab", ... `heapsize` is MicroPython's GC heap in bytes;
        `timeout` bounds the load; `host` is a host name (see HOSTS), a ready JSHost, or None for the
        automatic pick.
        """
        if not re.fullmatch(r"[a-z0-9]*(-[a-z0-9]+)*", variant):
            raise ValueError(f"invalid variant {variant!r}")
        if npm is None:
            npm = os.environ.get("MPWASM_NPM") or None
        if mjs_path is None or wasm_path is None:
            default_mjs, default_wasm = _build_files(npm, variant)
            mjs_path = mjs_path or default_mjs
            wasm_path = wasm_path or default_wasm
        for path in (mjs_path, wasm_path):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"file not found: {path}")
        with open(mjs_path, encoding="utf-8") as f:
            mjs = _js.patch_mjs(f.read())
        with open(wasm_path, "rb") as f:
            wasm_hex = f.read().hex()

        if host is None:
            self._host = default_host()
        elif isinstance(host, JSHost):
            self._host = host
        else:
            if host not in HOSTS:
                raise ValueError(f"unknown host {host!r}: expected one of {', '.join(HOSTS)}")
            self._host = HOSTS[host]()

        self._lock = threading.RLock()  # one call at a time per interpreter: its host is one channel
        # MicroPython writes UTF-8 bytes; a multi-byte character can arrive split across drains.
        self._decode_out = codecs.getincrementaldecoder("utf-8")("replace")
        self._decode_err = codecs.getincrementaldecoder("utf-8")("replace")
        self._eval(_js.PRELUDE)
        try:
            self._eval(mjs)
            self._eval(_js.SET_WASM.format(hex=wasm_hex, heapsize=heapsize))
            self._eval(_js.LOADER)
            state = self._wait(_js.STATE, "loading", timeout, "load")
        except (RuntimeError, TimeoutError) as exc:
            raise MicroPythonError(f"{exc}\n--- JS log ---\n{self.js_log()}") from None
        if state != "ready":
            raise MicroPythonError(f"MicroPython failed to load: {state}\n--- JS log ---\n{self.js_log()}")

    @property
    def host(self) -> str:
        """Name of the JavaScript host in use: jscontext, gi-jsc or node."""
        return self._host.name

    def _eval(self, src: str) -> str:
        return self._host.evaluate(src)

    def _wait(self, expr: str, pending: str, timeout: float, what: str) -> str:
        """Poll `expr` until it stops being `pending`, running due JS timers meanwhile."""
        deadline = time.time() + timeout
        while (state := self._eval(expr)) == pending:
            if time.time() > deadline:
                raise TimeoutError(f"timed out waiting for {what}")
            self._eval(_js.PUMP)
            time.sleep(0.005)
        return state

    def output(self) -> str:
        """Drain and return what MicroPython wrote to stdout since the last drain."""
        with self._lock:
            return self._decode_out.decode(self._eval(_js.DRAIN_OUT).encode("latin-1"))

    def error_output(self) -> str:
        """Drain and return what MicroPython wrote to stderr (`sys.stderr`) since the last drain."""
        with self._lock:
            return self._decode_err.decode(self._eval(_js.DRAIN_ERR).encode("latin-1"))

    def js_log(self) -> str:
        """Drain and return what the JavaScript side logged (console.*), for diagnosing loader problems."""
        with self._lock:
            return self._eval(_js.DRAIN_LOG)

    def run(self, code: str) -> str:
        """Run `code` synchronously and return what it printed to stdout, exactly (so `print("a")` gives
        "a\\n"). Raises MicroPythonError on an exception; what went to stderr stays in error_output()."""
        with self._lock:
            try:
                self._eval(_js.RUN.format(code=json.dumps(code)))
            except RuntimeError as exc:
                raise MicroPythonError(str(exc), self.output()) from None
            return self.output()

    def run_async(self, code: str, timeout: float = 60.0) -> str:
        """Run `code` with top-level `await` (asyncio works) and return what it printed."""
        with self._lock:
            self._eval(_js.RUN_ASYNC.format(code=json.dumps(code)))
            try:
                state = self._wait(_js.ASYNC_STATE, "pending", timeout, "run_async")
            except TimeoutError as exc:
                raise MicroPythonError(str(exc), self.output()) from None
            out = self.output()
            if state != "done":
                raise MicroPythonError(state, out)
            return out

    def repl_init(self) -> None:
        """Start MicroPython's own REPL (its banner and first prompt are in output()).

        Feed it keystrokes with repl_feed(); it does its own line editing, history, auto-indent and
        continuation lines, echoing to stdout like a terminal session. This is what the command line uses.
        """
        with self._lock:
            self._eval(_js.REPL_INIT)

    def repl_feed(self, data: bytes) -> None:
        """Send raw input bytes (what a terminal would send) to the REPL started by repl_init()."""
        with self._lock:
            self._eval(_js.REPL_FEED.format(data=json.dumps(list(data))))

    def pump(self) -> None:
        """Run JavaScript timers that are due (what makes asyncio tasks progress while nothing else runs)."""
        with self._lock:
            self._eval(_js.PUMP)

    def repl(self, source: str) -> str:
        """Run one statement or expression as at the interactive prompt (expression values are echoed)."""
        return self.run(_js.REPL.format(src=repr(source)))

    def close(self) -> None:
        """Release the JavaScript host."""
        with self._lock:
            self._host.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        self.close()
