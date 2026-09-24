"""Step-by-step check of mpwasm on Pythonista (or anywhere): run it and read where it stops.

Copy the `mpwasm/` folder (with its .mjs / .wasm files) next to this file, then Run. Each step prints OK or the
error, so if something fails you can see which layer it is: the JavaScript host, loading MicroPython, running
code, or the command line's choice of REPL.
"""

import importlib.util
import os
import sys
import sysconfig
import traceback
from collections.abc import Callable
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def step(title: str, fn: Callable[[], Any]) -> Any:
    print(f"\n--- {title}")
    try:
        result = fn()
        print("OK", "" if result is None else repr(result))
        return result
    except BaseException as exc:  # noqa: BLE001 -- a diagnostic: report everything
        print("FAILED:", type(exc).__name__, exc)
        traceback.print_exc(limit=4)
        return None


import mpwasm
from mpwasm import _cli

print("mpwasm", mpwasm.__file__)
step(
    "platform facts",
    lambda: {
        "platform": sys.platform,
        "sysconfig": sysconfig.get_platform(),
        "python": sys.version.split()[0],
        "objc_util": importlib.util.find_spec("objc_util") is not None,
        "termios": importlib.util.find_spec("termios") is not None,
        "isatty": sys.stdin.isatty(),
    },
)
step("stdin (mode / fileno)", lambda: (_cli._stdin_mode(), getattr(sys.stdin, "fileno", None) and "has fileno"))
step("what the command line thinks", lambda: {"on_ios": _cli._on_ios(), "stdin_is_a_script": _cli.stdin_is_a_script()})
step(
    "bundled files present",
    lambda: [f for f in ("micropython.mjs", "micropython.wasm") if os.path.isfile(mpwasm.asset_path(f))],
)
mp: mpwasm.MicroPython | None = step("start the JavaScript host + load MicroPython", lambda: mpwasm.MicroPython())
if mp is not None:
    loaded = mp  # (a lambda does not keep the narrowing of `mp`)
    step("host in use", lambda: loaded.host)
    step("run code", lambda: loaded.run("print('hello', 6 * 7)"))
    step("REPL-style expression", lambda: loaded.repl("6 * 7"))
    step("run_async", lambda: loaded.run_async("import asyncio\nawait asyncio.sleep(0.1)\nprint('async ok')"))
    step("JS log (empty is good)", loaded.js_log)
step("command line: -c", lambda: _cli.main(["-c", "print('from the CLI')"]))
print("\n--- command line: REPL (type  exit()  to leave)")
print("returned", _cli.main([]))
