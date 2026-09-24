"""JavaScript hosts for MicroPython's WebAssembly build.

Every host does one thing: evaluate a JavaScript source string in a *bare* context (no browser
globals; `_js.PRELUDE` adds the few that are needed) and return the result as a string.

    JSContextHost           JavaScriptCore via Pythonista's objc_util (iOS). What this exists for.
    GIJavaScriptCoreHost    WebKitGTK's JavaScriptCore via PyGObject (Linux): the same engine, driven
                            the same way -- the desktop rehearsal of the Pythonista setup.
    NodeHost                a long-lived `node` process running a `vm` context, which is bare like the
                            two above, so the same shims and patched loader apply unchanged.

Plain Python 3.10+, no third-party imports (objc_util / gi are Pythonista / Linux only and loaded
lazily), so it runs on PyPy and on Pythonista's interpreter.
"""

from __future__ import annotations

import atexit
import importlib
import json
import os
import shutil
import subprocess
from typing import Any, Final

__all__ = (
    "AUTO_ORDER",
    "HOSTS",
    "GIJavaScriptCoreHost",
    "JSContextHost",
    "JSHost",
    "NodeHost",
    "default_host",
)


class JSHost:
    """A bare JavaScript context. Subclasses implement `evaluate`."""

    name: str = "?"

    def evaluate(self, src: str) -> str:
        """Run `src` and return the value of its last expression as a string."""
        raise NotImplementedError

    def close(self) -> None:
        """Release the engine. Safe to call more than once."""


class JSContextHost(JSHost):
    """JavaScriptCore via Pythonista's objc_util."""

    name = "jscontext"

    def __init__(self) -> None:
        # Pythonista only, and untyped (Objective-C proxies): import it as an explicit Any.
        objc_util: Any = importlib.import_module("objc_util")
        self._ctx: Any = objc_util.ObjCClass("JSContext").alloc().init()
        kind = self.evaluate("typeof WebAssembly")
        if kind != "object":
            raise RuntimeError(f"WebAssembly is not available in this JSContext (typeof WebAssembly = {kind})")

    def evaluate(self, src: str) -> str:
        res = self._ctx.evaluateScript_(src)
        exc = self._ctx.exception()
        if exc:
            self._ctx.setException_(None)
            raise RuntimeError(f"[JS] {exc.toString()}")
        return str(res.toString())


class GIJavaScriptCoreHost(JSHost):
    """WebKitGTK's JavaScriptCore via PyGObject: `apt install gir1.2-javascriptcoregtk-4.1 python3-gi`."""

    name = "gi-jsc"

    def __init__(self) -> None:
        # PyGObject (Linux), untyped GObject-introspection proxies: import it as an explicit Any.
        gi: Any = importlib.import_module("gi")
        gi.require_version("JavaScriptCore", "4.1")
        javascriptcore: Any = importlib.import_module("gi.repository.JavaScriptCore")
        self._ctx: Any = javascriptcore.Context()
        kind = self.evaluate("typeof WebAssembly")
        if kind != "object":
            raise RuntimeError(f"WebAssembly is not available in this JSContext (typeof WebAssembly = {kind})")

    def evaluate(self, src: str) -> str:
        res = self._ctx.evaluate(src, -1)
        exc = self._ctx.get_exception()
        if exc:
            self._ctx.clear_exception()
            raise RuntimeError(f"[JS] {exc.to_string()}")
        return str(res.to_string())


# A `vm` context is a fresh global object with only the JS builtins (Promise, WebAssembly, Date, ...):
# no console, timers, TextDecoder or crypto -- as bare as a JSContext.
_NODE_LOOP = r"""
const vm = require('vm');
const ctx = vm.createContext({});
const rl = require('readline').createInterface({ input: process.stdin });
rl.on('line', (line) => {
    let reply;
    try { reply = { ok: true, value: String(vm.runInContext(JSON.parse(line), ctx)) }; }
    catch (e) { reply = { ok: false, value: String(e) }; }  // same text as JavaScriptCore's exception.toString()
    process.stdout.write(JSON.stringify(reply) + '\n');
});
"""


class NodeHost(JSHost):
    """A long-lived `node` process evaluating one JSON-encoded script per line in a `vm` context."""

    name = "node"

    def __init__(self, node: str | None = None) -> None:
        node = node or shutil.which("node")
        if not node:
            raise FileNotFoundError("node not found on PATH")
        self._proc = subprocess.Popen(
            [node, "-e", _NODE_LOOP],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            universal_newlines=True,
            encoding="utf-8",
        )
        if self._proc.stdin is None or self._proc.stdout is None:  # can't happen with PIPE; narrows the types
            raise RuntimeError("node started without stdin/stdout pipes")
        self._stdin = self._proc.stdin
        self._stdout = self._proc.stdout
        atexit.register(self.close)

    def evaluate(self, src: str) -> str:
        self._stdin.write(json.dumps(src) + "\n")
        self._stdin.flush()
        line = self._stdout.readline()
        if not line:
            raise RuntimeError(f"node exited (status {self._proc.poll()})")
        reply = json.loads(line)
        if not reply["ok"]:
            raise RuntimeError(f"[JS] {reply['value']}")
        return str(reply["value"])

    def close(self) -> None:
        if self._proc.poll() is None:
            self._stdin.close()
            self._proc.wait(timeout=5)
        if not self._stdout.closed:
            self._stdout.close()


HOSTS: Final[dict[str, type[JSHost]]] = {
    "jscontext": JSContextHost,
    "gi-jsc": GIJavaScriptCoreHost,
    "node": NodeHost,
}

# Tried in this order when nothing is chosen. Each constructor is its own availability probe: it raises
# when its runtime isn't there (ImportError for objc_util/gi, a missing `node` binary, an engine
# without WebAssembly), so "available" means "could actually start".
AUTO_ORDER: Final[tuple[str, ...]] = ("jscontext", "gi-jsc", "node")


def default_host() -> JSHost:
    """Start a host: $MPWASM_HOST if set, else the first of AUTO_ORDER that starts."""
    choice = os.environ.get("MPWASM_HOST", "").lower()
    if choice:
        if choice not in HOSTS:
            raise ValueError(f"MPWASM_HOST={choice!r}: expected one of {', '.join(HOSTS)}")
        return HOSTS[choice]()
    errors: list[str] = []
    for name in AUTO_ORDER:
        try:
            return HOSTS[name]()
        except Exception as exc:  # noqa: BLE001 -- not available here; try the next one
            errors.append(f"{name}: {exc}")
    raise RuntimeError(
        "No JavaScript host available (tried {}). Run in Pythonista, install PyGObject with "
        "JavaScriptCore, or put node on PATH.".format("; ".join(errors))
    )
