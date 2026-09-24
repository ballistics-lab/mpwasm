"""Serve MicroPython's REPL over a pty or a TCP socket, so `mpremote` (or anything speaking to a serial
console) can drive it like a board.

Same two transports as rp2040py's `micropython --pty` / `--tcp-port`, for the same reasons:

- `--pty` (POSIX): a real pseudo-terminal pair; the slave side is a genuine tty device
  (`/dev/pts/N`), which pySerial opens with its ordinary serial backend. `mpremote connect /dev/pts/N`
  works with everything, including its bare interactive `repl` (which needs the `.fd` only a real tty has).
- `--tcp-port`: a plain socket (`mpremote connect socket://127.0.0.1:PORT`), for hosts with no pty --
  Windows, sandboxed apps. `exec`, `fs`, `run` ... work; the bare `repl` of stock mpremote does not over
  `socket://` (pySerial gives it no `.fd`), which is mpremote's limit, not ours.

The bytes are MicroPython's own REPL protocol -- raw REPL included, which is what mpremote uses -- so no
protocol is implemented here: keystrokes go in through `MicroPython.repl_feed`, and what MicroPython writes
comes out with `\n` turned into `\r\n`, as a board's serial console does. One client at a time, and the
interpreter (its variables) lives on across reconnects, like a board that stays powered.

The one thing the WebAssembly build cannot do itself is a soft reset (Ctrl-D at an empty prompt, or in raw
REPL with nothing typed, which mpremote sends before every `exec`/`run`): there is no outer loop to restart. So
`Console` recognises that keystroke and does what a board does: prints "soft reboot", replaces the interpreter
with a fresh one (new heap, no old globals) and, if it was in raw REPL, goes back into it.
"""

from __future__ import annotations

import os
import re
import select
import socket
import sys
import threading
from collections.abc import Callable
from typing import Final

from . import MicroPython
from ._input import InputEditor, repl_is_affected

__all__ = ("Console", "cooked", "serve_pty", "serve_tcp")

_IDLE: Final = 0.02  # seconds between timer pumps while nobody types


def cooked(text: str) -> bytes:
    """MicroPython's output as a serial console sends it: every bare `\\n` becomes `\\r\\n`."""
    return re.sub(r"(?<!\r)\n", "\r\n", text).encode("utf-8")


def _log(message: str) -> None:
    sys.stderr.write(message + "\n")
    sys.stderr.flush()


_CTRL_A, _CTRL_B, _CTRL_C, _CTRL_D, _CTRL_E = 0x01, 0x02, 0x03, 0x04, 0x05
_RAW_PASTE: Final = b"\x05A\x01"  # mpremote's "raw paste" request; device answers R\x01 (yes) or R\x00 (no)


class Console:
    """A serial console in front of a MicroPython interpreter, with soft reset."""

    def __init__(self, factory: Callable[[], MicroPython]) -> None:
        self._factory = factory
        self._typed = 0  # raw REPL only: bytes since the last Ctrl-D / Ctrl-C, 0 meaning an empty script
        self._hold = b""  # the start of a raw-paste request cut off by the end of a read
        self._mp = factory()
        self._mp.repl_init()
        self._editor = InputEditor()
        # The banner belongs to boot, not to whichever client connects first -- but the editor still has to
        # see the prompt it ends with.
        self._drain()
        # Work around the empty-line restart bug of MicroPython 1.29.0-6's REPL only where it exists.
        self._editor.compensate = repl_is_affected(self._mp)
        self._drain()

    def close(self) -> None:
        self._mp.close()

    def _drain(self) -> bytes:
        out = cooked(self._mp.output() + self._mp.error_output())
        self._editor.note_output(out.decode("utf-8", "replace"))
        return out

    def idle(self) -> bytes:
        """Let due timers run (asyncio tasks) and return whatever that printed."""
        self._mp.pump()
        return self._drain()

    def feed(self, data: bytes) -> bytes:
        """Send keystrokes to the REPL; return everything MicroPython (or a soft reset) printed."""
        data = self._hold + data
        self._hold = b""
        editor = self._editor
        out = b""
        chunk = bytearray()

        def flush() -> None:
            nonlocal out
            if chunk:
                self._mp.repl_feed(bytes(chunk))
                chunk.clear()
            out += self._drain()

        i = 0
        while i < len(data):
            b = data[i]
            if editor.raw and b == _CTRL_E:
                rest = data[i : i + len(_RAW_PASTE)]
                if rest == _RAW_PASTE:
                    flush()
                    out += b"R\x00"  # raw paste unsupported: mpremote falls back to plain raw REPL
                    i += len(_RAW_PASTE)
                    continue
                if len(rest) < len(_RAW_PASTE) and _RAW_PASTE.startswith(rest):
                    self._hold = rest  # the request is split across reads: wait for the rest
                    break
            # Ctrl-D with nothing typed is a soft reset (raw REPL: an empty script; normal: an empty line).
            if b == _CTRL_D and (self._typed == 0 if editor.raw else editor.line_empty and not editor.holding):
                flush()
                out += self._soft_reset()
                i += 1
                continue
            was_raw = editor.raw
            chunk += editor.feed(bytes([b]))
            if editor.raw:
                self._typed = self._typed + 1 if was_raw and b not in (_CTRL_D, _CTRL_C) else 0
            i += 1
        flush()
        return out

    def _soft_reset(self) -> bytes:
        was_raw = self._editor.raw
        self._mp.close()
        self._mp = self._factory()
        self._mp.repl_init()
        self._editor.reset(raw=False)
        self._typed = 0
        banner = self._drain()
        if not was_raw:
            return b"MPY: soft reboot\r\n" + banner
        self._mp.repl_feed(bytes([_CTRL_A]))  # a board comes back into raw REPL after a soft reset in it
        self._editor.reset(raw=True)
        return b"soft reboot\r\n" + self._drain()


