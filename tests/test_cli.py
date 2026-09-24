"""The command line, in the style of the unix `micropython` binary (run as `python -m mpwasm`)."""

import io
import os
import pty
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from mpwasm import _cli

SRC = str(Path(__file__).resolve().parent.parent / "src")


def cli(*args: str, stdin: str | None = None, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    return subprocess.run(
        [sys.executable, "-m", "mpwasm", *args],
        input=stdin if stdin is not None else "",
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=cwd,
        check=False,
    )


# ── parsing ─────────────────────────────────────────────────────────────────────────────────────────


def test_parse_command_and_script_arguments() -> None:
    o = _cli.parse_args(["-v", "-O2", "-c", "print(1)", "a", "-x"])
    assert (o.command, o.argv, o.verbose, o.opt_level) == ("print(1)", ["-c", "a", "-x"], 1, 2)


def test_parse_file_stops_option_parsing() -> None:
    o = _cli.parse_args(["-i", "script.py", "-v", "arg"])
    assert (o.filename, o.argv, o.inspect, o.verbose) == ("script.py", ["script.py", "-v", "arg"], True, 0)


def test_parse_module() -> None:
    o = _cli.parse_args(["-m", "pkg.tool", "x"])
    assert (o.module, o.argv) == ("pkg.tool", ["pkg.tool", "x"])


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1024", 1024), ("64K", 65536), ("2M", 2 * 1024 * 1024), ("256w", 1024), ("4wK", 4 * 4 * 1024)],
)
def test_heapsize(text: str, expected: int) -> None:
    assert _cli.parse_heapsize(text) == expected


@pytest.mark.parametrize("bad", ["", "abc", "1.5M", "-1", "10G"])
def test_heapsize_rejects(bad: str) -> None:
    with pytest.raises(_cli.UsageError):
        _cli.parse_heapsize(bad)


def test_parse_selection_and_serving_flags() -> None:
    o = _cli.parse_args(
        ["--npm", "1.28", "--mjs", "a.mjs", "--wasm", "b.wasm", "--tcp-port", "0", "-X", "variant=ulab"]
    )
    assert (o.npm, o.mjs, o.wasm, o.tcp_port, o.variant) == ("1.28", "a.mjs", "b.wasm", 0, "ulab")


@pytest.mark.parametrize(
    "args",
    [
        ["-X"],
        ["-c"],
        ["--npm"],
        ["--tcp-port", "x"],
        ["-Z"],
        ["-X", "nope=1"],
        ["-X", "emit=native"],
        ["-X", "host=nope"],
    ],
)
def test_parse_rejects(args: list[str]) -> None:
    with pytest.raises(_cli.UsageError):
        _cli.parse_args(args)


# ── running ─────────────────────────────────────────────────────────────────────────────────────────


def test_command_and_argv() -> None:
    r = cli("-c", "import sys; print(sys.argv)", "a", "b")
    assert (r.returncode, r.stdout, r.stderr) == (0, "['-c', 'a', 'b']\n", "")


def test_stdout_and_stderr_are_separate() -> None:
    r = cli("-c", "import sys; print('out'); sys.stderr.write('err\\n')")
    assert (r.stdout, r.stderr) == ("out\n", "err\n")


def test_exact_output_no_added_newline() -> None:
    assert cli("-c", "print('a', end='')").stdout == "a"


def test_exit_status_follows_sys_exit() -> None:
    assert cli("-c", "import sys; sys.exit(3)").returncode == 3
    assert cli("-c", "import sys; sys.exit()").returncode == 0
    r = cli("-c", "import sys; sys.exit('bye')")
    assert (r.returncode, r.stderr) == (1, "bye\n")


def test_uncaught_exception_prints_traceback_and_fails() -> None:
    r = cli("-c", "print('before'); 1/0")
    assert r.returncode == 1
    assert r.stdout == "before\n"
    assert r.stderr.startswith("Traceback (most recent call last):\n") and r.stderr.endswith(
        "ZeroDivisionError: divide by zero\n"
    )
    assert "exec(compile" not in r.stderr and "[JS]" not in r.stderr


