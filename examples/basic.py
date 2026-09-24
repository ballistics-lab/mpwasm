"""Run MicroPython from CPython, PyPy or Pythonista.

    uv run python examples/basic.py     # from the repo root (`uv sync` fetches the MicroPython build)

In Pythonista: copy the `mpwasm/` folder (with its .mjs / .wasm files) next to this file and run it.
"""

import os
import sys
import time

# Run from a checkout without installing: make src/ importable. (Pythonista: mpwasm/ sits next to this
# file, which is already on sys.path.)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mpwasm import MicroPython

t0 = time.time()
mp = MicroPython()
print(f"MicroPython ready on {mp.host} in {time.time() - t0:.2f} s")

print(mp.run("import sys\nprint('hello from', sys.implementation.name, sys.version)"))
mp.run("total = sum(range(10))")
print("total =", mp.repl("total"))
print(
    mp.run_async(
        "import asyncio\n"
        "async def ticker():\n"
        "    for i in range(3):\n"
        "        print('tick', i)\n"
        "        await asyncio.sleep(0.2)\n"
        "await ticker()\n"
    )
)

try:
    mp.run("1 / 0")
except RuntimeError as exc:
    print(exc)

# A tiny REPL: an empty line at the first prompt exits.
while line := input("mpy>>> "):
    if line.rstrip().endswith(":"):
        lines = [line]
        while more := input("mpy... "):
            lines.append(more)
        line = "\n".join(lines) + "\n"
    try:
        out = mp.repl(line)
        if out:
            print(out)
    except RuntimeError as exc:
        print(exc)
