"""MicroPython running through mpwasm, on the JS runtime selected with --js-runtime (see conftest.py)."""

import os
import sys
import sysconfig
import textwrap
import threading
from collections.abc import Iterator

import pytest

import mpwasm
from mpwasm import MicroPython, MicroPythonError


@pytest.fixture(scope="module")
def mp() -> Iterator[MicroPython]:
    with MicroPython() as m:  # loading takes a moment: share one interpreter per module
        yield m


def test_runtime_is_the_requested_one(pytestconfig: pytest.Config, mp: MicroPython) -> None:
    runtime = pytestconfig.getoption("--js-runtime")
    if runtime:
        assert mp.host == runtime


def test_hello(mp: MicroPython) -> None:
    out = mp.run("import sys\nprint(sys.implementation.name)")
    assert out == "micropython\n"


def test_unicode_round_trip(mp: MicroPython) -> None:
    assert mp.run("print('Привіт, 世界 🚀')") == "Привіт, 世界 🚀\n"


def test_output_is_drained_per_call(mp: MicroPython) -> None:
    assert mp.run("print('a')") == "a\n"
    assert mp.run("print('b')") == "b\n"
    assert mp.output() == ""


def test_state_persists_between_calls(mp: MicroPython) -> None:
    mp.run("counter = 40")
    mp.run("counter += 2")
    assert mp.run("print(counter)") == "42\n"


def test_repl_echoes_expressions(mp: MicroPython) -> None:
    mp.run("x = 6")
    assert mp.repl("x * 7") == "42\n"
    assert mp.repl("x = 1") == ""  # a statement echoes nothing


def test_multiline_and_braces_in_code(mp: MicroPython) -> None:
    code = textwrap.dedent(
        """
        d = {"a": 1, "b": {"c": 2}}
        print("{} {}".format(d["a"], d["b"]["c"]))
        """
    )
    assert mp.run(code) == "1 2\n"


def test_exception_carries_message_and_earlier_output(mp: MicroPython) -> None:
    with pytest.raises(MicroPythonError, match="ZeroDivisionError") as info:
        mp.run("print('before')\n1 / 0")
    assert info.value.output == "before\n"
    assert mp.run("print('still alive')") == "still alive\n"  # the interpreter survives an exception


def test_syntax_error(mp: MicroPython) -> None:
    with pytest.raises(MicroPythonError, match="SyntaxError"):
        mp.run("def (")


def test_async_with_sleep(mp: MicroPython) -> None:
    out = mp.run_async(
        textwrap.dedent(
            """
            import asyncio
            async def ticker():
                for i in range(3):
                    print("tick", i)
                    await asyncio.sleep(0.05)
            await ticker()
            """
        )
    )
    assert out == "tick 0\ntick 1\ntick 2\n"


def test_async_exception(mp: MicroPython) -> None:
    with pytest.raises(MicroPythonError, match="ValueError"):
        mp.run_async("import asyncio\nawait asyncio.sleep(0)\nraise ValueError('boom')")


def test_async_timeout(mp: MicroPython) -> None:
    with pytest.raises(MicroPythonError, match="timed out"):
        mp.run_async("import asyncio\nawait asyncio.sleep(30)", timeout=0.2)


def test_interpreters_are_independent() -> None:
    with MicroPython() as a, MicroPython() as b:
        a.run("v = 'a'")
        b.run("v = 'b'")
        assert a.run("print(v)") == "a\n"
        assert b.run("print(v)") == "b\n"


def test_heapsize_is_honoured() -> None:
    with MicroPython(heapsize=64 * 1024) as small, pytest.raises(MicroPythonError, match="MemoryError"):
        small.run("x = bytearray(1024 * 1024)")


def test_ulab_variant() -> None:
    with MicroPython(variant="ulab") as u:
        assert (
            u.run("import ulab.numpy as np\nprint(np.array([1, 2, 3]) * 2)")
            == "array([2.0, 4.0, 6.0], dtype=float64)\n"
        )


def test_custom_paths_use_the_given_files() -> None:
    with MicroPython(
        mjs_path=mpwasm.asset_path("micropython.mjs"), wasm_path=mpwasm.asset_path("micropython.wasm")
    ) as m:
        assert m.run("print(1 + 1)") == "2\n"


def test_bad_arguments() -> None:
    with pytest.raises(ValueError, match="variant"):
        MicroPython(variant="No pe!")
    with pytest.raises(FileNotFoundError):
        MicroPython(variant="nope")  # a well-formed name, but the bundled build has no such variant
    with pytest.raises(ValueError, match="host"):
        MicroPython(host="nope")
    with pytest.raises(FileNotFoundError):
        MicroPython(wasm_path=os.path.join("no", "such.wasm"))


def test_threads_with_their_own_interpreters() -> None:
    results: dict[int, str] = {}

    def work(i: int) -> None:
        with MicroPython() as m:
            m.run(f"n = {i}")
            for _ in range(20):
                m.run("n += 1")
            results[i] = m.run("print(n)")

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == {i: f"{i + 20}\n" for i in range(4)}


def test_threads_sharing_one_interpreter(mp: MicroPython) -> None:
    # Each call gets its own output back, never another thread's.
    bad: list[tuple[int, str]] = []

    def work(i: int) -> None:
        for _ in range(25):
            out = mp.run(f"print({i})")
            if out != f"{i}\n":
                bad.append((i, out))

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert bad == []


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="not a free-threaded build")
def test_free_threaded_build_keeps_the_gil_disabled(mp: MicroPython) -> None:
    mp.run("print(1)")
    # sys._is_gil_enabled() exists only on 3.13+, and this test runs only on free-threaded builds (3.13t+).
    is_gil_enabled = getattr(sys, "_is_gil_enabled", lambda: True)
    assert not is_gil_enabled()
