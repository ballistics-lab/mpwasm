"""The serial-console bridge behind `--pty` / `--tcp-port`: raw REPL, soft reset, raw-paste refusal, sockets."""

import os
import select
import socket
import sys
import threading
import time
from collections.abc import Iterator

import pytest

from mpwasm import MicroPython
from mpwasm._serve import Console, cooked, serve_pty, serve_tcp


def make() -> MicroPython:
    return MicroPython()


@pytest.fixture
def console() -> Iterator[Console]:
    c = Console(make)
    yield c
    c.close()


def test_cooked_turns_bare_newlines_into_crlf() -> None:
    assert cooked("a\nb\r\nc\n") == b"a\r\nb\r\nc\r\n"


def test_normal_repl_echoes_and_prints(console: Console) -> None:
    assert b"42" in console.feed(b"print(6*7)\r")


def test_raw_repl_handshake_and_exec(console: Console) -> None:
    assert b"raw REPL; CTRL-B to exit\r\n>" in console.feed(b"\r\x03\r\x01")
    assert console.feed(b"print(6*7)\x04") == b"OK42\r\n\x04\x04>"
    assert b">>>" in console.feed(b"\x02")  # Ctrl-B: back to the normal REPL


def test_soft_reset_in_raw_repl_replaces_the_interpreter(console: Console) -> None:
    console.feed(b"\x01")
    console.feed(b"x = 1\x04")
    out = console.feed(b"\x04")  # Ctrl-D with nothing typed: what mpremote sends before every exec
    assert out == b"soft reboot\r\n\r\nraw REPL; CTRL-B to exit\r\n>"
    assert b"False" in console.feed(b"print('x' in dir())\x04")


def test_soft_reset_in_normal_repl(console: Console) -> None:
    console.feed(b"x = 1\r")
    out = console.feed(b"\x04")
    assert out.startswith(b"MPY: soft reboot\r\n") and b"MicroPython v" in out
    assert b"False" in console.feed(b"print('x' in dir())\r")


def test_ctrl_d_after_typing_does_not_reset(console: Console) -> None:
    console.feed(b"\x01")
    out = console.feed(b"x = 5\x04")  # Ctrl-D here runs what was typed
    assert b"soft reboot" not in out
    assert console.feed(b"print(x)\x04").startswith(b"OK5")


def test_raw_paste_is_declined_so_mpremote_falls_back(console: Console) -> None:
    console.feed(b"\x01")
    assert console.feed(b"\x05A\x01") == b"R\x00"
    assert console.feed(b"print(1)\x04").startswith(b"OK1")  # the console is still in plain raw REPL


def test_raw_paste_request_split_across_reads(console: Console) -> None:
    console.feed(b"\x01")
    assert console.feed(b"\x05A") == b""
    assert console.feed(b"\x01") == b"R\x00"


def test_ctrl_e_in_raw_repl_that_is_not_a_paste_request_reaches_the_repl(console: Console) -> None:
    console.feed(b"\x01")
    console.feed(b"\x05")  # might be the start of a paste request: held back
    console.feed(b"print(7)")  # ... but it is not, so it is passed on with what follows
    assert b"OK" in console.feed(b"\x04")


def _recv_until(sock: socket.socket, marker: bytes, timeout: float = 15.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while marker not in buf and time.time() < end:
        if select.select([sock], [], [], 0.1)[0]:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
    return buf


def test_tcp_server_speaks_raw_repl_and_keeps_state_across_clients() -> None:
    stop = threading.Event()
    ready = threading.Event()
    port: list[int] = []

    def on_listen(p: int) -> None:
        port.append(p)
        ready.set()

    thread = threading.Thread(target=serve_tcp, args=(make,), kwargs={"stop": stop, "on_listen": on_listen})
    thread.start()
    try:
        assert ready.wait(30)
        with socket.create_connection(("127.0.0.1", port[0]), timeout=10) as c:
            c.sendall(b"\r\x03\r\x01")
            assert b"raw REPL" in _recv_until(c, b"raw REPL; CTRL-B to exit\r\n>")
            c.sendall(b"y = 21\x04")
            _recv_until(c, b"\x04>")
        with socket.create_connection(("127.0.0.1", port[0]), timeout=10) as c:  # a second client: the board stayed up
            c.sendall(b"\r\x03\r\x01")
            _recv_until(c, b"raw REPL; CTRL-B to exit\r\n>")
            c.sendall(b"print(y * 2)\x04")
            assert _recv_until(c, b"\x04>").startswith(b"OK42")
    finally:
        stop.set()
        thread.join(30)
    assert not thread.is_alive()


@pytest.mark.skipif(sys.platform == "win32", reason="needs a POSIX pty")
def test_pty_server_speaks_raw_repl() -> None:
    stop = threading.Event()
    ready = threading.Event()
    paths: list[str] = []

    def on_listen(path: str) -> None:
        paths.append(path)
        ready.set()

    thread = threading.Thread(target=serve_pty, args=(make,), kwargs={"stop": stop, "on_listen": on_listen})
    thread.start()
    try:
        assert ready.wait(30)
        fd = os.open(paths[0], os.O_RDWR | os.O_NOCTTY)
        try:

            def read_until(marker: bytes) -> bytes:
                buf = b""
                end = time.time() + 15
                while marker not in buf and time.time() < end:
                    if select.select([fd], [], [], 0.1)[0]:
                        buf += os.read(fd, 4096)
                return buf

            os.write(fd, b"\r\x03\r\x01")
            assert b"raw REPL" in read_until(b"raw REPL; CTRL-B to exit\r\n>")
            os.write(fd, b"print(6*7)\x04")
            assert read_until(b"\x04>").startswith(b"OK42")
        finally:
            os.close(fd)
    finally:
        stop.set()
        thread.join(30)
    assert not thread.is_alive()
