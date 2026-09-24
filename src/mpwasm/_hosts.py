"""JavaScript hosts for MicroPython's WebAssembly build: `wasmhost`'s JavaScript engines, under mpwasm's own
environment variable.

Every host does one thing: evaluate a JavaScript source string in a *bare* context (no browser globals;
`_js.PRELUDE` adds the few that are needed) and return the result as a string.

    JSContextHost           JavaScriptCore via Pythonista's objc_util (iOS). What this exists for.
    GIJavaScriptCoreHost    WebKitGTK's JavaScriptCore via PyGObject (Linux): the same engine, driven
                            the same way -- the desktop rehearsal of the Pythonista setup.
    NodeHost                a long-lived `node` process running a `vm` context, which is bare like the
                            two above, so the same shims and patched loader apply unchanged.

(wasmhost also has native runtimes, wasmtime and wasm3; MicroPython's build ships JavaScript glue, so only the
engines can run it.)
"""

from __future__ import annotations

from wasmhost import JS_AUTO_ORDER as AUTO_ORDER
from wasmhost import JS_BACKENDS as HOSTS
from wasmhost import GIJavaScriptCoreBackend as GIJavaScriptCoreHost
from wasmhost import JSBackend as JSHost
from wasmhost import JSContextBackend as JSContextHost
from wasmhost import NodeBackend as NodeHost
from wasmhost import default_backend

__all__ = (
    "AUTO_ORDER",
    "HOSTS",
    "GIJavaScriptCoreHost",
    "JSContextHost",
    "JSHost",
    "NodeHost",
    "default_host",
)


def default_host() -> JSHost:
    """Start a host: $MPWASM_HOST if set, else the first of AUTO_ORDER that starts."""
    host = default_backend("MPWASM_HOST", js_only=True)
    if not isinstance(host, JSHost):  # can't happen with js_only; narrows the type
        raise TypeError(f"{host.name} is not a JavaScript engine")
    return host
