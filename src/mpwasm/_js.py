"""JavaScript that adapts MicroPython's browser-oriented ES module to a bare JS engine.

A bare engine (Pythonista's JSContext, WebKitGTK's JavaScriptCore, a Node `vm` context) has no
`console`, `setTimeout`, `TextDecoder` or `crypto`, and evaluates classic scripts, not ES modules
with top-level await. So the loader is patched into a classic script, wasm is instantiated
synchronously from bytes handed over by Python, and the missing browser APIs are shimmed. Timers are
driven by Python, which calls `__runTimers()` while it waits (`MicroPython._wait`).
"""

from __future__ import annotations

import re
from typing import Final

# ES module -> classic script. Regexes rather than exact strings so both npm's minified file and a
# custom (unminified) build patch the same way; each must match exactly once.
MJS_PATCHES: Final = (
    (r"export\s+default\s+\w+\s*;?", ""),
    (r"export\s+async\s+function\s+loadMicroPython\b", "async function loadMicroPython"),
    (r"let\s+Module\s*=\s*\{", "let Module={instantiateWasm:options.instantiateWasm,"),
)
MJS_IMPORT_META: Final = ("import.meta.url", '"file:///micropython.mjs"')
# Top-level await (api.js's Node CLI branch) is only valid in a module, so the whole source goes in an
# async IIFE; api.js publishes loadMicroPython on globalThis synchronously, before its first await.
MJS_WRAP: Final = '(async () => {{\n{src}\n}})().catch(e => console.error("mjs: " + (e && e.stack || e)));'

PRELUDE: Final = r"""
globalThis.window = globalThis;
globalThis.__log = [];
globalThis.console = {};
for (const k of ['log', 'info', 'warn', 'error', 'debug'])
    console[k] = (...a) => __log.push(a.map(String).join(' '));

globalThis.TextDecoder = class {
    decode(b) {
        if (!b) return '';
        b = b instanceof Uint8Array ? b : new Uint8Array(b.buffer || b);
        let s = '', i = 0;
        while (i < b.length) {
            let c = b[i++];
            if (c > 0x7f) {
                let n = c >= 0xf0 ? 3 : c >= 0xe0 ? 2 : c >= 0xc0 ? 1 : 0;
                c &= 0x3f >> n;
                while (n-- > 0 && i < b.length) c = (c << 6) | (b[i++] & 0x3f);
            }
            s += String.fromCodePoint(c);
        }
        return s;
    }
};

globalThis.crypto = {
    getRandomValues(a) {
        for (let i = 0; i < a.length; i++) a[i] = Math.random() * 256 | 0;
        return a;
    }
};

globalThis.__timers = new Map();
globalThis.__timerId = 0;
globalThis.setTimeout = (fn, ms = 0, ...args) => {
    const id = ++__timerId;
    __timers.set(id, { at: Date.now() + ms, fn, args });
    return id;
};
globalThis.clearTimeout = id => __timers.delete(id);
globalThis.__runTimers = () => {
    const now = Date.now();
    for (const [id, t] of [...__timers])
        if (t.at <= now) { __timers.delete(id); t.fn(...t.args); }
    return __timers.size;
};

globalThis.__hexToBytes = hex => {
    const out = new Uint8Array(hex.length >> 1);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
    return out;
};
"""

# linebuffer:false makes MicroPython hand over its output as raw bytes, one chunk per write, stdout and
# stderr separately: no lost trailing newline, no split of a partial line, and the REPL's prompts, echo and
# cursor control reach the terminal untouched. Python decodes the bytes (mpwasm.MicroPython.output).
LOADER: Final = r"""
globalThis.__mpOut = [];
globalThis.__mpErr = [];
globalThis.__mpState = 'loading';
globalThis.__sink = a => x => { if (typeof x === 'number') a.push(x); else for (const b of x) a.push(b); };
globalThis.__drain = a => {
    const bytes = a.splice(0);
    let s = '';
    for (let i = 0; i < bytes.length; i += 8192) s += String.fromCharCode.apply(null, bytes.slice(i, i + 8192));
    return s;   // one char per byte (0-255); Python turns it back into bytes and decodes it
};
loadMicroPython({
    heapsize: __heapsize,
    linebuffer: false,
    stdout: __sink(__mpOut),
    stderr: __sink(__mpErr),
    instantiateWasm(imports, done) {
        const bytes = __hexToBytes(__wasmHex);
        delete globalThis.__wasmHex;
        const instance = new WebAssembly.Instance(new WebAssembly.Module(bytes), imports);
        done(instance);
        return instance.exports;   // older Emscripten loaders (<= 1.24) take the exports from the return value
    },
}).then(
    mp => { globalThis.mp = mp; __mpState = 'ready'; },
    e => { __mpState = 'error: ' + (e && e.stack || e); }
);
"""

SET_WASM: Final = 'globalThis.__wasmHex = "{hex}"; globalThis.__heapsize = {heapsize};'
STATE: Final = "__mpState"
RUN: Final = "mp.runPython({code})"
RUN_ASYNC: Final = (
    "globalThis.__async = 'pending'; mp.runPythonAsync({code}).then("
    "() => __async = 'done', e => __async = 'error: ' + (e.message || e))"
)
ASYNC_STATE: Final = "__async"
PUMP: Final = "__runTimers()"
DRAIN_OUT: Final = "__drain(__mpOut)"
DRAIN_ERR: Final = "__drain(__mpErr)"
REPL_INIT: Final = "mp.replInit()"
REPL_FEED: Final = "for (const b of {data}) mp.replProcessChar(b);"
DRAIN_LOG: Final = '__log.splice(0).join("\\n")'

# What repl() runs: compile in 'single' mode so expression values are echoed like at the prompt.
REPL: Final = "exec(compile({src}, '<repl>', 'single'))"


def patch_mjs(src: str) -> str:
    """Turn MicroPython's ES-module loader into a classic script (see the module docstring)."""
    for pattern, new in MJS_PATCHES:
        src, n = re.subn(pattern, new, src)
        if n != 1:
            raise RuntimeError(
                f"this MicroPython loader has an unexpected layout (patch {pattern!r} matched {n} times, "
                "expected 1); builds older than 1.22 are not supported"
            )
    return MJS_WRAP.format(src=src.replace(*MJS_IMPORT_META))