def test_script_file_with_argv_and_traceback_filename(tmp_path: Path) -> None:
    script = tmp_path / "s.py"
    script.write_text("import sys\nprint('file', sys.argv[1:])\nraise ValueError('bad')\n")
    r = cli(str(script), "p", "q")
    assert r.stdout == "file ['p', 'q']\n"
    assert r.returncode == 1 and f'File "{script}", line 3' in r.stderr and "ValueError: bad" in r.stderr


def test_missing_file() -> None:
    r = cli("/no/such/file.py")
    assert r.returncode == 2 and "can't open file" in r.stderr


def test_module_from_cwd(tmp_path: Path) -> None:
    (tmp_path / "tool.py").write_text("print('as module', __name__)\n")
    assert cli("-m", "tool", cwd=tmp_path).stdout == "as module __main__\n"
    assert cli("-m", "nothing_here", cwd=tmp_path).returncode == 2


def test_script_from_stdin() -> None:
    r = cli(stdin="print('from stdin')\n")
    assert (r.returncode, r.stdout) == (0, "from stdin\n")


def test_compile_only() -> None:
    assert cli("-X", "compile-only", "-c", "def f(:").returncode == 1
    ok = cli("-X", "compile-only", "-c", "print('never runs')")
    assert (ok.returncode, ok.stdout) == (0, "")


def test_heapsize_option_limits_the_heap() -> None:
    r = cli("-X", "heapsize=64K", "-c", "bytearray(1024 * 1024)")
    assert r.returncode == 1 and "MemoryError" in r.stderr


def test_optimisation_level_is_accepted() -> None:
    assert cli("-O2", "-c", "print(1)").stdout == "1\n"


def test_version_and_help() -> None:
    v = cli("--version")
    assert v.returncode == 0 and v.stdout.startswith("MicroPython v") and "mpwasm" in v.stdout
    h = cli("-h")
    assert h.returncode == 0 and h.stdout.startswith("usage: mpwasm") and "--npm" in h.stdout


def test_usage_errors_exit_2() -> None:
    r = cli("-Z")
    assert r.returncode == 2 and r.stderr.startswith("mpwasm: unrecognised option")
    assert cli("--pty", "-c", "1").returncode == 2  # serving and running code are exclusive
    assert cli("--pty", "--tcp-port", "0").returncode == 2


def test_variant_ulab() -> None:
    assert cli("-X", "variant=ulab", "-c", "import ulab.numpy as np; print(np.array([1, 2]) * 2)").stdout == (
        "array([2.0, 4.0], dtype=float64)\n"
    )


def test_unknown_npm_version_is_a_clean_error() -> None:
    r = cli("--npm", "0.0.1-nope")
    assert r.returncode == 1 and r.stderr.startswith("mpwasm: ") and "Traceback" not in r.stderr


# ── the interactive REPL, on a real pseudo-terminal ─────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")
def test_interactive_repl_on_a_tty() -> None:
    master, slave = pty.openpty()
    env = {**os.environ, "PYTHONPATH": SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.Popen(
        [sys.executable, "-m", "mpwasm"], stdin=slave, stdout=slave, stderr=slave, close_fds=True, env=env
    )
    os.close(slave)
    buf = b""

    def read_until(marker: bytes, timeout: float = 20) -> None:
        nonlocal buf
        end = time.time() + timeout
        while marker not in buf[-4096:] and time.time() < end:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    buf += os.read(master, 4096)
                except OSError:
                    return

    try:
        read_until(b">>> ")
        assert b"MicroPython v" in buf
        for line, expect in ((b"x = 6\r", b">>> "), (b"x * 7\r", b"42\r\n>>> "), (b"print('hi')\r", b"hi\r\n>>> ")):
            buf = b""
            os.write(master, line)
            read_until(expect)
            assert expect in buf, (line, buf)  # note the \r\n: no staircase on a raw terminal
        os.write(master, b"\x04")  # Ctrl-D at an empty prompt leaves, like the unix binary
        assert proc.wait(timeout=20) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)


# ── consoles that are interactive but not ttys (Pythonista, Windows) ────────────────────────────────


FIFO, REG, SOCK, CHR = stat.S_IFIFO, stat.S_IFREG, stat.S_IFSOCK, stat.S_IFCHR