def serve_pty(
    factory: Callable[[], MicroPython],
    *,
    stop: threading.Event | None = None,
    on_listen: Callable[[str], None] | None = None,
) -> int:
    """Serve on a new pty until interrupted (or `stop` is set). Logs the slave's path for `mpremote connect`.

    `on_listen(path)` is called once the pty is open; both hooks exist for embedding and tests.
    """
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(slave)  # a raw byte pipe, whatever the client does (or doesn't) to its own termios
    os.set_blocking(master, False)
    path = os.ttyname(slave)
    _log(f"mpwasm: PTY REPL listening on {path} - e.g. `mpremote connect {path}`")
    console = Console(factory)
    if on_listen is not None:
        on_listen(path)
    pending = b""
    try:
        while stop is None or not stop.is_set():
            readable, writable, _ = select.select([master], [master] if pending else [], [], _IDLE)
            if readable:
                try:
                    data = os.read(master, 4096)
                except OSError:  # no client has the slave open right now: nothing to read
                    data = b""
                pending += console.feed(data) if data else b""
            else:
                pending += console.idle()
            if pending and (writable or not readable):
                try:
                    pending = pending[os.write(master, pending) :]
                except BlockingIOError:
                    pass
                except OSError:
                    pending = b""  # nobody is listening: drop it rather than grow without bound
    except KeyboardInterrupt:
        pass
    finally:
        console.close()
        os.close(master)
        os.close(slave)
    return 0


def serve_tcp(
    factory: Callable[[], MicroPython],
    host: str = "127.0.0.1",
    port: int = 0,
    *,
    stop: threading.Event | None = None,
    on_listen: Callable[[int], None] | None = None,
) -> int:
    """Serve on a TCP socket until interrupted (port 0 lets the OS pick; the real one is logged).

    `on_listen(port)` is called with the port actually bound; `stop` ends the loop (embedding and tests).
    """
    server = socket.create_server((host, port))
    server.setblocking(False)
    actual = server.getsockname()[1]
    _log(f"mpwasm: TCP REPL listening on {host}:{actual} - e.g. `mpremote connect socket://{host}:{actual}`")
    console = Console(factory)
    if on_listen is not None:
        on_listen(actual)
    client: socket.socket | None = None
    try:
        while stop is None or not stop.is_set():
            watch = [server] if client is None else [server, client]
            readable, _, _ = select.select(watch, [], [], _IDLE)
            out = b""
            if server in readable:
                conn, _addr = server.accept()
                if client is None:
                    conn.setblocking(False)
                    client = conn
                else:
                    conn.close()  # one client at a time, like a serial port
            if client is not None and client in readable:
                try:
                    data = client.recv(4096)
                except (BlockingIOError, InterruptedError):
                    data = None
                except OSError:
                    data = b""
                if data == b"":
                    client.close()
                    client = None
                elif data:
                    out += console.feed(data)
            if not readable:
                out += console.idle()
            if out and client is not None:
                try:
                    client.setblocking(True)
                    client.sendall(out)
                    client.setblocking(False)
                except OSError:
                    client.close()
                    client = None
    except KeyboardInterrupt:
        pass
    finally:
        if client is not None:
            client.close()
        console.close()
        server.close()
    return 0
