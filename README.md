# mpwasm

MicroPython, from **CPython, PyPy and Pythonista**. It is MicroPython's own WebAssembly build, run inside
whichever JavaScript engine is available: JavaScriptCore's `JSContext` in Pythonista on iOS,
WebKitGTK's JavaScriptCore on Linux, or Node. No C extension, no per-platform build.

```python
from mpwasm import MicroPython

mp = MicroPython()
print(mp.run("import sys\nprint(sys.implementation.name)"))   # micropython
mp.run("x = 6")                                                # state persists between calls
print(mp.repl("x * 7"))                                        # 42 -- expression values are echoed
print(mp.run_async("import asyncio\nawait asyncio.sleep(0.1)\nprint('done')"))   # top-level await works
```

`run()` returns what MicroPython printed. An exception in the Python code raises `MicroPythonError`
(a `RuntimeError`) with the traceback as its message and the output printed before it in `.output`.
See `examples/basic.py`, which ends in a small REPL.

- `heapsize=` sets MicroPython's GC heap (1 MiB by default); `timeout=` bounds the load.
- `run()` returns stdout exactly (`print("a")` gives `"a\n"`); `mp.error_output()` is what went to stderr.

## Which MicroPython

| | |
|---|---|
| default | the bundled build, no network needed (see "Build") |
| `MicroPython(npm="1.28")` | another release from npm: a version, a prefix (`1.28` is the newest 1.28.x), or a tag (`latest`). Downloaded once, checked against the registry's sha512 and cached in `~/.cache/mpwasm` (`$MPWASM_CACHE`); an exact cached version needs no network |
| `MicroPython(mjs_path=..., wasm_path=...)` | local files, for example a firmware with your own usermod compiled in (each falls back to the build above) |
| `variant="ulab"` | the build with numpy-like arrays (`import ulab.numpy as np`) |

`mpwasm.npm_versions()` lists what npm has. Releases from 1.22.0-335 on work; older loaders are laid out
differently.

## Command line

Like the unix `micropython` binary: `mpwasm [-i] [-v] [-O[N]] [-X opt] [-c cmd | -m module | file] [args]`.

```bash
mpwasm script.py arg1                 # run a file; sys.argv is set, sys.exit(n) is the exit status
mpwasm -c "print(1 + 1)"
mpwasm -i script.py                   # run it, then a REPL in its namespace
mpwasm                                # a REPL on a terminal; a script on stdin otherwise
mpwasm --npm 1.28 -X heapsize=256K -X variant=ulab script.py
mpwasm --mjs my.mjs --wasm my.wasm    # your own build
mpwasm --list-versions
```

`-X` options: `compile-only`, `heapsize=<n>[w][K|M]`, `host=`, `variant=`. On a terminal the REPL is MicroPython's
own (line editing, history, auto-indent). Its filesystem is in memory, so a script can't import sibling
files from disk, and output arrives when the script finishes (the REPL is interactive).

### mpremote

`--pty` (POSIX) or `--tcp-port` make mpwasm behave like a board on a serial console, the same two transports
as rp2040py's `micropython --pty` / `--tcp-port`:

```bash
mpwasm --pty                          # prints e.g. /dev/pts/4
mpremote connect /dev/pts/4 exec "print(1 + 1)"      # exec, run, fs, resume, and the interactive repl
mpwasm --tcp-port 0                   # any free port; prints it
mpremote connect socket://127.0.0.1:PORT run script.py
```

The stock `mpremote repl` needs the pty; over `socket://` use `exec`/`run`/`fs`. WebREPL is not offered:
`mpremote` 1.29 does not speak it. Like a board, the interpreter keeps its variables between connections
(`mpremote resume`), and mpremote's soft reset gives a fresh one. Raw paste is declined, and mpremote falls
back to plain raw REPL.

### A bug in MicroPython 1.29.0-6's REPL