@pytest.mark.parametrize(
    ("ios", "mode", "expected"),
    [
        (False, FIFO, True),  # `echo ... | mpwasm`
        (False, REG, True),  # `mpwasm < script.py`
        (False, SOCK, True),
        (False, CHR, False),  # a terminal (or /dev/null)
        (False, None, False),  # a console with no file descriptor of its own (Windows IDLE-like)
        (True, FIFO, False),  # Pythonista: interactive whatever fd 0 looks like
        (True, REG, False),
        (True, None, False),
    ],
)
def test_stdin_is_a_script_only_when_known_to_be_piped(
    monkeypatch: pytest.MonkeyPatch, ios: bool, mode: int | None, expected: bool
) -> None:
    monkeypatch.setattr(_cli, "_on_ios", lambda: ios)
    monkeypatch.setattr(_cli, "_stdin_mode", lambda: mode)
    assert _cli.stdin_is_a_script() is expected


def test_stdin_mode_survives_a_stdin_without_a_descriptor(monkeypatch: pytest.MonkeyPatch) -> None:
    class NoFileno:
        def fileno(self) -> int:
            raise io.UnsupportedOperation("fileno")

    monkeypatch.setattr(sys, "stdin", NoFileno())
    assert _cli._stdin_mode() is None


def test_ios_is_recognised_by_pythonistas_own_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(_cli.sysconfig, "get_platform", lambda: "linux-x86_64")
    monkeypatch.setattr(
        _cli.importlib.util, "find_spec", lambda name, *a, **k: object() if name == "objc_util" else None
    )
    assert _cli._on_ios()
    monkeypatch.setattr(_cli.importlib.util, "find_spec", lambda name, *a, **k: None)
    assert not _cli._on_ios()


def test_no_raw_terminal_on_ios_even_with_termios_and_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(_cli, "_on_ios", lambda: True)
    assert not _cli._raw_terminal_available()


def run_line_repl(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *lines: str | type
) -> tuple[int, str]:
    from mpwasm import MicroPython

    script = iter(lines)

    def fake_input(prompt: str = "") -> str:
        print(prompt, end="")
        item = next(script)
        if isinstance(item, type):
            raise item()
        return item

    monkeypatch.setattr(_cli, "stdin_is_a_script", lambda: False)  # as in Pythonista: an interactive console
    monkeypatch.setattr("builtins.input", fake_input)
    with MicroPython() as mp:
        status = _cli.line_repl(mp)
    return status, capsys.readouterr().out


def test_line_repl_runs_lines_and_blocks_and_exits(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out = run_line_repl(
        monkeypatch, capsys, "x = 6", "x * 7", "def f(n):", "    return n + 1", "", "f(41)", "exit()"
    )
    assert status == 0
    assert out.count("42\n") == 2 and ">>> " in out and "... " in out
    assert out.startswith("MicroPython v")


def test_line_repl_survives_errors_and_ends_on_eof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status, out = run_line_repl(monkeypatch, capsys, "1/0", "print('after')", EOFError)
    assert status == 0 and "after" in out


def test_line_repl_sys_exit_ends_the_session_with_its_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    status, _ = run_line_repl(monkeypatch, capsys, "import sys", "sys.exit(3)", "print('not reached')")
    assert status == 3


def test_line_repl_has_no_prompts_for_piped_input_on_posix(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mpwasm import MicroPython

    monkeypatch.setattr(_cli, "stdin_is_a_script", lambda: True)  # a pipe
    lines = iter(["print(1)", EOFError])

    def fake_input(prompt: str = "") -> str:
        assert prompt == ""  # like `micropython < file`: no prompts, no banner
        item = next(lines)
        if isinstance(item, type):
            raise item()
        return item

    monkeypatch.setattr("builtins.input", fake_input)
    with MicroPython() as mp:
        assert _cli.line_repl(mp) == 0
    assert capsys.readouterr().out == "1\n"


def test_main_module_runs_as_a_plain_file() -> None:
    # Pythonista's Run button executes the file, with no parent package to import from
    r = subprocess.run(
        [sys.executable, str(Path(SRC) / "mpwasm" / "__main__.py"), "-c", "print('as a file')"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert (r.returncode, r.stdout) == (0, "as a file\n")