In the bundled 1.29.0-6 build, every key pressed at an empty prompt redraws it (`>>> >>> >>>` for Backspace),
arrow keys print `[A`, Up doesn't recall and pasted CRLF gives two prompts. It comes from
`pyexec.c` (commit 2e3304a12), and 1.22.0-335 to 1.28.0-6 are fine. mpwasm probes the REPL at start and
works around it only where needed (`src/mpwasm/_input.py`); a healthy build gets its own editing and history.

## JavaScript hosts

| Host | Where | How it is detected |
|---|---|---|
| `jscontext` | Pythonista (iOS) | JavaScriptCore's `JSContext` through `objc_util` |
| `gi-jsc` | Linux | WebKitGTK's JavaScriptCore through PyGObject (`apt install gir1.2-javascriptcoregtk-4.1 python3-gi`) |
| `node` | anywhere with Node.js | `node` on `PATH` |

With nothing configured, the first host that starts wins, in the order shown. Each host's constructor
is its own probe: it fails when its runtime is missing (`objc_util` / `gi` won't import, `node` isn't on
`PATH`, the engine has no `WebAssembly`). Override it with `MPWASM_HOST=<name>` or
`MicroPython(host="<name>")`; `mp.host` says which one is in use.

Every host gets the same JavaScript: a bare context (Node runs it in a `vm` context, so it has no
`console`, timers or `TextDecoder` either), a few shims for the browser APIs MicroPython's loader
expects, and the loader patched from an ES module into a classic script (`src/mpwasm/_js.py`).
Timers are driven from Python while it waits, so `asyncio` works without an event loop in JS.

## Build

The MicroPython build is the MicroPython project's
[`@micropython/micropython-webassembly-pyscript`](https://www.npmjs.com/package/@micropython/micropython-webassembly-pyscript)
npm package (MIT), pinned in `package.json` / `package-lock.json`, so a Dependabot npm update changes
the bundled version. `setup.py` runs `build_assets.py` on every build: it downloads the locked tarball, checks
its sha512 against the lock's `integrity`, and puts `micropython.mjs`, `micropython.wasm` and
`micropython-ulab.wasm` into `src/mpwasm/`. Nothing needs to be installed beforehand, and the package
version comes from git tags through `setuptools_scm`.

```bash
uv sync                        # editable install; downloads the assets on first use
uv build                       # sdist + wheel (the wheel is built from the sdist, so it downloads too)
uv run python build_assets.py  # just re-fetch the assets
```

## Test

```bash
uv run pytest                          # on the automatically picked runtime
uv run pytest --js-runtime node        # ... on one specific runtime: node | gi-jsc
uv run pytest --cov                    # with coverage
uv run pyright && uv run ruff check    # types, lint
```

If the runtime passed to `--js-runtime` can't start, the run stops with an error; the tests are never
silently skipped. CI (`.github/workflows/tests.yml`) runs the suite on Node on Linux, Windows and macOS
with CPython 3.10, CPython 3.14 and PyPy 3.11. It also runs it on WebKitGTK JavaScriptCore with and
without JIT (iOS runs JavaScriptCore without one), then combines coverage and uploads it to Codecov.

## Pythonista

Copy `src/mpwasm/` (with the downloaded `.mjs` / `.wasm` files) into Pythonista next to your script and
`from mpwasm import MicroPython`. The package is plain Python 3.10 with no dependencies, and JSContext
is picked automatically. Each release also carries `mpwasm-pythonista.zip`, that folder ready to copy.

The command line works there too, with the console as stdin: `python -m mpwasm` (or `mpwasm` in StaSh) with no
script starts a REPL. Pythonista's console is interactive but is not a tty, so mpwasm recognises iOS and reads
it line by line with `input()`; `exit()`, `sys.exit(n)` or end of input leave it, as iOS keyboards have no Ctrl-D.
From the console you can also call `mpwasm._cli.run(["-c", "print(1)"])`, which returns the exit status instead
of exiting (`main()` exits, like rp2040py's). `--tcp-port` works there as well, for `rp2040py mpremote`.
